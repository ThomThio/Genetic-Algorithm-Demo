# Plan: FX price polling → entry heuristic → MT4 limit ("better-price") orders

## 0. Goal and terminology

1. **Poll** live FX prices (bid/ask) for a watchlist of pairs.
2. Run an **entry heuristic** on each update that answers three questions:
   - *Should we trade now?* (go / no-go)
   - *Which side?* (BUY or SELL)
   - *How far from the market should the entry sit?* (offset)
3. If it's a go, place a **resting entry order away from the current price**
   (the "fighter" entry in the request, read here as a passive entry that asks for a better price):
   - BUY  → `OP_BUYLIMIT`  at `Bid − offset` (bid below the market)
   - SELL → `OP_SELLLIMIT` at `Ask + offset` (offer above the market)

   The order only fills if price comes back to us, so we get a better fill than a
   market order and pay no spread-crossing slippage.
4. Manage the pending order: expire it, re-price it, or cancel it if the setup goes away.

(If "fighter" meant a breakout entry, one that follows price through a level
instead of waiting for it to come back, the plumbing stays the same. Only the order type
changes, to `OP_BUYSTOP` above / `OP_SELLSTOP` below. The design keeps order type as config.)

---

## 1. Architecture

MT4 has no official Python API (only MT5 has the `MetaTrader5` package), so we
use an **MQL4 Expert Advisor (EA) bridge** running inside the terminal, with
the strategy logic in Python.

```
┌─────────────────────────── MT4 terminal (Windows / Wine) ───────────────────────────┐
│  BridgeEA.mq4                                                                        │
│   • OnTimer(100–250ms): read MarketInfo(Bid/Ask/Spread) for watchlist → PUB ticks    │
│   • REP socket: receive JSON commands → OrderSend / OrderModify / OrderDelete        │
│   • PUB order events (fills, rejects, expiries) and account snapshot                 │
└───────────────▲───────────────────────────────────────────────┬─────────────────────┘
                │ commands (REQ/REP, tcp://:5555)                │ ticks + events (PUB/SUB, tcp://:5556)
┌───────────────┴───────────────────────────────────────────────▼─────────────────────┐
│  Python service                                                                      │
│   price_feed.py   – subscribes to ticks, builds bars (M1/M5/H1), keeps rolling state │
│   signals.py      – entry heuristic (pure functions, no I/O)                         │
│   risk.py         – position sizing, exposure / daily-loss limits, kill switch       │
│   order_manager.py– pending-order lifecycle (place, re-price, expire, cancel)        │
│   mt4_bridge.py   – ZeroMQ client, JSON protocol, retries, idempotency keys          │
│   backtest.py     – replays historical bars through signals + fill simulator         │
│   optimize_ga.py  – reuses this repo's GA to evolve heuristic parameters             │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### Transport options (pick one)

| Option | How | Pros | Cons |
|---|---|---|---|
| **ZeroMQ bridge (recommended)** | `mql-zmq` DLL in the EA, `pyzmq` in Python (same pattern as the open-source DWX ZeroMQ connector) | Low latency (ms), push-based ticks, two-way | Needs "Allow DLL imports"; DLL setup under Wine |
| File bridge | EA writes `ticks.csv` / reads `commands.json` in `MQL4/Files` | Zero dependencies, easy to debug | 100ms–1s latency, file-locking edge cases |
| HTTP | EA calls `WebRequest()` to a local Flask/FastAPI server | No DLLs | EA must poll for commands; URL whitelist config |

### Price source

**Poll prices from MT4 itself, not from a third-party feed.** Fills happen against the
broker's quotes, so the heuristic and the limit price must use the same bid/ask.
An external feed (OANDA, Polygon, TwelveData) is fine for **research and backtests** but not
for live pricing. You can run one next to MT4 as a sanity check on the broker's feed.

"Polling" in practice: the EA's `OnTimer` loop samples `MarketInfo(sym, MODE_BID/ASK)`
every 100–250 ms for all watchlist symbols. `OnTick` only fires for the chart's symbol,
so a timer is needed for multiple pairs. The EA publishes only when a price changed.

---

## 2. Bridge protocol (JSON over ZMQ)

Commands (Python → EA, REQ/REP):

```json
{"id":"c-8f1e", "action":"PLACE", "symbol":"EURUSD", "type":"BUY_LIMIT",
 "lots":0.10, "price":1.08412, "sl":1.08212, "tp":1.08812,
 "expiry_utc":"2026-09-25T14:30:00Z", "magic":20260925, "comment":"sig:pullback-v1"}
{"id":"c-8f1f", "action":"MODIFY", "ticket":123456, "price":1.08420, "sl":..., "tp":...}
{"id":"c-8f20", "action":"CANCEL", "ticket":123456}
{"id":"c-8f21", "action":"POSITIONS"}          // open + pending orders for our magic number
{"id":"c-8f22", "action":"ACCOUNT"}            // balance, equity, margin, leverage
```

Replies: `{"id":"c-8f1e","ok":true,"ticket":123456}` or `{"ok":false,"err":130,"msg":"invalid stops"}`.

Events (EA → Python, PUB): `TICK`, `FILLED`, `EXPIRED`, `CANCELLED`, `CLOSED`, `REJECTED`, `HEARTBEAT`.

EA rules that save a lot of debugging:
- Normalize prices with `NormalizeDouble(price, Digits)`. Check `MODE_STOPLEVEL` and
  `MODE_FREEZELEVEL`: a limit closer than the stop level gets rejected with error 130.
- Put the `id` in the order comment. On reconnect, Python calls `POSITIONS` and reconciles,
  so a retried command never creates a duplicate order (idempotency).
- Send a heartbeat every second. If Python sees no heartbeat for more than 5 s, it stops sending new orders.

---

## 3. Entry heuristic

Keep it as a **pure function** so the same code runs live, in backtests, and inside the GA:

```python
def evaluate_entry(state: MarketState, params: Params) -> EntryDecision | None:
    ...
```

`MarketState` holds the current bid/ask, rolling bars (M5 + H1), ATR, spread history,
session clock, open exposure, and upcoming news events.

### 3.1 Hard gates (all must pass, or no trade)

| Gate | Rule (defaults are starting points for tuning) |
|---|---|
| Spread | `spread ≤ max(spread_abs_pips, spread_atr_frac × ATR_M5)` and `spread ≤ 1.5 × median spread (last 1h)` |
| Session | Only London (07:00–16:00 UTC) and NY (12:00–21:00 UTC); skip 21:00–23:00 UTC rollover |
| News | No new orders from 15 min before to 15 min after high-impact events for either currency |
| Volatility regime | `ATR_M5` percentile (30-day window) between 20th and 90th: not dead, not chaotic |
| Exposure | Max N open/pending per pair, max total risk %, daily loss limit not hit |
| Staleness | Last tick is younger than 2 s and the bridge heartbeat is healthy |

### 3.2 Direction and setup score (0–1)

Combine a **trend filter** with a **pullback trigger**. We want to buy dips in an uptrend and
sell rallies in a downtrend, which suits resting limit orders naturally:

```
trend      = sign(EMA_H1(fast) − EMA_H1(slow)) and |slope(EMA_H1(slow))| > min_slope
direction  = BUY if trend > 0 else SELL
stretch_z  = (mid − EMA_M5(20)) / ATR_M5          # how far price has run from its mean
pullback   = −stretch_z if BUY else +stretch_z     # positive = price has pulled back
rsi_ok     = RSI_M5(14) < rsi_buy_max (BUY)  /  > rsi_sell_min (SELL)
momentum   = short-term rate of change is decelerating against us (pullback losing steam)

score = w1·trend_strength + w2·clip(pullback) + w3·rsi_ok + w4·momentum_fade
                          − w5·spread_penalty − w6·vol_penalty
go if score ≥ score_threshold
```

### 3.3 Entry offset (how far from the market to bid or offer)

The offset should scale with volatility and snap to real structure:

```
raw_offset   = k_atr × ATR_M5                        # e.g. k_atr = 0.3–0.8
struct_level = nearest swing low (BUY) / swing high (SELL) on M5 within lookback
offset       = choose(raw_offset, distance to struct_level + buffer)  # prefer structure if within [0.5×, 1.5×] raw_offset
offset       = max(offset, stop_level + 1 point)     # broker minimum distance
BUY  limit   = Bid − offset
SELL limit   = Ask + offset
```

Trade-off to tune: a **larger offset** gets a better price but a lower fill rate, and the fills skew
toward adverse moves (you get filled because price is running against you). The
backtest has to measure **fill rate** and **post-fill drift**, not just P&L.

### 3.4 Exits and sizing

- SL = entry ∓ `sl_atr × ATR_M5` (beyond the structure level); TP = `rr × SL distance`.
- Lots = `(equity × risk_pct) / (SL_pips × pip_value)`, rounded to `MODE_LOTSTEP` and clamped to
  `MODE_MINLOT`/`MODE_MAXLOT`.

---

## 4. Pending-order lifecycle (order_manager.py)

```
IDLE ──signal go──▶ PLACING ──ack──▶ WORKING ──fill──▶ FILLED (hand off to position mgmt)
                       │                │
                       └─reject─▶ IDLE  ├─ price ran > reprice_atr×ATR away  → MODIFY (max R re-prices)
                                        ├─ score < cancel_threshold            → CANCEL
                                        ├─ gate fails (news/spread/session)    → CANCEL
                                        └─ expiry (ttl_minutes)                → EXPIRED → IDLE (cooldown)
```

- Set the TTL in two places: MT4 `expiration` on the order, plus a local timer as backup,
  because some brokers reject `expiration` on pending orders.
- Cooldown per pair after a cancel or expiry, so the system doesn't keep re-placing the same order.
- Re-pricing is rate-limited (e.g. at most once every 30 s) so brokers don't flag excessive modifications.

---

## 5. Backtesting and tuning with this repo's GA

`ga.py` already has a bit-string chromosome with crossover and mutation. We can reuse
it to evolve the heuristic's parameters:

- **Genes** (encoded as N-bit ints mapped to ranges): `k_atr`, `score_threshold`,
  `w1..w6`, `rsi_buy_max`, `sl_atr`, `rr`, `ttl_minutes`, `reprice_atr`, EMA lengths.
- **Fitness** = a walk-forward backtest over tick or M1 data (with the spread modeled), e.g.
  `Sharpe × sqrt(trades)` with penalties for max drawdown above X% and fill rate below Y%.
- **Fill simulator**: a limit fills only if price trades **through** it (bid ≤ buy limit, or
  ask ≥ sell limit), which is a conservative assumption about queue position.
- Guard against overfitting: train/validate/test split by time, parameter-stability check (neighbours
  of the best chromosome should also be profitable), and a minimum trade count.

---

## 6. Build order (milestones)

1. **Bridge MVP**: EA publishes ticks for 3 pairs; a Python script prints them. Demo account only.
2. **Order round-trip**: place, modify, and cancel a BUY_LIMIT and SELL_LIMIT from Python on the demo
   account; handle errors 130/131/134/136/146; reconcile after an EA restart.
3. **State and bars**: tick → M1/M5/H1 bars, ATR/EMA/RSI, spread stats; verify against MT4 indicators.
4. **Heuristic v1**: pure function plus unit tests on hand-crafted states (each gate, each side).
5. **Backtester and fill simulator**: run heuristic v1 on 1–2 years of data and report fill rate, drift, P&L.
6. **GA optimization**: `optimize_ga.py` with walk-forward validation.
7. **Paper trading**: live demo account for 2–4 weeks, then compare live fills against the backtest.
8. **Live with minimal size**, kill switch, alerts (Telegram/email), daily P&L report.

## 7. Risk and ops checklist

- Global kill switch (file flag or command) that cancels all pending orders and stops new ones.
- Daily loss limit and max consecutive losses → pause until the next session.
- Run the EA on a VPS near the broker; Python on the same box to avoid network hops.
- Log every decision (inputs, score, and the reason a gate blocked) for post-mortems.
- Magic number isolates this system's orders from manual trades.
- Check the broker's FIFO/hedging rules (US accounts) and the minimum pending distance.
