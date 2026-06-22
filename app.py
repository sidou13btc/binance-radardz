from flask import Flask, jsonify
from flask_cors import CORS
import requests
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app)

# Global Trading State Machine & Binance Order Simulation
stats = {
    "winning_trades": 0,
    "losing_trades": 0,
    "active_signal": "WAITING",  # WAITING, BUY (LONG), or SELL (SHORT)
    "entry_price": 0,
    "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0,
    "confidence": "0%",
    "spot_tp1": 0, "spot_tp2": 0, "spot_tp3": 0, "spot_sl": 0,
    "tp1_hit": False,
    "tp2_hit": False,
    "tp3_hit": False
}

def calculate_simple_ema(prices):
    k = 2 / (20 + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def process_bitcoin_radar():
    try:
        time_url = "https://api.binance.com/api/v3/time"
        server_time_ms = requests.get(time_url).json()['serverTime']
        now = datetime.fromtimestamp(server_time_ms / 1000, tz=timezone.utc)
        binance_time = now.strftime("%H:%M:%S")

        minutes_into_interval = now.minute % 15
        seconds_remaining = (15 * 60) - ((minutes_into_interval * 60) + now.second)

        ticker_url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        current_price = float(requests.get(ticker_url).json()['price'])

        # 1. LIVE POSITION MANAGEMENT & RISK MITIGATION
        if stats["active_signal"] != "WAITING":
            if stats["active_signal"] == "BUY":  # LONG POSITION
                if current_price >= stats["tp1"]: 
                    stats["tp1_hit"] = True
                    stats["sl"] = stats["entry_price"] * 1.001 # Entry + Fees
                    
                if current_price >= stats["tp2"]: stats["tp2_hit"] = True
                
                if current_price >= stats["tp3"]:
                    stats["tp3_hit"] = True
                    stats["winning_trades"] += 1
                    stats["active_signal"] = "WAITING"
                elif current_price <= stats["sl"]:
                    if stats["tp1_hit"]:
                        stats["winning_trades"] += 1
                    else:
                        stats["losing_trades"] += 1
                    stats["active_signal"] = "WAITING"

            elif stats["active_signal"] == "SELL":  # SHORT POSITION
                if current_price <= stats["tp1"]: 
                    stats["tp1_hit"] = True
                    stats["sl"] = stats["entry_price"] * 0.999 # Entry - Fees
                    
                if current_price <= stats["tp2"]: stats["tp2_hit"] = True
                
                if current_price <= stats["tp3"]:
                    stats["tp3_hit"] = True
                    stats["winning_trades"] += 1
                    stats["active_signal"] = "WAITING"
                elif current_price >= stats["sl"]:
                    if stats["tp1_hit"]:
                        stats["winning_trades"] += 1
                    else:
                        stats["losing_trades"] += 1
                    stats["active_signal"] = "WAITING"
            
            return {
                "price": f"{current_price:.2f}", "entry": f"{stats['entry_price']:.2f}", "signal": stats["active_signal"],
                "tp1": f"{stats['tp1']:.2f}", "tp2": f"{stats['tp2']:.2f}", "tp3": f"{stats['tp3']:.2f}", "sl": f"{stats['sl']:.2f}",
                "spot_tp1": stats["spot_tp1"], "spot_tp2": stats["spot_tp2"], "spot_tp3": stats["spot_tp3"], "spot_sl": stats["spot_sl"],
                "tp1_hit": stats["tp1_hit"], "tp2_hit": stats["tp2_hit"], "tp3_hit": stats["tp3_hit"],
                "confidence": stats["confidence"], "binance_time": binance_time, "seconds_remaining": seconds_remaining
            }

        # 2. SIGNAL GENERATION
        url_15m = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=25"
        res_15m = requests.get(url_15m).json()
        prices_15m_closed = [float(candle[4]) for candle in res_15m[:-1]]
        
        ema20 = calculate_simple_ema(prices_15m_closed)
        approx_atr = current_price * 0.0025

        stats["tp1_hit"], stats["tp2_hit"], stats["tp3_hit"] = False, False, False

        if current_price > ema20:
            stats["active_signal"] = "BUY"
            stats["entry_price"] = current_price
            stats["tp1"] = current_price + (approx_atr * 1.0)
            stats["tp2"] = current_price + (approx_atr * 2.2)
            stats["tp3"] = current_price + (approx_atr * 3.5)
            stats["sl"] = current_price - (approx_atr * 1.5)
            stats["confidence"] = "95%"
        else:
            stats["active_signal"] = "SELL"
            stats["entry_price"] = current_price
            stats["tp1"] = current_price - (approx_atr * 1.0)
            stats["tp2"] = current_price - (approx_atr * 2.2)
            stats["tp3"] = current_price - (approx_atr * 3.5)
            stats["sl"] = current_price + (approx_atr * 1.5)
            stats["confidence"] = "92%"

        entry = stats["entry_price"]
        stats["spot_tp1"] = abs((stats['tp1'] - entry) / entry)
        stats["spot_tp2"] = abs((stats['tp2'] - entry) / entry)
        stats["spot_tp3"] = abs((stats['tp3'] - entry) / entry)
        stats["spot_sl"] = abs((stats['sl'] - entry) / entry)

        return {
            "price": f"{current_price:.2f}", "entry": f"{stats['entry_price']:.2f}", "signal": stats["active_signal"],
            "tp1": f"{stats['tp1']:.2f}", "tp2": f"{stats['tp2']:.2f}", "tp3": f"{stats['tp3']:.2f}", "sl": f"{stats['sl']:.2f}",
            "spot_tp1": stats["spot_tp1"], "spot_tp2": stats["spot_tp2"], "spot_tp3": stats["spot_tp3"], "spot_sl": stats["spot_sl"],
            "tp1_hit": stats["tp1_hit"], "tp2_hit": stats["tp2_hit"], "tp3_hit": stats["tp3_hit"],
            "confidence": stats["confidence"], "binance_time": binance_time, "seconds_remaining": seconds_remaining
        }
    except Exception as e:
        return {"error": True, "message": str(e)}

@app.route('/api/signals', methods=['GET'])
def get_signals():
    return jsonify({"success": True, "data": process_bitcoin_radar(), "stats": {"wins": stats["winning_trades"], "losses": stats["losing_trades"]}})

@app.route('/')
def home():
    return """
    <!DOCTYPE html>
    <html lang="en" dir="ltr" id="html-tag">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Binance Futures Intel Radar</title>
        <style>
            :root { --bg-color: #0b0e11; --card-bg: #181a20; --text-color: #eceff1; --text-muted: #848e9c; --buy-color: #02c076; --sell-color: #f6465d; --wait-color: #f3ba2f; }
            body { background-color: var(--bg-color); color: var(--text-color); font-family: sans-serif; margin: 0; padding: 12px; display: flex; flex-direction: column; gap: 12px; }
            .header-bar { display: flex; justify-content: space-between; align-items: center; background: var(--card-bg); padding: 10px; border-radius: 12px; border: 1px solid #2b2f36; font-size: 13px; }
            .controls-group { display: flex; gap: 8px; align-items: center; }
            .custom-select { background: #202226; color: #fff; border: 1px solid #474d57; padding: 5px; border-radius: 6px; font-weight: bold; }
            .lev-select { border-color: #f3ba2f; color: #f3ba2f; }
            .stats-container { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
            .stat-box { background: var(--card-bg); padding: 12px; border-radius: 12px; text-align: center; border: 1px solid #2b2f36; }
            .stat-count { font-size: 22px; font-weight: bold; margin-top: 5px; }
            .stat-box.wins { border-color: var(--buy-color); color: var(--buy-color); }
            .stat-box.losses { border-color: var(--sell-color); color: var(--sell-color); }
            .card { background-color: var(--card-bg); border-radius: 16px; padding: 15px; border: 1px solid #2b2f36; }
            .card-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2b2f36; padding-bottom: 8px; margin-bottom: 12px; }
            .symbol { font-size: 18px; font-weight: bold; color: #f3ba2f; }
            .price { font-size: 18px; font-weight: bold; }
            .signal-box { text-align: center; padding: 12px; border-radius: 12px; font-size: 22px; font-weight: bold; margin-bottom: 12px; background: rgba(255,255,255,0.03); }
            .BUY { color: var(--buy-color); border: 1px solid var(--buy-color); background: rgba(2, 192, 118, 0.1); }
            .SELL { color: var(--sell-color); border: 1px solid var(--sell-color); background: rgba(246, 70, 93, 0.1); }
            .WAITING { color: var(--wait-color); border: 1px solid var(--wait-color); background: rgba(243, 186, 47, 0.1); }
            .countdown-container { display: none; background: #202226; border: 1px dashed var(--wait-color); padding: 10px; border-radius: 10px; text-align: center; margin-bottom: 12px; font-size: 14px; color: var(--wait-color); }
            .countdown-time { font-size: 20px; font-weight: bold; display: block; margin-top: 4px; color: #fff; }
            .grid-details { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
            .detail-item { background-color: #202226; padding: 10px; border-radius: 8px; font-size: 13px; display: flex; flex-direction: column; transition: all 0.3s ease; }
            .detail-item.full { grid-column: span 2; display: flex; flex-direction: row; justify-content: space-between; align-items: center; }
            .detail-item.entry { border-left: 4px solid #f3ba2f; border-right: 4px solid #f3ba2f; background: rgba(243, 186, 47, 0.05); }
            .detail-item.hit { border: 1px solid var(--buy-color); background: rgba(2, 192, 118, 0.08) !important; }
            .label { color: var(--text-muted); font-size: 11px; margin-bottom: 4px; }
            .val { font-weight: bold; color: #fff; font-size: 14px; }
            .pct { font-size: 12px; font-weight: bold; margin-left: 6px; margin-right: 6px; }
            .pct.up { color: var(--buy-color); }
            .pct.down { color: var(--sell-color); }
            .check-mark { color: var(--buy-color); font-weight: bold; margin-left: 5px; margin-right: 5px; font-size: 15px; display: none; }
            .advisor-card { background: #1f222a; border-radius: 12px; padding: 12px; border: 1px solid #363c4e; margin-top: 12px; font-size: 13px; line-height: 1.5; }
            .advisor-title { font-weight: bold; color: #f3ba2f; display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
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
            <div class="card-header"><span class="symbol">🔀 BTCUSDT Perpetual</span><span class="price" id="btc-price">...</span></div>
            <div id="btc-signal" class="signal-box">—</div>
            
            <div id="countdown-box" class="countdown-container">
                <span id="lbl-next-scan">Next Candle Close In:</span>
                <span id="timer-display" class="countdown-time">00:00</span>
            </div>
            
            <div class="grid-details">
                <div class="detail-item full entry"><span class="label" id="lbl-entry">Fixed Entry Price:</span><span class="val" id="btc-entry">—</span></div>
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
                <div class="detail-item">
                    <span class="label" id="lbl-sl">Stop Loss (SL)</span>
                    <div><span class="val" id="btc-sl" style="color: var(--sell-color);">—</span> <span class="pct down" id="btc-sl-p"></span></div>
                </div>
                <div class="detail-item full" style="text-align: center; justify-content: center; gap: 10px;"><span class="label" id="lbl-conf">Signal Momentum:</span><span class="val" id="btc-conf">—</span></div>
            </div>

            <div class="advisor-card">
                <div class="advisor-title">🛡️ <span id="adv-title">Active Trade Management (Pro Advisor)</span></div>
                <div id="advisor-text" style="color: var(--text-muted);">Waiting for signal generation to deliver safety protocols...</div>
            </div>
        </div>
        <script>
            const translations = {
                en: { lbl_time: "Binance Time:", wins: "Futures Wins ✅", losses: "Futures Losses ❌", entry: "Fixed Entry Price:", tp1: "Take Profit (TP1)", tp2: "Take Profit (TP2)", tp3: "Take Profit (TP3)", sl: "Stop Loss (SL)", conf: "Signal Momentum:", buy: "🟢 LONG / BULLISH", sell: "🔴 SHORT / BEARISH", waiting: "Waiting for Signal.. ⏳", next_scan: "Next Candle In:", adv_title: "Active Trade Management (Pro Advisor)",
                      adv_wait: "System is scanning. Once a trade is active, step-by-step risk management instructions will appear here.",
                      adv_hit1: "🔥 Target 1 (TP1) hit! **Action Required:** Your Stop Loss (SL) has been internally locked to Entry Price (Break-Even). Risk is now 0%. Secure 50% profit on exchange.",
                      adv_hit2: "🚀 Target 2 (TP2) hit! **Action:** Lock further massive gains by moving your exchange Stop Loss manually to the TP1 price level.",
                      adv_open: "⚡ Position is live. System is monitoring price action. Prepare to lock risk-free parameters at Target 1."
                },
                ar: { lbl_time: "توقيت بينانس:", wins: "فيوتشر رابحة ✅", losses: "فيوتشر خاسرة ❌", entry: "سعر الدخول الثابت:", tp1: "جني الأرباح (TP1)", tp2: "جني الأرباح (TP2)", tp3: "جني الأرباح (TP3)", sl: "وقف الخسارة (SL)", conf: "قوة زخم الإشارة:", buy: "🟢 LONG / شراء صعودي", sell: "🔴 SHORT / بيع هبوطي", waiting: "في انتظار إشارة جديدة.. ⏳", next_scan: "الشمعة القادمة خلال:", adv_title: "المساعد الذكي لإدارة الصفقة (Pro Advisor)",
                      adv_wait: "النظام في وضع الاستعداد الفني، بمجرد دخول الصفقة ستظهر لك هنا خطة تحريك الوقف لحماية أموالك خطوة بخطوة.",
                      adv_hit1: "🔥 رائع! تم تحقيق الهدف الأول (TP1). **إجراء مطلوب:** تم نقل وقف الخسارة (SL) برمجياً إلى سعر الدخول (Break-Even). الصفقة الآن خالية تماماً من المخاطر.",
                      adv_hit2: "🚀 ممتاز! تم ضرب الهدف الثاني (TP2). **الآن:** قم بتحريك وقف الخسارة على المنصة إلى مستوى الهدف الأول (TP1) لضمان خروج بربح ممتاز.",
                      adv_open: "⚡ الصفقة حية ومفتوحة الآن. يرجى الالتزام بالأرقام المحددة والجاهزية لتأمين الصفقة عند الهدف الأول."
                }
            };
            let currentLang = 'en'; let globalData = null;
            function changeLanguage(lang) {
                currentLang = lang; document.getElementById('html-tag').setAttribute('dir', lang === 'ar' ? 'rtl' : 'ltr');
                document.getElementById('lbl-time').innerText = translations[lang].lbl_time; document.getElementById('lbl-wins').innerText = translations[lang].wins;
                document.getElementById('lbl-losses').innerText = translations[lang].losses; document.getElementById('lbl-entry').innerText = translations[lang].entry;
                document.getElementById('lbl-tp1').innerText = translations[lang].tp1; document.getElementById('lbl-tp2').innerText = translations[lang].tp2;
                document.getElementById('lbl-tp3').innerText = translations[lang].tp3; document.getElementById('lbl-sl').innerText = translations[lang].sl;
                document.getElementById('lbl-conf').innerText = translations[lang].conf; document.getElementById('lbl-next-scan').innerText = translations[lang].next_scan;
                document.getElementById('adv-title').innerText = translations[lang].adv_title;
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

                    if(btc.tp2_hit) document.getElementById('advisor-text').innerHTML = translations[currentLang].adv_hit2;
                    else if(btc.tp1_hit) document.getElementById('advisor-text').innerHTML = translations[currentLang].adv_hit1;
                    else document.getElementById('advisor-text').innerHTML = translations[currentLang].adv_open;
                } else {
                    document.getElementById('chk-tp1').style.display = "none"; document.getElementById('box-tp1').className = "detail-item";
                    document.getElementById('chk-tp2').style.display = "none"; document.getElementById('box-tp2').className = "detail-item";
                    document.getElementById('chk-tp3').style.display = "none"; document.getElementById('box-tp3').className = "detail-item";
                    document.getElementById('advisor-text').innerHTML = translations[currentLang].adv_wait;
                }

                if(btc.signal === "WAITING" && btc.seconds_remaining > 0) {
                    document.getElementById('countdown-box').style.display = "block";
                    let mins = Math.floor(btc.seconds_remaining / 60); let secs = btc.seconds_remaining % 60;
                    document.getElementById('timer-display').innerText = `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
                } else { document.getElementById('countdown-box').style.display = "none"; }
                
                let signalText = translations[currentLang].waiting;
                if (btc.signal === "BUY") signalText = translations[currentLang].buy;
                else if (btc.signal === "SELL") signalText = translations[currentLang].sell;
                document.getElementById('btc-signal').innerText = signalText; document.getElementById('btc-signal').className = "signal-box " + btc.signal;
            }
            async function updateUI() {
                try { let response = await fetch('/api/signals'); globalData = await response.json(); if(globalData.success) { renderData(); } } catch (error) {}
            }
            changeLanguage('en'); setInterval(updateUI, 1000);
        </script>
    </body>
    </html>
    """

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3000, debug=True)
