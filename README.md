# xau-scalp-bot

XAU (gold) scalping **signal** bot - signal-only, no order execution. Pulls
1-min XAU/USD candles from Twelve Data and prints BUY/SELL/HOLD with entry, TP, SL.

## Run
```bash
export TWELVEDATA_API_KEY=xxx      # free key: https://twelvedata.com
python3 xau_scalp.py               # one shot
python3 xau_scalp.py --loop        # poll every 60s
python3 xau_scalp.py --demo        # self-check, no network
```

## Strategy
EMA9 vs EMA21 sets trend; RSI(14) crossing 50 in the trend direction times entry.
BUY = uptrend + RSI turns up, SELL = downtrend + RSI turns down, else HOLD.
TP default 7 / SL default 3 (tune via `TP_DOLLARS` / `SL_DOLLARS`).

Heuristic, not financial advice. Watch it for a few sessions before trusting signals.
