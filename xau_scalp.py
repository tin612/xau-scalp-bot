#!/usr/bin/env python3
"""XAU (gold) scalping signal bot - signal-only, no order execution.

Pulls 1-min XAU/USD candles from Twelve Data, prints a BUY/SELL/HOLD signal
with entry, TP, SL. You place the trade by hand.

Setup:
    export TWELVEDATA_API_KEY=xxx        # free key: https://twelvedata.com
    python3 xau_scalp.py                 # one shot
    python3 xau_scalp.py --loop          # poll every 60s
    python3 xau_scalp.py --demo          # self-check, no network

Tune via env: TP_DOLLARS (default 7), SL_DOLLARS (default 3), SYMBOL, INTERVAL.
"""
import json
import os
import sys
import time
import urllib.request

API_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
SYMBOL = os.environ.get("SYMBOL", "XAU/USD")
INTERVAL = os.environ.get("INTERVAL", "1min")
TP = float(os.environ.get("TP_DOLLARS", "7"))   # 5-10 range
SL = float(os.environ.get("SL_DOLLARS", "3"))   # 2-5 range


def ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    out = [e]
    for v in values[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, period=14):
    # Wilder's smoothing (the standard RSI, correct on flat/edge cases).
    if len(closes) <= period:
        return [50.0] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out = [50.0] * (period + 1)
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l > 0 else float("inf")
        out.append(100 - 100 / (1 + rs))
    return out


def analyze(closes):
    """Return (signal, reason). Bull/bear trend from EMA9 vs EMA21, entry timed
    by RSI momentum crossing 50 in the trend direction.
    ponytail: naive EMA+RSI scalp heuristic. Upgrade path if win-rate lags:
    add ATR-based TP/SL, VWAP filter, or session/time-of-day gating.
    """
    if len(closes) < 30:
        return "HOLD", "need >=30 candles"
    e9, e21 = ema(closes, 9), ema(closes, 21)
    r = rsi(closes)
    bull = e9[-1] > e21[-1]
    bear = e9[-1] < e21[-1]
    r_now, r_prev = r[-1], r[-2]
    if bull and r_prev < 50 <= r_now:
        return "BUY", f"uptrend (EMA9>EMA21), RSI turned up {r_prev:.0f}->{r_now:.0f}"
    if bear and r_prev > 50 >= r_now:
        return "SELL", f"downtrend (EMA9<EMA21), RSI turned down {r_prev:.0f}->{r_now:.0f}"
    return "HOLD", f"trend={'bull' if bull else 'bear'}, RSI={r_now:.0f} (no entry trigger)"


def fetch_closes():
    if not API_KEY:
        sys.exit("Missing TWELVEDATA_API_KEY (free at twelvedata.com)")
    url = (f"https://api.twelvedata.com/time_series?symbol={SYMBOL}"
           f"&interval={INTERVAL}&outputsize=50&apikey={API_KEY}")
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.load(resp)
    if data.get("status") == "error":
        sys.exit(f"API error: {data.get('message')}")
    vals = data["values"]  # newest first
    return [float(v["close"]) for v in reversed(vals)]


def report(closes):
    price = closes[-1]
    sig, reason = analyze(closes)
    line = f"[{time.strftime('%H:%M:%S')}] {SYMBOL} {price:.2f}  ->  {sig}"
    if sig == "BUY":
        line += f"  entry {price:.2f} | TP {price + TP:.2f} | SL {price - SL:.2f}"
    elif sig == "SELL":
        line += f"  entry {price:.2f} | TP {price - TP:.2f} | SL {price + SL:.2f}"
    print(line + f"   ({reason})")


def demo():
    # Uptrend, then a pullback (RSI dips ~<50), then a bounce (RSI crosses up) = BUY.
    up = [2600 + i * 0.8 for i in range(30)] + [2620, 2616, 2613, 2619]
    s, r = analyze(up)
    assert s == "BUY", (s, r)
    down = [2700 - i * 0.8 for i in range(30)] + [2680, 2684, 2687, 2681]
    s, r = analyze(down)
    assert s == "SELL", (s, r)
    assert analyze([2650.0] * 40)[0] == "HOLD"  # flat market
    print("demo ok:", analyze(up), analyze(down))


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif "--loop" in sys.argv:
        while True:
            try:
                report(fetch_closes())
            except Exception as e:
                print("err:", e)
            time.sleep(60)
    else:
        report(fetch_closes())
