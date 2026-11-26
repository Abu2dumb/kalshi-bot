import math, time, json, sqlite3, requests, os, base64, traceback
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization
from scipy.stats import t as _t

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

_last_discord_time = 0
def send_discord(title, desc, color, tag=False):
    global _last_discord_time
    if not DISCORD_WEBHOOK_URL: return
    if time.time() - _last_discord_time < 2: return 
    try:
        payload = {
            "embeds": [{"title": title, "description": desc, "color": color, "timestamp": datetime.now(timezone.utc).isoformat()}],
            "allowed_mentions": {"parse": ["everyone"]} if tag else {"parse": []}
        }
        if tag: payload["content"] = "@everyone"
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        _last_discord_time = time.time()
    except: pass

def cdf(x, df=5):
    if _t is not None: return float(_t.cdf(x * math.sqrt(df / (df - 2)), df))
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def fee(p): return 0.07 * p * (1 - p)

def ema(v, n):
    k, out = 2 / (n + 1), [v[0]]
    for x in v[1:]: out.append(x * k + out[-1] * (1 - k))
    return out

def rsi(c, n=14):
    g = [max(b - a, 0) for a, b in zip(c, c[1:])][-n:]
    l = [max(a - b, 0) for a, b in zip(c, c[1:])][-n:]
    ag, al = sum(g) / n, sum(l) / n
    return 100 if al == 0 else 100 - 100 / (1 + ag / al)

def technicals(closes):
    e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
    macd = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    hist = macd[-1] - ema(macd, 9)[-1]
    r = rsi(closes); mom = closes[-1] / closes[-6] - 1
    c5 = closes[::-1][::5][::-1]
    htf = (1 if ema(c5, 9)[-1] > ema(c5, 21)[-1] else -1) if len(c5) >= 22 else 0
    votes = {"EMA": 1 if e9 > e21 else -1, "RSI": 1 if r > 55 else -1 if r < 45 else 0,
             "MACD": 1 if hist > 0 else -1, "MOM": 1 if mom > 0 else -1, "5M": htf}
    return votes, r

def sigma_ps(closes):
    rets = [math.log(b / a) for a, b in zip(closes[-61:], closes[-60:])]
    var = rets[0] ** 2
    for r in rets[1:]: var = 0.94 * var + 0.06 * r * r
    slow = math.sqrt(sum(r * r for r in rets) / len(rets))
    return max((0.5 * math.sqrt(var) + 0.5 * slow) / math.sqrt(60), 1e-7)

def settle_stat(S, secs, ticks, close_ts):
    L = 60.0
    if secs > L: return S, (secs - L) + L / 3
    real = [p for t, p in ticks if t >= close_ts - L]
    R = sum(real) / len(real) if real else S
    return ((L - secs) * R + secs * S) / L, max((secs / L) ** 2 * secs / 3, 0.5)

def market(series):
    ms = get_kalshi("/trade-api/v2/markets", series_ticker=series, status="open", limit=20).get("markets", [])
    now = datetime.now(timezone.utc)
    ms = [m for m in ms if datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) > now]
    return min(ms, key=lambda m: m["close_time"]) if ms else None

def dollars(m, k):
    v = m.get(f"{k}_dollars")
    if v not in (None, ""): return float(v)
    v = m.get(k)
    return float(v) / 100 if v not in (None, "") else None

def candles(product):
    try:
        c = requests.get(f"https://api.exchange.coinbase.com/products/{product}/candles", params={"granularity": 60}, timeout=10).json()[:120]
        c = list(reversed(c))
        return [x[4] for x in c], [x[5] for x in c]
    except: return [], []

def live_price(asset, product):
    out = {}
    try: out["Coinbase"] = float(requests.get(f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=6).json()["price"])
    except Exception: pass
    if asset == "BTC":
        try:
            r = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"}, timeout=6).json()["result"]
            out["Kraken"] = float(list(r.values())[0]["c"][0])
        except Exception: pass
    return list(out.values())

db = sqlite3.connect("calls3.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS pos(key TEXT PRIMARY KEY, ticker TEXT, asset TEXT, kind TEXT, side TEXT,
count INTEGER, entry REAL, peak REAL, close_time TEXT, status TEXT, exit REAL, pnl REAL, win INTEGER)""")
db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL)")
db.execute("""CREATE TABLE IF NOT EXISTS preds(ticker TEXT PRIMARY KEY, asset TEXT, model REAL, kalshi REAL,
final REAL, close_time TEXT, result TEXT)""")

def get_s(key, default):
    res = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return float(res[0]) if res else default

def get_wl():
    n, wins = db.execute("SELECT COUNT(*), COALESCE(SUM(win),0) FROM pos WHERE status!='open'").fetchone()
    return f"**Record:** {int(wins)}W / {int(n-wins)}L"

def grade():
    now = datetime.now(timezone.utc).isoformat()
    tks = {r[0] for r in db.execute("SELECT ticker FROM pos WHERE status='open' AND close_time < ?", (now,)).fetchall()}
    for t in list(tks)[:10]:
        try:
            m = get_kalshi(f"/trade-api/v2/markets/{t}").get("market", {})
            res = m.get("result")
            if res in ("yes", "no", "void"):
                for key, side, entry in db.execute("SELECT key,side,entry FROM pos WHERE ticker=? AND status='open'", (t,)).fetchall():
                    if res == "void":
                        db.execute("UPDATE pos SET status='settled', exit=?, pnl=0, win=0 WHERE key=?", (entry, key))
                    else:
                        won = res == side
                        ex = 1.0 if won else 0.0
                        db.execute("UPDATE pos SET status='settled', exit=?, pnl=?, win=? WHERE key=?", (ex, (ex - entry) / entry * 100, int(won), key))
                db.commit()
        except Exception as e:
            pass

def run_bot():
    print("Worker started running 24/7.")
    last_discord = 0
    assets = {
        "BTC": ("KXBTC15M", "BTC-USD"), "ETH": ("KXETH15M", "ETH-USD"), 
        "SOL": ("KXSOL15M", "SOL-USD"), "XRP": ("KXXRP15M", "XRP-USD"), "DOGE": ("KXDOGE15M", "DOGE-USD")
    }
    
    while True:
        try:
            grade()
            
            # Read all sliders from database
            MIN_EDGE = get_s("min_edge", -0.05)
            MIN_SECS = get_s("min_secs", 60)
            PRICE_CAP = get_s("price_cap", 0.90)
            SCALP_ON = get_s("scalp_on", 1.0) == 1.0
            SCALP_SL = get_s("scalp_sl", -0.06)
            MAX_TRADE_USD = get_s("max_trade_usd", 2.0)
            AUTO_SAFE = get_s("auto_safe", 1.0) == 1.0
            AUTO_SCALP = get_s("auto_scalp", 1.0) == 1.0
            AUTO_SELL = get_s("auto_sell", 1.0) == 1.0
            STD_TGT = get_s("profit_target", 0.35)
            SELL_CONF = get_s("sell_conf", 0.50)
            STOP_LOSS = get_s("stop_loss", -0.20)
            PAUSE_ON = get_s("pause_on", 1.0) == 1.0
            
            # Check if paused
            paused = False
            if PAUSE_ON:
                r = db.execute("SELECT win FROM pos WHERE status!='open' ORDER BY close_time DESC LIMIT 3").fetchall()
                if len(r) == 3 and all(x[0] == 0 for x in r):
                    paused = True

            for asset, (series, product) in assets.items():
                m = market(series)
                if not m: continue
                
                tk, ct, strike = m["ticker"], m["close_time"], float(m["floor_strike"])
                allc, vols = candles(product)
                px = live_price(asset, product)
                if not px or not allc: continue
                spot = sum(px) / len(px)

                votes, rsi_v = technicals(allc[:-1])
                ya, na, yb, nb = dollars(m, "yes_ask"), dollars(m, "no_ask"), dollars(m, "yes_bid"), dollars(m, "no_bid")
                if not all([ya, na, yb, nb]): continue
                
                k_yes = (yb + ya) / 2
                close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                secs = max(close_dt.timestamp() - time.time(), 1)
                
                sig = sigma_ps(allc[:-1])
                mu_S, vf = settle_stat(spot, secs, [], close_dt.timestamp())
                z = math.log(mu_S / strike) / math.sqrt(sig * sig * vf + 0.0003 ** 2)
                p_stat = cdf(z)
                w = 0.15 + 0.35 * min(secs / 900, 1)
                model = (1 - w) * p_stat + w * (0.5 + 0.25 * sum(votes.values()) / len(votes))
                up = 0.5 * model + 0.5 * k_yes
                
                ds = 1 if up >= 0.5 else -1
                side = "yes" if ds > 0 else "no"
                ask, bid = (ya, yb) if ds > 0 else (na, nb)
                ps = up if ds > 0 else 1 - up
                edge = (ps - ask - fee(ask))
                
                conf = min(100, abs(up - 0.5) * 100 + (sum(votes.values()) / 5)*30)
                
                # --- BUY LOGIC ---
                if not paused:
                    # SAFE BUY
                    if AUTO_SAFE and edge >= MIN_EDGE and ask <= PRICE_CAP and secs >= MIN_SECS:
                        if not db.execute("SELECT 1 FROM pos WHERE key=? AND status='open'", (tk+"safe",)).fetchone():
                            contracts = int(MAX_TRADE_USD // ask) if ask > 0 else 0
                            if contracts >= 1:
                                status, _ = place_order(tk, side, contracts, ask, action="buy")
                                if status == 201:
                                    db.execute("INSERT OR IGNORE INTO pos(key,ticker,asset,kind,side,count,entry,close_time,status) VALUES(?,?,?,?,?,?,?,?,'open')",
                                               (tk+"safe", tk, asset, "safe", side, contracts, ask, ct))
                                    db.commit()
                                    desc = f"Bought **{contracts}x {side.upper()}** @ **{ask*100:.0f}¢**\n**Edge:** `{edge*100:.1f}%`\n**Conf:** `{conf:.0f}%`\n{get_wl()}"
                                    send_discord(f"🟢 {asset} SAFE BUY", desc, 0x22c55e, tag=(asset=="BTC"))
                    
                    # SCALP BUY
                    elif SCALP_ON and AUTO_SCALP and secs >= 120 and abs(z) >= 0.3 and ask <= 0.78:
                        if (z > 0 and rsi_v < 40) or (z < 0 and rsi_v > 60):
                            if not db.execute("SELECT 1 FROM pos WHERE key=? AND status='open'", (tk+"scalp",)).fetchone():
                                contracts = int(MAX_TRADE_USD // ask) if ask > 0 else 0
                                if contracts >= 1:
                                    status, _ = place_order(tk, side, contracts, ask, action="buy")
                                    if status == 201:
                                        db.execute("INSERT OR IGNORE INTO pos(key,ticker,asset,kind,side,count,entry,close_time,status) VALUES(?,?,?,?,?,?,?,?,'open')",
                                                   (tk+"scalp", tk, asset, "scalp", side, contracts, ask, ct))
                                        db.commit()
                                        desc = f"Bought **{contracts}x {side.upper()}** @ **{ask*100:.0f}¢** (Scalp)\n**RSI:** `{rsi_v:.0f}`\n{get_wl()}"
                                        send_discord(f"⚡ {asset} SCALP BUY", desc, 0x8b5cf6, tag=(asset=="BTC"))

                # --- SELL / CASH OUT LOGIC ---
                for key, kind, s, c_count, entry in db.execute("SELECT key,kind,side,count,entry FROM pos WHERE ticker=? AND status='open'", (tk,)).fetchall():
                    b = yb if s == "yes" else nb
                    if not b: continue
                    pnl = (b - entry) / entry
                    p_side = up if s == "yes" else 1 - up
                    
                    sell = False
                    reason = ""
                    
                    if AUTO_SELL:
                        if kind == "scalp":
                            if pnl > 0.01: sell, reason = True, f"Scalp Profit (+{pnl*100:.1f}%)"
                            elif pnl <= SCALP_SL: sell, reason = True, f"Scalp Stop Out ({pnl*100:.1f}%)"
                        else:
                            if pnl >= STD_TGT: sell, reason = True, f"Target Hit (+{pnl*100:.1f}%)"
                            elif pnl <= STOP_LOSS: sell, reason = True, f"Stop Loss ({pnl*100:.1f}%)"
                            elif pnl > 0 and p_side < SELL_CONF: sell, reason = True, f"Confidence Dropped"
                    
                    if sell:
                        status, _ = place_order(tk, s, c_count, b, action="sell")
                        if status == 201:
                            won = 1 if pnl > 0 else 0
                            db.execute("UPDATE pos SET status='closed', exit=?, pnl=?, win=? WHERE key=?", (b, pnl*100, won, key))
                            db.commit()
                            desc = f"Sold **{c_count}x {s.upper()}** @ **{b*100:.0f}¢**\n**{reason}**\n{get_wl()}"
                            send_discord(f"💰 CASH OUT {asset}", desc, 0x10b981 if won else 0xef4444, tag=(asset=="BTC" and won))

            # 15-Second BTC Update
            if time.time() - last_discord > 15:
                btc_spot = live_price("BTC", "BTC-USD")
                if btc_spot:
                    allc, _ = candles("BTC-USD")
                    votes, r = technicals(allc[:-1])
                    send_discord("📊 BTC Update", f"**Price:** ${sum(btc_spot)/len(btc_spot):,.2f}\n**RSI:** {r:.0f}\n{get_wl()}", 0x3b82f6, tag=False)
                    last_discord = time.time()

        except Exception as e:
            print("Error:", e)
            traceback.print_exc()
        
        time.sleep(10)

if __name__ == "__main__":
    run_bot()
