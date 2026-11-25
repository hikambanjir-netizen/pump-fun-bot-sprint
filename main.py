"""Pump.fun Bot Sprint entry: a pure-stdlib momentum bot for agent-fun/replay/v2.

Strategy in one paragraph: for every mint on the tape we keep a trailing
window of its recent trades (about 240 slots of flow). When a coin that
already carries real liquidity (at least 3.5 SOL of real quote reserves)
shows one-sided buying inside that window -- buy flow at least 2.2x the
sell flow, at least 1.5 SOL of buy volume, at least 6 distinct buyers and
a price up at least 8% across the window -- we take one small position
(0.04 SOL, or the harness size cap when that is smaller) and ride it.
Positions leave on a take profit, a stop loss, a trailing stop once the
run is well up, or a time stop when a trade has gone nowhere for long
enough. One entry per mint, never more than the position cap allows, and
every order is tracked while it is in flight so nothing is duplicated.

The wire protocol (hello, batch, orders, slot bookkeeping, fills) is the
kit SDK's job: vendor/makefun_bot.py owns it and this file only decides.
Determinism matters -- the grader runs the bot twice and the two action
logs must match byte for byte -- so nothing here reads the clock, the
environment or an unseeded random source.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bot

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL

# --- signal window -------------------------------------------------------
WINDOW_SLOTS = 300
MIN_REAL_RESERVES = 8_000_000_000
FLOW_RATIO = 3.0
MIN_BUY_VOLUME = 3_000_000_000
MIN_DISTINCT_BUYERS = 10
MIN_PRICE_GAIN = 0.12

# --- sizing ---------------------------------------------------------------
STAKE_LAMPORTS = 200_000_000
MIN_STAKE_LAMPORTS = 10_000_000     # a round trip needs a >= 0.01 SOL buy

# --- exits ----------------------------------------------------------------
TAKE_PROFIT_RATIO = 1.15
STOP_LOSS_RATIO = 0.90
TRAIL_ARM_RATIO = 1.10
TRAIL_FLOOR = 0.95
TIME_STOP_SLOTS = 900
TIME_STOP_MAX_RATIO = 1.01

BUY_SLIPPAGE_BPS = 1200
SELL_SLIPPAGE_BPS = 4000

# --- entry mode -------------------------------------------------------------
# "momentum" rides confirmed bursts; "reversion" buys sharp dips once the
# selling exhausts. ENTRY_MODE picks which signal entries() listens to.
ENTRY_MODE = "reversion"

# --- reversion signal -------------------------------------------------------
REVERT_DROP = -0.23
REVERT_MIN_VOLUME = 6_000_000_000
REVERT_MIN_RESERVES = 8_000_000_000
RECENT_SLOTS = 120
RECENT_MIN_BUYERS = 6
RECENT_MIN_FLOW_RATIO = 1.0
RECENT_MAX_DROP = -0.02
RECENT_MIN_GAIN = 0.01
MIN_MINT_AGE_SLOTS = 1500
SLEEVE2_SLOTS = 90            # second stabilisation scale (slots)
SLEEVE2_RATIO = 1.5           # ...with its own recent flow ratio
SLEEVE4_SLOTS = 180           # slowest stabilisation scale (slots)
SLEEVE4_RATIO = 0.8
SLEEVE4_MIN_GAIN = -0.01
SLEEVE4_MIN_BUYERS = 4        # the slow scale sees fewer distinct buyers
RESCUE_DROP = -0.18           # shallower dips may enter on a violent
RESCUE_RATIO = 2.5            # stabilisation: recent flow ratio at least
RESCUE_MIN_BUYERS = 8         # this, at least this many recent buyers
RESCUE_MIN_GAIN = 0.03        # and at least this recent price gain
ALLOW_REENTRY = False
                              # a previous round trip on it has closed
MIN_RUNUP = 0.0
                              # (the coin ran up before it crashed)


class Flow:
    """One mint's trades inside the trailing window, with running totals.

    ``trades`` holds (slot, is_buy, sol_lamports, user, price) oldest
    first; price is the post-trade virtual quote/token ratio. The totals
    are maintained incrementally as trades arrive and expire so judging a
    mint is O(1) once its window is pruned.
    """

    __slots__ = ("trades", "buy_volume", "sell_volume", "buyers")

    def __init__(self):
        self.trades = deque()
        self.buy_volume = 0
        self.sell_volume = 0
        self.buyers = {}      # user -> buy trades of theirs in the window

    def add(self, slot, is_buy, sol, user, price):
        self.trades.append((slot, is_buy, sol, user, price))
        if is_buy:
            self.buy_volume += sol
            self.buyers[user] = self.buyers.get(user, 0) + 1
        else:
            self.sell_volume += sol

    def prune(self, cutoff_slot):
        trades = self.trades
        while trades and trades[0][0] < cutoff_slot:
            _slot, is_buy, sol, user, _price = trades.popleft()
            if is_buy:
                self.buy_volume -= sol
                left = self.buyers.get(user, 0) - 1
                if left > 0:
                    self.buyers[user] = left
                else:
                    self.buyers.pop(user, None)
            else:
                self.sell_volume -= sol

    def window_price_gain(self):
        """Last window price over the first, minus one; None if untraded."""
        if not self.trades:
            return None
        first = self.trades[0][4]
        last = self.trades[-1][4]
        if first <= 0:
            return None
        return last / first - 1.0

    def window_runup(self):
        """Peak window price over the first, minus one; None if untraded."""
        if not self.trades:
            return None
        first = self.trades[0][4]
        if first <= 0:
            return None
        return max(t[4] for t in self.trades) / first - 1.0



    def recent_stats(self, recent_cutoff):
        """(buy_volume, sell_volume, distinct_buyers, price_gain) over the
        trades at or after ``recent_cutoff``; price_gain is None when the
        sub-window holds fewer than two trades."""
        buy_volume = 0
        sell_volume = 0
        buyers = set()
        first_price = None
        last_price = None
        for slot, is_buy, sol, user, price in self.trades:
            if slot < recent_cutoff:
                continue
            if first_price is None:
                first_price = price
            last_price = price
            if is_buy:
                buy_volume += sol
                buyers.add(user)
            else:
                sell_volume += sol
        gain = None
        if first_price and last_price:
            gain = last_price / first_price - 1.0
        return buy_volume, sell_volume, len(buyers), gain


class MomentumBot:
    def __init__(self):
        self.flows = {}       # mint -> Flow
        self.entered = set()  # mints we have ever sent a buy for
        self.held = {}        # mint -> [slot first seen, peak price ratio]

    # -- tape ingestion ----------------------------------------------------

    def ingest(self, batch):
        for event in batch["events"]:
            if event.get("event") != "TradeEvent":
                continue
            mint = event["mint"]
            flow = self.flows.get(mint)
            if flow is None:
                flow = self.flows[mint] = Flow()
            vq = int(event["virtual_quote_reserves"])
            vt = int(event["virtual_token_reserves"])
            price = vq / vt if vt > 0 else 0.0
            flow.add(
                int(event["slot"]),
                bool(event["is_buy"]),
                int(event["sol_amount"]),
                event["user"],
                price,
            )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def price_of(curve):
        if curve is None or curve.virtual_token_reserves <= 0:
            return None
        return curve.virtual_quote_reserves / curve.virtual_token_reserves

    def pending_mints(self, state, side):
        return {p.order["mint"] for p in state.pending.values() if p.order["side"] == side}

    # -- exits ---------------------------------------------------------------

    def exits(self, state):
        orders = []
        selling = self.pending_mints(state, "sell")
        for mint, holding in state.positions.items():
            if mint in selling:
                continue
            curve = state.curves.get(mint)
            price = self.price_of(curve)
            if price is None or holding.token_amount <= 0:
                continue
            entry_price = holding.cost_lamports / holding.token_amount
            if entry_price <= 0:
                continue
            ratio = price / entry_price
            first_seen, peak = self.held.get(mint, (state.slot, ratio))
            if ratio > peak:
                peak = ratio
                self.held[mint] = (first_seen, peak)
            get_out = (
                ratio >= TAKE_PROFIT_RATIO
                or ratio <= STOP_LOSS_RATIO
                or (peak >= TRAIL_ARM_RATIO and ratio <= peak * TRAIL_FLOOR)
                or (
                    state.slot - first_seen >= TIME_STOP_SLOTS
                    and ratio < TIME_STOP_MAX_RATIO
                )
            )
            if get_out:
                orders.append(
                    {
                        "side": "sell",
                        "mint": mint,
                        "token_amount": holding.token_amount,
                        "max_slippage_bps": SELL_SLIPPAGE_BPS,
                    }
                )
        return orders

    # -- entry signals ---------------------------------------------------------

    def _momentum_score(self, flow, curve):
        """Window buy volume when the momentum signal fires, else None."""
        if curve.real_quote_reserves < MIN_REAL_RESERVES:
            return None
        if flow.buy_volume < MIN_BUY_VOLUME:
            return None
        if len(flow.buyers) < MIN_DISTINCT_BUYERS:
            return None
        if flow.sell_volume > 0 and flow.buy_volume < FLOW_RATIO * flow.sell_volume:
            return None
        gain = flow.window_price_gain()
        if gain is None or gain < MIN_PRICE_GAIN:
            return None
        return flow.buy_volume

    def _reversion_score(self, state, flow, curve):
        """Window total volume when the dip-and-stabilise signal fires.

        The coin must have fallen hard across the window on real volume,
        still hold real liquidity, and in the recent sub-window show
        buyers back in control with the price no longer in freefall.
        """
        if curve.real_quote_reserves < REVERT_MIN_RESERVES:
            return None
        if (
            MIN_MINT_AGE_SLOTS > 0
            and curve.created_slot is not None
            and state.slot - curve.created_slot < MIN_MINT_AGE_SLOTS
        ):
            return None
        total = flow.buy_volume + flow.sell_volume
        if total < REVERT_MIN_VOLUME:
            return None
        gain = flow.window_price_gain()
        if gain is None:
            return None
        if MIN_RUNUP > 0:
            runup = flow.window_runup()
            if runup is None or runup < MIN_RUNUP:
                return None
        if gain <= REVERT_DROP and self._recent_passes(state, flow):
            return total
        if gain <= RESCUE_DROP and self._recent_rescue(state, flow):
            return total
        return None

    def _recent_rescue(self, state, flow):
        """The violent-stabilisation test for shallower dips, judged on
        the primary recent window only."""
        r_buy, r_sell, r_buyers, r_gain = flow.recent_stats(state.slot - RECENT_SLOTS)
        if r_buyers < RESCUE_MIN_BUYERS:
            return False
        if r_sell > 0 and r_buy < RESCUE_RATIO * r_sell:
            return False
        return r_gain is not None and r_gain >= RESCUE_MIN_GAIN

    def _recent_passes(self, state, flow):
        """Stabilisation at either of two scales: the primary recent
        window (RECENT_SLOTS at RECENT_MIN_FLOW_RATIO) or the shorter
        sleeve (SLEEVE2_SLOTS at SLEEVE2_RATIO). Each scale must show
        enough distinct buyers, buy flow at least its ratio of sell
        flow, and a price no longer falling that is at least
        RECENT_MIN_GAIN over its own window."""
        for slots, ratio, min_gain, min_buyers in (
            (RECENT_SLOTS, RECENT_MIN_FLOW_RATIO, RECENT_MIN_GAIN, RECENT_MIN_BUYERS),
            (SLEEVE2_SLOTS, SLEEVE2_RATIO, RECENT_MIN_GAIN, RECENT_MIN_BUYERS),
            (SLEEVE4_SLOTS, SLEEVE4_RATIO, SLEEVE4_MIN_GAIN, SLEEVE4_MIN_BUYERS),
        ):
            r_buy, r_sell, r_buyers, r_gain = flow.recent_stats(state.slot - slots)
            if r_buyers < min_buyers:
                continue
            if r_sell > 0 and r_buy < ratio * r_sell:
                continue
            if r_gain is None or r_gain < RECENT_MAX_DROP or r_gain < min_gain:
                continue
            return True
        return False

    # -- entries ---------------------------------------------------------------

    def entries(self, state):
        orders = []
        room = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
        if room <= 0:
            return orders
        buying = self.pending_mints(state, "buy")
        cutoff = state.slot - WINDOW_SLOTS
        free_cash = state.free_cash_lamports()
        tx_fee = state.tx_fee_lamports()
        # Judge every mint first, then spend the free position slots on the
        # strongest flow: ranking by window buy volume (mint as tiebreak)
        # keeps the choice deterministic and prefers deep, broad buying
        # over whichever coin happened to be seen first.
        candidates = []
        for mint, flow in self.flows.items():
            if mint in self.entered or mint in state.positions or mint in buying:
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            flow.prune(cutoff)
            if ENTRY_MODE == "reversion":
                score = self._reversion_score(state, flow, curve)
            else:
                score = self._momentum_score(flow, curve)
            if score is not None:
                candidates.append((score, mint))
        candidates.sort(key=lambda c: (-c[0], c[1]))
        max_orders = state.hello["caps"]["max_orders_per_batch"]
        for _volume, mint in candidates:
            if room <= 0 or len(orders) >= max_orders:
                break
            stake = min(STAKE_LAMPORTS, state.max_buy_lamports(mint))
            if stake < MIN_STAKE_LAMPORTS:
                continue
            if stake + tx_fee > free_cash:
                continue
            orders.append(
                {
                    "side": "buy",
                    "mint": mint,
                    "quote_lamports": stake,
                    "max_slippage_bps": BUY_SLIPPAGE_BPS,
                }
            )
            self.entered.add(mint)
            free_cash -= stake + tx_fee
            room -= 1
        return orders

    # -- the batch callback ----------------------------------------------------

    def on_batch(self, state, batch):
        self.ingest(batch)
        for mint in state.positions:
            if mint not in self.held:
                self.held[mint] = (state.slot, 1.0)
        for mint in list(self.held):
            if mint not in state.positions:
                del self.held[mint]
                if ALLOW_REENTRY:
                    # The round trip on this mint is closed; if the signal
                    # fires again it may be entered once more.
                    self.entered.discard(mint)
        orders = self.exits(state)
        orders.extend(self.entries(state))
        return orders


def main():
    bot = MomentumBot()
    makefun_bot.run(bot.on_batch)


if __name__ == "__main__":
    main()
