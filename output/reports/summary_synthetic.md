# Discovery run summary: synthetic

| Symbol | Validated | Robustness | Edge | Win rate (OOS) | R:R (OOS) | Sharpe (OOS) | Trades (OOS) | Rule | Report |
|---|---|---|---|---|---|---|---|---|---|
| DEMO_TREND_PULLBACK | YES | 3/3 passed | +0.307 | 43% | 3.93 | 1.42 | 28 | MACD histogram crosses above zero AND price above SMA (period=20) | [3076caee](3076caee-2d7b-44de-9674-bdbe70755b1c.md) |
| DEMO_MEAN_REVERT | no | — | -0.161 | 39% | 2.99 | 0.57 | 18 | RSI turning up from oversold (period=14, threshold=34.8) | [44901000](44901000-9a8d-4e3f-9ea4-6d0b5b385e34.md) |
| DEMO_VOL_BREAKOUT | YES | 1/3 passed | +0.049 | 43% | 3.29 | 0.90 | 21 | close at/below lower Bollinger Band | [dbdba833](dbdba833-6e7a-41c6-bc19-f0281e7c7017.md) |
