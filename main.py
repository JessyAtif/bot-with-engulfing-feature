from flask import Flask
from threading import Thread
import websocket
import json
import requests
import pandas as pd
import mplfinance as mpf
import time
import os
import warnings
import queue
from datetime import datetime

# 🔥 MEMORY LEAK FIX IMPORTS 🔥
import matplotlib
matplotlib.use('Agg')  # Force headless non-interactive backend
import matplotlib.pyplot as plt

# --- CONFIGURATION ---
warnings.filterwarnings("ignore")

# 🔥 TELEGRAM SETTINGS 🔥
TELEGRAM_BOT_TOKEN = '8590924453:AAGPbmBml5qICTOn6ZKe2shUAw8a52GNFEY'  
TELEGRAM_CHAT_ID = '@brainlifttrader'                 

# 🔥 STRATEGY SETTINGS 🔥
MIN_PRICE = 0.0000001
COOLDOWN_SECONDS = 60
VOL_REQ_15M = 1000000      # 1M Volume

# --- GLOBAL VARIABLES ---
processed_coins = {}
analysis_queue = queue.Queue()
is_first_run = True 

# --- FOLDER SETUP ---
BASE_FOLDER = os.path.join(os.getcwd(), "Scanner_Alerts")
FOLDER_15M = os.path.join(BASE_FOLDER, "Strategy_Charts")

if not os.path.exists(FOLDER_15M):
    try: os.makedirs(FOLDER_15M)
    except: pass

def log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")

# ==========================================
# 🟢 KEEPALIVE FLASK SERVER
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Atif Bhai Ka Bot Zinda Hai! 🚀"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# ==========================================
# 🟢 TELEGRAM FUNCTIONS
# ==========================================
def send_telegram_text(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}
        requests.post(url, data=data, timeout=10) 
    except: pass

def send_telegram_alert(image_path, caption):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(image_path, 'rb') as photo:
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
            files = {'photo': photo}
            response = requests.post(url, data=data, files=files, timeout=15)
            if response.status_code == 200:
                log(f"✅ Telegram Chart Alert Sent Successfully!")
            else:
                log(f"❌ Telegram Image Error: {response.text}")
    except Exception as e:
        log(f"❌ Error sending chart to Telegram: {e}")

# ==========================================
# 🟢 NEW SNIPER STRATEGY LOGIC
# ==========================================
def detect_sniper_signal(df):
    """
    Evaluates the strict trending and bouncing engulfing logic.
    Returns: 'BUY', 'SELL', or None
    """
    if len(df) < 20:
        return None
        
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    
    # --- FILTER 0: AVOID SIDEWAYS MARKET ---
    # 0.2% gap minimum
    gap = abs(curr['ema9'] - curr['ema20']) / curr['ema20']
    if gap <= 0.002:
        return None
        
    # Check last 10 candles for trend consistency
    last_10 = df.iloc[-10:]
    bullish_trend = all(row['ema9'] > row['ema20'] for _, row in last_10.iterrows())
    bearish_trend = all(row['ema9'] < row['ema20'] for _, row in last_10.iterrows())
    
    if not bullish_trend and not bearish_trend:
        return None

    # Identify candle colors and volume
    curr_is_green = curr['close'] > curr['open']
    curr_is_red = curr['close'] < curr['open']
    prev_is_green = prev['close'] > prev['open']
    prev_is_red = prev['close'] < prev['open']
    
    vol_higher = curr['volume'] > prev['volume']
    
    # --- BULLISH SIGNAL ---
    if bullish_trend:
        # 1. Bouncing from EMA 9 and 20
        bouncing = (curr['low'] <= curr['ema20'] * 1.005) and (curr['close'] >= curr['ema9'])
        
        # 2. Wick-to-wick bullish engulfing
        engulfing = (curr['high'] > prev['high']) and (curr['low'] < prev['low'])
        
        # 3. Volume & Color validation (Current Green > Prev Red)
        color_check = curr_is_green and prev_is_red
        
        if bouncing and engulfing and vol_higher and color_check:
            return "BUY"

    # --- BEARISH SIGNAL ---
    if bearish_trend:
        # 1. Rejecting from EMA 9 and 20
        rejecting = (curr['high'] >= curr['ema20'] * 0.995) and (curr['close'] <= curr['ema9'])
        
        # 2. Wick-to-wick bearish engulfing
        engulfing = (curr['low'] < prev['low']) and (curr['high'] > prev['high'])
        
        # 3. Volume & Color validation (Current Red > Prev Green)
        color_check = curr_is_red and prev_is_green
        
        if rejecting and engulfing and vol_higher and color_check:
            return "SELL"
            
    return None

def get_24h_stats(symbol):
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        data = requests.get(url, timeout=5).json()
        return float(data['priceChangePercent']), float(data['quoteVolume'])
    except:
        return 0.0, 0.0

# ==========================================
# 🟢 MAIN ANALYSIS LOGIC
# ==========================================
def analyze_and_chart(symbol):
    try:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(max_retries=3)
        session.mount('https://', adapter)
        
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=200"
        response = session.get(url, timeout=10)
        data = response.json()
        
        df = pd.DataFrame(data, columns=['time', 'open', 'high', 'low', 'close', 'volume', 'x', 'y', 'z', 'taker_buy_base', 'taker_buy_quote', 'i'])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index('time', inplace=True)
        
        cols = ['open', 'high', 'low', 'close', 'volume']
        for col in cols:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema100'] = df['close'].ewm(span=100, adjust=False).mean() # Kept strictly for chart visualization

        signal_detected = detect_sniper_signal(df)

        if not signal_detected: 
            return None, None, None, None, None, None

        curr = df.iloc[-1]
        gap_percent = (abs(curr['ema9'] - curr['ema20']) / curr['ema20']) * 100
        current_price = curr['close']
        change_24h, vol_24h = get_24h_stats(symbol)

        # Build Chart Visualization
        apds = [
            mpf.make_addplot(df['ema9'], color='#00d8ff', width=1.5),
            mpf.make_addplot(df['ema20'], color='#ffaa00', width=1.5),
            mpf.make_addplot(df['ema100'], color='#aa00ff', width=1.5)
        ]

        mc = mpf.make_marketcolors(up='#0ecb81', down='#f6465d', edge='inherit', wick='inherit', volume='in')
        s = mpf.make_mpf_style(base_mpf_style='nightclouds', marketcolors=mc, facecolor='#131722', edgecolor='#131722', figcolor='#131722', gridstyle='')

        file_name = f"{symbol}_{signal_detected}_{int(time.time())}.png"
        save_path = os.path.join(FOLDER_15M, file_name)

        fig, axes = mpf.plot(
            df, type='candle', volume=True, style=s, addplot=apds,
            figsize=(19.2, 10.8), returnfig=True, tight_layout=True, ylabel='', ylabel_lower=''
        )
        
        fig.subplots_adjust(bottom=0.15) 
        fig.savefig(save_path, bbox_inches='tight', dpi=100)
        
        # 🔥 MEMORY LEAK FIX: Explicitly close figure and clear memory 🔥
        plt.close(fig)
        plt.close('all')
        
        return signal_detected, save_path, gap_percent, current_price, change_24h, vol_24h

    except Exception as e:
        plt.close('all')  # 🔥 Clear memory if plotting failed
        log(f"Error charting {symbol}: {e}")
        return None, None, None, None, None, None

def process_strategy(symbol):
    """Processes signals and formats telegram captions intelligently."""
    signal, image_path, gap, price, change, vol = analyze_and_chart(symbol)
    
    if signal and image_path:
        clean_symbol = "Gold (PAXG)" if symbol == "PAXGUSDT" else symbol.replace('USDT', '')
        
        def format_num(num):
            if num >= 1_000_000: return f"{num/1_000_000:.1f}M"
            if num >= 1_000: return f"{num/1_000:.1f}K"
            return str(int(num))

        vol_formatted = format_num(vol)

        # 📨 TELEGRAM TEXT FORMATTING
        if signal == "BUY":
            caption = (
                f"🟢 *BULLISH SNIPER ENTRY* 🟢\n"
                f"💎 *${clean_symbol}* | #{symbol}\n\n"
                f"Price: {price} ({change:+.1f}% in 24h)\n"
                f"Setup: EMA 9/20 Bounce + Outside Engulfing\n"
                f"Trend: Confirmed Bullish (10+ Candles)\n"
                f"Spread Gap: {gap:.2f}%\n"
                f"24h Vol: {vol_formatted} USDT"
            )
        else:
            caption = (
                f"🔴 *BEARISH SNIPER ENTRY* 🔴\n"
                f"💎 *${clean_symbol}* | #{symbol}\n\n"
                f"Price: {price} ({change:+.1f}% in 24h)\n"
                f"Setup: EMA 9/20 Reject + Outside Engulfing\n"
                f"Trend: Confirmed Bearish (10+ Candles)\n"
                f"Spread Gap: {gap:.2f}%\n"
                f"24h Vol: {vol_formatted} USDT"
            )

        log(f"🎯 {signal} triggered on {symbol}")
        send_telegram_alert(image_path, caption)
        
        try: os.remove(image_path)
        except: pass
        
        return True
    return False

# ==========================================
# 🟢 THREAD WORKERS
# ==========================================
def worker_thread():
    """Crypto & PAXG Gold Worker (Binance Websocket Queue)"""
    while True:
        symbol = analysis_queue.get()
        alert_sent = process_strategy(symbol)
        
        if alert_sent:
            time.sleep(3)
            
        analysis_queue.task_done()

# ==========================================
# 🟢 BINANCE WEBSOCKET (CRYPTO & GOLD)
# ==========================================
def on_message(ws, message):
    global is_first_run
    
    if is_first_run:
        startup_msg = "✅ *Atif Bhai Ka Bot Zinda Hai!*\n\n📡 Scanning Binance for Strict Trend & Wick Engulfing...\n⏳ Wait for signals..."
        send_telegram_text(startup_msg)
        is_first_run = False

    data = json.loads(message)
    for ticker in data:
        symbol = ticker['s']
        if not symbol.endswith('USDT'): continue
        
        current_time = time.time()
        last_time = processed_coins.get(symbol, 0)
        
        if current_time - last_time < COOLDOWN_SECONDS: continue
        
        quote_volume = float(ticker['q'])
        if quote_volume < VOL_REQ_15M: continue

        processed_coins[symbol] = current_time
        analysis_queue.put(symbol)

def on_error(ws, error): log(f"WebSocket Error: {error}")
def on_close(ws, close_status_code, close_msg): log("WebSocket Closed. Reconnecting...")
def on_open(ws): log("Binance WebSocket Connected!")

def run_bot():
    while True:
        try:
            url = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
            ws.run_forever()
        except Exception as e:
            log(f"Connection lost: {e}. Reconnecting in 5s...")
            time.sleep(5)

# ==========================================
# 🟢 APP STARTUP
# ==========================================
if __name__ == "__main__":
    keep_alive()  
    Thread(target=worker_thread, daemon=True).start() 
    run_bot()
