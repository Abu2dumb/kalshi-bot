import math, time, json, sqlite3, os, base64
import requests, pandas as pd, streamlit as st
import streamlit.components.v1 as components
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

try:
    from scipy.stats import t as _t
except Exception:
    _t = None

# ========== MUST be the first Streamlit command ==========
st.set_page_config(page_title="Kalshi 15-Min Desk", page_icon="⚡", layout="wide")

# ---------- Auth: read from Fly secrets (environment variables) ----------
KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
KALSHI_PEM = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "")

if not KALSHI_KEY_ID or not KALSHI_PEM:
    st.error("Missing KALSHI_KEY_ID or KALSHI_PRIVATE_KEY_PEM. Set them via `fly secrets set`.")
    st.stop()

# Handle the `\n` that appears when setting via fly secrets
KALSHI_PEM = KALSHI_PEM.replace("\\n", "\n")
_private_key = serialization.load_pem_private_key(KALSHI_PEM.encode(), password=None)
assert isinstance(_private_key, ed25519.Ed25519PrivateKey), "Key must be Ed25519"

def kalshi_headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path.split('?')[0]}".encode()
    sig = _private_key.sign(msg)
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }

# ---------- Constants ----------
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ASSETS = {
    "BTC": ("KXBTC15M", "BTC-USD", "COINBASE:BTCUSD", "Bitcoin"),
    "ETH": ("KXETH15M", "ETH-USD", "COINBASE:ETHUSD", "Ethereum"),
    "SOL": ("KXSOL15M", "SOL-USD", "COINBASE:SOLUSD", "Solana"),
    "XRP": ("KXXRP15M", "XRP-USD", "COINBASE:XRPUSD", "XRP"),
    "DOGE": ("KXDOGE15M", "DOGE-USD", "COINBASE:DOGEUSD", "Dogecoin"),
}
KRAKEN = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD", "DOGE": "XDGUSD"}

# ---------- Sidebar (simplified to keep the structure) ----------
MIN_EDGE = st.sidebar.slider("Minimum edge after fees (%)", 1, 15, 3) / 100
MIN_SECS = st.sidebar.slider("Min seconds left for new entry", 30, 300, 90)
PRICE_CAP = st.sidebar.slider("Max contract price for SAFE buys (%)", 55, 90, 78) / 100
BANKROLL = st.sidebar.number_input("Bankroll ($)", 10, 100000, 100)
picked = st.sidebar.multiselect("Assets", list(ASSETS), default=["BTC", "ETH", "SOL"])

# ---------- DB (use /data to persist; see Fly volume note below) ----------
DB_DIR = os.environ.get("DB_DIR", "/data")
os.makedirs(DB_DIR, exist_ok=True)
db = sqlite3.connect(os.path.join(DB_DIR, "calls.db"), check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS pos(
    key TEXT PRIMARY KEY, ticker TEXT, asset TEXT, kind TEXT, side TEXT,
    entry REAL, peak REAL, close_time TEXT, status TEXT, exit REAL, pnl REAL, win INTEGER
)""")

# ---------- Helpers (cdf, fee, ema, rsi, sigma_ps, settle_stat, market, dollars) ----------
def cdf(x, df=5):
    if _t is not None:
        return float(_t.cdf(x * math.sqrt(df / (df - 2)), df))
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def fee(p): return 0.07 * p * (1 - p)

def ema(v, n):
    k, out = 2 / (n + 1), [v[0]]
    for x in v[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out

def rsi(c, n=14):
    g = [max(b - a, 0) for a, b in zip(c, c[1:])][-n:]
    l = [max(a - b, 0) for a, b in zip(c, c[1:])][-n:]
    ag, al = sum(g) / n, sum(l) / n
    return 100 if al == 0 else 100 - 100 / (1 + ag / al)

def sigma_ps(closes):
    rets = [math.log(b / a) for a, b in zip(closes[-61:], closes[-60:])]
    var = rets[0] ** 2
    for r in rets[1:]:
        var = 0.94 * var + 0.06 * r * r
    slow = math.sqrt(sum(r * r for r in rets) / len(rets))
    return max((0.5 * math.sqrt(var) + 0.5 * slow) / math.sqrt(60), 1e-7)

def settle_stat(S, secs, ticks, close_ts):
    L = 60.0
    if secs > L:
        return S, (secs - L) + L / 3
    real = [p for t, p in ticks if t >= close_ts - L]
    R = sum(real) / len(real) if real else S
    return ((L - secs) * R + secs * S) / L, max((secs / L) ** 2 * secs / 3, 0.5)

def market(series):
    r = requests.get(f"{KALSHI}/markets", params={"series_ticker": series, "status": "open", "limit": 20},
                     headers=kalshi_headers("GET", f"/trade-api/v2/markets"), timeout=10)
    r.raise_for_status()
    ms = r.json().get("markets", [])
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
    r = requests.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                     params={"granularity": 60}, timeout=10)
    r.raise_for_status()
    c = list(reversed(r.json()[:120]))
    return [x[4] for x in c], [x[5] for x in c], c[-1][0]

@st.cache_data(ttl=4)
def live_price(asset, product):
    out = {}
    try:
        out["Coinbase"] = float(requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=6).json()["price"])
    except Exception:
        pass
    if asset in KRAKEN:
        try:
            r = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": KRAKEN[asset]}, timeout=6).json()
            out["Kraken"] = float(list(r["result"].values())[0]["c"][0])
        except Exception:
            pass
    return out

# ---------- Simplified analysis (retains core logic) ----------
def analyse(asset):
    series, product = ASSETS[asset][0], ASSETS[asset][1]
    m = market(series)
    if not m:
        return {"asset": asset, "err": "No open market"}
    strike, tk, ct = float(m["floor_strike"]), m["ticker"], m["close_time"]
    allc, vols, last_ts = candles(product)
    px = live_price(asset, product)
    spot = sum(px.values()) / len(px) if px else allc[-1]
    closed = allc[:-1]
    pct = (spot / strike - 1) * 100

    tb = st.session_state.setdefault("ticks", {}).setdefault(asset, [])
    tb.append((time.time(), spot))
    del tb[:-200]

    close_ts = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
    secs = max(close_ts - time.time(), 1)
    sig = sigma_ps(closed)
    mu_S, vf = settle_stat(spot, secs, tb, close_ts)
    z = math.log(mu_S / strike) / math.sqrt(sig * sig * vf + 0.0003 ** 2)
    p_stat = cdf(z)

    # Simplified model: only statistical part + RSI
    up = 0.7 * p_stat + 0.3 * (0.5 + (rsi(closed) - 50) / 200)
    up = min(0.98, max(0.02, up))

    ya, yb = dollars(m, "yes_ask"), dollars(m, "yes_bid")
    k_yes = (yb + ya) / 2 if yb and ya else (ya or 0.5)
    return dict(asset=asset, ticker=tk, spot=spot, strike=strike, pct=pct, secs=secs,
                up=up, k_yes=k_yes, mode="wait", side="yes" if up >= 0.5 else "no")

# ---------- UI ----------
@st.fragment(run_every=5)
def live():
    cols = st.columns(3)
    for i, a in enumerate(picked):
        try:
            d = analyse(a)
        except Exception as e:
            d = {"asset": a, "err": str(e)[:100]}
        with cols[i % 3]:
            if "err" in d:
                st.error(f"{a}: {d['err']}")
            else:
                st.metric(f"{a} · {int(d['secs'])}s left", f"${d['spot']:,.2f}",
                          f"{d['pct']:+.2f}% vs strike")
                st.progress(int(d["up"] * 100), text=f"UP {d['up']*100:.0f}%")
                st.caption(f"Kalshi YES {d['k_yes']*100:.0f}%")

live()

st.caption("Model prediction ≠ settlement. Kalshi 15m settles on CF Benchmarks 60s averages.")
