from flask import Flask, jsonify
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import os

app = Flask(__name__)
CORS(app)

def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window=period).mean()
    loss = -delta.clip(upper=0).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def compute_atr(df, period=14):
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def load_watchlist():
    try:
        with open("watchlist.txt", "r") as f:
            return [
                line.strip() for line in f
                if line.strip() and not line.startswith('#')
            ]
    except:
        return ["PNB.NS", "COALINDIA.NS", "BHEL.NS"]

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

        price     = float(last['Close'])
        ema20     = float(last['EMA20'])
        ema50     = float(last['EMA50'])
        rsi       = round(float(last['RSI']), 2)
        atr       = float(last['ATR'])
        volume    = int(last['Volume'])
        vol_avg   = int(last['VOL_AVG'])
        vol_ratio = round(volume / vol_avg, 2) if vol_avg > 0 else 0

        if price > 200:
            return jsonify({"error": f"Price over ₹200"}), 400

        ema_bullish  = float(prev['EMA20']) <= float(prev['EMA50']) and ema20 > ema50
        ema_bearish  = float(prev['EMA20']) >= float(prev['EMA50']) and ema20 < ema50
        vol_confirm  = vol_ratio >= 1.5
        rsi_bull_ok  = 45 <= rsi <= 65
        rsi_bear_ok  = 35 <= rsi <= 55
        trending_up  = price > ema20 > ema50
        trending_dn  = price < ema20 < ema50

        bull_score = sum([vol_confirm, rsi_bull_ok, trending_up])
        bear_score = sum([vol_confirm, rsi_bear_ok, trending_dn])

        signal = None
        if ema_bullish and vol_confirm and rsi_bull_ok and trending_up:
            signal = "BULLISH"
        elif ema_bearish and vol_confirm and rsi_bear_ok and trending_dn:
            signal = "BEARISH"
        elif ema_bullish and bull_score >= 2:
            signal = "WEAK_BULLISH"
        elif ema_bearish and bear_score >= 2:
            signal = "WEAK_BEARISH"

        sl_points     = round(atr * 2.0, 2)
        target_points = round(atr * 4.0, 2)

        if signal in ("BULLISH", "WEAK_BULLISH"):
            entry  = round(price, 2)
            sl     = round(price - sl_points, 2)
            target = round(price + target_points, 2)
        elif signal in ("BEARISH", "WEAK_BEARISH"):
            entry  = round(price, 2)
            sl     = round(price + sl_points, 2)
            target = round(price - target_points, 2)
        else:
            entry = sl = target = None

        return jsonify({
            "ticker":      ticker,
            "price":       round(price, 2),
            "ema20":       round(ema20, 2),
            "ema50":       round(ema50, 2),
            "rsi":         rsi,
            "atr":         round(atr, 2),
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
            "history":     [round(float(x), 2) for x in df['Close'].tail(30).tolist()]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ping")
def ping():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)