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
import datetime
import json
import os
import platform
import tempfile
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
# News blackout: stand aside around high-impact USD events (gold whipsaws).
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_BEFORE = int(os.environ.get("NEWS_BEFORE_MIN", "30"))  # minutes before event
NEWS_AFTER = int(os.environ.get("NEWS_AFTER_MIN", "15"))    # minutes after event
NEWS_CACHE = os.path.join(tempfile.gettempdir(), "xau_news_cache.json")
NEWS_TTL = 3600                                             # refetch feed at most hourly


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


def fetch_ohlc(want):
    # Bitget caps 1000 candles/request; page backwards with endTime to get `want`.
    sym = pick_symbol()
    base = (f"https://api.bitget.com/api/v2/mix/market/candles?symbol={sym}"
            f"&productType={PRODUCT}&granularity={INTERVAL}&limit=1000")
    rows, end = [], None
    while len(rows) < want:
        url = base + (f"&endTime={end}" if end else "")
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.load(resp)
        page = data.get("data") or []
        if not page:
            break
        rows = page + rows          # each page oldest-first; older pages prepend
        end = int(page[0][0])       # next page ends just before this page's oldest
        if len(page) < 1000:
            break
    if not rows:
        sys.exit(f"Bitget error: {data.get('msg')} ({sym})")
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    closes = [float(r[4]) for r in rows]
    return highs, lows, closes, sym


def simulate(highs, lows, closes):
    """Replay analyze() over history; for each BUY/SELL, scan forward for the
    first TP or SL hit. Non-overlapping (next signal only after prior resolves).
    ponytail: if a candle's range hits BOTH TP and SL, count SL first
    (conservative - don't inflate win-rate). Upgrade path: use tick data if
    the intrabar order actually matters for your edge.
    """
    trades, i, n = [], 30, len(closes)
    while i < n - 1:
        sig, _ = analyze(closes[:i + 1])
        if sig not in ("BUY", "SELL"):
            i += 1
            continue
        entry = closes[i]
        tp = entry + TP if sig == "BUY" else entry - TP
        sl = entry - SL if sig == "BUY" else entry + SL
        outcome, j = None, i + 1
        while j < n:
            hit_tp = highs[j] >= tp if sig == "BUY" else lows[j] <= tp
            hit_sl = lows[j] <= sl if sig == "BUY" else highs[j] >= sl
            if hit_sl:
                outcome = "SL"
                break
            if hit_tp:
                outcome = "TP"
                break
            j += 1
        if outcome is None:      # still open at end of data -> stop
            break
        trades.append((sig, outcome))
        i = j + 1                # resume after the trade closed
    return trades


def simulate_forced(highs, lows, closes, step=10):
    """Forced mode: every `step` minutes take the trend-side bias (entry_now),
    each signal an independent trade resolved by first TP/SL. Mirrors the live
    "a signal every 10 min" behaviour - trades overlap, as they do live.
    """
    trades, n = [], len(closes)
    for i in range(30, n - 1, step):
        sig, _ = entry_now(closes[:i + 1])
        if sig not in ("BUY", "SELL"):
            continue
        entry = closes[i]
        tp = entry + TP if sig == "BUY" else entry - TP
        sl = entry - SL if sig == "BUY" else entry + SL
        for j in range(i + 1, n):
            hit_tp = highs[j] >= tp if sig == "BUY" else lows[j] <= tp
            hit_sl = lows[j] <= sl if sig == "BUY" else highs[j] >= sl
            if hit_sl:
                trades.append((sig, "SL"))
                break
            if hit_tp:
                trades.append((sig, "TP"))
                break
    return trades


def run_sim(days=7, forced=False):
    highs, lows, closes, sym = fetch_ohlc(days * 1440)
    trades = simulate_forced(highs, lows, closes) if forced else simulate(highs, lows, closes)
    r = sim_account(trades, closes[-1])
    mode = "forced/10min" if forced else "selective"
    print(f"XAU sim {sym} [{mode}] TP{TP:.0f}/SL{SL:.0f} over ~{len(closes)/1440:.1f}d")
    print(f"  ${r['start']:.0f} -> ${r['final']:.2f}  ({r['trades']} trades, "
          f"maxDD {r['maxdd']:.0f}%)")
    print(f"  goal ${r['goal']:.0f}: " +
          (f"HIT at trade #{r['hit']}" if r['hit'] else "NOT reached"))
    print(f"  fees total ${r['fees']:.2f} | per-trade: win ${r['win_dollar']:.2f} "
          f"vs SL ${r['risk_dollar']:.2f} vs fee ${r['fee_per_trade']:.2f}")
    if r['fee_per_trade'] >= r['win_dollar']:
        print("  ** FEE >= WIN: edge is dead - fees eat every winning trade **")


def backtest(days=3, forced=False):
    highs, lows, closes, sym = fetch_ohlc(days * 1440)
    trades = simulate_forced(highs, lows, closes) if forced else simulate(highs, lows, closes)
    wins = sum(1 for _, o in trades if o == "TP")
    losses = len(trades) - wins
    wr = wins / len(trades) * 100 if trades else 0.0
    net = wins * TP - losses * SL
    d = len(closes) / 1440
    mode = "forced/10min" if forced else "selective"
    title = f"XAU backtest {sym} [{mode}]: {wr:.0f}% win ({len(trades)} trades)"
    body = (f"~{d:.1f}d | {len(trades)} trades | TP {wins} SL {losses} | "
            f"win-rate {wr:.1f}% | net {net:+.1f} pts (TP{TP:.0f}/SL{SL:.0f})")
    print(title + "\n" + body)
    push_phone(title, body, actionable=True)


def daily_summary():
    # End-of-day digest: today's price action + a 3-day backtest. Stateless (one
    # fetch), pushed silently to phone for reference. HOLD is fine here.
    highs, lows, closes, sym = fetch_ohlc(3 * 1440)
    price = closes[-1]
    day = closes[-1440:] if len(closes) >= 1440 else closes
    d_open, d_hi, d_lo = day[0], max(highs[-len(day):]), min(lows[-len(day):])
    chg = price - d_open
    pct = chg / d_open * 100 if d_open else 0.0
    sig, _ = analyze(closes)
    bias, _ = entry_now(closes)
    trades = simulate(highs, lows, closes)
    n = len(trades)
    wins = sum(1 for _, o in trades if o == "TP")
    wr = wins / n * 100 if n else 0.0
    net = wins * TP - (n - wins) * SL
    # today's $ P&L if you'd traded the signals on a $300 base (fee-aware, SIMULATED)
    base = float(os.environ.get("BASE_USD", "300"))
    today = simulate(highs[-1440:], lows[-1440:], closes[-1440:])
    r = sim_account(today, price, start=base)
    tw = sum(1 for _, o in today if o == "TP")
    pnl = r["final"] - base
    title = f"XAU tong ket {time.strftime('%Y-%m-%d')}"
    body = (f"{sym} {price:.2f} (24h {chg:+.1f} / {pct:+.2f}%) H{d_hi:.0f} L{d_lo:.0f}\n"
            f"trend: {bias} | tin hieu: {sig}\n"
            f"HOM NAY (mo phong ${base:.0f}, co phi): {pnl:+.2f}$ "
            f"({len(today)} lenh, {tw} TP)\n"
            f"backtest 3d: {wr:.0f}% win, {n} lenh, net {net:+.0f} diem")
    print(title + "\n" + body)
    push_phone(title, body, actionable=False, prio="low")  # reference, silent but visible


def simulate_capped(highs, lows, closes, per_day):
    """Like simulate() but takes at most `per_day` selective signals per 24h
    (1m candles: day bucket = index // 1440). Models 'I take up to N trades a day'.
    """
    trades, i, n, per = [], 30, len(closes), {}
    while i < n - 1:
        day = i // 1440
        if per.get(day, 0) >= per_day:
            i = (day + 1) * 1440          # daily quota hit -> skip to next day
            continue
        sig, _ = analyze(closes[:i + 1])
        if sig not in ("BUY", "SELL"):
            i += 1
            continue
        entry = closes[i]
        tp = entry + TP if sig == "BUY" else entry - TP
        sl = entry - SL if sig == "BUY" else entry + SL
        outcome, j = None, i + 1
        while j < n:
            hit_tp = highs[j] >= tp if sig == "BUY" else lows[j] <= tp
            hit_sl = lows[j] <= sl if sig == "BUY" else highs[j] >= sl
            if hit_sl:
                outcome = "SL"
                break
            if hit_tp:
                outcome = "TP"
                break
            j += 1
        if outcome is None:
            break
        trades.append((sig, outcome))
        per[day] = per.get(day, 0) + 1
        i = j + 1
    return trades


def paper_run():
    # Forward paper-trade: replay the window, take up to N selective signals PER
    # DAY on a $300 base (fee-aware). Deterministic strategy -> replaying the past
    # week == paper-trading it forward. Env: PAPER_DAYS(7) PAPER_PER_DAY(10) BASE_USD(300).
    days = int(os.environ.get("PAPER_DAYS", "7"))
    per_day = int(os.environ.get("PAPER_PER_DAY", "10"))
    base = float(os.environ.get("BASE_USD", "300"))
    highs, lows, closes, sym = fetch_ohlc(days * 1440)
    trades = simulate_capped(highs, lows, closes, per_day)
    n = len(trades)
    wins = sum(1 for _, o in trades if o == "TP")
    wr = wins / n * 100 if n else 0.0
    pnl = sim_account(trades, closes[-1], start=base)["final"] - base if n else 0.0
    pct = pnl / base * 100 if base else 0.0
    target = float(os.environ.get("TARGET_PCT", "10"))   # weekly target %, alert if hit
    hit = pct >= target
    # target hit -> loud alert; otherwise routine silent report showing the gap
    title = "XAU TARGET DAT {:+.0f}%!".format(pct) if hit else f"XAU paper {days}d"
    body = (f"backtest {days}d: {wr:.0f}% win, {n} lenh (~{n / days:.0f}/ngay)\n"
            f"net {pnl:+.2f}$ = {pct:+.1f}% tren ${base:.0f} "
            f"(target +{target:.0f}%: {'DAT' if hit else 'chua dat'})")
    print(title + "\n" + body)
    push_phone(title, body, actionable=hit, prio=("high" if hit else "low"))


def _parse_news(raw):
    out = []
    for e in raw:
        if e.get("impact") != "High" or e.get("country") != "USD":
            continue
        try:
            ts = datetime.datetime.fromisoformat(e["date"]).timestamp()
        except Exception:
            continue
        out.append((ts, e.get("title", "?")))
    return out


def _read_cache():
    with open(NEWS_CACHE) as f:
        return _parse_news(json.load(f))


def fetch_news():
    # High-impact USD events (Forex Factory). Cached hourly to dodge 429 rate
    # limits; on a fetch failure, fall back to the stale cache if we have one.
    try:
        if time.time() - os.path.getmtime(NEWS_CACHE) < NEWS_TTL:
            return _read_cache()
    except OSError:
        pass
    req = urllib.request.Request(NEWS_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.load(resp)
        with open(NEWS_CACHE, "w") as f:
            json.dump(raw, f)
        return _parse_news(raw)
    except Exception:
        return _read_cache()   # stale cache; raises OSError only if none exists


def news_now(now_ts=None, events=None):
    """Nearest high-impact event inside [now-AFTER, now+BEFORE], else None.
    Network failure -> None (never block signals on a flaky feed).
    """
    now_ts = time.time() if now_ts is None else now_ts
    if events is None:
        try:
            events = fetch_news()
        except Exception:
            return None
    best = None
    for ts, title in events:
        mins = (ts - now_ts) / 60
        if -NEWS_AFTER <= mins <= NEWS_BEFORE and (best is None or abs(mins) < abs(best[1])):
            best = (title, mins)
    return best


def show_news():
    now = time.time()
    try:
        events = fetch_news()
    except Exception as e:
        print(f"news feed unavailable ({e})")
        return
    up = sorted(t for t in events if t[0] > now)
    if not up:
        print("No upcoming high-impact USD events this week.")
        return
    for ts, title in up:
        when = datetime.datetime.fromtimestamp(ts)
        print(f"{when:%a %m-%d %H:%M} (in {(ts - now) / 3600:.1f}h)  {title}")


def sim_account(trades, price, start=None):
    """Walk the trade sequence as a real $ account: position sized by risk-%,
    compounding, with exchange fees on notional. Answers 'can $50 reach $100'.
    Env: ACCOUNT(50) GOAL(100) RISK_PCT(2) FEE_PCT(0.06 Bitget taker round-trip
    per side). XAUUSDT: 1 unit = $1 per point, so units = $/point.
    """
    acct = start = float(start if start is not None else os.environ.get("ACCOUNT", "50"))
    goal = float(os.environ.get("GOAL", "100"))
    risk_pct = float(os.environ.get("RISK_PCT", "2")) / 100
    fee_pct = float(os.environ.get("FEE_PCT", "0.06")) / 100
    peak, maxdd, hit, fees_paid = acct, 0.0, None, 0.0
    for k, (_, out) in enumerate(trades, 1):
        per_pt = (acct * risk_pct) / SL          # $ risked per point
        fee = per_pt * price * fee_pct * 2       # open + close
        fees_paid += fee
        acct += (TP if out == "TP" else -SL) * per_pt - fee
        peak = max(peak, acct)
        maxdd = max(maxdd, (peak - acct) / peak if peak else 0)
        if acct <= 0:
            acct = 0
            break
        if hit is None and acct >= goal:
            hit = k
    # per-trade fee vs risk: if fee >= win, the edge is dead on arrival
    ex_pt = per_pt if trades else 0
    return {"final": acct, "start": start, "goal": goal, "hit": hit,
            "trades": len(trades), "maxdd": maxdd * 100, "fees": fees_paid,
            "fee_per_trade": fees_paid / len(trades) if trades else 0,
            "win_dollar": TP * ex_pt, "risk_dollar": SL * ex_pt}


def notify(title, msg):
    if platform.system() != "Darwin":
        return
    subprocess.run(["osascript", "-e",
                    f'display notification "{msg}" with title "{title}"'],
                   check=False)


def push_phone(title, msg, actionable, prio=None):
    # ntfy.sh push -> Android/iOS app. HOLD = silent low prio, BUY/SELL = loud.
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=msg.encode(),
        headers={"Title": title,
                 # low = shows notification but no sound/vibration; min = history only
                 "Priority": prio or ("low" if actionable else "min"),
                 "Tags": "chart_with_upwards_trend" if actionable else "zzz"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("ntfy err:", e)


def report(closes, sym):
    price = closes[-1]
    if "--now" in sys.argv:
        sig, reason = entry_now(closes)
        reason = "forced bias - " + reason
        ev = news_now()
        if ev:
            reason += f" | WARN news {ev[0]} in {ev[1]:+.0f}m"
    else:
        ev = news_now()
        if ev:
            sig, reason = "HOLD", f"NEWS {ev[0]} in {ev[1]:+.0f}m - stand aside"
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
    # simulate: a BUY that runs +TP without hitting SL must score one TP win.
    seq = [2600 + i * 0.8 for i in range(30)] + [2620, 2616, 2613, 2619]
    seq += [seq[-1] + 3 * k for k in range(1, 6)]  # rally past TP
    hi = [c + 0.2 for c in seq]
    lo = [c - 0.2 for c in seq]                    # never dips to SL
    tr = simulate(hi, lo, seq)
    assert tr and tr[0] == ("BUY", "TP"), tr
    tf = simulate_forced(hi, lo, seq, step=5)   # forced also scores the rally
    assert ("BUY", "TP") in tf, tf
    # sim_account: all-wins grows, all-losses shrinks
    assert sim_account([("BUY", "TP")] * 20, 4120)["final"] > 50
    assert sim_account([("BUY", "SL")] * 20, 4120)["final"] < 50
    # news blackout window: [-AFTER, +BEFORE] around event T (default 15 / 30 min)
    T, evs = 1_000_000.0, [(1_000_000.0, "CPI")]
    assert news_now(T - 20 * 60, evs)[0] == "CPI"   # 20m before -> blackout
    assert news_now(T + 10 * 60, evs)[0] == "CPI"   # 10m after  -> blackout
    assert news_now(T - 40 * 60, evs) is None       # 40m before -> clear
    assert news_now(T + 20 * 60, evs) is None       # 20m after  -> clear
    print("demo ok:", analyze(up), analyze(down), entry_now(up), "bt", tr)


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif "--news" in sys.argv:
        show_news()
    elif "--daily" in sys.argv:
        daily_summary()
    elif "--paper" in sys.argv:
        paper_run()
    elif "--sim" in sys.argv:
        nums = [int(a) for a in sys.argv if a.isdigit()]
        run_sim(nums[0] if nums else 7, forced="--forced" in sys.argv)
    elif "--backtest" in sys.argv:
        nums = [int(a) for a in sys.argv if a.isdigit()]
        backtest(nums[0] if nums else 3, forced="--forced" in sys.argv)   # days
    elif "--loop" in sys.argv:
        while True:
            try:
                report(*fetch_closes())
            except Exception as e:
                print("err:", e)
            time.sleep(60)
    else:
        report(*fetch_closes())
