# xau-scalp-bot

XAU (gold) scalping **signal** bot - signal-only, no order execution. Pulls
1-min candles from **Bitget USDT-M futures** (`XAUUSDT` gold perp - same price
shown on Bitget/BingX) and prints BUY/SELL/HOLD with entry, TP, SL. Public
market data, **no API key**.

## Run
```bash
python3 xau_scalp.py         # one shot, current signal
python3 xau_scalp.py --now   # force BUY/SELL bias now (never HOLD)
python3 xau_scalp.py --loop  # poll every 60s
python3 xau_scalp.py --demo  # self-check, no network
```

Phone push: set `NTFY_TOPIC` and subscribe that topic in the ntfy app.
On-demand from phone: GitHub Actions tab -> xau-signal -> Run workflow.

## Strategy
EMA9 vs EMA21 sets trend; RSI(14) crossing 50 in the trend direction times entry.
BUY = uptrend + RSI turns up, SELL = downtrend + RSI turns down, else HOLD.
`--now` skips the RSI filter and returns the trend-side bias immediately.
TP default 7 / SL default 3 (tune via `TP_DOLLARS` / `SL_DOLLARS`).

Heuristic, not financial advice. Watch it for a few sessions before trusting it.
