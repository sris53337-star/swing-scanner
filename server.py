from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import requests as req
import os
import threading
import time
from datetime import datetime

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=False)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SHEETS_URL       = "https://script.google.com/macros/s/AKfycbzu0aDb-n4re6qw_RtkkAYA-EbdhQcTnS9DoDd4wxhb4DTMKE89SUFxqtoeAa2mBx_V/exec"
CAPITAL          = 5000
RISK_PCT         = 2

sent_signals  = {}
active_trades = {}
eod_sent      = False

# ── Helpers ───────────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
    except:
        pass

def log_to_sheets(data):
    try:
        req.post(SHEETS_URL, json=data, timeout=10)
    except:
        pass

def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(window=period).mean()
    loss  = -delta.clip(upper=0).rolling(window=period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))

def compute_atr(df, period=14):
    high_low   = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close  = (df['Low']  - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calculate_position_size(entry, sl):
    risk_amount    = CAPITAL * (RISK_PCT / 100)
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0 or entry == 0:
        return 0, 0, 0, 0
    risk_based    = int(risk_amount / risk_per_share)
    capital_based = int(CAPITAL / entry)
    shares        = min(risk_based, capital_based)
    if shares <= 0:
        return 0, 0, 0, 0
    cost     = round(shares * entry, 2)
    max_loss = round(shares * risk_per_share, 2)
    max_gain = round(max_loss * 2, 2)
    return shares, cost, max_loss, max_gain

def load_watchlist():
    try:
        with open("watchlist.txt", "r") as f:
            return [line.strip() for line in f
                    if line.strip() and not line.startswith('#')]
    except:
        return ["PNB.NS", "BHEL.NS", "COALINDIA.NS"]

# ── Trade Monitor Thread ──────────────────────────────────────────────────────
def monitor_trades():
    global eod_sent
    while True:
        try:
            now = datetime.utcnow()
            ist_minutes = now.hour * 60 + now.minute + 330
            ist_hour    = (ist_minutes // 60) % 24
            ist_minute  = ist_minutes % 60

            # EOD summary at 3:35 PM IST
            if ist_hour == 15 and ist_minute >= 35 and not eod_sent:
                send_eod_summary()
                eod_sent = True

            # Reset at midnight IST
            if ist_hour == 0 and ist_minute < 5:
                eod_sent = False
                sent_signals.clear()
                active_trades.clear()

            # Check active swing trades daily
            for ticker in list(active_trades.keys()):
                trade = active_trades[ticker]
                try:
                    df = yf.download(ticker, period="5d", interval="1d", progress=False)
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    if df.empty:
                        continue

                    current_price = float(df['Close'].iloc[-1])
                    entry  = trade['entry']
                    sl     = trade['sl']
                    target = trade['target']
                    signal = trade['signal']
                    shares = trade['shares']
                    days   = trade.get('days', 0) + 1

                    result     = None
                    exit_price = None

                    if signal == 'BULLISH':
                        if current_price >= target:
                            result = 'TARGET HIT ✅'
                            exit_price = target
                        elif current_price <= sl:
                            result = 'SL HIT ❌'
                            exit_price = sl
                    else:
                        if current_price <= target:
                            result = 'TARGET HIT ✅'
                            exit_price = target
                        elif current_price >= sl:
                            result = 'SL HIT ❌'
                            exit_price = sl

                    # Force exit after 15 days
                    if days >= 15 and not result:
                        result = 'MAX DAYS EXIT 📤'
                        exit_price = current_price

                    active_trades[ticker]['days'] = days

                    if result:
                        pnl   = round(shares * (exit_price - entry) *
                                      (1 if signal == 'BULLISH' else -1) - 40, 2)
                        emoji = ("🎯" if "TARGET" in result
                                 else "🛑" if "SL" in result else "📤")
                        msg = (
                            f"{emoji} <b>SWING {result}</b>\n"
                            f"📌 <b>{ticker}</b>\n\n"
                            f"Entry:    ₹{entry}\n"
                            f"Exit:     ₹{exit_price}\n"
                            f"Shares:   {shares}\n"
                            f"Days held: {days}\n\n"
                            f"💰 Net P&L: <b>₹{pnl}</b>\n"
                            f"(after ₹40 brokerage)"
                        )
                        send_telegram(msg)
                        log_to_sheets({
                            "action":     "update_result",
                            "ticker":     ticker,
                            "result":     result,
                            "exit_price": exit_price,
                            "pnl":        pnl
                        })
                        del active_trades[ticker]
                except:
                    pass
        except:
            pass
        time.sleep(3600)  # check every 1 hour (swing = daily candles)

def send_eod_summary():
    try:
        watchlist = load_watchlist()
        bullish   = [t for t, d in sent_signals.items() if d.get('signal') == 'BULLISH']
        bearish   = [t for t, d in sent_signals.items() if d.get('signal') == 'BEARISH']
        total     = len(sent_signals)

        msg = (
            f"📊 <b>SWING EOD SUMMARY</b> — {datetime.utcnow().strftime('%d %b %Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 BULLISH signals: {len(bullish)}\n"
            f"⚠️  BEARISH signals: {len(bearish)}\n"
            f"📈 Total signals:   {total}\n"
            f"🔍 Stocks scanned:  {len(watchlist)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )
        if bullish:
            msg += "🚀 Bullish: " + ", ".join(
                [t.replace('.NS','').replace('_BULLISH','') for t in bullish]) + "\n"
        if bearish:
            msg += "⚠️  Bearish: " + ", ".join(
                [t.replace('.NS','').replace('_BEARISH','') for t in bearish]) + "\n"
        if total == 0:
            msg += "😴 No swing signals today\n"
        msg += (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Hold 5-15 days for targets!"
        )
        send_telegram(msg)
    except:
        pass

monitor_thread = threading.Thread(target=monitor_trades, daemon=True)
monitor_thread.start()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/watchlist")
def get_watchlist():
    return jsonify(load_watchlist())

@app.route("/scan/<ticker>")
def scan(ticker):
    try:
        df = yf.download(ticker, period="1y", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()

        if len(df) < 60:
            return jsonify({"error": "Not enough data"}), 404

        df['EMA20']   = compute_ema(df['Close'], 20)
        df['EMA50']   = compute_ema(df['Close'], 50)
        df['RSI']     = compute_rsi(df['Close'], 14)
        df['ATR']     = compute_atr(df, 14)
        df['VOL_AVG'] = df['Volume'].rolling(window=20).mean()
        df = df.dropna()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        price       = float(last['Close'])
        ema20       = round(float(last['EMA20']), 2)
        ema50       = round(float(last['EMA50']), 2)
        rsi         = round(float(last['RSI']), 2)
        atr_val     = round(float(last['ATR']), 2)
        volume      = int(last['Volume'])
        vol_avg     = int(last['VOL_AVG'])
        vol_ratio   = round(volume / vol_avg, 2) if vol_avg > 0 else 0
        trending_up = price > ema20 > ema50

        ema_bullish  = float(prev['EMA20']) <= float(prev['EMA50']) and ema20 > ema50
        ema_bearish  = float(prev['EMA20']) >= float(prev['EMA50']) and ema20 < ema50
        vol_ok       = vol_ratio >= 1.5
        rsi_bull_ok  = 47 <= rsi <= 63
        rsi_bear_ok  = 40 <= rsi <= 58

        bull_score = sum([ema_bullish, vol_ok, trending_up,      rsi_bull_ok])
        bear_score = sum([ema_bearish, vol_ok, not trending_up,  rsi_bear_ok])

        signal = None
        if ema_bullish and vol_ok and trending_up and rsi_bull_ok:
            signal = "BULLISH"
        elif ema_bearish and vol_ok and not trending_up and rsi_bear_ok:
            signal = "BEARISH"
        elif trending_up and bull_score >= 3:
            signal = "WEAK_BULLISH"
        elif not trending_up and bear_score >= 3:
            signal = "WEAK_BEARISH"

        # Swing uses 2x ATR SL, 4x ATR target
        if signal in ("BULLISH", "WEAK_BULLISH"):
            entry  = round(price, 2)
            sl     = round(price - atr_val * 2, 2)
            target = round(price + atr_val * 4, 2)
        elif signal in ("BEARISH", "WEAK_BEARISH"):
            entry  = round(price, 2)
            sl     = round(price + atr_val * 2, 2)
            target = round(price - atr_val * 4, 2)
        else:
            entry = sl = target = None

        # ── Send alert — NO DUPLICATES, NO TIME RESTRICTION for swing ────────
        if signal in ("BULLISH", "BEARISH") and entry and sl:
            signal_key = f"{ticker}_{signal}"

            if signal_key not in sent_signals:
                shares, cost, max_loss, max_gain = calculate_position_size(entry, sl)
                net_gain  = max_gain - 40
                direction = "BUY" if signal == "BULLISH" else "SELL"
                emoji     = "🚀" if signal == "BULLISH" else "⚠️"

                sent_signals[signal_key] = {'signal': signal}

                msg = (
                    f"{emoji} <b>SWING {signal}</b>\n"
                    f"📌 <b>{ticker}</b> @ ₹{round(price, 2)}\n\n"
                    f"✅ Entry:  ₹{entry}\n"
                    f"🛑 SL:     ₹{sl}\n"
                    f"🎯 Target: ₹{target}\n\n"
                    f"💰 <b>POSITION SIZE:</b>\n"
                    f"Action:    <b>{direction} {shares} shares</b>\n"
                    f"Cost:      ₹{cost}\n"
                    f"Max Loss:  ₹{max_loss}\n"
                    f"Max Gain:  ₹{max_gain}\n"
                    f"Brokerage: ₹40\n"
                    f"Net Gain:  ₹{net_gain}\n\n"
                    f"📊 RSI: {rsi} | Vol: {vol_ratio}x\n"
                    f"📈 Trend: {'UP ✅' if trending_up else 'DOWN ❌'}\n"
                    f"📉 EMA20: ₹{ema20} | EMA50: ₹{ema50}\n"
                    f"⚡ ATR: ₹{atr_val}\n\n"
                    f"📅 Hold: 5-15 days\n"
                    f"💡 SL = 2x ATR | Target = 4x ATR"
                )
                send_telegram(msg)

                now = datetime.utcnow()
                log_to_sheets({
                    "date":      now.strftime("%d-%b-%Y"),
                    "time":      now.strftime("%H:%M"),
                    "ticker":    ticker,
                    "signal":    f"SWING {signal}",
                    "entry":     entry,
                    "sl":        sl,
                    "target":    target,
                    "shares":    shares,
                    "cost":      cost,
                    "max_loss":  max_loss,
                    "max_gain":  max_gain,
                    "rsi":       rsi,
                    "vol_ratio": vol_ratio
                })

                active_trades[ticker] = {
                    'signal': signal,
                    'entry':  entry,
                    'sl':     sl,
                    'target': target,
                    'shares': shares,
                    'days':   0
                }

        return jsonify({
            "ticker":      ticker,
            "price":       round(price, 2),
            "ema20":       ema20,
            "ema50":       ema50,
            "rsi":         rsi,
            "atr":         atr_val,
            "volume":      volume,
            "vol_avg":     vol_avg,
            "vol_ratio":   vol_ratio,
            "trending_up": trending_up,
            "signal":      signal,
            "score":       bull_score if ema_bullish else bear_score,
            "entry":       entry,
            "sl":          sl,
            "target":      target,
            "hold_days":   "5-15 days",
            "history":     [round(float(x), 2) for x in df['Close'].tail(20).tolist()]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/backtest/<ticker>")
def backtest(ticker):
    try:
        period = request.args.get("period", "3mo")
        df = yf.download(ticker, period=period, interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) < 30:
            return jsonify({"error": "Not enough data"}), 404
        return jsonify({
            "dates":   [str(d)[:10] for d in df.index.tolist()],
            "opens":   [round(float(x), 2) for x in df['Open'].tolist()],
            "highs":   [round(float(x), 2) for x in df['High'].tolist()],
            "lows":    [round(float(x), 2) for x in df['Low'].tolist()],
            "closes":  [round(float(x), 2) for x in df['Close'].tolist()],
            "volumes": [int(x) for x in df['Volume'].tolist()],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ping")
def ping():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
