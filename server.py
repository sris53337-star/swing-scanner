from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import requests as req
import os
import threading
import time
import gc
import json
from datetime import datetime

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=False)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SHEETS_URL       = "https://script.google.com/macros/s/AKfycbzu0aDb-n4re6qw_RtkkAYA-EbdhQcTnS9DoDd4wxhb4DTMKE89SUFxqtoeAa2mBx_V/exec"
CAPITAL          = 5000
RISK_PCT         = 2
BROKERAGE        = 40
MIN_GAIN         = 50
MIN_VOL          = 0.5
COOLDOWN_DAYS    = 3

sent_signals  = {}
active_trades = {}
_signal_times = {}
TRADES_FILE   = "swing_trades.json"
_scan_running = False

def save_trades():
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(active_trades, f)
    except:
        pass

def load_trades():
    try:
        with open(TRADES_FILE, "r") as f:
            data = json.load(f)
            active_trades.update(data)
            print(f"Restored {len(data)} swing trades")
    except:
        pass

load_trades()

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req.post(url, json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     message,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True
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
    tr = high_low.combine(high_close, max).combine(low_close, max)
    return tr.rolling(window=period).mean()

def calculate_position_size(entry, sl):
    risk_amount    = CAPITAL * (RISK_PCT / 100)
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0 or entry == 0:
        return 0, 0, 0, 0
    shares = min(int(risk_amount / risk_per_share), int(CAPITAL / entry))
    if shares <= 0:
        return 0, 0, 0, 0
    cost     = round(shares * entry, 2)
    max_loss = round(shares * risk_per_share, 2)
    max_gain = round(max_loss * 2, 2)
    return shares, cost, max_loss, max_gain

def load_watchlist():
    try:
        with open("watchlist.txt", "r") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith('#')]
    except:
        return ["PNB.NS", "BHEL.NS", "COALINDIA.NS"]

def monitor_trades():
    global eod_sent_date
    eod_sent_date = ""
    while True:
        try:
            now        = datetime.utcnow()
            ist_mins   = now.hour * 60 + now.minute + 330
            ist_hour   = (ist_mins // 60) % 24
            ist_minute = ist_mins % 60
            today      = now.strftime("%d-%b-%Y")

            if ist_hour == 15 and ist_minute >= 35 and eod_sent_date != today:
                send_eod_summary()
                eod_sent_date = today

            if ist_hour == 0 and ist_minute < 5:
                sent_signals.clear()
                _signal_times.clear()
                save_trades()

            for ticker in list(active_trades.keys()):
                trade = active_trades[ticker]
                df    = None
                try:
                    df = yf.download(ticker, period="5d", interval="1d", progress=False)
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    if df.empty:
                        continue

                    current_price = float(df['Close'].iloc[-1])
                    del df
                    df = None

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
                            result, exit_price = 'TARGET HIT', target
                        elif current_price <= sl:
                            result, exit_price = 'SL HIT', sl
                    else:
                        if current_price <= target:
                            result, exit_price = 'TARGET HIT', target
                        elif current_price >= sl:
                            result, exit_price = 'SL HIT', sl

                    if days >= 15 and not result:
                        result, exit_price = 'MAX DAYS EXIT', current_price

                    active_trades[ticker]['days'] = days

                    if result:
                        pnl   = round(shares * (exit_price - entry) *
                                      (1 if signal == 'BULLISH' else -1) - BROKERAGE, 2)
                        emoji = "🎯" if "TARGET" in result else "🛑" if "SL" in result else "📤"

                        msg = (
                            f"{emoji} <b>SWING {result}</b>\n"
                            f"📌 <b>{ticker.replace('.NS','')}</b>\n\n"
                            f"Entry:     ₹{entry}\n"
                            f"Exit:      ₹{exit_price}\n"
                            f"Shares:    {shares}\n"
                            f"Days held: {days}\n\n"
                            f"💰 Net P&L: <b>₹{pnl}</b>\n"
                            f"(after ₹{BROKERAGE} brokerage)"
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
                        save_trades()
                except:
                    pass
                finally:
                    if df is not None:
                        del df

            gc.collect()

        except:
            pass
        time.sleep(3600)

def send_eod_summary():
    try:
        watchlist = load_watchlist()
        bullish   = [t for t, d in sent_signals.items() if d.get('signal') == 'BULLISH']
        bearish   = [t for t, d in sent_signals.items() if d.get('signal') == 'BEARISH']
        total     = len(sent_signals)
        msg = (
            f"📊 <b>SWING EOD SUMMARY</b> — {datetime.utcnow().strftime('%d %b %Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 Bullish: {len(bullish)} | ⚠️ Bearish: {len(bearish)}\n"
            f"📈 Total: {total} | 🔍 Scanned: {len(watchlist)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )
        if bullish:
            msg += "🚀 " + ", ".join([t.replace('.NS','') for t in bullish]) + "\n"
        if bearish:
            msg += "⚠️ "  + ", ".join([t.replace('.NS','') for t in bearish]) + "\n"
        if total == 0:
            msg += "😴 No swing signals today\n"
        msg += "📅 Hold 5-15 days for targets!"
        send_telegram(msg)
    except:
        pass

monitor_thread = threading.Thread(target=monitor_trades, daemon=True)
monitor_thread.start()

def auto_scan_loop():
    global _scan_running
    time.sleep(180)
    print("Swing auto-scan started.")
    while True:
        try:
            now      = datetime.utcnow()
            ist_mins = now.hour * 60 + now.minute + 330
            ist_hour = (ist_mins // 60) % 24
            ist_min  = ist_mins % 60

            market_open  = (ist_hour > 9) or (ist_hour == 9 and ist_min >= 30)
            market_close = (ist_hour > 15) or (ist_hour == 15 and ist_min >= 15)
            in_market    = market_open and not market_close

            if in_market and not _scan_running:
                _scan_running = True
                watchlist = load_watchlist()
                print(f"Swing scan: {len(watchlist)} stocks | IST {ist_hour:02d}:{ist_min:02d}")
                for ticker in watchlist:
                    try:
                        with app.test_request_context():
                            scan(ticker)
                    except Exception as e:
                        print(f"Swing scan error {ticker}: {e}")
                    time.sleep(3)
                gc.collect()
                _scan_running = False
            else:
                print(f"Swing scan: market closed | IST {ist_hour:02d}:{ist_min:02d}")
        except Exception as e:
            _scan_running = False
            print(f"Swing scan loop error: {e}")
        time.sleep(600)

@app.route("/watchlist")
def get_watchlist():
    return jsonify(load_watchlist())

@app.route("/scan/<ticker>")
def scan(ticker):
    df = None
    try:
        df = yf.download(ticker, period="6mo", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[['Open','High','Low','Close','Volume']].dropna()

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
        history     = [round(float(x), 2) for x in df['Close'].tail(20).tolist()]

        del df
        df = None

        ema_bullish = float(prev['EMA20']) <= float(prev['EMA50']) and ema20 > ema50
        ema_bearish = float(prev['EMA20']) >= float(prev['EMA50']) and ema20 < ema50
        vol_ok      = vol_ratio >= 1.5
        rsi_bull_ok = 47 <= rsi <= 63
        rsi_bear_ok = 40 <= rsi <= 58

        bull_score = sum([ema_bullish, vol_ok, trending_up,     rsi_bull_ok])
        bear_score = sum([ema_bearish, vol_ok, not trending_up, rsi_bear_ok])

        signal = None
        if ema_bullish and vol_ok and trending_up and rsi_bull_ok:
            signal = "BULLISH"
        elif ema_bearish and vol_ok and not trending_up and rsi_bear_ok:
            signal = "BEARISH"

        if signal in ("BULLISH", "BEARISH"):
            entry  = round(price, 2)
            sl     = round(price - atr_val * 2, 2) if signal == "BULLISH" else round(price + atr_val * 2, 2)
            target = round(price + atr_val * 3, 2) if signal == "BULLISH" else round(price - atr_val * 3, 2)
        else:
            entry = sl = target = None

        if signal in ("BULLISH", "BEARISH") and entry and sl:
            if vol_ratio < MIN_VOL:
                return jsonify({"ticker": ticker, "price": round(price,2), "signal": signal,
                                "message": f"Volume {vol_ratio}x too low"})

            shares, cost, max_loss, max_gain = calculate_position_size(entry, sl)
            net_gain = round(max_gain - BROKERAGE, 2)

            if net_gain < MIN_GAIN:
                return jsonify({"ticker": ticker, "price": round(price,2), "signal": signal,
                                "message": f"Net gain ₹{net_gain} below minimum ₹{MIN_GAIN}"})

            signal_key = f"{ticker}_{signal}"
            now_ts     = time.time()
            last_fired = _signal_times.get(signal_key, 0)
            cooldown_ok = (now_ts - last_fired) > (COOLDOWN_DAYS * 86400)

            if signal_key not in sent_signals and cooldown_ok:
                _signal_times[signal_key] = now_ts
                sent_signals[signal_key]  = {'signal': signal}

                direction = "BUY" if signal == "BULLISH" else "SELL"
                emoji     = "🚀" if signal == "BULLISH" else "⚠️"
                tv_symbol = ticker.replace('.NS', '')

                msg = (
                    f"{emoji} <b>SWING {signal}</b>\n"
                    f"📌 <b>{tv_symbol}</b> @ ₹{round(price, 2)}\n\n"
                    f"✅ Entry:  ₹{entry}\n"
                    f"🛑 SL:     ₹{sl}\n"
                    f"🎯 Target: ₹{target}\n\n"
                    f"💰 <b>POSITION SIZE:</b>\n"
                    f"Action:    <b>{direction} {shares} shares</b>\n"
                    f"Cost:      ₹{cost}\n"
                    f"Max Loss:  ₹{max_loss}\n"
                    f"Max Gain:  ₹{max_gain}\n"
                    f"Brokerage: ₹{BROKERAGE}\n"
                    f"Net Gain:  ₹{net_gain}\n\n"
                    f"📊 RSI: {rsi} | Vol: {vol_ratio}x\n"
                    f"📈 Trend: {'UP ✅' if trending_up else 'DOWN ❌'}\n"
                    f"📉 EMA20: ₹{ema20} | EMA50: ₹{ema50}\n"
                    f"⚡ ATR: ₹{atr_val}\n\n"
                    f"📊 <a href='https://www.tradingview.com/chart/?symbol=NSE:{tv_symbol}'>View on TradingView</a>\n\n"
                    f"📅 Hold: 5-15 days\n"
                    f"💡 SL = 2x ATR | Target = 3x ATR"
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
                save_trades()

        return jsonify({
            "ticker":      ticker,
            "price":       round(price, 2),
            "ema20":       ema20,
            "ema50":       ema50,
            "rsi":         rsi,
            "atr":         atr_val,
            "vol_ratio":   vol_ratio,
            "trending_up": trending_up,
            "signal":      signal,
            "score":       bull_score if ema_bullish else bear_score,
            "entry":       entry,
            "sl":          sl,
            "target":      target,
            "history":     history
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if df is not None:
            del df
        gc.collect()

@app.route("/backtest/<ticker>")
def backtest(ticker):
    df = None
    try:
        period = request.args.get("period", "3mo")
        df = yf.download(ticker, period=period, interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) < 30:
            return jsonify({"error": "Not enough data"}), 404
        result = {
            "dates":   [str(d)[:10] for d in df.index.tolist()],
            "opens":   [round(float(x), 2) for x in df['Open'].tolist()],
            "highs":   [round(float(x), 2) for x in df['High'].tolist()],
            "lows":    [round(float(x), 2) for x in df['Low'].tolist()],
            "closes":  [round(float(x), 2) for x in df['Close'].tolist()],
            "volumes": [int(x) for x in df['Volume'].tolist()],
        }
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if df is not None:
            del df
        gc.collect()

@app.route("/ping")
def ping():
    return "ok"

scan_thread = threading.Thread(target=auto_scan_loop, daemon=True)
scan_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
