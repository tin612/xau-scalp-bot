#!/usr/bin/env python3
"""XAU (gold) scalping signal bot - signal-only, no order execution.

Pulls 1-min candles from Bitget USDT-M futures. Auto-switches source by session:
weekday gold CFD (XAUTUSDT, tracks spot) Mon-Fri, 24/7 perp (XAUUSDT) on the
weekend. Prints a BUY/SELL/HOLD signal with entry, TP, SL. Public market data,
no API key. You place the trade by hand.

    python3 xau_scalp.py                 # one shot
    python3 xau_scalp.py --now           # force BUY/SELL bias now (no HOLD)
    python3 xau_scalp.py --loop          # poll every 60s
    python3 xau_scalp.py --demo          # self-check, no network

Tune via env: TP_DOLLARS (7), SL_DOLLARS (3), CFD_SYMBOL (XAUTUSDT),
FUT_SYMBOL (XAUUSDT), PRODUCT (usdt-futures), INTERVAL (1m).
"""
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request

# Weekday = gold CFD (XAUTUSDT, tracks spot, follows session); weekend = 24/7 perp.
CFD_SYMBOL = os.environ.get("CFD_SYMBOL", "XAUTUSDT")  # Mon-Fri gold session
FUT_SYMBOL = os.environ.get("FUT_SYMBOL", "XAUUSDT")   # weekend 24/7 futures
PRODUCT = os.environ.get("PRODUCT", "usdt-futures")
INTERVAL = os.environ.get("INTERVAL", "1m")
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


def entry_now(closes):
    """On-demand 'if I enter right now, which side?' - forced BUY/SELL from trend
    (EMA9 vs EMA21). NOT a confirmed trigger like analyze(); it's directional bias.
    """
    if len(closes) < 22:
        return "HOLD", "need >=22 candles"
    e9, e21 = ema(closes, 9), ema(closes, 21)
    r = rsi(closes)[-1]
    if e9[-1] >= e21[-1]:
        return "BUY", f"trend bull (EMA9>=EMA21), RSI {r:.0f}"
    return "SELL", f"trend bear (EMA9<EMA21), RSI {r:.0f}"


def fetch_closes():
    sym = pick_symbol()
    url = (f"https://api.bitget.com/api/v2/mix/market/candles?symbol={sym}"
           f"&productType={PRODUCT}&granularity={INTERVAL}&limit=100")
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.load(resp)
    if data.get("msg") != "success" or not data.get("data"):
        sys.exit(f"Bitget error: {data.get('msg')} ({sym}/{PRODUCT})")
    rows = data["data"]  # oldest first; row = [ts, open, high, low, close, ...]
    return [float(r[4]) for r in rows], sym


def is_weekend(t=None):
    # Gold spot/CFD session is closed: Fri >=21:00 UTC, all Sat, Sun <22:00 UTC.
    t = t or time.gmtime()
    wd, h = t.tm_wday, t.tm_hour  # Mon=0 .. Sun=6
    return wd == 5 or (wd == 6 and h < 22) or (wd == 4 and h >= 21)


def pick_symbol():
    return FUT_SYMBOL if is_weekend() else CFD_SYMBOL


def notify(title, msg):
    if platform.system() != "Darwin":
        return
    subprocess.run(["osascript", "-e",
                    f'display notification "{msg}" with title "{title}"'],
                   check=False)


def push_phone(title, msg, actionable):
    # ntfy.sh push -> Android/iOS app. HOLD = silent low prio, BUY/SELL = loud.
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=msg.encode(),
        headers={"Title": title,
                 "Priority": "high" if actionable else "min",
                 "Tags": "chart_with_upwards_trend" if actionable else "zzz"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("ntfy err:", e)


def report(closes, sym):
    price = closes[-1]
    if "--now" in sys.argv:
        sig, reason = entry_now(closes)
        reason = "ENTRY NOW bias - " + reason
    else:
        sig, reason = analyze(closes)
    line = f"[{time.strftime('%H:%M:%S')}] {sym} {price:.2f}  ->  {sig}"
    tail = ""
    if sig == "BUY":
        tail = f"  entry {price:.2f} | TP {price + TP:.2f} | SL {price - SL:.2f}"
    elif sig == "SELL":
        tail = f"  entry {price:.2f} | TP {price - TP:.2f} | SL {price + SL:.2f}"
    print(line + tail + f"   ({reason})")
    title, body = f"XAU {sig} @ {price:.2f}", (tail.strip() or reason)
    if "--notify" in sys.argv:
        notify(title, body)
    push_phone(title, body, actionable=sig in ("BUY", "SELL"))


def demo():
    # Uptrend, then a pullback (RSI dips ~<50), then a bounce (RSI crosses up) = BUY.
    up = [2600 + i * 0.8 for i in range(30)] + [2620, 2616, 2613, 2619]
    s, r = analyze(up)
    assert s == "BUY", (s, r)
    down = [2700 - i * 0.8 for i in range(30)] + [2680, 2684, 2687, 2681]
    s, r = analyze(down)
    assert s == "SELL", (s, r)
    assert analyze([2650.0] * 40)[0] == "HOLD"  # flat market
    # entry_now: never HOLD on trending data, picks the trend side.
    assert entry_now([2600 + i for i in range(30)])[0] == "BUY"
    assert entry_now([2700 - i for i in range(30)])[0] == "SELL"
    # weekend switch (wday: Mon=0..Sun=6)
    mk = lambda wd, h: time.struct_time((2026, 7, 1, h, 0, 0, wd, 1, 0))
    assert is_weekend(mk(5, 10))          # Saturday
    assert is_weekend(mk(6, 10))          # Sunday morning
    assert not is_weekend(mk(6, 23))      # Sunday after 22:00 UTC reopen
    assert not is_weekend(mk(2, 12))      # Wednesday
    assert is_weekend(mk(4, 22))          # Friday after close
    assert not is_weekend(mk(4, 12))      # Friday midday
    print("demo ok:", analyze(up), analyze(down), entry_now(up))


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif "--loop" in sys.argv:
        while True:
            try:
                report(*fetch_closes())
            except Exception as e:
                print("err:", e)
            time.sleep(60)
    else:
        report(*fetch_closes())
