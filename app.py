from flask import Flask, jsonify
from flask_cors import CORS
import requests
from datetime import datetime, timezone
import threading
import json
import os
import time

app = Flask(__name__)
CORS(app)

STATE_FILE = "trading_state.json"

DEFAULT_STATS = {
    "winning_trades": 0,
    "losing_trades": 0,
    "active_signal": "WAITING",
    "entry_price": 0,
    "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0,
    "confidence": "0%",
    "spot_tp1": 0, "spot_tp2": 0, "spot_tp3": 0, "spot_sl": 0,
    "tp1_hit": False,
    "tp2_hit": False,
    "tp3_hit": False,
    "trailing_stop": 0,
    "trailing_activated": False,
    "highest_price": 0,
    "lowest_price": 0,
    "current_price_str": "0.00",
    "binance_time": "00:00:00"
}

stats = {}
state_lock = threading.Lock()

def load_state():
    global stats
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                stats = json.load(f)
        except Exception:
            stats = DEFAULT_STATS.copy()
    else:
        stats = DEFAULT_STATS.copy()

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(stats, f, indent=4)
    except Exception as e:
        print(f"Error saving state: {e}")

# --- الحسابات الفنية الأصلية الدقيقة ---
def calculate_ema(prices, period):
    if len(prices) == 0: return 0
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < 2: return 0
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    if len(tr_list) <= period: return sum(tr_list) / len(tr_list)
    atr = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        atr = (tr_list[i] + atr * (period - 1)) / period
    return atr

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff >= 0:
            gains.append(diff); losses.append(0)
        else:
            gains.append(0); losses.append(abs(diff))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (gains[i] + avg_gain * (period - 1)) / period
        avg_loss = (losses[i] + avg_loss * (period - 1)) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(closes):
    return calculate_ema(closes, 12) - calculate_ema(closes, 26)

# --- جلب السعر والوقت عبر الروابط البديلة المضادة للحظر المخصصة للسحاب ---
def fetch_binance_data_safe():
    # شبكة سيرفرات بينانس الاحتياطية (api1, api2, api3) لا تخضع لنفس حظر السيرفر الرئيسي
    endpoints = [
        "https://api3.binance.com",
        "https://api1.binance.com",
        "https://api2.binance.com",
        "https://api.binance.com"
    ]
    
    price = 0
    b_time = "00:00:00"
    
    # 1. محاولة جلب السعر
    for base in endpoints:
        try:
            res = requests.get(f"{base}/api/v3/ticker/price?symbol=BTCUSDT", timeout=2).json()
            if 'price' in res:
                price = float(res['price'])
                break
        except: continue
        
    # 2. محاولة جلب الوقت
    for base in endpoints:
        try:
            res = requests.get(f"{base}/api/v3/time", timeout=2).json()
            if 'serverTime' in res:
                server_time_ms = res['serverTime']
                b_time = datetime.fromtimestamp(server_time_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")
                break
        except: continue
        
    # إذا فشلت كل السيرفرات في جلب وقت بينانس، استخدم وقت السيرفر الداخلي كخطة بديلة لحماية الساعة من التجمد
    if b_time == "00:00:00":
        b_time = datetime.now(timezone.utc).strftime("%H:%M:%S")
        
    return price, b_time

def get_multi_timeframe_klines():
    timeframes = {'15m': '15m', '1h': '1h', '4h': '4h'}
    results = {}
    endpoints = ["https://api3.binance.com", "https://api1.binance.com", "https://api2.binance.com"]
    
    for tf_name, interval in timeframes.items():
        success = False
        for base in endpoints:
            try:
                url = f"{base}/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit=100"
                res = requests.get(url, timeout=3).json()
                if isinstance(res, list) and len(res) > 0:
                    results[tf_name] = {
                        'closes': [float(candle[4]) for candle in res],
                        'highs': [float(candle[2]) for candle in res],
                        'lows': [float(candle[3]) for candle in res],
                        'volumes': [float(candle[5]) for candle in res]
                    }
                    success = True
                    break
            except: pass
        if not success: return {}
    return results

# --- محرك الرادار الخلفي الثابت والمحمي بمؤقت أمان ---
def background_radar_worker():
    global stats
    load_state()
    
    while True:
        try:
            current_price, binance_time = fetch_binance_data_safe()
            
            with state_lock:
                stats["binance_time"] = binance_time
                if current_price > 0:
                    stats["current_price_str"] = f"{current_price:.2f}"
            
            if current_price == 0:
                time.sleep(2)
                continue
                
            with state_lock:
                active_sig = stats.get("active_signal", "WAITING")
                
            # 1. إدارة الصفقة الحية
            if active_sig != "WAITING":
                with state_lock:
                    state_changed = False
                    if stats["active_signal"] == "BUY":
                        if current_price > stats["highest_price"]:
                            stats["highest_price"] = current_price
                            state_changed = True
                        potential_trail = stats["highest_price"] * 0.992
                        if potential_trail > stats["sl"] and stats["tp1_hit"]:
                            stats["sl"] = potential_trail
                            stats["trailing_activated"] = True
                            stats["trailing_stop"] = potential_trail
                            state_changed = True
                        if current_price >= stats["tp1"] and not stats["tp1_hit"]:
                            stats["tp1_hit"] = True
                            stats["sl"] = stats["entry_price"]
                            state_changed = True
                        if current_price >= stats["tp2"] and not stats["tp2_hit"]:
                            stats["tp2_hit"] = True
                            state_changed = True
                        if current_price >= stats["tp3"]:
                            stats["tp3_hit"] = True
                            stats["winning_trades"] += 1
                            stats["active_signal"] = "WAITING"
                            state_changed = True
                        elif current_price <= stats["sl"]:
                            if stats["tp1_hit"]: stats["winning_trades"] += 1
                            else: stats["losing_trades"] += 1
                            stats["active_signal"] = "WAITING"
                            state_changed = True

                    elif stats["active_signal"] == "SELL":
                        if current_price < stats["lowest_price"]:
                            stats["lowest_price"] = current_price
                            state_changed = True
                        potential_trail = stats["lowest_price"] * 1.008
                        if potential_trail < stats["sl"] and stats["tp1_hit"]:
                            stats["sl"] = potential_trail
                            stats["trailing_activated"] = True
                            stats["trailing_stop"] = potential_trail
                            state_changed = True
                        if current_price <= stats["tp1"] and not stats["tp1_hit"]:
                            stats["tp1_hit"] = True
                            stats["sl"] = stats["entry_price"]
                            state_changed = True
                        if current_price <= stats["tp2"] and not stats["tp2_hit"]:
                            stats["tp2_hit"] = True
                            state_changed = True
                        if current_price <= stats["tp3"]:
                            stats["tp3_hit"] = True
                            stats["winning_trades"] += 1
                            stats["active_signal"] = "WAITING"
                            state_changed = True
                        elif current_price >= stats["sl"]:
                            if stats["tp1_hit"]: stats["winning_trades"] += 1
                            else: stats["losing_trades"] += 1
                            stats["active_signal"] = "WAITING"
                            state_changed = True
                    if state_changed: save_state()
                    
            # 2. منطق استخراج الإشارات
            else:
                multi_tf = get_multi_timeframe_klines()
                if multi_tf and '15m' in multi_tf:
                    signals = {}
                    atr_15m = 0
                    for tf_name, data in multi_tf.items():
                        closes, highs, lows, volumes = data['closes'], data['highs'], data['lows'], data['volumes']
                        ema20, ema50 = calculate_ema(closes, 20), calculate_ema(closes, 50)
                        atr, rsi, macd = calculate_atr(highs, lows, closes, 14), calculate_rsi(closes, 14), calculate_macd(closes)
                        if tf_name == '15m': atr_15m = atr
                        avg_volume = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else volumes[-1]
                        volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 1
                        
                        score = 0
                        if closes[-1] > ema20 > ema50: score += 2
                        elif closes[-1] > ema20: score += 1
                        elif closes[-1] < ema20 < ema50: score -= 2
                        elif closes[-1] < ema20: score -= 1
                        if rsi < 35: score += 1
                        elif rsi > 65: score -= 1
                        if macd > 0: score += 1
                        else: score -= 1
                        if volume_ratio > 1.2:
                            if score > 0: score += 1
                            elif score < 0: score -= 1
                        signals[tf_name] = score
                    
                    final_score = (signals.get('15m', 0) * 3 + signals.get('1h', 0) * 2 + signals.get('4h', 0) * 1) / 6
                    
                    with state_lock:
                        stats["tp1_hit"] = stats["tp2_hit"] = stats["tp3_hit"] = stats["trailing_activated"] = False
                        stats["trailing_stop"] = 0
                        state_triggered = False
                        
                        if final_score >= 1.0 and atr_15m > 0:
                            stats["active_signal"] = "BUY"
                            stats["entry_price"] = current_price
                            stats["highest_price"] = stats["lowest_price"] = current_price
                            stats["tp1"], stats["tp2"], stats["tp3"] = current_price + (atr_15m * 1.5), current_price + (atr_15m * 3.0), current_price + (atr_15m * 5.0)
                            stats["sl"] = current_price - (atr_15m * 2.0)
                            stats["confidence"] = f"{min(95, int(abs(final_score) * 25))}%"
                            state_triggered = True
                        elif final_score <= -1.0 and atr_15m > 0:
                            stats["active_signal"] = "SELL"
                            stats["entry_price"] = current_price
                            stats["highest_price"] = stats["lowest_price"] = current_price
                            stats["tp1"], stats["tp2"], stats["tp3"] = current_price - (atr_15m * 1.5), current_price - (atr_15m * 3.0), current_price - (atr_15m * 5.0)
                            stats["sl"] = current_price + (atr_15m * 2.0)
                            stats["confidence"] = f"{min(92, int(abs(final_score) * 25))}%"
                            state_triggered = True
                        else:
                            stats["active_signal"] = "WAITING"
                            stats["confidence"] = "0%"

                        entry = stats["entry_price"]
                        if stats["active_signal"] != "WAITING" and entry > 0:
                            stats["spot_tp1"] = abs((stats['tp1'] - entry) / entry)
                            stats["spot_tp2"] = abs((stats['tp2'] - entry) / entry)
                            stats["spot_tp3"] = abs((stats['tp3'] - entry) / entry)
                            stats["spot_sl"] = abs((stats['sl'] - entry) / entry)
                        else:
                            stats["spot_tp1"] = stats["spot_tp2"] = stats["spot_tp3"] = stats["spot_sl"] = 0
                        if state_triggered: save_state()
                        
        except Exception: pass
        time.sleep(2) # تحديث مستمر آمن كل ثانيتين دون الضغط على السيرفر

# --- نفس كود صفحة الـ HTML والـ API الخاص بك دون أي تعديل ---
@app.route('/api/signals', methods=['GET'])
def get_signals():
    with state_lock:
        return jsonify({
            "success": True, 
            "data": {
                "price": stats.get("current_price_str", "0.00"), 
                "entry": f"{stats['entry_price']:.2f}" if stats['active_signal'] != 'WAITING' else "—", 
                "signal": stats["active_signal"],
                "tp1": f"{stats['tp1']:.2f}" if stats['active_signal'] != 'WAITING' else "—", 
                "tp2": f"{stats['tp2']:.2f}" if stats['active_signal'] != 'WAITING' else "—", 
                "tp3": f"{stats['tp3']:.2f}" if stats['active_signal'] != 'WAITING' else "—", 
                "sl": f"{stats['sl']:.2f}" if stats['active_signal'] != 'WAITING' else "—",
                "spot_tp1": stats.get("spot_tp1", 0), "spot_tp2": stats.get("spot_tp2", 0), 
                "spot_tp3": stats.get("spot_tp3", 0), "spot_sl": stats.get("spot_sl", 0),
                "tp1_hit": stats.get("tp1_hit", False), "tp2_hit": stats.get("tp2_hit", False), "tp3_hit": stats.get("tp3_hit", False),
                "trailing_activated": stats.get("trailing_activated", False),
                "trailing_stop": f"{stats.get('trailing_stop', 0):.2f}",
                "confidence": stats.get("confidence", "0%"), "binance_time": stats.get("binance_time", "00:00:00")
            }, 
            "stats": {
                "wins": stats.get("winning_trades", 0), 
                "losses": stats.get("losing_trades", 0)
            }
        })

@app.route('/')
def home():
    return """
    <!DOCTYPE html>
    <html lang="en" dir="ltr" id="html-tag">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Binance Futures Intel Radar Pro</title>
        <style>
            :root { --bg-color: #0b0e11; --card-bg: #181a20; --text-color: #eceff1; --text-muted: #848e9c; --buy-color: #02c076; --sell-color: #f6465d; --wait-color: #f3ba2f; }
            body { background-color: var(--bg-color); color: var(--text-color); font-family: 'Segoe UI', sans-serif; margin: 0; padding: 12px; display: flex; flex-direction: column; gap: 12px; }
            .header-bar { display: flex; justify-content: space-between; align-items: center; background: var(--card-bg); padding: 12px; border-radius: 12px; border: 1px solid #2b2f36; font-size: 13px; }
            .controls-group { display: flex; gap: 10px; align-items: center; }
            .custom-select { background: #202226; color: #fff; border: 1px solid #474d57; padding: 6px 10px; border-radius: 8px; font-weight: bold; cursor: pointer; }
            .lev-select { border-color: #f3ba2f; color: #f3ba2f; }
            .stats-container { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
            .stat-box { background: var(--card-bg); padding: 14px; border-radius: 12px; text-align: center; border: 1px solid #2b2f36; }
            .stat-count { font-size: 24px; font-weight: bold; margin-top: 5px; }
            .stat-box.wins { border-color: var(--buy-color); color: var(--buy-color); }
            .stat-box.losses { border-color: var(--sell-color); color: var(--sell-color); }
            .card { background-color: var(--card-bg); border-radius: 16px; padding: 15px; border: 1px solid #2b2f36; }
            .card-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2b2f36; padding-bottom: 10px; margin-bottom: 12px; }
            .symbol { font-size: 18px; font-weight: bold; color: #f3ba2f; }
            .price { font-size: 20px; font-weight: bold; }
            .signal-box { text-align: center; padding: 14px; border-radius: 12px; font-size: 24px; font-weight: bold; margin-bottom: 12px; background: rgba(255,255,255,0.03); }
            .BUY { color: var(--buy-color); border: 1px solid var(--buy-color); background: rgba(2, 192, 118, 0.1); }
            .SELL { color: var(--sell-color); border: 1px solid var(--sell-color); background: rgba(246, 70, 93, 0.1); }
            .WAITING { color: var(--wait-color); border: 1px solid var(--wait-color); background: rgba(243, 186, 47, 0.1); }
            .trailing-badge { display: none; background: #f3ba2f; color: #000; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: bold; margin-left: 8px; }
            .trailing-badge.active { display: inline-block; }
            .grid-details { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
            .detail-item { background-color: #202226; padding: 10px; border-radius: 8px; font-size: 13px; display: flex; flex-direction: column; transition: all 0.3s ease; }
            .detail-item.full { grid-column: span 2; display: flex; flex-direction: row; justify-content: space-between; align-items: center; }
            .detail-item.entry { border-left: 4px solid #f3ba2f; border-right: 4px solid #f3ba2f; background: rgba(243, 186, 47, 0.05); }
            .detail-item.hit { border: 1px solid var(--buy-color); background: rgba(2, 192, 118, 0.08) !important; }
            .detail-item.sl-updated { border: 1px solid #f3ba2f; }
            .label { color: var(--text-muted); font-size: 11px; margin-bottom: 4px; }
            .val { font-weight: bold; color: #fff; font-size: 14px; }
            .pct { font-size: 12px; font-weight: bold; margin-left: 6px; margin-right: 6px; }
            .pct.up { color: var(--buy-color); }
            .pct.down { color: var(--sell-color); }
            .check-mark { color: var(--buy-color); font-weight: bold; margin-left: 5px; font-size: 15px; display: none; }
            .advisor-card { background: #1f222a; border-radius: 12px; padding: 12px; border: 1px solid #363c4e; margin-top: 12px; font-size: 13px; line-height: 1.5; }
            .advisor-title { font-weight: bold; color: #f3ba2f; display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
        </style>
    </head>
    <body>
        <div class="header-bar">
            <div><span id="lbl-time">Binance Time:</span> <span id="binance-clock" style="font-weight: bold; color: #f3ba2f;">00:00:00</span></div>
            <div class="controls-group">
                <select id="lev-selector" class="custom-select lev-select" onchange="updateLeverage()">
                    <option value="1">1x (Spot)</option>
                    <option value="5">5x</option>
                    <option value="10" selected>10x</option>
                    <option value="20">20x</option>
                    <option value="50">50x</option>
                </select>
                <select class="custom-select" onchange="changeLanguage(this.value)">
                    <option value="en">English</option>
                    <option value="ar">العربية</option>
                </select>
            </div>
        </div>
        <div class="stats-container">
            <div class="stat-box wins"><span id="lbl-wins">Futures Wins ✅</span><div class="stat-count" id="count-wins">0</div></div>
            <div class="stat-box losses"><span id="lbl-losses">Futures Losses ❌</span><div class="stat-count" id="count-losses">0</div></div>
        </div>
        <div class="card">
            <div class="card-header">
                <span class="symbol">🔀 BTCUSDT Perpetual</span>
                <span class="price" id="btc-price">...</span>
            </div>
            <div style="display: flex; align-items: center; justify-content: center; gap: 10px;">
                <div id="btc-signal" class="signal-box">—</div>
                <span id="trailing-badge" class="trailing-badge">🔄 TRAILING SL</span>
            </div>
            
            <div class="grid-details">
                <div class="detail-item full entry">
                    <span class="label" id="lbl-entry">Fixed Entry Price:</span>
                    <span class="val" id="btc-entry">—</span>
                </div>
                <div id="box-tp1" class="detail-item">
                    <span class="label" id="lbl-tp1">Take Profit (TP1)</span>
                    <div><span class="val" id="btc-tp1" style="color: var(--buy-color);">—</span> <span class="pct up" id="btc-tp1-p"></span><span id="chk-tp1" class="check-mark">✔️</span></div>
                </div>
                <div id="box-tp2" class="detail-item">
                    <span class="label" id="lbl-tp2">Take Profit (TP2)</span>
                    <div><span class="val" id="btc-tp2" style="color: var(--buy-color);">—</span> <span class="pct up" id="btc-tp2-p"></span><span id="chk-tp2" class="check-mark">✔️</span></div>
                </div>
                <div id="box-tp3" class="detail-item">
                    <span class="label" id="lbl-tp3">Take Profit (TP3)</span>
                    <div><span class="val" id="btc-tp3" style="color: var(--buy-color);">—</span> <span class="pct up" id="btc-tp3-p"></span><span id="chk-tp3" class="check-mark">✔️</span></div>
                </div>
                <div id="box-sl" class="detail-item">
                    <span class="label" id="lbl-sl">Stop Loss (SL)</span>
                    <div><span class="val" id="btc-sl" style="color: var(--sell-color);">—</span> <span class="pct down" id="btc-sl-p"></span></div>
                </div>
                <div class="detail-item full" style="text-align: center; justify-content: center; gap: 10px;">
                    <span class="label" id="lbl-conf">Signal Momentum:</span>
                    <span class="val" id="btc-conf">—</span>
                </div>
            </div>

            <div class="advisor-card">
                <div class="advisor-title">🛡️ <span id="adv-title">Active Trade Management (Pro Advisor)</span></div>
                <div id="advisor-text" style="color: var(--text-muted);">Waiting for signal generation to deliver safety protocols...</div>
            </div>
        </div>
        <script>
            const translations = {
                en: { 
                    lbl_time: "Binance Time:", wins: "Futures Wins ✅", losses: "Futures Losses ❌", 
                    entry: "Fixed Entry Price:", tp1: "Take Profit (TP1)", tp2: "Take Profit (TP2)", 
                    tp3: "Take Profit (TP3)", sl: "Stop Loss (SL)", conf: "Signal Momentum:", 
                    buy: "🟢 LONG / BULLISH", sell: "🔴 SHORT / BEARISH", waiting: "Waiting for Signal.. ⏳", 
                    adv_title: "Active Trade Management (Pro Advisor)",
                    adv_wait: "System is scanning multiple timeframes via Safe API (15m/1h/4h). Once a trade is active, risk management instructions will appear.",
                    adv_hit1: "🔥 Target 1 (TP1) hit! Stop Loss moved to Entry Price (Break-Even). Risk is now 0%. Secure 50% profit on exchange.",
                    adv_hit2: "🚀 Target 2 (TP2) hit! Lock further massive gains by moving your exchange Stop Loss manually to the TP1 price level.",
                    adv_trail: "🔄 Trailing Stop is ACTIVE! The Stop Loss is automatically moving up with the price to protect your profits.",
                    adv_open: "⚡ Position is live. System is monitoring with Trailing Stop ready to activate after TP1. Prepare to lock risk-free parameters."
                },
                ar: { 
                    lbl_time: "توقيت بينانس:", wins: "فيوتشر رابحة ✅", losses: "فيوتشر خاسرة ❌", 
                    entry: "سعر الدخول الثابت:", tp1: "جني الأرباح (TP1)", tp2: "جني الأرباح (TP2)", 
                    tp3: "جني الأرباح (TP3)", sl: "وقف الخسارة (SL)", conf: "قوة زخم الإشارة:", 
                    buy: "🟢 LONG / شراء صعودي", sell: "🔴 SHORT / بيع هبوطي", waiting: "في انتظار إشارة جديدة.. ⏳", 
                    adv_title: "المساعد الذكي لإدارة الصفقة (Pro Advisor)",
                    adv_wait: "النظام يحلل السوق مباشرة عبر خلافية السيرفر المحمية من الحظر. بمجرد دخول الصفقة ستظهر خطة تأمين الأرباح.",
                    adv_hit1: "🔥 رائع! تم تحقيق الهدف الأول (TP1). تم نقل وقف الخسارة إلى سعر الدخول (Break-Even). الصفقة الآن خالية تماماً من المخاطر.",
                    adv_hit2: "🚀 ممتاز! تم ضرب الهدف الثاني (TP2). قم بتحريك وقف الخسارة على المنصة إلى مستوى الهدف الأول (TP1) لضمان خروج بربح ممتاز.",
                    adv_trail: "🔄 وقف الخسارة المتحرك نشط! يتم تحريك وقف الخسارة تلقائياً مع السعر لحماية أرباحك.",
                    adv_open: "⚡ الصفقة حية. النظام يراقب مع وقف خسارة متحرك جاهز للتفعيل بعد الهدف الأول. التزم بالأرقام المحددة."
                }
            };
            
            let currentLang = 'en'; let globalData = null;
            
            function changeLanguage(lang) {
                currentLang = lang; localStorage.setItem('radar_lang', lang);
                document.getElementById('html-tag').setAttribute('dir', lang === 'ar' ? 'rtl' : 'ltr');
                document.getElementById('lbl-time').innerText = translations[lang].lbl_time; document.getElementById('lbl-wins').innerText = translations[lang].wins;
                document.getElementById('lbl-losses').innerText = translations[lang].losses; document.getElementById('lbl-entry').innerText = translations[lang].entry;
                document.getElementById('lbl-tp1').innerText = translations[lang].tp1; document.getElementById('lbl-tp2').innerText = translations[lang].tp2;
                document.getElementById('lbl-tp3').innerText = translations[lang].tp3; document.getElementById('lbl-sl').innerText = translations[lang].sl;
                document.getElementById('lbl-conf').innerText = translations[lang].conf; document.getElementById('adv-title').innerText = translations[lang].adv_title;
                renderData();
            }
            
            function updateLeverage() { renderData(); }
            
            function renderData() {
                if(!globalData) return;
                let btc = globalData.data; let lev = parseInt(document.getElementById('lev-selector').value);
                document.getElementById('count-wins').innerText = globalData.stats.wins; document.getElementById('count-losses').innerText = globalData.stats.losses;
                document.getElementById('binance-clock').innerText = btc.binance_time; document.getElementById('btc-price').innerText = "$" + btc.price;
                document.getElementById('btc-entry').innerText = btc.entry === "—" ? "—" : "$" + btc.entry;
                document.getElementById('btc-tp1').innerText = btc.tp1 === "—" ? "—" : "$" + btc.tp1; document.getElementById('btc-tp2').innerText = btc.tp2 === "—" ? "—" : "$" + btc.tp2;
                document.getElementById('btc-tp3').innerText = btc.tp3 === "—" ? "—" : "$" + btc.tp3; document.getElementById('btc-sl').innerText = btc.sl === "—" ? "—" : "$" + btc.sl;
                document.getElementById('btc-conf').innerText = btc.confidence;
                
                let trailBadge = document.getElementById('trailing-badge');
                if (btc.trailing_activated) { trailBadge.classList.add('active'); } else { trailBadge.classList.remove('active'); }
                
                if(btc.entry !== "—") {
                    document.getElementById('btc-tp1-p').innerText = `(ROE +${(btc.spot_tp1 * lev * 100).toFixed(2)}%)`;
                    document.getElementById('btc-tp2-p').innerText = `(ROE +${(btc.spot_tp2 * lev * 100).toFixed(2)}%)`;
                    document.getElementById('btc-tp3-p').innerText = `(ROE +${(btc.spot_tp3 * lev * 100).toFixed(2)}%)`;
                    document.getElementById('btc-sl-p').innerText = `(ROE -${(btc.spot_sl * lev * 100).toFixed(2)}%)`;
                } else {
                    document.getElementById('btc-tp1-p').innerText = ""; document.getElementById('btc-tp2-p').innerText = "";
                    document.getElementById('btc-tp3-p').innerText = ""; document.getElementById('btc-sl-p').innerText = "";
                }

                if(btc.signal !== "WAITING") {
                    document.getElementById('chk-tp1').style.display = btc.tp1_hit ? "inline-block" : "none";
                    document.getElementById('box-tp1').className = btc.tp1_hit ? "detail-item hit" : "detail-item";
                    document.getElementById('chk-tp2').style.display = btc.tp2_hit ? "inline-block" : "none";
                    document.getElementById('box-tp2').className = btc.tp2_hit ? "detail-item hit" : "detail-item";
                    document.getElementById('chk-tp3').style.display = btc.tp3_hit ? "inline-block" : "none";
                    document.getElementById('box-tp3').className = btc.tp3_hit ? "detail-item hit" : "detail-item";
                    document.getElementById('box-sl').className = btc.trailing_activated ? "detail-item sl-updated" : "detail-item";

                    if (btc.trailing_activated) { document.getElementById('advisor-text').innerHTML = translations[currentLang].adv_trail; }
                    else if(btc.tp2_hit) { document.getElementById('advisor-text').innerHTML = translations[currentLang].adv_hit2; }
                    else if(btc.tp1_hit) { document.getElementById('advisor-text').innerHTML = translations[currentLang].adv_hit1; }
                    else { document.getElementById('advisor-text').innerHTML = translations[currentLang].adv_open; }
                } else {
                    document.getElementById('chk-tp1').style.display = "none"; document.getElementById('box-tp1').className = "detail-item";
                    document.getElementById('chk-tp2').style.display = "none"; document.getElementById('box-tp2').className = "detail-item";
                    document.getElementById('chk-tp3').style.display = "none"; document.getElementById('box-tp3').className = "detail-item";
                    document.getElementById('box-sl').className = "detail-item";
                    document.getElementById('advisor-text').innerHTML = translations[currentLang].adv_wait;
                }
                let signalText = translations[currentLang].waiting;
                if (btc.signal === "BUY") signalText = translations[currentLang].buy;
                else if (btc.signal === "SELL") signalText = translations[currentLang].sell;
                document.getElementById('btc-signal').innerText = signalText; 
                document.getElementById('btc-signal').className = "signal-box " + btc.signal;
            }
            
            async function updateUI() {
                try { 
                    let response = await fetch('/api/signals'); 
                    globalData = await response.json(); 
                    if(globalData.success) { renderData(); } 
                } catch (error) {}
            }
            
            const savedLang = localStorage.getItem('radar_lang') || 'en';
            document.querySelector('select[onchange="changeLanguage(this.value)"]').value = savedLang;
            changeLanguage(savedLang); 
            
            setInterval(updateUI, 1000);
        </script>
    </body>
    </html>
    """

if __name__ == '__main__':
    bg_thread = threading.Thread(target=background_radar_worker, daemon=True)
    bg_thread.start()
    app.run(host='0.0.0.0', port=3000, debug=False)
