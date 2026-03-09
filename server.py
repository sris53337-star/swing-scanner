from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import requests as req
import os
import threading
import time
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
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

def send_telegram_photo(image_bytes, caption=""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        req.post(url, data={
            "chat_id":    TELEGRAM_CHAT_ID,
            "caption":    caption,
            "parse_mode": "HTML"
        }, files={"photo": ("chart.png", image_bytes, "image/png")}, timeout=15)
    except:
        pass

def send_telegram_album(images, caption=""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import json
        url   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMediaGroup"
        files = {}
        media = []
        for i, img_bytes in enumerate(images):
            key = f"photo{i}"
            files[key] = (f"chart{i}.png", img_bytes, "image/png")
            item = {"type": "photo", "media": f"attach://{key}"}
            if i == 0:
                item["caption"]    = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        req.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "media":   json.dumps(media)
        }, files=files, timeout=20)
    except Exception as e:
        print(f"Album error: {e}")

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

# ── Chart Generator ───────────────────────────────────────────────────────────
def generate_chart(ticker, signal, entry, sl, target,
                   interval="1d", period="3mo", title="DAILY",
                   ema_fast=20, ema_slow=50):
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) < 10:
            return None

        df = df.tail(60)
        df['EMA_FAST'] = compute_ema(df['Close'], ema_fast)
        df['EMA_SLOW'] = compute_ema(df['Close'], ema_slow)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6),
                                        gridspec_kw={'height_ratios': [3, 1]},
                                        facecolor='#0a0a0a')
        ax1.set_facecolor('#0d0d0d')
        ax2.set_facecolor('#0d0d0d')

        # Candles
        for i, (idx, row) in enumerate(df.iterrows()):
            o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
            color = '#00ff88' if c >= o else '#ff4455'
            ax1.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.7)
            ax1.bar(i, abs(c - o), bottom=min(c, o),
                    color=color, alpha=0.85, width=0.6)

        # EMA lines
        ax1.plot(range(len(df)), df['EMA_FAST'].values,
                 color='#58a6ff', linewidth=1.8,
                 label=f'EMA{ema_fast}', alpha=0.9)
        ax1.plot(range(len(df)), df['EMA_SLOW'].values,
                 color='#ff8800', linewidth=1.8,
                 label=f'EMA{ema_slow}', alpha=0.9)

        # Entry / SL / Target lines
        ax1.axhline(y=entry,  color='#58a6ff', linestyle='--', linewidth=1.2, alpha=0.8)
        ax1.axhline(y=sl,     color='#ff4455', linestyle='--', linewidth=1.2, alpha=0.8)
        ax1.axhline(y=target, color='#00ff88', linestyle='--', linewidth=1.2, alpha=0.8)

        ax1.text(len(df)-1, target, f' TGT ₹{target}', color='#00ff88',
                 fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, entry,  f' ENT ₹{entry}',  color='#58a6ff',
                 fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, sl,     f' SL  ₹{sl}',     color='#ff4455',
                 fontsize=7, va='top',    fontfamily='monospace')

        # Volume
        for i, (idx, row) in enumerate(df.iterrows()):
            color = '#00ff8840' if row['Close'] >= row['Open'] else '#ff445540'
            ax2.bar(i, row['Volume'], color=color, width=0.6)

        # Styling
        sig_color = '#00ff88' if signal == 'BULLISH' else '#ff4455'
        sig_emoji = '🚀' if signal == 'BULLISH' else '⚠️'
        ax1.set_title(
            f'{sig_emoji} {ticker.replace(".NS","")} — SWING {signal} | {title} CHART',
            color=sig_color, fontsize=11, fontfamily='monospace',
            fontweight='bold', pad=8
        )

        for ax in [ax1, ax2]:
            ax.tick_params(colors='#444', labelsize=7)
            for spine in ax.spines.values():
                spine.set_color('#222')

        ax1.tick_params(axis='x', labelbottom=False)
        ax1.set_ylabel('Price (₹)', color='#444', fontsize=8)
        ax2.set_ylabel('Volume',    color='#444', fontsize=8)

        step = max(1, len(df)//6)
        ax2.set_xticks(range(0, len(df), step))
        fmt = '%d %b' if interval in ('1d', '1wk') else '%d %b %H:%M'
        ax2.set_xticklabels(
            [df.index[i].strftime(fmt) for i in range(0, len(df), step)],
            rotation=20, fontsize=6, color='#444'
        )
        ax1.legend(loc='upper left', facecolor='#111', edgecolor='#222',
                   labelcolor='white', fontsize=8)

        plt.tight_layout(pad=1.0)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120,
                    facecolor='#0a0a0a', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return buf.read()

    except Exception as e:
        print(f"Chart error {title}: {e}")
        return None

def generate_all_charts(ticker, signal, entry, sl, target):
    """Generate 3 swing charts: 1HR, DAILY, WEEKLY"""
    charts = []

    # Chart 1: 1 Hour — entry timing
    c1 = generate_chart(ticker, signal, entry, sl, target,
                        interval="1h", period="1mo",
                        title="1 HOUR", ema_fast=9, ema_slow=21)
    if c1: charts.append(c1)

    # Chart 2: Daily — main signal
    c2 = generate_chart(ticker, signal, entry, sl, target,
                        interval="1d", period="3mo",
                        title="DAILY", ema_fast=20, ema_slow=50)
    if c2: charts.append(c2)

    # Chart 3: Weekly — big picture
    c3 = generate_chart(ticker, signal, entry, sl, target,
                        interval="1wk", period="2y",
                        title="WEEKLY", ema_fast=10, ema_slow=20)
    if c3: charts.append(c3)

    return charts

def generate_result_chart(ticker, signal, entry, sl, target,
                          exit_price, result, pnl, shares):
    try:
        df = yf.download(ticker, period="3mo", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna().tail(60)
        df['EMA20'] = compute_ema(df['Close'], 20)
        df['EMA50'] = compute_ema(df['Close'], 50)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6),
                                        gridspec_kw={'height_ratios': [3, 1]},
                                        facecolor='#0a0a0a')
        ax1.set_facecolor('#0d0d0d')
        ax2.set_facecolor('#0d0d0d')

        for i, (idx, row) in enumerate(df.iterrows()):
            o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
            color = '#00ff88' if c >= o else '#ff4455'
            ax1.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.7)
            ax1.bar(i, abs(c - o), bottom=min(c, o),
                    color=color, alpha=0.85, width=0.6)

        ax1.plot(range(len(df)), df['EMA20'].values,
                 color='#58a6ff', linewidth=1.8, label='EMA20')
        ax1.plot(range(len(df)), df['EMA50'].values,
                 color='#ff8800', linewidth=1.8, label='EMA50')

        # Shade profit/loss zone
        is_win = "TARGET" in result
        ax1.axhspan(min(entry, exit_price), max(entry, exit_price),
                    alpha=0.25, color='#00ff88' if is_win else '#ff4455')

        ax1.axhline(y=entry,      color='#58a6ff', linestyle='--', linewidth=1.5)
        ax1.axhline(y=sl,         color='#ff4455', linestyle='--', linewidth=1.0, alpha=0.6)
        ax1.axhline(y=target,     color='#00ff88', linestyle='--', linewidth=1.0, alpha=0.6)
        ax1.axhline(y=exit_price, color='#ffcc00', linestyle='-',  linewidth=2.0)

        ax1.text(len(df)-1, target,     f' TGT ₹{target}',     color='#00ff88', fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, entry,      f' ENT ₹{entry}',      color='#58a6ff', fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, sl,         f' SL  ₹{sl}',         color='#ff4455', fontsize=7, va='top',    fontfamily='monospace')
        ax1.text(len(df)-1, exit_price, f' EXIT ₹{exit_price}',color='#ffcc00', fontsize=8, va='bottom', fontfamily='monospace', fontweight='bold')

        pnl_color = '#00ff88' if pnl >= 0 else '#ff4455'
        pnl_text  = f"{'✅ PROFIT' if pnl >= 0 else '❌ LOSS'}  ₹{abs(pnl)}  ({shares} shares)"
        ax1.text(0.5, 0.97, pnl_text,
                 transform=ax1.transAxes,
                 color=pnl_color, fontsize=10, fontweight='bold',
                 ha='center', va='top', fontfamily='monospace',
                 bbox=dict(boxstyle='round,pad=0.3', facecolor='#111',
                           edgecolor=pnl_color, alpha=0.8))

        for i, (idx, row) in enumerate(df.iterrows()):
            color = '#00ff8840' if row['Close'] >= row['Open'] else '#ff445540'
            ax2.bar(i, row['Volume'], color=color, width=0.6)

        result_emoji = "🎯" if "TARGET" in result else "🛑" if "SL" in result else "📤"
        ax1.set_title(
            f'{result_emoji} SWING {ticker.replace(".NS","")} — {result} | P&L: ₹{pnl}',
            color=pnl_color, fontsize=11, fontfamily='monospace',
            fontweight='bold', pad=8
        )

        for ax in [ax1, ax2]:
            ax.tick_params(colors='#444', labelsize=7)
            for spine in ax.spines.values():
                spine.set_color('#222')

        ax1.tick_params(axis='x', labelbottom=False)
        ax1.set_ylabel('Price (₹)', color='#444', fontsize=8)
        ax2.set_ylabel('Volume',    color='#444', fontsize=8)

        step = max(1, len(df)//6)
        ax2.set_xticks(range(0, len(df), step))
        ax2.set_xticklabels(
            [df.index[i].strftime('%d %b') for i in range(0, len(df), step)],
            rotation=20, fontsize=6, color='#444'
        )
        ax1.legend(loc='upper left', facecolor='#111', edgecolor='#222',
                   labelcolor='white', fontsize=8)

        plt.tight_layout(pad=1.0)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120,
                    facecolor='#0a0a0a', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return buf.read()

    except Exception as e:
        print(f"Result chart error: {e}")
        return None

# ── Trade Monitor Thread ──────────────────────────────────────────────────────
def monitor_trades():
    global eod_sent
    while True:
        try:
            now = datetime.utcnow()
            ist_minutes = now.hour * 60 + now.minute + 330
            ist_hour    = (ist_minutes // 60) % 24
            ist_minute  = ist_minutes % 60

            if ist_hour == 15 and ist_minute >= 35 and not eod_sent:
                send_eod_summary()
                eod_sent = True

            if ist_hour == 0 and ist_minute < 5:
                eod_sent = False
                sent_signals.clear()
                active_trades.clear()

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

                    if days >= 15 and not result:
                        result = 'MAX DAYS EXIT 📤'
                        exit_price = current_price

                    active_trades[ticker]['days'] = days

                    if result:
                        pnl   = round(shares * (exit_price - entry) *
                                      (1 if signal == 'BULLISH' else -1) - 40, 2)
                        emoji = ("🎯" if "TARGET" in result
                                 else "🛑" if "SL" in result else "📤")

                        # Send result chart
                        result_chart = generate_result_chart(
                            ticker, signal, entry, sl, target,
                            exit_price, result, pnl, shares
                        )
                        if result_chart:
                            send_telegram_photo(result_chart,
                                caption=f"{emoji} SWING {ticker.replace('.NS','')} — {result} | P&L: ₹{pnl}")

                        msg = (
                            f"{emoji} <b>SWING {result}</b>\n"
                            f"📌 <b>{ticker}</b>\n\n"
                            f"Entry:     ₹{entry}\n"
                            f"Exit:      ₹{exit_price}\n"
                            f"Shares:    {shares}\n"
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
        elif trending_up and bull_score >= 3:
            signal = "WEAK_BULLISH"
        elif not trending_up and bear_score >= 3:
            signal = "WEAK_BEARISH"

        # 1:3 RR for swing
        if signal in ("BULLISH", "WEAK_BULLISH"):
            entry  = round(price, 2)
            sl     = round(price - atr_val * 2, 2)
            target = round(price + atr_val * 3, 2)
        elif signal in ("BEARISH", "WEAK_BEARISH"):
            entry  = round(price, 2)
            sl     = round(price + atr_val * 2, 2)
            target = round(price - atr_val * 3, 2)
        else:
            entry = sl = target = None

        if signal in ("BULLISH", "BEARISH") and entry and sl:
            signal_key = f"{ticker}_{signal}"

            if signal_key not in sent_signals:
                shares, cost, max_loss, max_gain = calculate_position_size(entry, sl)
                net_gain  = max_gain - 40
                direction = "BUY" if signal == "BULLISH" else "SELL"
                emoji     = "🚀" if signal == "BULLISH" else "⚠️"
                tv_symbol = ticker.replace('.NS', '')

                sent_signals[signal_key] = {'signal': signal}

                # Generate 3 swing charts
                charts = generate_all_charts(ticker, signal, entry, sl, target)

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
                    f"📊 <a href='https://www.tradingview.com/chart/?symbol=NSE:{tv_symbol}'>View on TradingView</a>\n\n"
                    f"📅 Hold: 5-15 days\n"
                    f"💡 SL = 2x ATR | Target = 3x ATR"
                )

                if charts:
                    send_telegram_album(charts,
                        caption=f"{emoji} SWING {ticker.replace('.NS','')} — {signal}")
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
