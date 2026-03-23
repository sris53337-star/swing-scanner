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
SHEETS_URL       = "https://script.google.com/macros/s/AKfycbxAIhJDoCRwhZ0f9_cSDikU9bmr7nR2ja5q5SfBendSNhlx99G4ngUG5EIe3ahjH7gUIQ/exec"

CAPITAL          = 5000
RISK_PCT         = 5
BROKERAGE        = 40
MIN_CONFLUENCE   = 5
COOLDOWN_DAYS    = 3

sent_signals  = {}
active_trades = {}
_signal_times = {}
eod_sent_date = ""

TRADES_FILE       = "swing_trades.json"
SIGNAL_TIMES_FILE = "swing_signal_times.json"

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

def save_signal_times():
    try:
        with open(SIGNAL_TIMES_FILE, "w") as f:
            json.dump(_signal_times, f)
    except:
        pass

def load_signal_times():
    try:
        with open(SIGNAL_TIMES_FILE, "r") as f:
            data = json.load(f)
            _signal_times.update(data)
            print(f"Restored {len(data)} swing signal times")
    except:
        pass

load_trades()
load_signal_times()

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

def compute_macd_hist(series):
    ema12  = series.ewm(span=12, adjust=False).mean()
    ema26  = series.ewm(span=26, adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd - signal

def compute_adx(df, period=14):
    try:
        high     = df['High']
        low      = df['Low']
        plus_dm  = high.diff()
        minus_dm = -low.diff()
        plus_dm[plus_dm < 0]   = 0
        minus_dm[minus_dm < 0] = 0
        tr       = compute_atr(df, period)
        plus_di  = 100 * (plus_dm.ewm(span=period).mean() / tr)
        minus_di = 100 * (minus_dm.ewm(span=period).mean() / tr)
        dx       = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
        return dx.ewm(span=period).mean()
    except:
        return pd.Series([0] * len(df), index=df.index)

def compute_supertrend(df, period=10, multiplier=3.0):
    try:
        hl2        = (df['High'] + df['Low']) / 2
        atr        = compute_atr(df, period)
        upper      = hl2 + multiplier * atr
        lower      = hl2 - multiplier * atr
        supertrend = [True] * len(df)
        upper_band = upper.copy()
        lower_band = lower.copy()

        for i in range(1, len(df)):
            if df['Close'].iloc[i-1] <= upper_band.iloc[i-1]:
                upper_band.iloc[i] = min(upper.iloc[i], upper_band.iloc[i-1])
            else:
                upper_band.iloc[i] = upper.iloc[i]
            if df['Close'].iloc[i-1] >= lower_band.iloc[i-1]:
                lower_band.iloc[i] = max(lower.iloc[i], lower_band.iloc[i-1])
            else:
                lower_band.iloc[i] = lower.iloc[i]
            if supertrend[i-1] and df['Close'].iloc[i] < lower_band.iloc[i]:
                supertrend[i] = False
            elif not supertrend[i-1] and df['Close'].iloc[i] > upper_band.iloc[i]:
                supertrend[i] = True
            else:
                supertrend[i] = supertrend[i-1]

        return pd.Series(supertrend, index=df.index)
    except:
        return pd.Series([True] * len(df), index=df.index)

def get_weekly_trend(ticker):
    try:
        df = yf.download(ticker, period="6mo", interval="1wk", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) >= 10:
            ema10 = compute_ema(df['Close'], 10)
            ema20 = compute_ema(df['Close'], 20)
            if float(ema10.iloc[-1]) > float(ema20.iloc[-1]):
                return "BULLISH"
            elif float(ema10.iloc[-1]) < float(ema20.iloc[-1]):
                return "BEARISH"
        del df
        return "NEUTRAL"
    except:
        return "NEUTRAL"

def load_watchlist():
    try:
        with open("watchlist.txt", "r") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith('#')]
    except:
        return ["PNB.NS", "BHEL.NS", "COALINDIA.NS"]

def monitor_trades():
    global eod_sent_date
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
                save_signal_times()

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
                        msg   = (
                            f"{emoji} <b>SWING {result}</b>\n"
                            f"<b>{ticker.replace('.NS','')}</b>\n\n"
                            f"Entry:     Rs.{entry}\n"
                            f"Exit:      Rs.{exit_price}\n"
                            f"Shares:    {shares}\n"
                            f"Days held: {days}\n\n"
                            f"Net P&L: <b>Rs.{pnl}</b>\n"
                            f"(incl. Rs.{BROKERAGE} brokerage)"
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
            f"🟢 Bullish: {len(bullish)} | 🔴 Bearish: {len(bearish)}\n"
            f"📈 Total: {total} | 🔍 Scanned: {len(watchlist)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )
        if bullish:
            msg += "🟢 " + ", ".join([t.replace('.NS','') for t in bullish]) + "\n"
        if bearish:
            msg += "🔴 " + ", ".join([t.replace('.NS','') for t in bearish]) + "\n"
        if total == 0:
            msg += "😴 No swing signals today\n"
        msg += "📅 Hold 5-15 days for targets!"
        send_telegram(msg)
    except:
        pass

def delayed_start():
    time.sleep(5)
    monitor_thread = threading.Thread(target=monitor_trades, daemon=True)
    monitor_thread.start()

starter = threading.Thread(target=delayed_start, daemon=True)
starter.start()

@app.route("/scan/<ticker>")
def scan(ticker):
    df    = None
    df_wk = None
    try:
        df = yf.download(ticker, period="1y", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[['Open','High','Low','Close','Volume']].dropna()

        if len(df) < 60:
            return jsonify({"error": "Not enough data"}), 404

        df['EMA20']   = compute_ema(df['Close'], 20)
        df['EMA50']   = compute_ema(df['Close'], 50)
        df['RSI']     = compute_rsi(df['Close'], 14)
        df['ATR']     = compute_atr(df, 14)
        df['ADX']     = compute_adx(df, 14)
        df['MACD_H']  = compute_macd_hist(df['Close'])
        df['VOL_AVG'] = df['Volume'].rolling(window=20).mean()
        df['ST_BULL'] = compute_supertrend(df, period=10, multiplier=3.0)
        df = df.dropna()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        price       = float(last['Close'])
        ema20       = round(float(last['EMA20']), 2)
        ema50       = round(float(last['EMA50']), 2)
        rsi         = round(float(last['RSI']), 2)
        atr_val     = round(float(last['ATR']), 2)
        adx_val     = round(float(last['ADX']), 2)
        macd_hist   = round(float(last['MACD_H']), 4)
        volume      = int(last['Volume'])
        vol_avg     = int(last['VOL_AVG']) if last['VOL_AVG'] > 0 else 1
        vol_ratio   = round(volume / vol_avg, 2)
        trending_up = price > ema20 > ema50
        st_bullish  = bool(last['ST_BULL'])
        history     = [round(float(x), 2) for x in df['Close'].tail(20).tolist()]

        ema_bullish = float(prev['EMA20']) <= float(prev['EMA50']) and ema20 > ema50
        ema_bearish = float(prev['EMA20']) >= float(prev['EMA50']) and ema20 < ema50

        del df
        df = None

        weekly_trend = get_weekly_trend(ticker)

        if ema_bullish:
            direction = "BULLISH"
        elif ema_bearish:
            direction = "BEARISH"
        else:
            return jsonify({"ticker": ticker, "price": round(price,2), "signal": None,
                            "score": 0, "message": "No EMA crossover"})

        scores = {}
        scores['ema_cross']    = True
        scores['volume']       = vol_ratio >= 1.5
        scores['rsi']          = (direction == "BULLISH" and 47 <= rsi <= 63) or (direction == "BEARISH" and 40 <= rsi <= 58)
        scores['trend']        = (direction == "BULLISH" and trending_up) or (direction == "BEARISH" and not trending_up)
        scores['supertrend']   = st_bullish if direction == "BULLISH" else not st_bullish
        scores['macd_hist']    = (macd_hist > 0) if direction == "BULLISH" else (macd_hist < 0)
        scores['adx']          = adx_val >= 25
        scores['weekly_trend'] = (direction == weekly_trend or weekly_trend == "NEUTRAL")

        total_score = sum(scores.values())

        print(f"SWING {ticker} | {direction} | score={total_score}/8 | "
              f"ema={scores['ema_cross']} vol={scores['volume']}({vol_ratio}x) "
              f"rsi={scores['rsi']}({rsi}) trend={scores['trend']} "
              f"st={scores['supertrend']} macd={scores['macd_hist']}({macd_hist}) "
              f"adx={scores['adx']}({adx_val}) weekly={scores['weekly_trend']}({weekly_trend})")

        if total_score < MIN_CONFLUENCE:
            return jsonify({"ticker": ticker, "price": round(price,2), "signal": direction,
                            "score": total_score, "message": f"Score {total_score}/8 below minimum {MIN_CONFLUENCE}"})

        entry  = round(price, 2)
        sl     = round(price - atr_val * 2, 2) if direction == "BULLISH" else round(price + atr_val * 2, 2)
        target = round(price + atr_val * 4, 2) if direction == "BULLISH" else round(price - atr_val * 4, 2)

        if total_score >= 7:
            trade_capital = CAPITAL
            signal_grade  = "STRONG"
            grade_emoji   = "✅ STRONG SIGNAL"
        elif total_score >= 6:
            trade_capital = CAPITAL * 0.5
            signal_grade  = "MODERATE"
            grade_emoji   = "⚠️ MODERATE SIGNAL"
        else:
            trade_capital = CAPITAL * 0.25
            signal_grade  = "WEAK"
            grade_emoji   = "👀 WEAK SIGNAL"

        risk_amount    = trade_capital * (RISK_PCT / 100)
        risk_per_share = abs(entry - sl)
        if risk_per_share == 0:
            return jsonify({"error": "Zero risk per share"}), 400

        shares = min(int(risk_amount / risk_per_share), int(trade_capital / entry))
        if shares <= 0:
            shares = 1

        cost     = round(shares * entry, 2)
        max_loss = round(shares * risk_per_share + BROKERAGE, 2)
        max_gain = round(shares * abs(target - entry) - BROKERAGE, 2)

        signal_key  = f"{ticker}_{direction}"
        now_ts      = time.time()
        last_fired  = _signal_times.get(signal_key, 0)
        cooldown_ok = (now_ts - last_fired) > (COOLDOWN_DAYS * 86400)

        if signal_key not in sent_signals and cooldown_ok:
            _signal_times[signal_key] = now_ts
            save_signal_times()
            sent_signals[signal_key] = {'signal': direction, 'score': total_score}

            tv_symbol  = ticker.replace('.NS', '')
            dir_arrow  = "BUY"  if direction == "BULLISH" else "SELL"
            dir_emoji  = "🟢"  if direction == "BULLISH" else "🔴"

            conf_lines = (
                f"{'✅' if scores['ema_cross']    else '❌'} EMA 20/50 Cross\n"
                f"{'✅' if scores['volume']       else '❌'} Volume {vol_ratio}x\n"
                f"{'✅' if scores['rsi']          else '❌'} RSI {rsi}\n"
                f"{'✅' if scores['trend']        else '❌'} Trend Aligned\n"
                f"{'✅' if scores['supertrend']   else '❌'} Supertrend {'BULL' if st_bullish else 'BEAR'}\n"
                f"{'✅' if scores['macd_hist']    else '❌'} MACD Hist {macd_hist}\n"
                f"{'✅' if scores['adx']          else '❌'} ADX {adx_val}\n"
                f"{'✅' if scores['weekly_trend'] else '❌'} Weekly {weekly_trend}"
            )

            msg = (
                f"📈 <b>SWING SCANNER</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"{dir_emoji} <b>SWING {direction}</b>\n"
                f"<b>{tv_symbol}</b> @ Rs.{round(price, 2)}\n\n"
                f"<b>CONFLUENCES ({total_score}/8):</b>\n"
                f"<code>{conf_lines}</code>\n\n"
                f"<b>{grade_emoji}</b>\n\n"
                f"Entry:  Rs.{entry}\n"
                f"SL:     Rs.{sl}\n"
                f"Target: Rs.{target}\n\n"
                f"<b>POSITION ({signal_grade}):</b>\n"
                f"Capital:   Rs.{trade_capital}\n"
                f"Action:    {dir_arrow} {shares} shares\n"
                f"Cost:      Rs.{cost}\n"
                f"Risk:      Rs.{max_loss}\n"
                f"Max Gain:  Rs.{max_gain}\n"
                f"Brokerage: Rs.{BROKERAGE}\n\n"
                f"ATR: Rs.{atr_val} | RR: 1:2\n\n"
                f"<a href='https://www.tradingview.com/chart/?symbol=NSE:{tv_symbol}'>📊 View on TradingView</a>\n\n"
                f"📅 Hold: 5-15 days | SL=2x ATR | Target=4x ATR"
            )
            send_telegram(msg)

            now_utc = datetime.utcnow()
            log_to_sheets({
                "date":      now_utc.strftime("%d-%b-%Y"),
                "time":      now_utc.strftime("%H:%M"),
                "ticker":    ticker,
                "signal":    f"SWING {direction}",
                "score":     total_score,
                "grade":     signal_grade,
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
                'signal': direction,
                'entry':  entry,
                'sl':     sl,
                'target': target,
                'shares': shares,
                'days':   0
            }
            save_trades()

        return jsonify({
            "ticker":       ticker,
            "price":        round(price, 2),
            "signal":       direction,
            "score":        total_score,
            "grade":        signal_grade,
            "ema20":        ema20,
            "ema50":        ema50,
            "rsi":          rsi,
            "atr":          atr_val,
            "adx":          adx_val,
            "vol_ratio":    vol_ratio,
            "trending_up":  trending_up,
            "supertrend":   st_bullish,
            "weekly_trend": weekly_trend,
            "entry":        entry,
            "sl":           sl,
            "target":       target,
            "history":      history
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if df is not None:
            del df
        gc.collect()

@app.route("/watchlist")
def get_watchlist():
    return jsonify(load_watchlist())

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

_scan_running = False

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
                print(f"Swing scan START: {len(watchlist)} stocks | IST {ist_hour:02d}:{ist_min:02d}")
                for ticker in watchlist:
                    try:
                        with app.test_request_context():
                            scan(ticker)
                    except Exception as e:
                        print(f"Swing scan error {ticker}: {e}")
                    time.sleep(3)
                gc.collect()
                _scan_running = False
                print("Swing scan DONE")
            else:
                print(f"Swing scan: market closed | IST {ist_hour:02d}:{ist_min:02d}")
        except Exception as e:
            _scan_running = False
            print(f"Swing scan loop error: {e}")
        time.sleep(600)

scan_thread = threading.Thread(target=auto_scan_loop, daemon=True)
scan_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, threaded=True)
