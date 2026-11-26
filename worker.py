import math, time, json, sqlite3, requests, os, base64, traceback
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization

# --- CONFIG ---
KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID")
PEM = os.environ.get("KALSHI_PEM")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
KALSHI = "https://api.elections.kalshi.com"

key = serialization.load_pem_private_key(PEM.encode(), password=None)

def get_headers(method, path):
    ts = str(int(time.time() * 1000))
    sig = key.sign(f"{ts}{method}{path}".encode())
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json"
    }

def place_order(ticker, side, count, price_dollars, action="buy", tif="immediate_or_cancel"):
    api_side = "bid" if side == "yes" else "ask"
    path = "/trade-api/v2/portfolio/events/orders"
    payload = {
        "ticker": ticker, "client_order_id": f"bot-{int(time.time()*1000)}",
        "side": api_side, "action": action, "count": f"{int(count)}.00",
        "price": f"{price_dollars:.4f}", "time_in_force": tif,
        "self_trade_prevention_type": "taker_at_cross" 
    }
    res = requests.post(f"{KALSHI}{path}", json=payload, headers=get_headers("POST", path), timeout=10)
    return res.status_code, res.text

def get_kalshi(path, **params):
    try:
        r = requests.get(f"{KALSHI}{path}", params=params, headers=get_headers("GET", path), timeout=10)
        return r.json() if r.status_code == 200 else {}
    except: return {}

def send_discord(title, desc, color, tag=False):
    if not DISCORD_WEBHOOK_URL: return
    try:
        payload = {"embeds": [{"title": title, "description": desc, "color": color, "timestamp": datetime.now(timezone.utc).isoformat()}]}
        if tag: payload["content"] = "@everyone"; payload["allowed_mentions"] = {"parse": ["everyone"]}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except: pass

# --- MATH ---
def ema(v, n):
    k, out = 2 / (n + 1), [v[0]]
    for x in v[1:]: out.append(x * k + out[-1] * (1 - k))
    return out

def rsi(c, n=14):
    g = [max(b - a, 0) for a, b in zip(c, c[1:])][-n:]
    l = [max(a - b, 0) for a, b in zip(c, c[1:])][-n:]
    ag, al = sum(g) / n, sum(l) / n
    return 100 if al == 0 else 100 - 100 / (1 + ag / al)

def get_technicals(closes):
    e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
    macd = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    hist = macd[-1] - ema(macd, 9)[-1]
    r = rsi(closes); mom = closes[-1] / closes[-6] - 1
    return {"EMA": 1 if e9 > e21 else -1, "RSI": 1 if r > 55 else -1 if r < 45 else 0, "MACD": 1 if hist > 0 else -1, "MOM": 1 if mom > 0 else -1}, r

def get_candles(product):
    c = requests.get(f"https://api.exchange.coinbase.com/products/{product}/candles", params={"granularity": 60}, timeout=10).json()[:60]
    return [x[4] for x in reversed(c)]

def get_price(product):
    try: return float(requests.get(f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=5).json()["price"])
    except: return None

# --- DATABASE ---
db = sqlite3.connect("calls3.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS pos(key TEXT PRIMARY KEY, ticker TEXT, asset TEXT, kind TEXT, side TEXT,
count INTEGER, entry REAL, peak REAL, close_time TEXT, status TEXT, exit REAL, pnl REAL, win INTEGER)""")
db.execute("""CREATE TABLE IF NOT EXISTS preds(ticker TEXT PRIMARY KEY, asset TEXT, model REAL, kalshi REAL,
final REAL, close_time TEXT, result TEXT)""")

def grade():
    now = datetime.now(timezone.utc).isoformat()
    tks = {r[0] for r in db.execute("SELECT ticker FROM pos WHERE status='open' AND close_time < ?", (now,)).fetchall()}
    for t in list(tks):
        m = get_kalshi(f"/trade-api/v2/markets/{t}").get("market", {})
        res = m.get("result")
        if res in ("yes", "no", "void"):
            for key, side, entry in db.execute("SELECT key,side,entry FROM pos WHERE ticker=? AND status='open'", (t,)).fetchall():
                if res == "void": db.execute("UPDATE pos SET status='settled', exit=?, pnl=0, win=0 WHERE key=?", (entry, key))
                else:
                    won = res == side
                    ex = 1.0 if won else 0.0
                    db.execute("UPDATE pos SET status='settled', exit=?, pnl=?, win=? WHERE key=?", (ex, (ex - entry) / entry * 100, int(won), key))
            db.commit()

# --- TRADING ENGINE ---
def run_bot():
    print("Worker started.")
    last_discord = 0
    while True:
        try:
            grade()
            # Only trade BTC and ETH to keep it safe and focused
            for asset, series, product in [("BTC", "KXBTC15M", "BTC-USD"), ("ETH", "KXETH15M", "ETH-USD")]:
                m = get_kalshi("/trade-api/v2/markets", series_ticker=series, status="open", limit=5).get("markets", [])
                if not m: continue
                
                mkt = min([x for x in m if datetime.fromisoformat(x["close_time"].replace("Z", "+00:00")) > datetime.now(timezone.utc)], key=lambda x: x["close_time"], default=None)
                if not mkt: continue

                tk, ct, strike = mkt["ticker"], mkt["close_time"], float(mkt["floor_strike"])
                closes = get_candles(product)
                spot = get_price(product)
                if not spot: continue

                votes, r = get_technicals(closes)
                ya, na = mkt.get("yes_ask", 50) / 100, mkt.get("no_ask", 50) / 100
                
                # Math Logic
                z = math.log(spot / strike) / 0.002 # Simplified momentum math
                up = 0.5 + (z * 0.1) + (sum(votes.values()) * 0.05)
                up = max(0.01, min(0.99, up))
                conf = abs(up - 0.5) * 100 + 20

                side = "yes" if up >= 0.5 else "no"
                ask = ya if side == "yes" else na
                ps = up if side == "yes" else 1 - up
                
                # RISK MANAGEMENT (The fix for your losses)
                # 1. Price must be under 75c (prevents buying expensive favorites)
                # 2. We must have at least a 3% mathematical edge after Kalshi fees
                edge = ps - ask - (0.07 * ask * (1 - ask))
                if ask > 0.75 or edge < 0.03 or ask < 0.10:
                    continue

                # Check if already bought
                if db.execute("SELECT 1 FROM pos WHERE key=? AND status='open'", (tk,)).fetchone():
                    continue

                # Execute Buy
                status, _ = place_order(tk, side, 2, ask, action="buy") # Buying 2 contracts
                if status == 201:
                    db.execute("INSERT OR IGNORE INTO pos(key,ticker,asset,kind,side,count,entry,close_time,status) VALUES(?,?,?,?,?,?,?,?,'open')",
                               (tk+"auto", tk, asset, "auto", side, 2, ask, ct))
                    db.commit()
                    desc = f"Bought **2x {side.upper()}** @ **{ask*100:.0f}¢**\n\n**Confidence:** `{conf:.0f}%`\n**Edge:** `{edge*100:.1f}%`\n**RSI:** `{r:.0f}`"
                    send_discord(f"🟢 {asset} AUTO-BUY", desc, 0x22c55e, tag=(asset=="BTC"))

            # --- AUTO SELL / CASH OUT LOOP ---
            for key, tk, side, entry, count in db.execute("SELECT key,ticker,side,entry,count FROM pos WHERE status='open'").fetchall():
                m = get_kalshi(f"/trade-api/v2/markets/{tk}").get("market", {})
                if not m: continue
                
                bid = (m.get("yes_bid", 0) / 100) if side == "yes" else (m.get("no_bid", 0) / 100)
                if bid == 0: continue
                
                pnl = (bid - entry) / entry * 100
                
                # Sell if we made 30% profit OR if we lost 15% (Stop Loss)
                if pnl >= 30.0 or pnl <= -15.0:
                    status, _ = place_order(tk, side, count, bid, action="sell")
                    if status == 201:
                        won = 1 if pnl > 0 else 0
                        db.execute("UPDATE pos SET status='closed', exit=?, pnl=?, win=? WHERE key=?", (bid, pnl, won, key))
                        db.commit()
                        reason = f"Profit Target Hit (+{pnl:.1f}%)" if pnl > 0 else f"Stop Loss Triggered ({pnl:.1f}%)"
                        send_discord(f"💰 CASH OUT {tk}", f"Sold **{count}x {side.upper()}** @ **{bid*100:.0f}¢**\n**{reason}**", 0x10b981, tag=(tk.startswith("KXBTC") and pnl>0))

            # 15-Second Discord Update for BTC
            if time.time() - last_discord > 15:
                btc_m = get_kalshi("/trade-api/v2/markets", series_ticker="KXBTC15M", status="open", limit=1).get("markets", [{}])[0]
                if btc_m:
                    btc_spot = get_price("BTC-USD")
                    votes, r = get_technicals(get_candles("BTC-USD"))
                    if btc_spot:
                        send_discord("📊 BTC 15s Update", f"**Price:** ${btc_spot:,.2f}\n**RSI:** {r:.0f}\n**Technicals:** EMA({'▲' if votes['EMA']>0 else '▼'}) MACD({'▲' if votes['MACD']>0 else '▼'})", 0x3b82f6, tag=False)
                        last_discord = time.time()

        except Exception as e:
            print("Error in loop:", e)
            traceback.print_exc()
        
        time.sleep(10) # Wait 10 seconds before scanning again

if __name__ == "__main__":
    run_bot()
