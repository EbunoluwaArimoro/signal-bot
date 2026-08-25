"""
Rich Genie Signal Bot v4 — Honest Accounting Edition
=====================================================
Built after auditing ~100 live signals across v2 and v3.

WHAT WAS WRONG IN V3 (found in the live logs):
  1. Every stop-out printed "-$5.00". Actual loss on a $5 position with a
     1.3% stop is ~$0.065. Losses were overstated ~77x while wins were
     reported accurately. The log was unreadable as a performance record.
  2. The bot told you "move SL to break-even at TP1" but the tracker kept
     watching the ORIGINAL stop forever. Trades that hit TP1 and TP2 and
     then pulled back were logged as full losses. Signal #0054 BNB was
     actually +$0.026; it was recorded as -$5.00 and counted against WR.
  3. state["capital"] was never written to. Every weekly report said
     "$100.00 -> $100.00" no matter what happened.
  4. Signals fired in dead-flat markets (BB width 0.47%, 0.52%, 0.75%)
     where a 1.3% ATR stop sits INSIDE normal noise. Guaranteed stop-outs.
  5. RSI_MAX_BUY=78 still allowed buys at RSI 73.7 / 76.0 / 77.1 — tops.

WHAT V4 CHANGES:
  FIX 1 — Real P&L. Partial exits modelled (40/40/20). Every number the
          bot prints is the actual dollar result of the position it sized.
  FIX 2 — Break-even stop is TRACKED, not just suggested. Once TP1 hits,
          the stop moves to entry in the tracker too.
  FIX 3 — Capital updates on every close. Weekly report reflects reality.
  FIX 4 — Volatility floor. No signal unless ATR >= 1.2% of price AND
          Bollinger width >= 1.5%. Kills the noise-stop-out cluster.
  FIX 5 — Stops widened to 2.5x ATR (was 1.5x) so ordinary wiggle doesn't
          take you out. TPs re-derived to keep R:R at 1.5 / 2.5 / 4.0.
  FIX 6 — RSI blocks tightened: no BUY above 68, no SELL below 32.
  FIX 7 — Position sizing is honest. Risk-based size, hard-capped at 20%
          of capital, and the bot reports the REAL dollar risk that
          implies rather than a made-up "max loss" figure.
  FIX 8 — Validation gate. Until 30 closed trades, the bot labels itself
          UNVALIDATED and reports win rate + expectancy, not projections.

ON TARGETS — READ THIS:
  There is no weekly percentage target in this bot, deliberately.
  25%/week compounds $100 into $10.9M in a year; no fund or trader has
  ever sustained anything close. Adding capital scales dollars, never
  percentages. The only metric here that matters is WIN RATE and
  EXPECTANCY over 30+ closed trades. If win rate clears ~40% at these
  R:R levels, the strategy has a real edge worth funding. If it doesn't,
  no amount of capital fixes it.

  This is analysis software, not financial advice. An edge that held
  historically can stop working at any time.
"""

import os, time, logging, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ─── CONFIGURATION ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID        = os.environ.get("CHAT_ID",        "YOUR_CHAT_ID_HERE")
CAPITAL        = float(os.environ.get("CAPITAL",   "100"))
RISK_PCT       = float(os.environ.get("RISK_PCT",  "2"))    # % of capital risked per trade
MAX_POS_PCT    = float(os.environ.get("MAX_POS_PCT","20"))  # hard cap on position size
SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL","3600"))

# ── Quality gates ──
MIN_CONDITIONS     = 5      # of 7
MAX_SIGNALS_DAY    = 2
COOLDOWN_HOURS     = 8
MIN_VOLUME_RATIO   = 0.6
MIN_ATR_PCT        = 1.2    # FIX 4: ATR must be >=1.2% of price
MIN_BB_WIDTH_PCT   = 1.5    # FIX 4: Bollinger width floor
MAX_EMA50_DIST_PCT = 4.0
RSI_MAX_BUY        = 68     # FIX 6 (was 78)
RSI_MIN_SELL       = 32     # FIX 6 (was 22)
RSI_BUY_EXTREME    = 35
RSI_SELL_EXTREME   = 65

# ── Level geometry (FIX 5) ──
SL_ATR_MULT = 2.5
TP1_R, TP2_R, TP3_R = 1.5, 2.5, 4.0
TP1_SIZE, TP2_SIZE, TP3_SIZE = 0.40, 0.40, 0.20

MIN_TRADES_TO_VALIDATE = 30
TARGET_WIN_RATE        = 40.0

PAIRS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
         "ADAUSDT","DOGEUSDT","AVAXUSDT","MATICUSDT","LINKUSDT"]

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)
BINANCE = "https://api.binance.com/api/v3"

state = {
    "capital": CAPITAL, "starting_capital": CAPITAL,
    "signals_today": 0, "signals_week": 0,
    "wins": 0, "losses": 0, "breakeven": 0,
    "wins_week": 0, "losses_week": 0,
    "gross_win": 0.0, "gross_loss": 0.0,
    "closed_trades": 0,
    "last_signal_time": {}, "open_signals": [],
    "week_start": datetime.now(timezone.utc),
    "day_start":  datetime.now(timezone.utc),
    "counter": 0,
}

# ─── DATA ───────────────────────────────────────────────────────

def klines(symbol, interval, limit=150):
    try:
        r = requests.get(f"{BINANCE}/klines",
            params={"symbol":symbol,"interval":interval,"limit":limit}, timeout=10)
        r.raise_for_status()
        return [{"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),
                 "close":float(k[4]),"volume":float(k[5])} for k in r.json()]
    except Exception as e:
        log.warning(f"klines {symbol} {interval}: {e}")
        return []

def ticker(symbol):
    try:
        r = requests.get(f"{BINANCE}/ticker/24hr", params={"symbol":symbol}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"ticker {symbol}: {e}")
        return {}

# ─── INDICATORS ─────────────────────────────────────────────────

def ema(closes, period):
    if len(closes) < period: return closes[-1]
    k = 2/(period+1); e = sum(closes[:period])/period
    for v in closes[period:]: e = v*k + e*(1-k)
    return e

def rsi(closes, period=14):
    if len(closes) < period+2: return 50.0
    g, l = [], []
    for i in range(1,len(closes)):
        d = closes[i]-closes[i-1]; g.append(max(d,0)); l.append(max(-d,0))
    ag, al = sum(g[-period:])/period, sum(l[-period:])/period
    return 100.0 if al == 0 else 100-(100/(1+ag/al))

def atr(candles, period=14):
    trs=[]
    for i in range(1,len(candles)):
        trs.append(max(candles[i]["high"]-candles[i]["low"],
                       abs(candles[i]["high"]-candles[i-1]["close"]),
                       abs(candles[i]["low"]-candles[i-1]["close"])))
    t = trs[-period:] if len(trs)>=period else trs
    return sum(t)/len(t) if t else 0.0

def macd(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow+sig+5: return 0.0
    line = ema(closes,fast)-ema(closes,slow)
    vals=[]
    for i in range(sig+5,0,-1):
        vals.append(ema(closes[:-i],fast)-ema(closes[:-i],slow))
    return line - (ema(vals,sig) if vals else 0)

def supertrend(candles, period=10, mult=3.0):
    if len(candles) < period+5: return 0
    c=[x["close"] for x in candles]; h=[x["high"] for x in candles]; l=[x["low"] for x in candles]
    trs=[]
    for i in range(1,len(candles)):
        trs.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    sm=[sum(trs[:period])/period]
    for v in trs[period:]: sm.append((sm[-1]*(period-1)+v)/period)
    if len(sm)<5: return 0
    hl2=[(a+b)/2 for a,b in zip(h[1:],l[1:])]
    up=[m+mult*a for m,a in zip(hl2,sm)]; dn=[m-mult*a for m,a in zip(hl2,sm)]
    pu = up[-2] if len(up)>1 else up[-1]; pl = dn[-2] if len(dn)>1 else dn[-1]
    pc, cc = c[-2], c[-1]
    fu = min(up[-1],pu) if cc<=pu else up[-1]
    fl = max(dn[-1],pl) if cc>=pl else dn[-1]
    d = -1 if pc>pu else 1
    if d==1 and cc<fl: d=-1
    elif d==-1 and cc>fu: d=1
    return d

def bollinger(closes, period=20, sd=2.0):
    if len(closes)<period: return closes[-1],closes[-1],closes[-1],0.0
    w=closes[-period:]; mid=sum(w)/period
    std=(sum((x-mid)**2 for x in w)/period)**0.5
    u,l = mid+sd*std, mid-sd*std
    return u, mid, l, ((u-l)/mid if mid else 0)

def vol_ratio(candles, lookback=20):
    if len(candles)<lookback+1: return 1.0
    avg=sum(c["volume"] for c in candles[-lookback-1:-1])/lookback
    return candles[-1]["volume"]/avg if avg>0 else 1.0

def regime(symbol):
    c4=klines(symbol,"4h",100); c1d=klines(symbol,"1d",30)
    if not c4 or not c1d or len(c4)<20: return "neutral"
    st=supertrend(c4); r=rsi([x["close"] for x in c1d], period=min(14,len(c1d)-2))
    if st==1 and r>45: return "bull"
    if st==-1 and r<55: return "bear"
    return "neutral"

# ─── P&L HELPERS (FIX 1) ────────────────────────────────────────

def pnl_pct(entry, exit_price, direction):
    return (exit_price-entry)/entry if direction=="BUY" else (entry-exit_price)/entry

def realise(sig, exit_price, portion):
    """Book P&L on `portion` of the position at exit_price. Returns dollars."""
    d = sig["pos_value"] * portion * pnl_pct(sig["entry"], exit_price, sig["direction"])
    sig["realized"]  += d
    sig["remaining"] -= portion
    return d

# ─── SIGNAL ENGINE ──────────────────────────────────────────────

def analyse(symbol):
    c1=klines(symbol,"1h",150); c4=klines(symbol,"4h",100)
    if not c1 or not c4 or len(c1)<50: return None
    tk=ticker(symbol)
    if not tk: return None

    price=c1[-1]["close"]
    cl1=[x["close"] for x in c1]
    chg=float(tk.get("priceChangePercent",0))

    st1, st4 = supertrend(c1), supertrend(c4)
    r1, r4   = rsi(cl1), rsi([x["close"] for x in c4])
    mh       = macd(cl1)
    bu,bm,bl,bw = bollinger(cl1)
    vr       = vol_ratio(c1)
    e9,e21,e50 = ema(cl1,9), ema(cl1,21), ema(cl1,50)
    a        = atr(c1)
    reg      = regime(symbol)

    atr_pct = (a/price*100) if price else 0
    bb_pct  = bw*100

    # ── FIX 4: volatility floor — the single biggest source of v3 losses ──
    if atr_pct < MIN_ATR_PCT:
        log.info(f"  {symbol}: ATR {atr_pct:.2f}% < {MIN_ATR_PCT}% floor (dead market). Skip.")
        return None
    if bb_pct < MIN_BB_WIDTH_PCT:
        log.info(f"  {symbol}: BB width {bb_pct:.2f}% < {MIN_BB_WIDTH_PCT}% floor (squeeze). Skip.")
        return None
    if vr < MIN_VOLUME_RATIO:
        log.info(f"  {symbol}: volume {vr:.2f}x too low. Skip.")
        return None
    if e50 and abs(price-e50)/e50*100 > MAX_EMA50_DIST_PCT:
        log.info(f"  {symbol}: overextended from EMA50. Skip.")
        return None

    buy, sell = [], []
    if st1==1:  buy.append("Supertrend 1H bullish")
    if st4==1:  buy.append("Supertrend 4H confirms")
    if r1 < RSI_BUY_EXTREME: buy.append(f"RSI oversold ({r1:.0f})")
    if mh > 0:  buy.append("MACD bullish momentum")
    if price <= bl*1.01: buy.append("Price at BB lower band")
    if vr >= 1.3: buy.append(f"Volume conviction ({vr:.1f}x)")
    if e9>e21>e50: buy.append("Full EMA stack bullish")

    if st1==-1: sell.append("Supertrend 1H bearish")
    if st4==-1: sell.append("Supertrend 4H confirms")
    if r1 > RSI_SELL_EXTREME: sell.append(f"RSI overbought ({r1:.0f})")
    if mh < 0:  sell.append("MACD bearish momentum")
    if price >= bu*0.99: sell.append("Price at BB upper band")
    if vr >= 1.3: sell.append(f"Volume conviction ({vr:.1f}x)")
    if e9<e21<e50: sell.append("Full EMA stack bearish")

    if reg=="bull" and r1 < 75: sell=[]
    elif reg=="bear" and r1 > 25: buy=[]

    if r1 > RSI_MAX_BUY:
        if buy: log.info(f"  {symbol}: BUY blocked, RSI {r1:.0f} > {RSI_MAX_BUY}")
        buy=[]
    if r1 < RSI_MIN_SELL:
        if sell: log.info(f"  {symbol}: SELL blocked, RSI {r1:.0f} < {RSI_MIN_SELL}")
        sell=[]

    if len(buy)<MIN_CONDITIONS and len(sell)<MIN_CONDITIONS: return None
    if len(buy)>=len(sell) and len(buy)>=MIN_CONDITIONS:
        direction, conds = "BUY", buy
    elif len(sell)>MIN_CONDITIONS-1 and len(sell)>len(buy):
        direction, conds = "SELL", sell
    else:
        return None

    # ── FIX 5: wider stops ──
    sl_dist = a * SL_ATR_MULT
    if sl_dist <= 0: return None
    if direction=="BUY":
        entry=price; sl=entry-sl_dist
        tp1,tp2,tp3 = entry+sl_dist*TP1_R, entry+sl_dist*TP2_R, entry+sl_dist*TP3_R
    else:
        entry=price; sl=entry+sl_dist
        tp1,tp2,tp3 = entry-sl_dist*TP1_R, entry-sl_dist*TP2_R, entry-sl_dist*TP3_R

    # ── FIX 7: honest position sizing ──
    cap = state["capital"]
    risk_target   = cap * (RISK_PCT/100)
    qty_by_risk   = risk_target / sl_dist
    qty_by_cap    = (cap * MAX_POS_PCT/100) / entry
    qty           = min(qty_by_risk, qty_by_cap)
    pos_value     = qty * entry
    actual_risk   = qty * sl_dist            # the REAL worst case in dollars
    capped        = qty_by_cap < qty_by_risk

    state["counter"] += 1
    return {
        "id": f"#{state['counter']:04d}", "symbol":symbol,
        "display":symbol.replace("USDT","/USDT"), "direction":direction,
        "entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,
        "sl_dist":sl_dist,"conds":conds,"score":len(conds),
        "confidence":min(int(len(conds)/7*100),97),
        "r1":round(r1,1),"r4":round(r4,1),"vr":round(vr,2),
        "atr_pct":round(atr_pct,2),"bb_pct":round(bb_pct,2),
        "st1":st1,"st4":st4,"regime":reg,"chg":chg,
        "qty":qty,"pos_value":round(pos_value,2),
        "actual_risk":round(actual_risk,2),"capped":capped,
        "fired_at":datetime.now(timezone.utc),
        # tracking state
        "remaining":1.0,"realized":0.0,"sl_at_entry":False,
        "tp1_hit":False,"tp2_hit":False,"tp3_hit":False,"closed":False,
    }

# ─── OUTCOME TRACKING (FIX 1 + FIX 2 + FIX 3) ──────────────────

def close_trade(sig, label):
    """Book the trade, update capital and stats, notify."""
    net = sig["realized"]
    state["capital"] += net
    state["closed_trades"] += 1
    if net > 0.0001:
        state["wins"] += 1; state["wins_week"] += 1; state["gross_win"] += net
        verdict = "WIN"
    elif net < -0.0001:
        state["losses"] += 1; state["losses_week"] += 1; state["gross_loss"] += abs(net)
        verdict = "LOSS"
    else:
        state["breakeven"] += 1
        verdict = "BREAK-EVEN"
    sig["closed"] = True
    send(f"{'🏆' if net>0 else '🛑' if net<0 else '⚖️'} <b>CLOSED {sig['id']} — {sig['display']}</b>\n"
         f"━━━━━━━━━━━━━━━━━━━━━━━\n"
         f"Reason: {label}\n"
         f"Entry: {fmt(sig['entry'])}\n"
         f"<b>Net P&L: ${net:+.4f}</b>  ({verdict})\n"
         f"Capital: ${state['capital']:.4f}\n"
         f"Record: {state['wins']}W / {state['losses']}L / {state['breakeven']}BE")

def check_open():
    done=[]
    for s in state["open_signals"]:
        if s["closed"]:
            done.append(s); continue
        try:
            tk=ticker(s["symbol"])
            if not tk: continue
            p=float(tk["lastPrice"]); d=s["direction"]

            hit = lambda lvl: (d=="BUY" and p>=lvl) or (d=="SELL" and p<=lvl)
            stop_level = s["entry"] if s["sl_at_entry"] else s["sl"]
            stopped = (d=="BUY" and p<=stop_level) or (d=="SELL" and p>=stop_level)

            if hit(s["tp3"]) and not s["tp3_hit"]:
                s["tp1_hit"]=s["tp2_hit"]=s["tp3_hit"]=True
                if s["remaining"]>0: realise(s, s["tp3"], s["remaining"])
                close_trade(s,"TP3 — full target"); done.append(s); continue

            if hit(s["tp2"]) and not s["tp2_hit"]:
                if not s["tp1_hit"]:
                    realise(s, s["tp1"], TP1_SIZE); s["tp1_hit"]=True; s["sl_at_entry"]=True
                got = realise(s, s["tp2"], TP2_SIZE); s["tp2_hit"]=True
                send(f"🎯 <b>TP2 {s['id']} — {s['display']}</b>\n"
                     f"Booked ${got:+.4f} on 40%. Running P&L ${s['realized']:+.4f}\n"
                     f"Stop is at entry. Watching TP3 {fmt(s['tp3'])} 👀")

            elif hit(s["tp1"]) and not s["tp1_hit"]:
                got = realise(s, s["tp1"], TP1_SIZE)
                s["tp1_hit"]=True; s["sl_at_entry"]=True
                send(f"✅ <b>TP1 {s['id']} — {s['display']}</b>\n"
                     f"Booked ${got:+.4f} on 40%. Stop moved to entry — trade is risk-free.\n"
                     f"Watching TP2 {fmt(s['tp2'])} 👀")

            elif stopped:
                realise(s, stop_level, s["remaining"])
                close_trade(s, "Stop at entry (protected)" if s["sl_at_entry"] else "Stop loss")
                done.append(s); continue

            age=(datetime.now(timezone.utc)-s["fired_at"]).total_seconds()/3600
            if age>48 and not s["closed"]:
                if s["remaining"]>0: realise(s, p, s["remaining"])
                close_trade(s,"48h expiry — closed at market"); done.append(s)

        except Exception as e:
            log.warning(f"track {s.get('id','?')}: {e}")
    for s in done:
        if s in state["open_signals"]: state["open_signals"].remove(s)

# ─── GATES ──────────────────────────────────────────────────────

def can_signal(sym):
    if state["signals_today"] >= MAX_SIGNALS_DAY: return False
    last=state["last_signal_time"].get(sym)
    if last and (datetime.now(timezone.utc)-last).total_seconds()/3600 < COOLDOWN_HOURS:
        return False
    return True

def rollover():
    now=datetime.now(timezone.utc)
    if (now-state["day_start"]).total_seconds()>86400:
        state["signals_today"]=0; state["day_start"]=now
    if (now-state["week_start"]).total_seconds()>604800:
        weekly_report()
        state["signals_week"]=state["wins_week"]=state["losses_week"]=0
        state["week_start"]=now

# ─── FORMAT ─────────────────────────────────────────────────────

def fmt(p):
    if p>=10000: return f"${p:,.0f}"
    if p>=1000:  return f"${p:,.2f}"
    if p>=1:     return f"${p:.4f}"
    return f"${p:.6f}"

def send(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id":CHAT_ID,"text":text,"parse_mode":"HTML"}, timeout=10).raise_for_status()
    except Exception as e:
        log.error(f"telegram: {e}")

def signal_msg(s):
    arrow="🟢" if s["direction"]=="BUY" else "🔴"
    now=datetime.now(ZoneInfo("Africa/Lagos")).strftime("%d %b %Y · %H:%M WAT")
    reg={"bull":"🐂 Bull","bear":"🐻 Bear","neutral":"↔️ Neutral"}[s["regime"]]
    conds="\n".join(f"  ✓ {c}" for c in s["conds"])
    bar="█"*round(s["confidence"]/10)+"░"*(10-round(s["confidence"]/10))
    cap_note = "\n<i>(size capped at %d%% of capital)</i>" % MAX_POS_PCT if s["capped"] else ""
    return (f"{arrow} <b>SIGNAL {s['id']} — {s['display']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n📅 {now}\n"
        f"⚡ {s['direction']}   🌍 {reg}\n"
        f"💪 {s['confidence']}% [{bar}]  ({s['score']}/7)\n\n"
        f"<b>📍 ENTRY</b>  {fmt(s['entry'])}\n"
        f"<b>🛑 STOP</b>   {fmt(s['sl'])}  ({s['sl_dist']/s['entry']*100:.2f}% away)\n\n"
        f"<b>🎯 TARGETS</b>\n"
        f"TP1 {fmt(s['tp1'])}  1:{TP1_R}  close 40% → stop to entry\n"
        f"TP2 {fmt(s['tp2'])}  1:{TP2_R}  close 40%\n"
        f"TP3 {fmt(s['tp3'])}  1:{TP3_R}  close final 20%\n\n"
        f"<b>💰 POSITION</b>\n"
        f"Buy ${s['pos_value']} ({s['qty']:.6f} {s['symbol'].replace('USDT','')})\n"
        f"<b>Real risk if stopped: ${s['actual_risk']:.3f}</b>{cap_note}\n\n"
        f"<b>📋 WHY</b>\n{conds}\n\n"
        f"RSI 1H {s['r1']} | 4H {s['r4']}   ATR {s['atr_pct']}%   BB {s['bb_pct']}%\n"
        f"Vol {s['vr']}x   24h {s['chg']:+.1f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ Analysis only, not financial advice.")

def weekly_report():
    w,l,be = state["wins"], state["losses"], state["breakeven"]
    n = state["closed_trades"]
    wr = (w/(w+l)*100) if (w+l) else 0
    net = state["capital"]-state["starting_capital"]
    pf = (state["gross_win"]/state["gross_loss"]) if state["gross_loss"]>0 else 0
    avg = (net/n) if n else 0

    if n < MIN_TRADES_TO_VALIDATE:
        verdict=(f"⏳ <b>UNVALIDATED</b> — {n}/{MIN_TRADES_TO_VALIDATE} closed trades.\n"
                 f"Too early to judge. Keep collecting data.")
    elif wr >= TARGET_WIN_RATE and net > 0:
        verdict=(f"✅ <b>EDGE CONFIRMED</b> over {n} trades.\n"
                 f"Win rate {wr:.0f}% clears the {TARGET_WIN_RATE:.0f}% bar and P&L is positive.\n"
                 f"This is the point where scaling capital is a rational decision.")
    else:
        verdict=(f"❌ <b>NO EDGE YET</b> over {n} trades.\n"
                 f"Win rate {wr:.0f}% vs {TARGET_WIN_RATE:.0f}% needed.\n"
                 f"Do not add capital. More money multiplies a negative expectancy.")

    send(f"📊 <b>PERFORMANCE REPORT</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
         f"Capital: ${state['starting_capital']:.2f} → <b>${state['capital']:.4f}</b>\n"
         f"Net: ${net:+.4f}  ({net/state['starting_capital']*100:+.2f}%)\n\n"
         f"Closed trades: {n}\n"
         f"W/L/BE: {w} / {l} / {be}\n"
         f"<b>Win rate: {wr:.1f}%</b>  (need {TARGET_WIN_RATE:.0f}%)\n"
         f"Profit factor: {pf:.2f}  (need >1.0)\n"
         f"Avg per trade: ${avg:+.4f}\n"
         f"Signals this week: {state['signals_week']}\n"
         f"━━━━━━━━━━━━━━━━━━━━━━━\n{verdict}")

# ─── RENDER WEB SERVER ──────────────────────────────────────────
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(
            f"Rich Genie v4 alive | capital ${state['capital']:.4f} | "
            f"{state['wins']}W/{state['losses']}L/{state['breakeven']}BE | "
            f"{state['closed_trades']} closed".encode())
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass

def run_health_server():
    port=int(os.environ.get("PORT",8080))
    HTTPServer(("0.0.0.0",port), Health).serve_forever()

# ─── MAIN ───────────────────────────────────────────────────────

def main():
    log.info("Rich Genie v4 starting")
    send(f"🤖 <b>Rich Genie Bot v4 LIVE</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Honest accounting edition.</b>\n\n"
        f"Fixed from v3:\n"
        f"  ✓ Real dollar P&L (v3 overstated every loss ~77x)\n"
        f"  ✓ Break-even stop now actually tracked\n"
        f"  ✓ Capital updates on every close\n"
        f"  ✓ Volatility floor: ATR ≥{MIN_ATR_PCT}%, BB ≥{MIN_BB_WIDTH_PCT}%\n"
        f"  ✓ Stops widened to {SL_ATR_MULT}× ATR\n"
        f"  ✓ No BUY above RSI {RSI_MAX_BUY}, no SELL below {RSI_MIN_SELL}\n"
        f"  ✓ Honest position sizing + real risk shown\n\n"
        f"💰 ${CAPITAL} | risk {RISK_PCT}%/trade | max {MAX_POS_PCT}% position\n"
        f"Max {MAX_SIGNALS_DAY} signals/day, {MIN_CONDITIONS}/7 conditions minimum\n\n"
        f"<b>There is no weekly % target in this version.</b>\n"
        f"The only question that matters: does win rate clear "
        f"{TARGET_WIN_RATE:.0f}% over {MIN_TRADES_TO_VALIDATE}+ closed trades?\n"
        f"Scale capital only if it does.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\nStanding by…")

    time.sleep(60); n=0
    while True:
        n+=1
        log.info(f"─── scan #{n} ───")
        rollover()
        if state["open_signals"]: check_open()

        if state["signals_today"] < MAX_SIGNALS_DAY:
            for sym in PAIRS:
                if state["signals_today"] >= MAX_SIGNALS_DAY: break
                if not can_signal(sym): continue
                log.info(f"  analysing {sym}")
                s=analyse(sym)
                if s:
                    state["signals_today"]+=1; state["signals_week"]+=1
                    state["last_signal_time"][sym]=datetime.now(timezone.utc)
                    state["open_signals"].append(s)
                    send(signal_msg(s))
                    log.info(f"  ✓ {s['id']} {sym} {s['direction']} {s['score']}/7 "
                             f"pos ${s['pos_value']} risk ${s['actual_risk']}")
                    time.sleep(2)
        else:
            log.info("daily cap reached")

        log.info(f"scan #{n} done, sleeping {SCAN_INTERVAL//60}m")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    main()
    