# Discovery run summary: synthetic

| Symbol | Validated | Robustness | Edge | Win rate (OOS) | R:R (OOS) | Sharpe (OOS) | Trades (OOS) | Rule | Report |
|---|---|---|---|---|---|---|---|---|---|
| DEMO_TREND_PULLBACK | no | — | -0.436 | 44% | 0.65 | -1.14 | 18 | MACD histogram crosses above zero | [180f46b6](180f46b6-69f0-4130-9794-60409a321b60.md) |
| DEMO_TREND_PULLBACK | no | — | -0.436 | 44% | 0.65 | -1.14 | 18 | MACD line above signal AND MACD histogram crosses above zero | [3bb1b6f2](3bb1b6f2-b053-4f97-a409-7705363c7042.md) |
| DEMO_MEAN_REVERT | no | — | +0.328 | 56% | 2.26 | 1.08 | 9 | RSI oversold (period=14, threshold=37.7) AND stochastic oversold (period=14, threshold=21.0) | [c062a010](c062a010-8855-4288-9fc2-77e3f5e0535f.md) |
| DEMO_MEAN_REVERT | no | — | -0.093 | 38% | 3.25 | 1.19 | 24 | stochastic oversold (period=14, threshold=17.7) | [5188cf64](5188cf64-42e8-4b96-83bc-8b14f05ecf1c.md) |
| DEMO_VOL_BREAKOUT | no | — | -0.288 | 38% | 2.49 | 0.55 | 16 | fast SMA above slow SMA (fast=10, slow=50) AND MACD line above signal | [9bd57882](9bd57882-72e9-4e34-a0ad-76e44337dce4.md) |
