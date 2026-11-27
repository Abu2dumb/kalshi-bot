import math, time, json, sqlite3, requests, pandas as pd, streamlit as st, os, base64
import streamlit.components.v1 as components
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization

try:
    from scipy.stats import t as _t
except Exception:
    _t = None

# ==========================================
# CONFIGURATION & AUTHENTICATION
# ==========================================
KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID")
PEM = os.environ.get("KALSHI_PEM")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
KALSHI = "https://api.elections.kalshi.com"

if not KALSHI_KEY_ID or not PEM:
    st.error("Missing KALSHI_KEY_ID or KALSHI_PEM environment variables in Fly.io Secrets.")
    st.stop()

key = serialization.load_pem_private_key(PEM.encode(), password=None)

def get_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path}".encode()
    sig = key.sign(msg)
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
        "ticker": ticker,
        "client_order_id": f"bot-{int(time.time()*1000)}",
        "side": api_side,
        "action": action,
        "count": f"{int(count)}.00",
        "price": f"{price_dollars:.4f}",
        "time_in_force": tif,
        "self_trade_prevention_type": "taker_at_cross"
    }
    headers = get_headers("POST", path)
    res = requests.post(f"{KALSHI}{path}", json=payload, headers=headers, timeout=10)
    if res.status_code != 201:
        print(f"Order Error {res.status_code}: {res.text}")
    return res.status_code, res.text

def get_kalshi(path, **params):
    try:
        r = requests.get(f"{KALSHI}{path}", params=params, headers=get_headers("GET", path), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}

# ==========================================
# DISCORD NOTIFICATION FUNCTION
# ==========================================
def send_discord_message(title, description, color=0x22c55e, tag_everyone=False):
    if not DISCORD_WEBHOOK_URL: return
    cooldown = st.session_state.get("discord_cooldown", 0)
    if time.time() < cooldown: return

    try:
        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Kalshi 15-Min Bot"}
            }]
        }
        if tag_everyone:
            payload["content"] = "@everyone"
            payload["allowed_mentions"] = {"parse": ["everyone"]}

        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        if r.status_code == 429:
            retry_after = r.json().get("retry_after", 5)
            st.session_state["discord_cooldown"] = time.time() + retry_after
    except Exception:
        pass

# ==========================================
# UI STYLING
# ==========================================
st.set_page_config(page_title="Kalshi 15-Min Desk", page_icon="⚡", layout="wide")
st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@500;700&display=swap');
#MainMenu, footer, header {visibility:hidden}
.stApp {background:radial-gradient(1200px 600px at 10% -10%,#16223b 0%,#0a0e17 55%);font-family:'Inter',sans-serif}
.block-container {padding-top:1.2rem;max-width:1500px}
.top {display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.title {font-size:1.7rem;font-weight:800;color:#e8eefc}
.sub {color:#7d8aa5;font-size:.85rem}.live {color:#22c55e;font-weight:700;font-size:.8rem;text-align:right}
.stats {display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:18px}
.stat {background:#101728;border:1px solid #1f2a40;border-radius:14px;padding:14px 18px}
.stat .l {color:#7d8aa5;font-size:.72rem;text-transform:uppercase;letter-spacing:.08em}
.stat .v {color:#e8eefc;font-size:1.5rem;font-weight:800;font-family:'JetBrains Mono',monospace}
.stat .s {color:#5b6885;font-size:.7rem}
.card {background:linear-gradient(160deg,#131a29,#0d1320);border:1px solid #1f2a40;border-radius:16px;padding:18px;margin-bottom:16px;box-shadow:0 8px 24px rgba(0,0,0,.35)}
.hdr {display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.sym {font-size:1.25rem;font-weight:800;color:#e8eefc}.nm {color:#7d8aa5;font-size:.8rem;margin-left:8px}
.pill {background:#1a2338;color:#9fb0d0;border-radius:999px;padding:3px 10px;font-size:.75rem;font-family:'JetBrains Mono',monospace}
.banner {border-radius:12px;padding:12px;text-align:center;font-weight:800;font-size:1.05rem}
.plan {text-align:center;color:#9fb0d0;font-size:.75rem;margin-top:6px;line-height:1.4}
.yes {background:linear-gradient(90deg,#0b3d2a,#0f5a3b);color:#4ade80;border:1px solid #166534}
.no {background:linear-gradient(90deg,#4a1220,#661a2c);color:#fb7185;border:1px solid #9f1239}
.scalp {background:linear-gradient(90deg,#2e1065,#4c1d95);color:#c4b5fd;border:1px solid #7c3aed}
.wait {background:#3a2a08;color:#fbbf24;border:1px solid #92400e}
.none {background:#161d2e;color:#7d8aa5;border:1px solid #232f48}
.price {font-family:'JetBrains Mono',monospace;font-size:1.8rem;font-weight:700;color:#e8eefc;margin-top:12px}
.up {color:#22c55e;font-weight:700}.dn {color:#ef4444;font-weight:700}.gold {color:#f5c542;font-weight:700}
.grid {display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}
.k {color:#7d8aa5;font-size:.7rem;text-transform:uppercase;letter-spacing:.08em}
.val {font-family:'JetBrains Mono',monospace;font-size:1.0rem;color:#e8eefc;font-weight:600}
.bl {color:#7d8aa5;font-size:.7rem;margin:12px 0 4px;text-transform:uppercase;letter-spacing:.08em;display:flex;justify-content:space-between}
.tug {position:relative;display:flex;height:32px;border-radius:9px;overflow:hidden;font-family:'JetBrains Mono',monospace;font-weight:700;font-size:.8rem}
.tu {background:linear-gradient(90deg,#15803d,#22c55e);color:#04210f;display:flex;align-items:center;padding-left:10px}
.td {flex:1;background:linear-gradient(90deg,#dc2626,#991b1b);color:#fee2e2;display:flex;align-items:center;justify-content:flex-end;padding-right:10px}
.mid {position:absolute;left:50%;top:0;bottom:0;width:2px;background:#ffffffaa}
.cm {position:relative;height:12px;border-radius:8px;background:linear-gradient(90deg,#ef4444,#f5c542,#22c55e)}
.cm i {position:absolute;top:-4px;width:6px;height:20px;border-radius:3px;background:#fff;box-shadow:0 0 6px #000}
.chips {display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
.chip {font-size:.7rem;padding:3px 8px;border-radius:6px;background:#161d2e;font-family:'JetBrains Mono',monospace}
.cash {margin-top:10px;border-radius:12px;padding:12px;font-size:.82rem;background:#101728;border:1px solid #1f2a40;color:#9fb0d0;line-height:1.5}
.cash.go {background:#0b3d2a;border-color:#22c55e}.cash.cut {background:#4a1220;border-color:#ef4444}
.cash .act {font-size:1.02rem;font-weight:800}
.tb {height:4px;background:#1a2338;border-radius:4px;margin-top:14px}
.tb div {height:4px;border-radius:4px;background:linear-gradient(90deg,#f5c542,#fb923c)}
.foot {display:flex;justify-content:space-between;color:#5b6885;font-size:.72rem;margin-top:8px}
</style>""", unsafe_allow_html=True)

DB_FILE = "calls3.db"
db = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
db.execute("""CREATE TABLE IF NOT EXISTS pos(key TEXT PRIMARY KEY, ticker TEXT, asset TEXT, kind TEXT, side TEXT,
count INTEGER, entry REAL, peak REAL, close_time TEXT, status TEXT, exit REAL, pnl REAL, win INTEGER)""")
try:
    db.execute("ALTER TABLE pos ADD COLUMN count INTEGER")
except sqlite3.OperationalError:
    pass

db.execute("""CREATE TABLE IF NOT EXISTS preds(ticker TEXT PRIMARY KEY, asset TEXT, model REAL, kalshi REAL,
final REAL, close_time TEXT, result TEXT)""")

ASSETS = {
    "BTC": ("KXBTC15M", "BTC-USD", "COINBASE:BTCUSD", "Bitcoin"),
    "ETH": ("KXETH15M", "ETH-USD", "COINBASE:ETHUSD", "Ethereum"),
    "SOL": ("KXSOL15M", "SOL-USD", "COINBASE:SOLUSD", "Solana"),
    "XRP": ("KXXRP15M", "XRP-USD", "COINBASE:XRPUSD", "XRP"),
    "DOGE": ("KXDOGE15M", "DOGE-USD", "COINBASE:DOGEUSD", "Dogecoin"),
}
KRAKEN = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD", "DOGE": "XDGUSD"}

# ==========================================
# SIDEBAR SETTINGS
# ==========================================
MIN_EDGE = st.sidebar.slider("Minimum edge after fees (%)", -10, 15, -5) / 100
MIN_SECS = st.sidebar.slider("No new entries with less than (sec) left", 30, 300, 60)
PRICE_CAP = st.sidebar.slider("Max contract price for SAFE buys (%)", 55, 95, 90) / 100
SCALP_ON = st.sidebar.checkbox("Enable SCALP signals", True)
SCALP_SL = st.sidebar.slider("Scalp stop loss (%)", 4, 12, 6) / 100
BANKROLL = st.sidebar.number_input("Bankroll for stake suggestions ($)", 10, 100000, 100)
KFRAC = st.sidebar.slider("Kelly fraction (smaller = safer)", 0.05, 0.5, 0.25)
KCAP = st.sidebar.slider("Max stake per trade (% of bankroll)", 1, 10, 4) / 100
PAUSE_ON = st.sidebar.checkbox("Pause new calls after 3 losses in a row", True)

st.sidebar.markdown("---")
st.sidebar.header("🟢 Auto-Trading")
AUTO_TRADE_SAFE = st.sidebar.toggle("Auto-Trade SAFE signals", value=True)
AUTO_TRADE_SCALP = st.sidebar.toggle("Auto-Trade SCALP signals", value=True)
MAX_TRADE_USD = st.sidebar.slider("Max $ per trade", 1.0, 50.0, 2.0, step=1.0)

st.sidebar.markdown("---")
st.sidebar.header("💰 Auto-Sell Settings")
AUTO_SELL_ON = st.sidebar.toggle("Enable Auto-Selling", value=True)
STANDARD_PROFIT_TARGET = st.sidebar.slider("Standard Trade Profit Target (%)", 10, 100, 35) / 100
SELL_CONFIDENCE = st.sidebar.slider("Sell standard if confidence drops below (%)", 20, 80, 50) / 100

st.sidebar.markdown("---")
st.sidebar.header("💬 Discord Alerts")
DISCORD_TAG = st.sidebar.toggle("Tag @everyone in alerts", value=False)

if st.sidebar.button("Reset history"):
    db.execute("DELETE FROM pos"); db.execute("DELETE FROM preds"); db.commit(); st.session_state.pop("toasted", None)
    for k in list(st.session_state.keys()):
        if k.startswith("hold_sent_") or k.startswith("retest_sent_"):
            del st.session_state[k]

# ==========================================
# MATH & CORE LOGIC
# ==========================================
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

@st.cache_data(ttl=10)
def candles(product):
    c = requests.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                     params={"granularity": 60}, timeout=10).json()[:120]
    c = list(reversed(c))
    return [x[4] for x in c], [x[5] for x in c], c[-1][0]

@st.cache_data(ttl=4)
def live_price(asset, product):
    out = {}
    try: out["Coinbase"] = float(requests.get(f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=6).json()["price"])
    except Exception: pass
    if asset in KRAKEN:
        try:
            r = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": KRAKEN[asset]}, timeout=6).json()["result"]
            out["Kraken"] = float(list(r.values())[0]["c"][0])
        except Exception: pass
    return out

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
                        db.execute("UPDATE pos SET status='settled', exit=?, pnl=?, win=? WHERE key=?",
                                   (ex, (ex - entry) / entry * 100, int(won), key))
                db.commit()
        except Exception: pass

# ==========================================
# UI COMPONENTS
# ==========================================
def spark(vals, lo=None, hi=None, color="#9fb0d0", mid=None):
    if len(vals) < 2: return '<div class="sub">collecting data…</div>'
    lo = min(vals) if lo is None else lo; hi = max(vals) if hi is None else hi
    if mid is not None: lo, hi = min(lo, mid), max(hi, mid)
    rng = (hi - lo) or 1e-9
    pts = " ".join(f"{i*260/(len(vals)-1):.1f},{38-(v-lo)/rng*36:.1f}" for i, v in enumerate(vals))
    ml = ""
    if mid is not None:
        y = 38 - (mid - lo) / rng * 36
        ml = f'<line x1="0" x2="260" y1="{y:.1f}" y2="{y:.1f}" stroke="#f5c542" stroke-width="1" stroke-dasharray="4"/>'
    return (f'<svg viewBox="0 0 260 40" width="100%" height="40" preserveAspectRatio="none">{ml}'
            f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/></svg>')

def chip(n, v):
    c, a = ("#22c55e", "▲") if v > 0 else ("#ef4444", "▼") if v < 0 else ("#7d8aa5", "●")
    return f'<span class="chip" style="color:{c}">{n} {a}</span>'

def pos_html(p):
    kind = {"scalp": "SCALP", "early": "EARLY", "own": "OWN"}.get(p["kind"], "SAFE")
    head = f'{kind} {p["side"].upper()} @ {p["entry"]*100:.0f}¢'
    if p["status"] != "open":
        pn = p["pnl"] or 0
        return (f'<div class="cash"><div class="act">✅ CLOSED · {head}</div>Exit {p["exit"]*100:.0f}¢ · '
                f'<span class="{"up" if pn >= 0 else "dn"}">{pn:+.1f}%</span></div>')
    return (f'<div class="cash {p["cls"]}"><div class="act">{p["act"]}</div>{p["why"]}<br>{head} · exit now {p["bid"]*100:.0f}¢ · '
            f'<span class="{"up" if p["pnl"] >= 0 else "dn"}">{p["pnl"]:+.1f}%</span></div>')

def card(d):
    a = d["asset"]
    if "err" in d:
        return (f'<div class="card"><div class="hdr"><span class="sym">{a}</span><span class="pill">OFFLINE</span></div>'
                f'<div style="color:#fbbf24;font-weight:700;font-size:.95rem">{d["err"][:160]}</div></div>')
    mode, sd_ = d["mode"], d["side"]
    if mode == "safe": bc, bt = sd_, f'{"▲" if sd_ == "yes" else "▼"} SAFE BUY {sd_.upper()} @ {d["price"]*100:.0f}%'
    elif mode == "scalp": bc, bt = "scalp", f'⚡ SCALP {sd_.upper()} @ {d["price"]*100:.0f}%'
    elif mode == "retest": bc, bt = "wait", "⏳ WAIT FOR RETEST"
    elif mode == "conflict": bc, bt = "none", "⚠ NO TRADE"
    else: bc, bt = "none", "WAIT · NO TRADE YET"
    s, u, cf = int(d["secs"]), round(d["up"] * 100), d["conf"]
    cl = "HIGH" if cf >= 70 else "MEDIUM" if cf >= 45 else "LOW"
    c1, c2 = ("up" if d["pct"] >= 0 else "dn"), ("up" if d["chg5"] >= 0 else "dn")
    chips = ("".join(chip(k, v) for k, v in d["votes"].items()) + f'<span class="chip" style="color:#9fb0d0">RSI {d["rsi"]:.0f}</span>')
    ph = "".join(pos_html(p) for p in d["poss"]) or '<div class="cash">No open position · <b>nothing to cash out</b></div>'
    return "".join([
        f'<div class="card"><div class="hdr"><span><span class="sym">{a}</span><span class="nm">{ASSETS[a][3]}</span></span>',
        f'<span class="pill">⏱ {s//60}:{s%60:02d}</span></div>',
        f'<div class="banner {bc}">{bt}</div><div class="plan">{d["plan"]}</div>',
        f'<div class="price">${d["spot"]:,.4f}</div>',
        f'<div><span class="{c1}">{"▲" if d["pct"] >= 0 else "▼"} {d["pct"]:+.3f}% vs target</span> &nbsp; <span class="{c2}">{d["chg5"]:+.2f}% (5m)</span></div>',
        f'<div class="bl"><span>Up vs Down</span><span>model prediction</span></div>',
        f'<div class="tug"><div class="tu" style="width:{u}%">▲ UP {u}%</div><div class="td">DOWN {100-u}% ▼</div><div class="mid"></div></div>',
        f'<div class="bl"><span>Confidence</span><span>{cl} · {cf:.0f}%</span></div>',
        f'<div class="cm"><i style="left:calc({cf:.0f}% - 3px)"></i></div>',
        f'<div class="chips">{chips}</div>',
        f'<div class="bl"><span>1-min price vs target</span><span>last 15 min</span></div>',
        spark(d["closes"], color="#22c55e" if d["pct"] >= 0 else "#ef4444", mid=d["strike"]),
        f'<div class="grid"><div><div class="k">Kalshi target</div><div class="val gold">${d["strike"]:,.4f}</div></div>',
        f'<div><div class="k">Edge after fees</div><div class="val {"up" if d["edge"] > 0 else "dn"}">{d["edge"]:+.1f}%</div></div></div>',
        f'<div class="k">Kalshi says</div><div class="val">YES {d["k_yes"]*100:.0f}% / NO {100-d["k_yes"]*100:.0f}%</div>',
        ph,
        f'<div class="tb"><div style="width:{min(d["secs"]/9,100):.0f}%"></div></div>',
        f'<div class="foot"><span>{d["ticker"]}</span><span>{d["srcs"]}</span></div></div>'])

# ==========================================
# ANALYSIS LOGIC
# ==========================================
def analyse(asset):
    series, product = ASSETS[asset][0], ASSETS[asset][1]
    m = market(series)
    if not m: return {"asset": asset, "err": "No open market right now"}
    if m.get("floor_strike") is None: return {"asset": asset, "err": "Market has no target price"}
    strike, tk, ct = float(m["floor_strike"]), m["ticker"], m["close_time"]

    close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
    close_str = close_dt.isoformat()

    allc, vols, last_ts = candles(product)
    px = live_price(asset, product)
    spot = sum(px.values()) / len(px) if px else allc[-1]

    closed = allc[:-1]
    pct = (spot / strike - 1) * 100
    now_ts = time.time()
    tb = st.session_state.setdefault("ticks", {}).setdefault(asset, [])
    tb.append((now_ts, spot)); del tb[:-200]
    secs = max(close_dt.timestamp() - now_ts, 1)

    sig = sigma_ps(closed)
    mu_S, vf = settle_stat(spot, secs, tb, close_dt.timestamp())
    z = math.log(mu_S / strike) / math.sqrt(sig * sig * vf + 0.0003 ** 2)
    p_stat = cdf(z)

    votes, rsi_v = technicals(closed)

    # Cosmetic chart pattern label
    _bull = sum(1 for v in votes.values() if v == 1)
    _bear = sum(1 for v in votes.values() if v == -1)
    if _bull >= 4:
        pat = {"name": "Strong Bull Alignment", "state": "bullish"}
    elif _bear >= 4:
        pat = {"name": "Strong Bear Alignment", "state": "bearish"}
    elif _bull > _bear:
        pat = {"name": "Mild Bull Bias", "state": "bullish"}
    elif _bear > _bull:
        pat = {"name": "Mild Bear Bias", "state": "bearish"}
    else:
        pat = {"name": "Mixed / Chop", "state": "neutral"}

    w = 0.15 + 0.35 * min(secs / 900, 1)
    model = (1 - w) * p_stat + w * (0.5 + 0.25 * sum(votes.values()) / len(votes))

    ya, na, yb, nb = dollars(m, "yes_ask"), dollars(m, "no_ask"), dollars(m, "yes_bid"), dollars(m, "no_bid")
    k_yes = (yb + ya) / 2 if yb and ya else (ya or 0.5)
    up = 0.5 * model + 0.5 * k_yes

    ds = 1 if up >= 0.5 else -1
    side = "yes" if ds > 0 else "no"
    ask, bid = (ya, yb) if ds > 0 else (na, nb)
    ps = up if ds > 0 else 1 - up
    ready = ask is not None and bid is not None
    edge = (ps - ask - fee(ask)) if ready else -1
    agree = (z > 0) == (ds > 0) and (k_yes >= 0.5) == (ds > 0)

    conf = min(100, abs(up - 0.5) * 100 + (sum(votes.values()) / 5) * 30)

    mode, plan, price = "wait", "Waiting for price, charts and Kalshi to line up.", None
    just_bought = set()

    votes_text = " | ".join([f"{k}: {'▲ UP' if v == 1 else '▼ DOWN' if v == -1 else '● NEUT'}" for k, v in votes.items()])
    rsi_status = "Overbought" if rsi_v > 70 else "Oversold" if rsi_v < 30 else "Neutral"

    # 1. SAFE SIGNAL
    if ready and secs >= MIN_SECS and edge >= MIN_EDGE and agree:
        mode, price = "safe", ask
        plan = f"Max pay {(ps - MIN_EDGE - fee(ps))*100:.0f}% · target {min(0.93, ask + 0.15)*100:.0f}%"

        if AUTO_TRADE_SAFE:
            existing = db.execute("SELECT 1 FROM pos WHERE key=? AND status='open'", (tk + mode,)).fetchone()
            if not existing:
                contracts = int(MAX_TRADE_USD // ask) if ask else 0
                if contracts >= 1:
                    status, resp = place_order(tk, side, contracts, ask, action="buy")
                    if status == 201:
                        just_bought.add(tk + mode)
                        st.toast(f"✅ {asset}: AUTO-BOUGHT SAFE {contracts}x {side.upper()}", icon="💰")
                        if asset == "BTC":
                            direction = "UP" if up >= 0.5 else "DOWN"
                            sure_text = "YES, I AM SURE" if conf >= 70 else "NOT SURE"
                            target_px = min(0.93, ask + 0.15)
                            desc = (
                                f"**BUY** {contracts}x **{side.upper()}** @ **{ask*100:.0f}¢** ({ask*100:.0f}%)\n"
                                f"**Cash out target:** {target_px*100:.0f}¢ ({target_px*100:.0f}%)\n\n"
                                f"**Prediction:** `{direction}`\n"
                                f"**Confidence:** `{conf:.0f}%` ({sure_text})\n"
                                f"**Chart Pattern:** `{pat['name']}` ({pat['state']})\n"
                                f"**RSI:** `{rsi_v:.1f}` ({rsi_status})\n"
                                f"**Technicals:** {votes_text}"
                            )
                            send_discord_message("🟢 BTC SAFE BUY", desc, 0x22c55e, tag_everyone=DISCORD_TAG)

                        db.execute("INSERT OR IGNORE INTO pos(key,ticker,asset,kind,side,count,entry,peak,close_time,status) VALUES(?,?,?,?,?,?,?,?,?,'open')",
                                   (tk + mode, tk, asset, mode, side, contracts, ask, ask, close_str))
                        db.commit()
                    else:
                        st.toast(f"❌ {asset}: Order failed {status}", icon="🚨")

    # 2. SCALP SIGNAL
    elif ready and SCALP_ON and secs >= 120 and abs(z) >= 0.3:
        if (z > 0 and rsi_v < 40) or (z < 0 and rsi_v > 60):
            mode, price = "scalp", ask
            scalp_target = ask + 0.06
            scalp_stop = ask - SCALP_SL
            plan = f"Quick bounce trade · target {scalp_target*100:.0f}% · stop {scalp_stop*100:.0f}%"

            if AUTO_TRADE_SCALP:
                existing = db.execute("SELECT 1 FROM pos WHERE key=? AND status='open'", (tk + mode,)).fetchone()
                if not existing:
                    contracts = int(MAX_TRADE_USD // ask) if ask else 0
                    if contracts >= 1:
                        status, resp = place_order(tk, side, contracts, ask, action="buy")
                        if status == 201:
                            just_bought.add(tk + mode)
                            st.toast(f"⚡ {asset}: AUTO-BOUGHT SCALP {contracts}x {side.upper()}", icon="⚡")
                            if asset == "BTC":
                                direction = "UP" if up >= 0.5 else "DOWN"
                                sure_text = "YES, I AM SURE" if conf >= 70 else "NOT SURE"
                                desc = (
                                    f"⚡ **SCALP BUY INCOMING**\n\n"
                                    f"**Buy at:** {ask*100:.0f}¢ ({ask*100:.0f}%)\n"
                                    f"**Cash out at:** {scalp_target*100:.0f}¢ ({scalp_target*100:.0f}%) — +6¢ target\n"
                                    f"**Stop loss:** {scalp_stop*100:.0f}¢ ({scalp_stop*100:.0f}%) — -{SCALP_SL*100:.0f}¢\n\n"
                                    f"Bought **{contracts}x {side.upper()}** @ **{ask*100:.0f}¢**\n"
                                    f"**Prediction:** `{direction}`\n"
                                    f"**Confidence:** `{conf:.0f}%` ({sure_text})\n"
                                    f"**Chart Pattern:** `{pat['name']}` ({pat['state']})\n"
                                    f"**RSI:** `{rsi_v:.1f}` ({rsi_status})\n"
                                    f"**Technicals:** {votes_text}"
                                )
                                send_discord_message("⚡ BTC SCALP BUY INCOMING", desc, 0x8b5cf6, tag_everyone=DISCORD_TAG)

                            db.execute("INSERT OR IGNORE INTO pos(key,ticker,asset,kind,side,count,entry,peak,close_time,status) VALUES(?,?,?,?,?,?,?,?,?,'open')",
                                       (tk + mode, tk, asset, mode, side, contracts, ask, ask, close_str))
                            db.commit()
                        else:
                            st.toast(f"❌ {asset}: Scalp Order failed {status}", icon="🚨")

    # 3. WAIT FOR RETEST — direction agrees but edge not good enough yet
    elif ready and agree and edge < MIN_EDGE:
        mode, price = "retest", ask
        plan = f"Direction {'UP' if ds > 0 else 'DOWN'} · edge {edge*100:+.1f}% < {MIN_EDGE*100:+.1f}% · wait for better entry"

        if asset == "BTC":
            retest_key = f"retest_sent_{asset}"
            if not st.session_state.get(retest_key):
                direction = "UP" if ds > 0 else "DOWN"
                desc = (
                    f"**WAIT FOR RETEST** ⏳\n\n"
                    f"**Direction:** `{direction}` (model & Kalshi agree)\n"
                    f"**YES ask:** {ask*100:.0f}¢\n"
                    f"**Current edge:** {edge*100:+.1f}% (need {MIN_EDGE*100:+.1f}%)\n\n"
                    f"**Confidence:** `{conf:.0f}%`\n"
                    f"**Chart Pattern:** `{pat['name']}` ({pat['state']})\n"
                    f"**RSI:** `{rsi_v:.1f}` ({rsi_status})\n"
                    f"**Technicals:** {votes_text}\n\n"
                    f"_Will fire as **SAFE BUY** when edge improves._"
                )
                send_discord_message("⏳ BTC WAIT FOR RETEST", desc, 0xfbbf24, tag_everyone=DISCORD_TAG)
                st.session_state[retest_key] = True

    if mode != "retest":
        st.session_state[f"retest_sent_{asset}"] = False

    # --- MANAGE POSITIONS & CASH OUT ---
    poss = []
    for key, kind, s, c_count, entry, peak, status, ex, pn in db.execute(
            "SELECT key,kind,side,count,entry,peak,status,exit,pnl FROM pos WHERE ticker=?", (tk,)).fetchall():
        if status != "open":
            poss.append(dict(kind=kind, side=s, entry=entry, status=status, exit=ex, pnl=pn)); continue

        b = yb if s == "yes" else nb
        if b is None: continue

        pnl = (b - entry) / entry * 100
        p_side = up if s == "yes" else 1 - up

        should_sell = False
        sell_reason = ""

        if AUTO_SELL_ON:
            if kind == "scalp":
                if pnl > 1.0:
                    should_sell = True
                    sell_reason = f"Scalp Profit Secured (+{pnl:.1f}%)"
            else:
                if pnl >= (STANDARD_PROFIT_TARGET * 100):
                    should_sell = True
                    sell_reason = f"Standard Profit Target Hit ({pnl:.1f}%)"
                elif pnl > 0 and p_side < SELL_CONFIDENCE:
                    should_sell = True
                    sell_reason = "Confidence dropped while in profit"

        if should_sell:
            status_sell, resp = place_order(tk, s, c_count or 1, b, action="sell")
            if status_sell == 201:
                st.toast(f"💰 {asset}: AUTO-SOLD {kind.upper()} {c_count}x {s.upper()} · {sell_reason}", icon="💰")
                if asset == "BTC":
                    direction = "UP" if up >= 0.5 else "DOWN"
                    sure_text = "YES, I AM SURE" if conf >= 70 else "NOT SURE"
                    desc = (
                        f"**SELL / CASH OUT AT PROFIT** 💰\n\n"
                        f"**Bought at:** {entry*100:.0f}¢ ({entry*100:.0f}%)\n"
                        f"**Cash out at:** {b*100:.0f}¢ ({b*100:.0f}%)\n"
                        f"**Profit:** `+{pnl:.1f}%`\n"
                        f"**Reason:** {sell_reason}\n\n"
                        f"Sold **{c_count}x {s.upper()}** @ **{b*100:.0f}¢**\n"
                        f"**Prediction:** `{direction}`\n"
                        f"**Confidence:** `{conf:.0f}%` ({sure_text})\n"
                        f"**Chart Pattern:** `{pat['name']}` ({pat['state']})\n"
                        f"**RSI:** `{rsi_v:.1f}` ({rsi_status})\n"
                        f"**Technicals:** {votes_text}"
                    )
                    send_discord_message("💰 BTC CASH OUT AT PROFIT", desc, 0x10b981, tag_everyone=DISCORD_TAG)

                db.execute("UPDATE pos SET status='closed', exit=?, pnl=?, win=1 WHERE key=?", (b, pnl, key))
                db.commit()
                poss.append(dict(kind=kind, side=s, entry=entry, status="closed", exit=b, pnl=pnl))
                st.session_state.pop(f"hold_sent_{key}", None)
                continue
            else:
                st.toast(f"❌ {asset}: Auto-sell failed {status_sell}", icon="🚨")

        if kind == "scalp":
            if b >= entry + 0.06: a = ("💰 CASH OUT NOW", "go", "Scalp target hit.")
            elif b <= entry - SCALP_SL: a = ("✂️ STOP OUT", "cut", "Scalp failed.")
            else: a = ("✋ HOLD", "", "Waiting for target.")
        else:
            if p_side < 0.35 and b > 0.05: a = ("✂️ CUT LOSS", "cut", "Model favors opposite side.")
            elif pnl >= (STANDARD_PROFIT_TARGET * 100): a = ("💰 CASH OUT NOW", "go", "Profit target hit.")
            elif pnl > 0 and p_side < SELL_CONFIDENCE: a = ("💰 SELL NOW", "go", "Confidence dropped below sell threshold.")
            else: a = ("✋ HOLD", "", f"Holding (~{p_side*100:.0f}%) beats selling.")

        if asset == "BTC" and key not in just_bought and a[0] == "✋ HOLD":
            hold_key = f"hold_sent_{key}"
            if not st.session_state.get(hold_key):
                direction = "UP" if p_side >= 0.5 else "DOWN"
                desc = (
                    f"**HOLD** ✋\n\n"
                    f"Holding **{c_count or 1}x {s.upper()}** @ **{entry*100:.0f}¢** ({entry*100:.0f}%)\n"
                    f"**Current bid:** {b*100:.0f}¢ ({b*100:.0f}%)\n"
                    f"**Unrealized:** `{pnl:+.1f}%`\n\n"
                    f"**Why hold:** {a[2]}\n"
                    f"**Prediction:** `{direction}`\n"
                    f"**Confidence:** `{conf:.0f}%`\n"
                    f"**Chart Pattern:** `{pat['name']}` ({pat['state']})\n"
                    f"**RSI:** `{rsi_v:.1f}` ({rsi_status})"
                )
                send_discord_message("✋ BTC HOLD", desc, 0x9fb0d0, tag_everyone=DISCORD_TAG)
                st.session_state[hold_key] = True

        poss.append(dict(kind=kind, side=s, entry=entry, status="open", bid=b, act=a[0], cls=a[1], why=a[2], pnl=pnl))

    return dict(asset=asset, ticker=tk, spot=spot, strike=strike, pct=pct, chg5=(spot / allc[-6] - 1) * 100,
                secs=secs, k_yes=k_yes, up=up, edge=edge * 100, mode=mode, side=side, price=price, plan=plan,
                conf=conf, votes=votes, rsi=rsi_v, poss=poss, srcs="+".join(px) or "Coinbase", closes=allc[-15:])

# ==========================================
# LIVE DASHBOARD — refreshes every 15s
# ==========================================
@st.fragment(run_every=15)
def live():
    grade()

    res = []
    for a in ASSETS:
        try: res.append(analyse(a))
        except Exception as e: res.append({"asset": a, "err": str(e)[:160]})

    n, wins, avg = db.execute("SELECT COUNT(*), COALESCE(SUM(win),0), COALESCE(AVG(pnl),0) FROM pos WHERE status!='open'").fetchone()
    op = db.execute("SELECT COUNT(*) FROM pos WHERE status='open'").fetchone()[0]
    wr = f"{wins/n*100:.1f}%" if n else "—"
    now = datetime.now(timezone.utc)

    auto_color = "#22c55e" if AUTO_TRADE_SAFE else "#ef4444"
    auto_text = "ON" if AUTO_TRADE_SAFE else "OFF"

    st.markdown(
        f"""
        <div class="top">
            <div>
                <div class="title">⚡ Kalshi 15-Min Crypto Desk</div>
                <div class="sub">Settlement-aware model · multi-exchange price · self-check · auto-trading enabled</div>
            </div>
            <div class="live">● LIVE · {now.strftime("%H:%M:%S")} UTC<br><span class="sub">refresh every 15s</span></div>
        </div>
        <div class="stats">
            <div class="stat"><div class="l">Win rate</div><div class="v">{wr}</div></div>
            <div class="stat"><div class="l">Record</div><div class="v">{int(wins)} / {n-int(wins)}</div></div>
            <div class="stat"><div class="l">Avg result</div><div class="v">{avg:+.1f}%</div></div>
            <div class="stat"><div class="l">Open pos</div><div class="v">{op}</div></div>
            <div class="stat"><div class="l">Auto-Trade</div><div class="v" style="color:{auto_color}">{auto_text}</div></div>
        </div>
        """,
        unsafe_allow_html=True
    )

    cols = st.columns(3)
    for i, d in enumerate(res):
        with cols[i % 3]:
            st.markdown(card(d), unsafe_allow_html=True)

live()
