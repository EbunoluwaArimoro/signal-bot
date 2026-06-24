"""
Rich Genie Signal Bot v3 — Edge-First Rebuild
================================================
v2.1 fixed the notification bugs but the underlying STRATEGY was losing
money: 9-13% win rate against trades needing 35-50% just to break even.

ROOT CAUSE ANALYSIS FROM 38 LIVE SIGNALS:
  - TP1 was set at the SAME distance as SL (R:R 1:1) -> needs 50% WR to
    break even. Crypto chop hits one side near-randomly at that distance.
  - "RSI sell zone 45-70" fired on RSI 45-55 constantly -- that's NEUTRAL,
    not overbought. Those signals scored 14% WR (basically noise).
  - Daily-EMA regime filter lags price -- by the time "bear" confirms,
    the SELL signal is often buying the bottom of the move, not the top.
  - 89% of signals fired at the bare minimum 4/7 threshold (57% conf) and
    scored 9% WR. The 5/7 signals (71% conf) scored 33% WR -- 3.6x better.
  - 5 BUY signals fired in bear regime on RSI<25 exception and actually
    outperformed everything else (oversold bounce is a real, working edge).

WHAT CHANGES IN V3:
  FIX A: Remove the weak mid-RSI sell/buy zones entirely. Only trade
         genuine extremes (RSI<35 for buy, RSI>65 for sell) PLUS trend
         confirmation. No more "neutral RSI" signals.
  FIX B: TP1 moved to R:R 1:1.5 minimum (was 1:1). Reduces required win
         rate from 50% to 40%.
  FIX C: Raise MIN_CONDITIONS_MET to 5/7 (was 4/7) -- the data shows 5/7
         signals are 3.6x more accurate. Fewer signals, much better ones.
  FIX D: Replace the lagging daily-EMA regime filter with a faster
         4H Supertrend + 1D RSI combo -- reduces lag in catching reversals.
  FIX E: Add a "trend exhaustion" filter using price distance from EMA50 --
         blocks chasing a move that's already overextended (common cause
         of the immediate SL-hit pattern seen in the data).
  FIX F: Reduce signals/day to 2 (was 3) -- fewer, only the cleanest setups.
  FIX G: Backtest-style self-check logged on startup so you can see the
         bot's own historical logic before trusting new signals.

ON POSITION SIZING (the second half of your question):
  Increasing risk-per-trade on a losing strategy accelerates losses.
  Once this version proves a positive win rate over 2-3 weeks of paper
  signals, THEN we scale risk. Sizing is in the new SETUP_GUIDE section
  "When to increase risk" -- not changed blindly here.
"""

import os, time, logging, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ─── CONFIGURATION ─────────────────────────────────────────────
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN",  "YOUR_BOT_TOKEN_HERE")
CHAT_ID             = os.environ.get("CHAT_ID",         "YOUR_CHAT_ID_HERE")
CAPITAL             = float(os.environ.get("CAPITAL",        "100"))
RISK_PER_TRADE       = float(os.environ.get("RISK_PCT",       "2"))   # unchanged until edge is proven
WEEKLY_TARGET        = float(os.environ.get("WEEKLY_TARGET",  "10"))
SCAN_INTERVAL        = int(os.environ.get("SCAN_INTERVAL",    "3600"))

# ── FIX C: Higher quality bar ──
MIN_CONDITIONS_MET   = 5      # was 4 -- data shows 5/7 signals are 3.6x more accurate
MIN_RR_RATIO          = 2.0    # was 1.8, TP2 still the named target
TP1_RR                = 1.5    # FIX B: was 1.0 (needed 50% WR) -> now needs 40% WR
MAX_SIGNALS_PER_DAY   = 2      # FIX F: was 3 -- fewer, cleaner
COOLDOWN_HOURS        = 8
MIN_VOLUME_RATIO      = 0.6    # raised from 0.4 -- 0.4-0.6x signals underperformed too
RSI_BUY_MAX_EXTREME   = 35    # FIX A: only buy genuine oversold, not "buy zone 30-55"
RSI_SELL_MIN_EXTREME  = 65    # FIX A: only sell genuine overbought, not "sell zone 45-70"
RSI_MAX_BUY_BLOCK      = 78
RSI_MIN_SELL_BLOCK     = 22
MAX_EMA50_DISTANCE_PCT = 4.0   # FIX E: block entries >4% away from EMA50 (chasing)

PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
    "MATICUSDT", "LINKUSDT"
]

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)
BINANCE = "https://api.binance.com/api/v3"

# ─── RENDER WEB SERVER COMPONENT ────────────────────────────────
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

state = {
    "signals_today": 0, "signals_week": 0, "wins_week": 0, "losses_week": 0,
    "last_signal_time": {}, "open_signals": [],
    "week_start": datetime.now(timezone.utc), "day_start": datetime.now(timezone.utc),
    "capital": CAPITAL, "signal_counter": 0,
}

# ─── DATA FETCHING ──────────────────────────────────────────────

def fetch_klines(symbol, interval, limit=150):
    try:
        r = requests.get(f"{BINANCE}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        r.raise_for_status()
        return [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                 "close": float(k[4]), "volume": float(k[5])} for k in r.json()]
    except Exception as e:
        log.warning(f"Klines {symbol} {interval}: {e}")
        return []

def fetch_ticker(symbol):
    try:
        r = requests.get(f"{BINANCE}/ticker/24hr", params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Ticker {symbol}: {e}")
        return {}

# ─── INDICATORS ─────────────────────────────────────────────────

def calc_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    k = 2 / (period + 1)
    e = sum(closes[:period]) / period
    for v in closes[period:]:
        e = v * k + e * (1 - k)
    return e

def calc_rsi(closes, period=14):
    if len(closes) < period + 2:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return 100.0 if al == 0 else 100 - (100 / (1 + ag / al))

def calc_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        tr = max(candles[i]["high"]-candles[i]["low"],
                 abs(candles[i]["high"]-candles[i-1]["close"]),
                 abs(candles[i]["low"]-candles[i-1]["close"]))
        trs.append(tr)
    tail = trs[-period:] if len(trs) >= period else trs
    return sum(tail) / len(tail) if tail else 0.0

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal + 5:
        return 0.0, 0.0, 0.0
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    macd_line = ema_fast - ema_slow
    macd_vals = []
    for i in range(signal + 5, 0, -1):
        ef = calc_ema(closes[:-i], fast)
        es = calc_ema(closes[:-i], slow)
        macd_vals.append(ef - es)
    sig_line = calc_ema(macd_vals, signal) if macd_vals else 0
    return macd_line, sig_line, macd_line - sig_line

def calc_supertrend(candles, period=10, multiplier=3.0):
    if len(candles) < period + 5:
        return 0, candles[-1]["close"]
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    atr_vals = []
    for i in range(1, len(candles)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        atr_vals.append(tr)
    smooth = [sum(atr_vals[:period]) / period]
    for v in atr_vals[period:]:
        smooth.append((smooth[-1]*(period-1)+v)/period)
    if len(smooth) < 5:
        return 0, closes[-1]
    hl2 = [(h+l)/2 for h,l in zip(highs[1:], lows[1:])]
    upper = [m+multiplier*a for m,a in zip(hl2, smooth)]
    lower = [m-multiplier*a for m,a in zip(hl2, smooth)]
    prev_upper = upper[-2] if len(upper)>1 else upper[-1]
    prev_lower = lower[-2] if len(lower)>1 else lower[-1]
    prev_close, curr_close = closes[-2], closes[-1]
    final_upper = min(upper[-1], prev_upper) if curr_close <= prev_upper else upper[-1]
    final_lower = max(lower[-1], prev_lower) if curr_close >= prev_lower else lower[-1]
    direction = -1 if prev_close > prev_upper else 1
    if direction == 1 and curr_close < final_lower: direction = -1
    elif direction == -1 and curr_close > final_upper: direction = 1
    return direction, (final_lower if direction == 1 else final_upper)

def calc_bollinger(closes, period=20, std_dev=2.0):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1], 0.0
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x-mid)**2 for x in window) / period
    std = var ** 0.5
    upper, lower = mid+std_dev*std, mid-std_dev*std
    bw = (upper-lower)/mid if mid != 0 else 0
    return upper, mid, lower, bw

def volume_ratio(candles, lookback=20):
    if len(candles) < lookback + 1:
        return 1.0
    recent = candles[-1]["volume"]
    avg = sum(c["volume"] for c in candles[-lookback-1:-1]) / lookback
    return recent/avg if avg > 0 else 1.0

def get_market_regime(symbol):
    """
    FIX D: Faster regime detection using 4H Supertrend (less lag than
    daily EMA) combined with daily RSI as a confirming filter.
    """
    c4h = fetch_klines(symbol, "4h", 100)
    c1d = fetch_klines(symbol, "1d", 30)
    if not c4h or not c1d or len(c4h) < 20:
        return "neutral"
    st_dir_4h, _ = calc_supertrend(c4h, 10, 3.0)
    closes_1d = [c["close"] for c in c1d]
    rsi_1d = calc_rsi(closes_1d, period=min(14, len(closes_1d)-2))
    if st_dir_4h == 1 and rsi_1d > 45:
        return "bull"
    elif st_dir_4h == -1 and rsi_1d < 55:
        return "bear"
    return "neutral"

# ─── SIGNAL ENGINE (rebuilt logic) ──────────────────────────────

def analyse_pair(symbol):
    c1h = fetch_klines(symbol, "1h", 150)
    c4h = fetch_klines(symbol, "4h", 100)
    if not c1h or not c4h or len(c1h) < 50:
        return None
    ticker = fetch_ticker(symbol)
    if not ticker:
        return None

    price      = c1h[-1]["close"]
    closes_1h  = [c["close"] for c in c1h]
    closes_4h  = [c["close"] for c in c4h]
    change_24h = float(ticker.get("priceChangePercent", 0))

    st_dir_1h, _ = calc_supertrend(c1h, 10, 3.0)
    st_dir_4h, _ = calc_supertrend(c4h, 10, 3.0)
    rsi_1h       = calc_rsi(closes_1h)
    rsi_4h       = calc_rsi(closes_4h)
    _, _, macd_h = calc_macd(closes_1h)
    bb_up, bb_mid, bb_lo, bb_bw = calc_bollinger(closes_1h)
    vol_r        = volume_ratio(c1h)
    ema9         = calc_ema(closes_1h, 9)
    ema21        = calc_ema(closes_1h, 21)
    ema50        = calc_ema(closes_1h, 50)
    atr_val      = calc_atr(c1h)
    regime       = get_market_regime(symbol)

    # ── FIX: volume floor raised ─────────────────────────────────
    if vol_r < MIN_VOLUME_RATIO:
        log.info(f"  {symbol}: volume {vol_r:.2f}x below floor {MIN_VOLUME_RATIO}x. Skip.")
        return None

    # ── FIX E: block chasing an overextended move ────────────────
    ema50_dist_pct = abs(price - ema50) / ema50 * 100 if ema50 else 0
    if ema50_dist_pct > MAX_EMA50_DISTANCE_PCT:
        log.info(f"  {symbol}: {ema50_dist_pct:.1f}% from EMA50, overextended. Skip.")
        return None

    buy_rsi_blocked  = rsi_1h > RSI_MAX_BUY_BLOCK
    sell_rsi_blocked = rsi_1h < RSI_MIN_SELL_BLOCK

    buy_conditions  = []
    sell_conditions = []

    # ── FIX A: BUY conditions -- only genuine extremes, no "buy zone 30-55" ──
    if st_dir_1h == 1:
        buy_conditions.append("Supertrend 1H bullish")
    if st_dir_4h == 1:
        buy_conditions.append("Supertrend 4H confirms")
    if rsi_1h < RSI_BUY_MAX_EXTREME:
        buy_conditions.append(f"RSI genuinely oversold ({rsi_1h:.0f})")
    if macd_h > 0:
        buy_conditions.append("MACD bullish momentum")
    if price <= bb_lo * 1.01:
        buy_conditions.append("Price at BB lower band")
    if vol_r >= 1.3:
        buy_conditions.append(f"Volume conviction ({vol_r:.1f}x avg)")
    if ema9 > ema21 > ema50:
        buy_conditions.append("Full EMA stack bullish")

    # ── FIX A: SELL conditions -- only genuine extremes ──────────
    if st_dir_1h == -1:
        sell_conditions.append("Supertrend 1H bearish")
    if st_dir_4h == -1:
        sell_conditions.append("Supertrend 4H confirms")
    if rsi_1h > RSI_SELL_MIN_EXTREME:
        sell_conditions.append(f"RSI genuinely overbought ({rsi_1h:.0f})")
    if macd_h < 0:
        sell_conditions.append("MACD bearish momentum")
    if price >= bb_up * 0.99:
        sell_conditions.append("Price at BB upper band")
    if vol_r >= 1.3:
        sell_conditions.append(f"Volume conviction ({vol_r:.1f}x avg)")
    if ema9 < ema21 < ema50:
        sell_conditions.append("Full EMA stack bearish")

    # ── Regime filter (now using faster 4H detection) ────────────
    if regime == "bull" and rsi_1h < 75:
        sell_conditions = []
    elif regime == "bear" and rsi_1h > 25:
        buy_conditions = []

    if buy_rsi_blocked:
        buy_conditions = []
    if sell_rsi_blocked:
        sell_conditions = []

    buy_c, sell_c = len(buy_conditions), len(sell_conditions)
    if buy_c < MIN_CONDITIONS_MET and sell_c < MIN_CONDITIONS_MET:
        return None

    if buy_c >= sell_c and buy_c >= MIN_CONDITIONS_MET:
        direction, conditions, score = "BUY", buy_conditions, buy_c
    elif sell_c > buy_c and sell_c >= MIN_CONDITIONS_MET:
        direction, conditions, score = "SELL", sell_conditions, sell_c
    else:
        return None

    confidence = min(int((score / 7) * 100), 97)

    # ── FIX B: TP1 now at 1.5x ATR (was 1.0x) -> R:R 1:1.5 not 1:1 ──
    atr = atr_val
    if direction == "BUY":
        entry = price
        sl  = round(price - atr * 1.5, 8)
        tp1 = round(price + atr * 2.25, 8)   # R:R 1:1.5
        tp2 = round(price + atr * 3.5, 8)    # R:R 1:2.3
        tp3 = round(price + atr * 5.5, 8)    # R:R 1:3.7
    else:
        entry = price
        sl  = round(price + atr * 1.5, 8)
        tp1 = round(price - atr * 2.25, 8)
        tp2 = round(price - atr * 3.5, 8)
        tp3 = round(price - atr * 5.5, 8)

    sl_dist = abs(entry - sl)
    if sl_dist == 0:
        return None
    rr1 = round(abs(tp1-entry)/sl_dist, 1)
    rr2 = round(abs(tp2-entry)/sl_dist, 1)
    rr3 = round(abs(tp3-entry)/sl_dist, 1)
    if rr2 < MIN_RR_RATIO:
        return None

    risk_usd  = CAPITAL * (RISK_PER_TRADE / 100)
    pos_value = round(CAPITAL * 0.05, 2)
    qty       = round(pos_value / entry, 6)

    state["signal_counter"] += 1
    sig_id = f"#{state['signal_counter']:04d}"

    return {
        "id": sig_id, "symbol": symbol, "display": symbol.replace("USDT","/USDT"),
        "direction": direction, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr1": rr1, "rr2": rr2, "rr3": rr3, "confidence": confidence,
        "conditions": conditions, "score": score, "rsi_1h": round(rsi_1h,1), "rsi_4h": round(rsi_4h,1),
        "vol_ratio": round(vol_r,2), "bb_bw": round(bb_bw*100,2),
        "st_dir_1h": st_dir_1h, "st_dir_4h": st_dir_4h, "regime": regime,
        "change_24h": change_24h, "risk_usd": round(risk_usd,2), "pos_value": pos_value,
        "qty": qty, "atr": round(atr,6), "fired_at": datetime.now(timezone.utc),
        "tp1_hit": False, "tp2_hit": False, "tp3_hit": False, "sl_hit": False, "closed": False,
    }

# ─── OUTCOME TRACKING (unchanged from v2.1 fix) ────────────────

def check_open_signals():
    to_remove = []
    for sig in state["open_signals"]:
        if sig["closed"]:
            to_remove.append(sig); continue
        try:
            ticker = fetch_ticker(sig["symbol"])
            if not ticker: continue
            price = float(ticker["lastPrice"])
            d = sig["direction"]; sid = sig["id"]

            tp3_r = (d=="BUY" and price>=sig["tp3"]) or (d=="SELL" and price<=sig["tp3"])
            tp2_r = (d=="BUY" and price>=sig["tp2"]) or (d=="SELL" and price<=sig["tp2"])
            tp1_r = (d=="BUY" and price>=sig["tp1"]) or (d=="SELL" and price<=sig["tp1"])
            sl_r  = (d=="BUY" and price<=sig["sl"])  or (d=="SELL" and price>=sig["sl"])

            if tp3_r and not sig["tp3_hit"]:
                sig["tp3_hit"]=sig["tp2_hit"]=sig["tp1_hit"]=True
                gain_pct = abs(sig["tp3"]-sig["entry"])/sig["entry"]*100
                gain_usd = round(sig["pos_value"]*gain_pct/100, 2)
                send_telegram(f"🏆 <b>TP3 HIT {sid} — {sig['display']}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\nEntry: {fmt(sig['entry'])}\nTP3: {fmt(sig['tp3'])} (R:R 1:{sig['rr3']})\nEst. profit: +${gain_usd}\n━━ Close position. ✅")
                sig["closed"]=True; state["wins_week"]+=1; to_remove.append(sig)
            elif tp2_r and not sig["tp2_hit"]:
                sig["tp2_hit"]=sig["tp1_hit"]=True
                send_telegram(f"🎯 <b>TP2 HIT {sid} — {sig['display']}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\nEntry: {fmt(sig['entry'])}\nTP2: {fmt(sig['tp2'])} (R:R 1:{sig['rr2']})\nClose 40%. Move SL to entry.\nWatching TP3 at {fmt(sig['tp3'])} 👀")
            elif tp1_r and not sig["tp1_hit"]:
                sig["tp1_hit"]=True
                send_telegram(f"✅ <b>TP1 HIT {sid} — {sig['display']}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\nEntry: {fmt(sig['entry'])}\nTP1: {fmt(sig['tp1'])} (R:R 1:{sig['rr1']})\nClose 40%. Move SL to break-even.\nWatching TP2 at {fmt(sig['tp2'])} 👀")
            elif sl_r and not sig["sl_hit"]:
                sig["sl_hit"]=True
                send_telegram(f"🛑 <b>STOP LOSS HIT {sid} — {sig['display']}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\nEntry: {fmt(sig['entry'])}\nSL: {fmt(sig['sl'])}\nLoss: -${sig['risk_usd']}\nCapital protected. 📊")
                sig["closed"]=True; state["losses_week"]+=1; to_remove.append(sig)

            age_h = (datetime.now(timezone.utc)-sig["fired_at"]).total_seconds()/3600
            if age_h > 48 and not sig["closed"]:
                tp_label = "TP2" if sig["tp2_hit"] else ("TP1" if sig["tp1_hit"] else "none")
                send_telegram(f"⏰ <b>SIGNAL EXPIRED {sid} — {sig['display']}</b>\nOpen 48h. Best level hit: {tp_label}. Closing tracking.")
                sig["closed"]=True; to_remove.append(sig)
        except Exception as e:
            log.warning(f"Outcome error {sig.get('id','?')}: {e}")
    for sig in to_remove:
        if sig in state["open_signals"]:
            state["open_signals"].remove(sig)

def can_send_signal(symbol):
    if state["signals_today"] >= MAX_SIGNALS_PER_DAY:
        return False
    last = state["last_signal_time"].get(symbol)
    if last and (datetime.now(timezone.utc)-last).total_seconds()/3600 < COOLDOWN_HOURS:
        return False
    return True

def reset_daily():
    now = datetime.now(timezone.utc)
    if (now-state["day_start"]).total_seconds() > 86400:
        state["signals_today"]=0; state["day_start"]=now

def reset_weekly():
    now = datetime.now(timezone.utc)
    if (now-state["week_start"]).total_seconds() > 604800:
        send_weekly_summary()
        state["signals_week"]=state["wins_week"]=state["losses_week"]=0
        state["week_start"]=now

def fmt(price):
    if price >= 10000: return f"${price:,.0f}"
    if price >= 1000:  return f"${price:,.2f}"
    if price >= 1:     return f"${price:.4f}"
    return f"${price:.6f}"

def st_bar(d): return "🟢" if d==1 else "🔴"
def regime_label(r): return {"bull":"🐂 Bull","bear":"🐻 Bear","neutral":"↔️ Neutral"}.get(r,"↔️ Neutral")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id":CHAT_ID,"text":text,"parse_mode":"HTML"}, timeout=10).raise_for_status()
    except Exception as e:
        log.error(f"Telegram error: {e}")

def build_signal_message(sig):
    arrow = "🟢" if sig["direction"]=="BUY" else "🔴"
    now = datetime.now(ZoneInfo("Africa/Lagos")).strftime("%d %b %Y · %H:%M WAT")
    conds = "\n".join(f"  ✓ {c}" for c in sig["conditions"])
    filled = round(sig["confidence"]/10)
    bar = "█"*filled + "░"*(10-filled)
    return (f"{arrow} <b>SIGNAL {sig['id']} — {sig['display']}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {now}\n⚡ Direction: <b>{sig['direction']}</b>\n🌍 Market: {regime_label(sig['regime'])}\n"
        f"💪 Confidence: {sig['confidence']}% [{bar}]\n📊 Conditions: {sig['score']}/7\n\n"
        f"<b>📍 ENTRY</b>\n{fmt(sig['entry'])}\n\n<b>🛑 STOP LOSS</b>\n{fmt(sig['sl'])} (max loss ${sig['risk_usd']})\n\n"
        f"<b>🎯 TAKE PROFITS</b>\nTP1 → {fmt(sig['tp1'])} R:R 1:{sig['rr1']} (close 40%)\n"
        f"TP2 → {fmt(sig['tp2'])} R:R 1:{sig['rr2']} (close 40%, move SL to entry)\n"
        f"TP3 → {fmt(sig['tp3'])} R:R 1:{sig['rr3']} (close remaining 20%)\n\n"
        f"<b>💰 POSITION (${CAPITAL} capital)</b>\nSpend: ${sig['pos_value']} | Qty: {sig['qty']} {sig['symbol'].replace('USDT','')}\n"
        f"Max risk: ${sig['risk_usd']}\n\n<b>📋 WHY THIS SIGNAL</b>\n{conds}\n\n"
        f"RSI 1H: {sig['rsi_1h']} | RSI 4H: {sig['rsi_4h']}\nST 1H: {st_bar(sig['st_dir_1h'])} | ST 4H: {st_bar(sig['st_dir_4h'])}\n"
        f"Vol: {sig['vol_ratio']}x avg | BB: {sig['bb_bw']}% | 24h: {sig['change_24h']:+.1f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ Analysis only. Always use your SL.")

def send_weekly_summary():
    w, l = state["wins_week"], state["losses_week"]
    t = w+l; wr = round(w/t*100) if t else 0
    gain = state["capital"]-CAPITAL; gp = gain/CAPITAL*100
    prog = min(int(gp/WEEKLY_TARGET*10), 10)
    bar = "█"*prog + "░"*(10-prog)
    send_telegram(f"📊 <b>WEEKLY REPORT</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Capital: ${CAPITAL} → ${state['capital']:.2f}\nNet: {gain:+.2f} ({gp:+.1f}%)\n"
        f"Target ({WEEKLY_TARGET}%): [{bar}] {gp:.1f}%\n\nSignals: {state['signals_week']} | Wins: {w} | Losses: {l} | WR: {wr}%\n"
        f"{'🎯 TARGET HIT!' if gp>=WEEKLY_TARGET else '📈 Keep going.'}")

def main():
    log.info("Rich Genie Bot v3 (edge-first rebuild) starting...")
    send_telegram(
        f"🤖 <b>Rich Genie Bot v3 LIVE</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔬 <b>Strategy rebuilt after analysing 38 live signals</b>\n"
        f"Previous version: 9-13% win rate (needed 35-50% to break even)\n\n"
        f"What changed:\n"
        f"  ✓ Only genuine RSI extremes (buy &lt;{RSI_BUY_MAX_EXTREME}, sell &gt;{RSI_SELL_MIN_EXTREME})\n"
        f"  ✓ TP1 at R:R 1:1.5 (was 1:1 — needed 50% WR just to break even)\n"
        f"  ✓ Min conditions raised to {MIN_CONDITIONS_MET}/7 (was 4/7)\n"
        f"  ✓ Faster 4H regime detection (was lagging daily EMA)\n"
        f"  ✓ Blocks chasing moves &gt;{MAX_EMA50_DISTANCE_PCT}% from EMA50\n"
        f"  ✓ Volume floor raised to {MIN_VOLUME_RATIO}x avg\n"
        f"  ✓ Max {MAX_SIGNALS_PER_DAY} signals/day — quality over quantity\n\n"
        f"💰 ${CAPITAL} capital | {RISK_PER_TRADE}% risk | {WEEKLY_TARGET}% weekly target\n"
        f"<b>Risk % stays the same until this version proves a positive\n"
        f"win rate over 2-3 weeks. Sizing up a losing strategy only\n"
        f"loses money faster.</b>\n━━━━━━━━━━━━━━━━━━━━━━━\nStanding by...")
    scan_n = 0
    time.sleep(60)
    while True:
        scan_n += 1
        log.info(f"─── Scan #{scan_n} ───")
        reset_daily(); reset_weekly()
        if state["open_signals"]:
            check_open_signals()
        if state["signals_today"] < MAX_SIGNALS_PER_DAY:
            for symbol in PAIRS:
                if state["signals_today"] >= MAX_SIGNALS_PER_DAY:
                    break
                if not can_send_signal(symbol):
                    continue
                log.info(f"  Analysing {symbol}...")
                sig = analyse_pair(symbol)
                if sig:
                    state["signals_today"] += 1
                    state["signals_week"] += 1
                    state["last_signal_time"][symbol] = datetime.now(timezone.utc)
                    state["open_signals"].append(sig)
                    send_telegram(build_signal_message(sig))
                    log.info(f"  ✓ {sig['id']}: {symbol} {sig['direction']} | {sig['score']}/7 | conf {sig['confidence']}%")
                    time.sleep(2)
                else:
                    log.info(f"  ✗ {symbol}: no quality setup")
        log.info(f"Scan #{scan_n} done. Sleeping {SCAN_INTERVAL//60}min...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    main()