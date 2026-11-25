# Pump.fun Bot Sprint — Dip-Reversion Bot

![Competition](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)
![Rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/hikambanjir-netizen/pump-fun-bot-sprint/rank.svg)
![Reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/hikambanjir-netizen/pump-fun-bot-sprint/reward.svg)

A deterministic trading bot for the make.fun **Pump.fun Bot Sprint**
(pumpfun-replay/v2 harness), written in pure Python stdlib on top of the
competition SDK shim in `vendor/makefun_bot.py`.

## Strategy

Momentum chasing lost money on every variant we tested, so the bot trades
**dip reversion**: it buys sharp crashes only after the selling exhausts
and buyers step back in.

- Per mint it keeps a trailing 300-slot flow window (buy/sell SOL volume,
  distinct buyers, price path) plus three shorter "recent" windows.
- **Entry** needs real reserves ≥ 8 SOL, mint age ≥ 1,500 slots, ≥ 6 SOL
  of window flow, and a ≥ 23% window drawdown — confirmed by a
  stabilisation test at one of three time scales (90/120/180 slots):
  recent buy flow at least matching recent sell flow, enough distinct
  recent buyers, and the recent price no longer falling. Shallower dips
  (≥ 18%) may enter only on a violent stabilisation (recent flow ≥ 2.5×,
  ≥ 8 buyers, ≥ +3% recent gain).
- **Stake** is 0.20 SOL per position (capped by the harness's own
  per-order reserve limit), at most one entry per mint, max 5 open.
- **Exits**: take profit +15%, stop loss −10%, a trailing stop once a
  position is up 10% (sell on a 5% pullback from its peak), and a
  900-slot time stop for positions that never get going.

## Local gate result (dev tape)

- Net PnL **+0.1425 SOL** on 10 SOL capital, 21 round trips over
  **21 distinct mints** (guardrails met: ≥ 20 mints, drawdown ≤ 50%)
- Win rate 61.9%, max drawdown 0.86%
- Still positive excluding the top-3 coins (+0.03) and at 2× fees (+0.05)

## Layout

- `main.py` — the bot (entry point the harness drives)
- `vendor/makefun_bot.py` — the competition SDK shim (unmodified)

---
Referral: dfkzjsa3
