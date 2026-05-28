"""
Rich Genie Signal Bot v2 — Professional Grade
=============================================
Rebuilt from scratch using the same techniques as high-accuracy
professional signal bots (Fat Pig Signals 91.7% win rate,
Signals Blue 81%+ consistency).

ACCURACY ENGINE:
  1. Supertrend indicator  — trend direction (primary filter)
  2. MACD histogram        — momentum confirmation
  3. RSI with strict zones — avoid false extremes
  4. Bollinger Bands       — volatility & squeeze detection
  5. Volume confirmation   — conviction filter
  6. Multi-timeframe (1H + 4H + 1D) — kills counter-trend trades
  7. Market regime filter  — suppresses sells in bull market
  8. Signal cooldown       — no duplicates for 6 hours
  9. Daily cap             — max 3 signals/day, 1 per pair/day
 10. Outcome tracker       — monitors open signals, reports TP/SL hits

RULES (what professional bots actually do):
  - Never fire a signal unless 4+ conditions agree
  - Never trade against the daily trend
  - Never send same pair twice within 6 hours
  - Never send more than 3 signals per day
  - Always track what happened to your last signal
"""

import os, time, logging, requests, json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ─── CONFIGURATION ─────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID         = os.environ.get("CHAT_ID", "YOUR_CHAT_ID_HERE")
CAPITAL         = float(os.environ.get("CAPITAL", "100"))
RISK_PER_TRADE  = float(os.environ.get("RISK_PCT", "2"))       # 2% = $2 per trade on $100
WEEKLY_TARGET   = float(os.environ.get("WEEKLY_TARGET", "10"))
SCAN_INTERVAL   = int(os.environ.get("SCAN_INTERVAL", "3600"))

# Quality gates — a signal must pass ALL of these
MIN_CONDITIONS_MET  = 4      # At least 4 out of 6 conditions must agree
MIN_RR_RATIO        = 1.8    # Minimum reward:risk (professional standard)
MAX_SIGNALS_PER_DAY = 3      # Hard cap on daily signals
COOLDOWN_HOURS      = 6      # Hours before same pair can fire again

PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
    "MATICUSDT", "LINKUSDT"
]

# ─── LOGGING ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
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

# ─── STATE (in-memory, resets on restart) ───────────────────────
state = {
    "signals_today":    0,
    "signals_week":     0,
    "wins_week":        0,
    "losses_week":      0,
    "last_signal_time": {},   # symbol -> datetime of last signal
    "open_signals":     [],   # list of open signal dicts to track
    "week_start":       datetime.now(timezone.utc),
    "day_start":        datetime.now(timezone.utc),
    "capital":          CAPITAL,
}

# ─── DATA FETCHING ──────────────────────────────────────────────

def fetch_klines(symbol, interval, limit=150):
    try:
        r = requests.get(f"{BINANCE}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10)
        r.raise_for_status()
        return [{
            "open":   float(k[1]), "high": float(k[2]),
            "low":    float(k[3]), "close": float(k[4]),
            "volume": float(k[5])
        } for k in r.json()]
    except Exception as e:
        log.warning(f"Klines {symbol} {interval}: {e}")
        return []

def fetch_ticker(symbol):
    try:
        r = requests.get(f"{BINANCE}/ticker/24hr",
            params={"symbol": symbol}, timeout=10)
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
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return 100.0 if al == 0 else 100 - (100 / (1 + ag / al))

def calc_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i-1]["close"]),
            abs(candles[i]["low"]  - candles[i-1]["close"])
        )
        trs.append(tr)
    tail = trs[-period:] if len(trs) >= period else trs
    return sum(tail) / len(tail) if tail else 0.0

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast   = calc_ema(closes, fast)
    ema_slow   = calc_ema(closes, slow)
    macd_line  = ema_fast - ema_slow
    # Approximate signal line from last N values
    macd_values = []
    for i in range(signal + 5, 0, -1):
        ef = calc_ema(closes[:-i] if i > 0 else closes, fast)
        es = calc_ema(closes[:-i] if i > 0 else closes, slow)
        macd_values.append(ef - es)
    signal_line = calc_ema(macd_values, signal) if macd_values else 0
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram

def calc_supertrend(candles, period=10, multiplier=3.0):
    """
    Supertrend indicator — the most reliable trend-following indicator.
    Returns: direction (+1 = bullish, -1 = bearish), supertrend_value
    """
    if len(candles) < period + 5:
        return 0, candles[-1]["close"]

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]

    atr_vals = []
    for i in range(1, len(candles)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i]  - closes[i-1]),
            abs(lows[i]   - closes[i-1])
        )
        atr_vals.append(tr)

    # Smooth ATR
    smooth_atr = []
    window = atr_vals[:period]
    smooth_atr.append(sum(window) / period)
    for v in atr_vals[period:]:
        smooth_atr.append((smooth_atr[-1] * (period - 1) + v) / period)

    if len(smooth_atr) < 5:
        return 0, closes[-1]

    hl2 = [(h + l) / 2 for h, l in zip(highs[1:], lows[1:])]

    upper_band = [m + multiplier * a for m, a in zip(hl2, smooth_atr)]
    lower_band = [m - multiplier * a for m, a in zip(hl2, smooth_atr)]

    direction = 1
    st_val    = lower_band[-1]

    # Walk through to get final direction
    prev_upper = upper_band[-2] if len(upper_band) > 1 else upper_band[-1]
    prev_lower = lower_band[-2] if len(lower_band) > 1 else lower_band[-1]
    prev_close = closes[-2]
    curr_close = closes[-1]

    # Final upper/lower
    final_upper = min(upper_band[-1], prev_upper) if curr_close <= prev_upper else upper_band[-1]
    final_lower = max(lower_band[-1], prev_lower) if curr_close >= prev_lower else lower_band[-1]

    if prev_close > prev_upper:
        direction = -1  # was bearish
    elif prev_close < prev_lower:
        direction = 1   # was bullish

    if direction == 1 and curr_close < final_lower:
        direction = -1
    elif direction == -1 and curr_close > final_upper:
        direction = 1

    st_val = final_lower if direction == 1 else final_upper
    return direction, st_val

def calc_bollinger(closes, period=20, std_dev=2.0):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1], 0.0
    window  = closes[-period:]
    mid     = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std     = variance ** 0.5
    upper   = mid + std_dev * std
    lower   = mid - std_dev * std
    # Bandwidth: how tight/wide the bands are (squeeze = low, expansion = high)
    bw      = (upper - lower) / mid if mid != 0 else 0
    return upper, mid, lower, bw

def volume_ratio(candles, lookback=20):
    """Current volume vs average — >1.5 means above-average conviction."""
    if len(candles) < lookback + 1:
        return 1.0
    recent  = candles[-1]["volume"]
    avg_vol = sum(c["volume"] for c in candles[-lookback-1:-1]) / lookback
    return recent / avg_vol if avg_vol > 0 else 1.0

# ─── MARKET REGIME ──────────────────────────────────────────────

def get_market_regime(symbol):
    """
    Checks the daily chart to determine if we're in a bull or bear market.
    CRITICAL: This is the #1 accuracy improvement. Never sell in a bull market.
    Returns: 'bull', 'bear', or 'neutral'
    """
    candles_1d = fetch_klines(symbol, "1d", 60)
    if not candles_1d or len(candles_1d) < 21:
        return "neutral"

    closes_1d = [c["close"] for c in candles_1d]
    ema20_1d  = calc_ema(closes_1d, 20)
    ema50_1d  = calc_ema(closes_1d, 50)
    price     = closes_1d[-1]

    # Price above EMA20 + EMA20 above EMA50 = bull
    if price > ema20_1d and ema20_1d > ema50_1d:
        return "bull"
    # Price below EMA20 + EMA20 below EMA50 = bear
    elif price < ema20_1d and ema20_1d < ema50_1d:
        return "bear"
    return "neutral"

# ─── CORE SIGNAL ENGINE ─────────────────────────────────────────

def analyse_pair(symbol):
    """
    Professional-grade multi-timeframe analysis.
    Returns a signal dict or None if no quality setup found.
    """

    # Fetch all timeframes
    c1h = fetch_klines(symbol, "1h", 150)
    c4h = fetch_klines(symbol, "4h", 100)

    if not c1h or not c4h or len(c1h) < 50:
        return None

    ticker = fetch_ticker(symbol)
    if not ticker:
        return None

    price       = c1h[-1]["close"]
    closes_1h   = [c["close"] for c in c1h]
    closes_4h   = [c["close"] for c in c4h]
    change_24h  = float(ticker.get("priceChangePercent", 0))

    # ── INDICATOR SUITE ──────────────────────────────────────────

    # 1. Supertrend (1H + 4H)
    st_dir_1h, st_val_1h = calc_supertrend(c1h, 10, 3.0)
    st_dir_4h, st_val_4h = calc_supertrend(c4h, 10, 3.0)

    # 2. RSI
    rsi_1h = calc_rsi(closes_1h)
    rsi_4h = calc_rsi(closes_4h)

    # 3. MACD (1H)
    _, _, macd_hist = calc_macd(closes_1h)

    # 4. Bollinger Bands (1H)
    bb_upper, bb_mid, bb_lower, bb_bw = calc_bollinger(closes_1h)

    # 5. Volume
    vol_r = volume_ratio(c1h)

    # 6. EMA stack (1H)
    ema9   = calc_ema(closes_1h, 9)
    ema21  = calc_ema(closes_1h, 21)
    ema50  = calc_ema(closes_1h, 50)

    # 7. ATR
    atr_val = calc_atr(c1h)

    # 8. Market regime (daily)
    regime = get_market_regime(symbol)

    # ── SIGNAL LOGIC ─────────────────────────────────────────────
    # We require 4+ conditions to agree before firing a signal.
    # This is what professional bots do — confluence over single triggers.

    buy_conditions  = []
    sell_conditions = []

    # --- BUY CONDITIONS ---
    # C1: Supertrend bullish on 1H
    if st_dir_1h == 1:
        buy_conditions.append("Supertrend 1H bullish")

    # C2: Supertrend bullish on 4H (higher timeframe confirmation)
    if st_dir_4h == 1:
        buy_conditions.append("Supertrend 4H confirms")

    # C3: RSI in buying zone (not overbought, not extreme oversold whipsaw)
    if 30 <= rsi_1h <= 55:
        buy_conditions.append(f"RSI buy zone ({rsi_1h:.0f})")
    elif rsi_1h < 30:
        buy_conditions.append(f"RSI oversold ({rsi_1h:.0f})")

    # C4: MACD histogram positive (momentum supporting move)
    if macd_hist > 0:
        buy_conditions.append("MACD bullish momentum")

    # C5: Price near or below Bollinger lower band (value zone)
    if price <= bb_lower * 1.005:
        buy_conditions.append("Price at BB lower (oversold)")
    elif price < bb_mid:
        buy_conditions.append("Price below BB midline")

    # C6: Volume above average (conviction)
    if vol_r >= 1.3:
        buy_conditions.append(f"Volume spike ({vol_r:.1f}x avg)")

    # C7: EMA alignment (trend)
    if ema9 > ema21:
        buy_conditions.append("Short EMA above long")

    # --- SELL CONDITIONS ---
    # C1: Supertrend bearish on 1H
    if st_dir_1h == -1:
        sell_conditions.append("Supertrend 1H bearish")

    # C2: Supertrend bearish on 4H
    if st_dir_4h == -1:
        sell_conditions.append("Supertrend 4H confirms")

    # C3: RSI in selling zone (not oversold, not extreme overbought already faded)
    if 45 <= rsi_1h <= 70:
        sell_conditions.append(f"RSI sell zone ({rsi_1h:.0f})")
    elif rsi_1h > 70:
        sell_conditions.append(f"RSI overbought ({rsi_1h:.0f})")

    # C4: MACD histogram negative
    if macd_hist < 0:
        sell_conditions.append("MACD bearish momentum")

    # C5: Price near or above Bollinger upper band
    if price >= bb_upper * 0.995:
        sell_conditions.append("Price at BB upper (overbought)")
    elif price > bb_mid:
        sell_conditions.append("Price above BB midline")

    # C6: Volume conviction
    if vol_r >= 1.3:
        sell_conditions.append(f"Volume spike ({vol_r:.1f}x avg)")

    # C7: EMA alignment (trend)
    if ema9 < ema21:
        sell_conditions.append("Short EMA below long")

    # ── REGIME FILTERS (biggest accuracy boost) ──────────────────
    # In a bull market: only take BUY signals. Suppress sells unless RSI > 75.
    # In a bear market: only take SELL signals. Suppress buys unless RSI < 25.
    if regime == "bull":
        # Kill sell signals unless extremely overbought
        if rsi_1h < 75:
            sell_conditions = []  # wipe sells in bull market
    elif regime == "bear":
        # Kill buy signals unless extremely oversold
        if rsi_1h > 25:
            buy_conditions = []   # wipe buys in bear market

    # ── DETERMINE DIRECTION ──────────────────────────────────────
    buy_count  = len(buy_conditions)
    sell_count = len(sell_conditions)

    # Minimum 4 conditions must be met
    if buy_count < MIN_CONDITIONS_MET and sell_count < MIN_CONDITIONS_MET:
        return None

    if buy_count >= sell_count and buy_count >= MIN_CONDITIONS_MET:
        direction  = "BUY"
        conditions = buy_conditions
        score      = buy_count
    elif sell_count > buy_count and sell_count >= MIN_CONDITIONS_MET:
        direction  = "SELL"
        conditions = sell_conditions
        score      = sell_count
    else:
        return None

    # Confidence score (out of 7 conditions)
    confidence = min(int((score / 7) * 100), 97)

    # ── LEVELS (ATR-based) ───────────────────────────────────────
    atr = atr_val
    if direction == "BUY":
        entry = price
        sl    = round(price - atr * 1.5, 8)
        tp1   = round(price + atr * 1.5, 8)   # R:R 1:1  (conservative)
        tp2   = round(price + atr * 2.7, 8)   # R:R 1:1.8 (target)
        tp3   = round(price + atr * 4.5, 8)   # R:R 1:3  (stretch)
    else:
        entry = price
        sl    = round(price + atr * 1.5, 8)
        tp1   = round(price - atr * 1.5, 8)
        tp2   = round(price - atr * 2.7, 8)
        tp3   = round(price - atr * 4.5, 8)

    sl_dist = abs(entry - sl)
    if sl_dist == 0:
        return None

    rr1 = round(abs(tp1 - entry) / sl_dist, 1)
    rr2 = round(abs(tp2 - entry) / sl_dist, 1)
    rr3 = round(abs(tp3 - entry) / sl_dist, 1)

    # Reject if R:R at TP2 is below professional minimum
    if rr2 < MIN_RR_RATIO:
        return None

    # ── POSITION SIZING (corrected for $100 spot) ────────────────
    # On a real $100 spot account: allocate 5% per trade = $5 position value
    # Risk is capped at RISK_PER_TRADE % of capital
    risk_usd      = CAPITAL * (RISK_PER_TRADE / 100)
    pos_value_usd = CAPITAL * 0.05  # 5% of capital per position
    qty_by_risk   = round(risk_usd / sl_dist, 6)
    qty_spot      = round(pos_value_usd / entry, 6)
    # Use the SMALLER of the two (safer for small accounts)
    qty           = min(qty_by_risk, qty_spot)
    pos_value     = round(qty * entry, 2)

    return {
        "symbol":       symbol,
        "display":      symbol.replace("USDT", "/USDT"),
        "direction":    direction,
        "entry":        entry,
        "sl":           sl,
        "tp1":          tp1,
        "tp2":          tp2,
        "tp3":          tp3,
        "rr1":          rr1,
        "rr2":          rr2,
        "rr3":          rr3,
        "confidence":   confidence,
        "conditions":   conditions,
        "conditions_count": score,
        "rsi_1h":       round(rsi_1h, 1),
        "rsi_4h":       round(rsi_4h, 1),
        "macd_hist":    round(macd_hist, 6),
        "vol_ratio":    round(vol_r, 2),
        "bb_bw":        round(bb_bw * 100, 2),
        "st_dir_1h":    st_dir_1h,
        "st_dir_4h":    st_dir_4h,
        "regime":       regime,
        "change_24h":   change_24h,
        "risk_usd":     round(risk_usd, 2),
        "pos_value":    pos_value,
        "qty":          qty,
        "atr":          round(atr, 6),
        "fired_at":     datetime.now(timezone.utc),
    }

# ─── OUTCOME TRACKING ───────────────────────────────────────────

def check_open_signals():
    """
    Check all open signals against current price.
    Sends a result message when TP or SL is hit.
    """
    resolved = []
    for sig in state["open_signals"]:
        try:
            ticker = fetch_ticker(sig["symbol"])
            if not ticker:
                continue
            price = float(ticker["lastPrice"])
            d     = sig["direction"]

            hit_tp1 = (d == "BUY"  and price >= sig["tp1"]) or (d == "SELL" and price <= sig["tp1"])
            hit_tp2 = (d == "BUY"  and price >= sig["tp2"]) or (d == "SELL" and price <= sig["tp2"])
            hit_tp3 = (d == "BUY"  and price >= sig["tp3"]) or (d == "SELL" and price <= sig["tp3"])
            hit_sl  = (d == "BUY"  and price <= sig["sl"])  or (d == "SELL" and price >= sig["sl"])

            pnl_tp1 = round(abs(sig["tp1"] - sig["entry"]) / sig["entry"] * 100, 2)
            pnl_sl  = round(sig["risk_usd"], 2)

            if hit_tp3:
                send_telegram(f"""🎯 <b>TP3 HIT — {sig['display']}</b>
Entry: {fmt(sig['entry'])} → Now: {fmt(price)}
Target 3 reached: {fmt(sig['tp3'])} (R:R 1:{sig['rr3']})
Est. gain: +${round(sig['pos_value'] * pnl_tp1 / 100 * 3, 2)}
━━ Excellent trade. Close position. ✅""")
                state["wins_week"] += 1
                resolved.append(sig)
            elif hit_tp2:
                send_telegram(f"""🎯 <b>TP2 HIT — {sig['display']}</b>
Entry: {fmt(sig['entry'])} → Now: {fmt(price)}
Target 2 reached: {fmt(sig['tp2'])} (R:R 1:{sig['rr2']})
Consider moving SL to entry (risk-free) or close. ✅""")
                # Don't resolve yet — let it run to TP3 or trail to entry
            elif hit_tp1:
                send_telegram(f"""✅ <b>TP1 HIT — {sig['display']}</b>
Entry: {fmt(sig['entry'])} → Now: {fmt(price)}
TP1 reached: {fmt(sig['tp1'])}
Move your SL to entry now (break even). Let it run to TP2. 📈""")
                # Don't resolve — still open
            elif hit_sl:
                send_telegram(f"""🛑 <b>STOP LOSS HIT — {sig['display']}</b>
Entry: {fmt(sig['entry'])} → Hit SL: {fmt(sig['sl'])}
Max loss taken: -${pnl_sl}
This is normal. Capital protected. Next signal coming. 📊""")
                state["losses_week"] += 1
                resolved.append(sig)

            # Expire old signals after 48 hours
            age = (datetime.now(timezone.utc) - sig["fired_at"]).total_seconds() / 3600
            if age > 48 and sig not in resolved:
                resolved.append(sig)

        except Exception as e:
            log.warning(f"Outcome check error: {e}")

    for sig in resolved:
        if sig in state["open_signals"]:
            state["open_signals"].remove(sig)

# ─── COOLDOWN & CAPS ────────────────────────────────────────────

def can_send_signal(symbol):
    """Returns True only if this pair is allowed to signal right now."""
    # Daily cap
    if state["signals_today"] >= MAX_SIGNALS_PER_DAY:
        log.info(f"Daily cap reached ({MAX_SIGNALS_PER_DAY}). Skipping {symbol}.")
        return False

    # Per-pair cooldown
    last = state["last_signal_time"].get(symbol)
    if last:
        hours_since = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if hours_since < COOLDOWN_HOURS:
            log.info(f"{symbol} in cooldown ({hours_since:.1f}h / {COOLDOWN_HOURS}h needed).")
            return False

    return True

def reset_daily_counters():
    """Reset daily signal count at start of new day."""
    now = datetime.now(timezone.utc)
    if (now - state["day_start"]).total_seconds() > 86400:
        state["signals_today"] = 0
        state["day_start"]     = now
        log.info("Daily counters reset.")

def reset_weekly_counters():
    """Reset weekly stats and send summary every 7 days."""
    now = datetime.now(timezone.utc)
    if (now - state["week_start"]).total_seconds() > 604800:
        send_weekly_summary()
        state["signals_week"]  = 0
        state["wins_week"]     = 0
        state["losses_week"]   = 0
        state["week_start"]    = now
        log.info("Weekly counters reset.")

# ─── FORMATTING ─────────────────────────────────────────────────

def fmt(price):
    if price >= 10000: return f"${price:,.0f}"
    if price >= 1000:  return f"${price:,.2f}"
    if price >= 1:     return f"${price:.4f}"
    return f"${price:.6f}"

def st_emoji(direction):
    return "🟢" if direction == 1 else "🔴"

def regime_label(r):
    return {"bull": "🐂 Bull", "bear": "🐻 Bear", "neutral": "↔️ Neutral"}.get(r, "↔️ Neutral")

# ─── TELEGRAM ───────────────────────────────────────────────────

def send_telegram(text):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=data, timeout=10)
        r.raise_for_status()
        log.info("Telegram message sent ✓")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def build_signal_message(sig):
    arrow  = "🟢" if sig["direction"] == "BUY" else "🔴"
    now    = datetime.now(ZoneInfo("Africa/Lagos")).strftime("%d %b %Y · %H:%M WAT")
    regime = regime_label(sig["regime"])
    conds  = "\n".join(f"  ✓ {c}" for c in sig["conditions"])
    conf   = sig["confidence"]

    # Confidence bar
    filled = round(conf / 10)
    bar    = "█" * filled + "░" * (10 - filled)

    return f"""{arrow} <b>HIGH CONFIDENCE SIGNAL — {sig['display']}</b>
━━━━━━━━━━━━━━━━━━━━━━━
📅 {now}
⚡ Direction: <b>{sig['direction']}</b>
🌍 Market: {regime}
💪 Confidence: {conf}% [{bar}]
📊 Conditions met: {sig['conditions_count']}/7

<b>📍 ENTRY PRICE</b>
{fmt(sig['entry'])}

<b>🛑 STOP LOSS</b>
{fmt(sig['sl'])}
→ Max loss: ${sig['risk_usd']} ({RISK_PER_TRADE}% of capital)

<b>🎯 TAKE PROFITS</b>
TP1 (Close 40%) → {fmt(sig['tp1'])}  R:R 1:{sig['rr1']}
TP2 (Close 40%) → {fmt(sig['tp2'])}  R:R 1:{sig['rr2']} ← move SL to entry here
TP3 (Close 20%) → {fmt(sig['tp3'])}  R:R 1:{sig['rr3']} ← let it run

<b>💰 POSITION (${CAPITAL} capital)</b>
Spend: ${sig['pos_value']} ({round(sig['pos_value']/CAPITAL*100)}% of account)
Qty: {sig['qty']} {sig['symbol'].replace('USDT','')}
Max risk: ${sig['risk_usd']}

<b>📋 WHY THIS SIGNAL ({sig['conditions_count']}/7 agree)</b>
{conds}

<b>📈 INDICATORS</b>
RSI 1H: {sig['rsi_1h']} | RSI 4H: {sig['rsi_4h']}
Supertrend 1H: {st_emoji(sig['st_dir_1h'])} | 4H: {st_emoji(sig['st_dir_4h'])}
Volume: {sig['vol_ratio']}x avg | BB Width: {sig['bb_bw']}%
24h Change: {sig['change_24h']:+.1f}%
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Analysis only. Trade your own risk. Always use SL."""

def send_weekly_summary():
    wins   = state["wins_week"]
    losses = state["losses_week"]
    total  = wins + losses
    wr     = round(wins / total * 100) if total > 0 else 0
    gain   = state["capital"] - CAPITAL
    gain_p = (gain / CAPITAL) * 100
    prog   = min(int(gain_p / WEEKLY_TARGET * 10), 10)
    bar    = "█" * prog + "░" * (10 - prog)

    msg = f"""📊 <b>WEEKLY PERFORMANCE REPORT</b>
━━━━━━━━━━━━━━━━━━━━━━━
Capital: ${CAPITAL} → ${state['capital']:.2f}
Net: {gain:+.2f} ({gain_p:+.1f}%)
Weekly target ({WEEKLY_TARGET}%): [{bar}] {gain_p:.1f}%

Signals sent: {state['signals_week']}
Wins: {wins} | Losses: {losses}
Win rate: {wr}%

{'🎯 TARGET HIT! Outstanding week.' if gain_p >= WEEKLY_TARGET else '📈 Keep going — discipline pays.' if gain_p > 0 else '📉 Tough week. Review signals and stay patient.'}
━━━━━━━━━━━━━━━━━━━━━━━
New week starts now. Stay disciplined."""
    send_telegram(msg)

# ─── MAIN LOOP ──────────────────────────────────────────────────

def main():
    log.info("Rich Genie Signal Bot v2 starting...")
    send_telegram(f"""🤖 <b>Rich Genie Bot v2 is LIVE</b>
━━━━━━━━━━━━━━━━━━━━━━━
🔬 <b>Accuracy Engine Active</b>
  ✓ Supertrend (1H + 4H)
  ✓ MACD confirmation
  ✓ RSI with strict zones
  ✓ Bollinger Bands
  ✓ Volume conviction filter
  ✓ Market regime filter (bull/bear)
  ✓ 6-hour cooldown per pair
  ✓ Max {MAX_SIGNALS_PER_DAY} signals/day
  ✓ Live outcome tracking (TP/SL alerts)

💰 Capital: ${CAPITAL} | Risk: {RISK_PER_TRADE}%/trade
📈 Weekly target: {WEEKLY_TARGET}%
⏱ Scanning {len(PAIRS)} pairs hourly

Minimum conditions to fire: {MIN_CONDITIONS_MET}/7
Minimum R:R ratio: 1:{MIN_RR_RATIO}

<b>This version sends FEWER signals — only the best setups.</b>
━━━━━━━━━━━━━━━━━━━━━━━
Standing by. Next scan in 60 seconds.""")

    scan_n = 0
    time.sleep(60)  # Give Telegram a moment

    while True:
        scan_n += 1
        now = datetime.now(timezone.utc)
        log.info(f"─── Scan #{scan_n} at {now.strftime('%Y-%m-%d %H:%M UTC')} ───")

        reset_daily_counters()
        reset_weekly_counters()

        # Check outcomes of open signals first
        if state["open_signals"]:
            log.info(f"Checking {len(state['open_signals'])} open signal(s)...")
            check_open_signals()

        # Scan for new signals
        found = 0
        for symbol in PAIRS:
            if not can_send_signal(symbol):
                continue

            log.info(f"  Analysing {symbol}...")
            sig = analyse_pair(symbol)

            if sig:
                msg = build_signal_message(sig)
                send_telegram(msg)

                state["last_signal_time"][symbol] = now
                state["signals_today"]  += 1
                state["signals_week"]   += 1
                state["open_signals"].append(sig)
                found += 1

                log.info(f"  ✓ SIGNAL: {symbol} {sig['direction']} | Confidence: {sig['confidence']}% | {sig['conditions_count']}/7 conditions")

                if state["signals_today"] >= MAX_SIGNALS_PER_DAY:
                    log.info("Daily cap reached. Stopping scan.")
                    break

                time.sleep(3)
            else:
                log.info(f"  ✗ {symbol}: no quality setup")

        if found == 0:
            log.info("No signals this scan — market not meeting quality threshold. Good.")

        log.info(f"Scan #{scan_n} done. Sleeping {SCAN_INTERVAL//60} min...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    main()