import os
import time
import logging
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ─────────────────────────────────────────────
#  CONFIGURATION — Edit these before deploying
# ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID        = os.environ.get("CHAT_ID", "YOUR_CHAT_ID_HERE")
CAPITAL        = float(os.environ.get("CAPITAL", "100"))      # Your starting capital in USD
RISK_PER_TRADE = float(os.environ.get("RISK_PCT", "2"))       # % of capital to risk per trade (2% = $2 on $100)
WEEKLY_TARGET  = float(os.environ.get("WEEKLY_TARGET", "10")) # Weekly profit target in % (10% = $10 on $100)
SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL", "3600")) # Seconds between scans (3600 = 1 hour)

PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
    "MATICUSDT", "LINKUSDT"
]

MIN_SIGNAL_STRENGTH = 60   # Only send signals above this score (0-100)
MIN_RR_RATIO        = 1.5  # Minimum reward:risk ratio to send a signal
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

BINANCE = "https://api.binance.com/api/v3"

# Track weekly stats in memory
weekly_stats = {
    "signals_sent": 0,
    "buys": 0,
    "sells": 0,
    "week_start": datetime.now(timezone.utc).isoformat(),
    "capital": CAPITAL
}

# ─── Render Web Server Component ─────────────
# This keeps Render from force-stopping your bot
# ─── Render Web Server Component ─────────────
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
    
# ─── Binance data fetching ───────────────────
def get_klines(symbol, interval="1h", limit=100):
    try:
        r = requests.get(f"{BINANCE}/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         timeout=10)
        r.raise_for_status()
        data = r.json()
        return [{
            "open":   float(k[1]),
            "high":   float(k[2]),
            "low":    float(k[3]),
            "close":  float(k[4]),
            "volume": float(k[5])
        } for k in data]
    except Exception as e:
        log.warning(f"Klines error {symbol}: {e}")
        return []

def get_ticker(symbol):
    try:
        r = requests.get(f"{BINANCE}/ticker/24hr",
                         params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Ticker error {symbol}: {e}")
        return {}

# ─── Technical Indicators ────────────────────
def ema(values, period):
    if len(values) < period:
        return values[-1]
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    gains  = gains[-period:]
    losses = losses[-period:]
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    if avg_l == 0:
        return 100
    return 100 - (100 / (1 + avg_g / avg_l))

def atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i-1]["close"]),
            abs(candles[i]["low"]  - candles[i-1]["close"])
        )
        trs.append(tr)
    tail = trs[-period:] if len(trs) >= period else trs
    return sum(tail) / len(tail) if tail else 0

def volume_spike(candles, lookback=20):
    if len(candles) < lookback + 1:
        return False
    recent_vol = candles[-1]["volume"]
    avg_vol = sum(c["volume"] for c in candles[-lookback-1:-1]) / lookback
    return recent_vol > avg_vol * 1.5

# ─── Signal Engine ────────────────────────────
def analyse(symbol):
    candles_1h = get_klines(symbol, "1h", 100)
    candles_4h = get_klines(symbol, "4h", 60)
    ticker     = get_ticker(symbol)
    if not candles_1h or not candles_4h or not ticker:
        return None

    closes_1h = [c["close"] for c in candles_1h]
    closes_4h = [c["close"] for c in candles_4h]
    price     = closes_1h[-1]

    e9  = ema(closes_1h, 9)
    e21 = ema(closes_1h, 21)
    e50 = ema(closes_1h, 50)

    e21_4h = ema(closes_4h, 21)
    e50_4h = ema(closes_4h, 50)

    rsi_1h = rsi(closes_1h)
    atr_val = atr(candles_1h)
    vol_spike = volume_spike(candles_1h)
    change_24h = float(ticker.get("priceChangePercent", 0))

    score     = 0
    direction = None
    reasons   = []

    bull_1h = e9 > e21 > e50
    bear_1h = e9 < e21 < e50
    bull_4h = e21_4h > e50_4h
    bear_4h = e21_4h < e50_4h

    if bull_1h:
        score += 25; reasons.append("📈 EMA bullish (1H)")
    elif bear_1h:
        score += 25; reasons.append("📉 EMA bearish (1H)")

    if bull_1h and bull_4h:
        score += 20; reasons.append("✅ 4H trend confirms")
    elif bear_1h and bear_4h:
        score += 20; reasons.append("✅ 4H trend confirms")

    if rsi_1h < 35 and not bear_1h:
        score += 20; reasons.append(f"🔵 RSI oversold ({rsi_1h:.0f})")
    elif rsi_1h > 65 and not bull_1h:
        score += 20; reasons.append(f"🔴 RSI overbought ({rsi_1h:.0f})")
    elif 40 < rsi_1h < 60:
        score += 10; reasons.append(f"⚪ RSI neutral ({rsi_1h:.0f})")

    if vol_spike:
        score += 15; reasons.append("⚡ Volume spike (conviction)")

    if change_24h > 2:
        score += 10; reasons.append(f"🚀 +{change_24h:.1f}% 24h momentum")
    elif change_24h < -2:
        score += 10; reasons.append(f"💧 {change_24h:.1f}% 24h sell pressure")

    if bull_1h and (rsi_1h < 65) and bull_4h:
        direction = "BUY"
    elif bear_1h and (rsi_1h > 35) and bear_4h:
        direction = "SELL"
    elif rsi_1h < 30 and not bear_1h:
        direction = "BUY"; score = max(score, 60)
    elif rsi_1h > 70 and not bull_1h:
        direction = "SELL"; score = max(score, 60)

    if not direction or score < MIN_SIGNAL_STRENGTH:
        return None

    atr_m = atr_val
    if direction == "BUY":
        entry = price
        sl    = round(price - atr_m * 1.5,  6)
        tp1   = round(price + atr_m * 1.5,  6)
        tp2   = round(price + atr_m * 2.5,  6)
        tp3   = round(price + atr_m * 4.0,  6)
    else:
        entry = price
        sl    = round(price + atr_m * 1.5,  6)
        tp1   = round(price - atr_m * 1.5,  6)
        tp2   = round(price - atr_m * 2.5,  6)
        tp3   = round(price - atr_m * 4.0,  6)

    sl_dist = abs(entry - sl)
    rr1 = round(abs(tp1 - entry) / sl_dist, 1) if sl_dist else 0
    rr2 = round(abs(tp2 - entry) / sl_dist, 1) if sl_dist else 0
    rr3 = round(abs(tp3 - entry) / sl_dist, 1) if sl_dist else 0

    if rr2 < MIN_RR_RATIO:
        return None

    risk_usd   = CAPITAL * (RISK_PER_TRADE / 100)
    qty        = round(risk_usd / sl_dist, 6) if sl_dist else 0
    pos_value  = round(qty * entry, 2)

    return {
        "symbol":     symbol.replace("USDT", "/USDT"),
        "direction":  direction,
        "entry":      entry,
        "sl":         sl,
        "tp1":        tp1,
        "tp2":        tp2,
        "tp3":        tp3,
        "rr1":        rr1,
        "rr2":        rr2,
        "rr3":        rr3,
        "score":      min(score, 100),
        "rsi":        round(rsi_1h, 1),
        "change_24h": change_24h,
        "risk_usd":   round(risk_usd, 2),
        "qty":        qty,
        "pos_value":  pos_value,
        "reasons":    reasons,
        "atr":        round(atr_val, 6),
    }

def fmt(price):
    if price >= 1000: return f"${price:,.2f}"
    if price >= 1:    return f"${price:.4f}"
    return f"${price:.6f}"

def send_telegram(text):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=data, timeout=10)
        r.raise_for_status()
        log.info("Message sent to Telegram ✓")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")

def build_signal_message(sig):
    arrow  = "🟢" if sig["direction"] == "BUY" else "🔴"
    d      = sig["direction"]
    now    = datetime.now(ZoneInfo("Africa/Lagos")).strftime("%d %b %Y · %H:%M WAT")
    msg = f"""{arrow} <b>SIGNAL — {sig['symbol']}</b>
━━━━━━━━━━━━━━━━━━━━━
📅 {now}
⚡ Direction: <b>{d}</b>
💪 Confidence: <b>{sig['score']}%</b>
📊 RSI: {sig['rsi']} | 24h: {sig['change_24h']:+.1f}%

<b>📍 ENTRY</b>
{fmt(sig['entry'])}

<b>🛑 STOP LOSS</b>
{fmt(sig['sl'])}  (lose max ${sig['risk_usd']})

<b>🎯 TAKE PROFIT OPTIONS</b>
TP1 (Safe)       → {fmt(sig['tp1'])}  · R:R 1:{sig['rr1']}
TP2 (Moderate) → {fmt(sig['tp2'])}  · R:R 1:{sig['rr2']}
TP3 (Stretch)   → {fmt(sig['tp3'])}  · R:R 1:{sig['rr3']}

<b>💰 POSITION SIZE (${CAPITAL} capital, {RISK_PER_TRADE}% risk)</b>
Qty: {sig['qty']} {sig['symbol'].split('/')[0]}
Value: ${sig['pos_value']}
Max loss: ${sig['risk_usd']}

<b>📋 WHY THIS SIGNAL</b>
{chr(10).join(sig['reasons'])}
━━━━━━━━━━━━━━━━━━━━━
⚠️ Always honour your SL. This is analysis, not financial advice."""
    return msg

def send_weekly_summary():
    capital_now = weekly_stats["capital"]
    gain        = capital_now - CAPITAL
    gain_pct    = (gain / CAPITAL) * 100
    target_pct  = WEEKLY_TARGET
    progress    = min((gain_pct / target_pct) * 100, 100) if target_pct else 0
    bar_filled  = int(progress / 10)
    bar         = "█" * bar_filled + "░" * (10 - bar_filled)
    msg = f"""📊 <b>WEEKLY SUMMARY</b>
━━━━━━━━━━━━━━━━━━━━━
Starting capital: ${CAPITAL:.2f}
Current capital:  ${capital_now:.2f}
Net change:       {gain:+.2f} ({gain_pct:+.1f}%)
Weekly target: {target_pct}%
Progress: [{bar}] {progress:.0f}%

Signals sent this week: {weekly_stats['signals_sent']}
  🟢 Buy signals:  {weekly_stats['buys']}
  🔴 Sell signals: {weekly_stats['sells']}

{'🎯 TARGET HIT! Great week.' if gain_pct >= target_pct else '⏳ Keep going — stay disciplined.'}
━━━━━━━━━━━━━━━━━━━━━
Next week starts fresh. Trust the process."""
    send_telegram(msg)

def reset_weekly():
    weekly_stats["signals_sent"] = 0
    weekly_stats["buys"]         = 0
    weekly_stats["sells"]        = 0
    weekly_stats["week_start"]   = datetime.now(timezone.utc).isoformat()
    log.info("Weekly stats reset")

def main():
    log.info("🚀 Crypto Signal Bot starting...")
    send_telegram(f"""🤖 <b>Signal Bot is LIVE</b>
━━━━━━━━━━━━━━━━━━━━━
Capital: ${CAPITAL}
Risk per trade: {RISK_PER_TRADE}%
Weekly target: {WEEKLY_TARGET}%
Scanning {len(PAIRS)} pairs every {SCAN_INTERVAL//60} minutes
Pairs: {', '.join(p.replace('USDT','/USDT') for p in PAIRS)}
━━━━━━━━━━━━━━━━━━━━━
I will message you whenever a high-quality setup appears. Stand by.""")
    
    scan_count   = 0
    last_week_dt = datetime.now(timezone.utc)
    while True:
        now = datetime.now(timezone.utc)
        days_elapsed = (now - last_week_dt).days
        if days_elapsed >= 7:
            send_weekly_summary()
            reset_weekly()
            last_week_dt = now
        scan_count += 1
        log.info(f"Scan #{scan_count} — {now.strftime('%Y-%m-%d %H:%M UTC')}")
        found = 0
        for symbol in PAIRS:
            log.info(f"  Analysing {symbol}...")
            sig = analyse(symbol)
            if sig:
                msg = build_signal_message(sig)
                send_telegram(msg)
                weekly_stats["signals_sent"] += 1
                if sig["direction"] == "BUY":
                    weekly_stats["buys"] += 1
                else:
                    weekly_stats["sells"] += 1
                found += 1
                time.sleep(2)
        if found == 0:
            log.info(f"  No signals this scan. Market quiet or conditions not met.")
        log.info(f"  Scan complete. Next scan in {SCAN_INTERVAL//60} minutes.")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    # Start the background server for Render immediately
    threading.Thread(target=run_health_server, daemon=True).start()
    main()