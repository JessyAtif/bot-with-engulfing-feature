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

# --- CONFIGURATION ---
warnings.filterwarnings("ignore")

# 🔥 TELEGRAM SETTINGS 🔥
TELEGRAM_BOT_TOKEN = '8590924453:AAGPbmBml5qICTOn6ZKe2shUAw8a52GNFEY'  
TELEGRAM_CHAT_ID = '@brainlifttrader'                 

# 🔥 STRATEGY SETTINGS 🔥
MIN_PRICE = 0.0000001
COOLDOWN_SECONDS = 60
VOL_REQ_15M = 1000000      # 1M Volume
GAP_THRESHOLD = 0.005
MIN_SPREAD_PERCENT = 0.2
MAX_SPREAD_PERCENT = 1.0

# --- GLOBAL VARIABLES ---
processed_coins = {}
analysis_queue = queue.Queue()
is_first_run = True 

# --- FOLDER SETUP ---
desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
BASE_FOLDER = os.path.join(desktop_path, "Scanner_Alerts")
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
# 🟢 ENGULFING STRATEGY LOGIC
# ==========================================
def detect_engulfing(prev, curr):
    """
    Checks if a proper Engulfing candle occurred WITH a dynamic EMA touch/rejection.
    Returns: Signal Name, Touched EMA name, Calculated Stop Loss
    """
    emas = ['ema9', 'ema20', 'ema100']
    
    for ema in emas:
        # 1. BULLISH ENGULFING (Red followed by Green)
        if prev['close'] < prev['open'] and curr['close'] > curr['open']:
            if curr['close'] >= prev['open'] and curr['open'] <= prev['close']:
                # EMA Touch (Support Check)
                if curr['low'] <= curr[ema] and curr['close'] > curr[ema]:
                    sl = min(curr['low'], prev['low']) # SL below recent swing low
                    return "BULLISH_ENGULFING", ema, sl
                    
        # 2. BEARISH ENGULFING (Green followed by Red)
        elif prev['close'] > prev['open'] and curr['close'] < curr['open']:
            if curr['close'] <= prev['open'] and curr['open'] >= prev['close']:
                # EMA Touch (Resistance Check)
                if curr['high'] >= curr[ema] and curr['close'] < curr[ema]:
                    sl = max(curr['high'], prev['high']) # SL above recent swing high
                    return "BEARISH_ENGULFING", ema, sl
                    
    return None, None, None

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
        # Fetch data using Binance API
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
        df['ema100'] = df['close'].ewm(span=100, adjust=False).mean()

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        
        signal_detected = None
        ema_touched = None
        stop_loss = None
        
        gap_percent = ((curr['ema9'] - curr['ema20']) / curr['ema20']) * 100
        is_spread_valid = MIN_SPREAD_PERCENT <= gap_percent <= MAX_SPREAD_PERCENT

        # 🧠 1. CHECK ENGULFING STRATEGY FIRST
        eng_signal, eng_ema, eng_sl = detect_engulfing(prev, curr)
        
        if eng_signal:
            signal_detected = eng_signal
            ema_touched = eng_ema
            stop_loss = eng_sl
        else:
            # 🧠 2. CHECK EXISTING STRATEGIES
            if prev['ema9'] <= prev['ema20'] and curr['ema9'] > curr['ema20']:
                signal_detected = "CROSSOVER"
            elif curr['ema9'] > curr['ema20'] and curr['ema20'] > curr['ema100']:
                if is_spread_valid:
                    if curr['low'] <= curr['ema9'] and curr['close'] > curr['ema20']:
                        signal_detected = "SUPER_SIGNAL"

        # If no signal found, ignore and exit
        if not signal_detected: 
            return None, None, None, None, None, None, None, None

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
        
        return signal_detected, save_path, gap_percent, current_price, change_24h, vol_24h, ema_touched, stop_loss

    except Exception as e:
        log(f"Error charting {symbol}: {e}")
        return None, None, None, None, None, None, None, None

def process_strategy(symbol):
    """Processes signals and formats telegram captions intelligently."""
    signal, image_path, gap, price, change, vol, ema_touched, sl = analyze_and_chart(symbol)
    
    if signal and image_path:
        # Display PAXGUSDT clearly as Gold in Telegram
        if symbol == "PAXGUSDT":
            clean_symbol = "Gold (PAXG)"
        else:
            clean_symbol = symbol.replace('USDT', '')
        
        def format_num(num):
            if num >= 1_000_000: return f"{num/1_000_000:.1f}M"
            if num >= 1_000: return f"{num/1_000:.1f}K"
            return str(int(num))

        vol_formatted = format_num(vol)

        # 📨 TELEGRAM TEXT FORMATTING
        if "ENGULFING" in signal:
            direction = "🟢 LONG (Buy)" if signal == "BULLISH_ENGULFING" else "🔴 SHORT (Sell)"
            caption = (
                f"🔥 *${clean_symbol}* | EMA Engulfing\n\n"
                f"🚨 *Signal:* {direction}\n"
                f"💰 *Entry Price:* {price}\n"
                f"🛡️ *Dynamic S/R:* {ema_touched.upper()} Touch\n"
                f"🛑 *Stop Loss:* {sl:.4f} (Swing Point)\n\n"
                f"⚠️ *Rules:* SL/TP fix rakhein. No FOMO!"
            )
        elif signal == "SUPER_SIGNAL":
            caption = (
                f"💎 *${clean_symbol}* | #{symbol}\n"
                f"Price: {price} ({change:+.1f}% in 24h)\n"
                f"Strategy: Gap + Pullback (15m)\n"
                f"Spread Gap: {gap:.2f}%\n"
                f"24h Vol: {vol_formatted} USDT (Binance)"
            )
        else:
            caption = (
                f"🚀 *${clean_symbol}* | #{symbol}\n"
                f"Price: {price} ({change:+.1f}% in 24h)\n"
                f"Strategy: Fresh EMA 9/20 Crossover (15m)\n"
                f"24h Vol: {vol_formatted} USDT (Binance)"
            )

        log(f"🎯 {signal} triggered on {symbol}")
        send_telegram_alert(image_path, caption)
        
        # Cleanup image
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
        process_strategy(symbol)
        analysis_queue.task_done()

# ==========================================
# 🟢 BINANCE WEBSOCKET (CRYPTO & GOLD)
# ==========================================
def on_message(ws, message):
    global is_first_run
    
    if is_first_run:
        startup_msg = "✅ *Atif Bhai Ka Bot Zinda Hai!*\n\n📡 Scanning Binance for Crypto & Gold (PAXGUSDT) Engulfing...\n⏳ Wait for signals..."
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
    keep_alive()  # Starts the Flask Server 
    Thread(target=worker_thread, daemon=True).start() # Starts Queue processing
    run_bot()     # Starts WebSocket (Blocking main thread)
