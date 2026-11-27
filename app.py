import math, time, os, base64, uuid, sqlite3
import requests, streamlit as st
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

try:
    from scipy.stats import t as _t
except:
    _t = None

st.set_page_config(page_title="Kalshi Full Auto Bot", page_icon="⚡", layout="wide")

# ====================== AUTH ======================
KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
KALSHI_PEM = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "")

if not KALSHI_KEY_ID or not KALSHI_PEM:
    st.error("Missing KALSHI_KEY_ID or KALSHI_PRIVATE_KEY_PEM")
    st.stop()

KALSHI_PEM = KALSHI_PEM.replace("\\n", "\n")
_private_key = serialization.load_pem_private_key(KALSHI_PEM.encode(), password=None)

def kalshi_headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path.split('?')[0]}".encode()
    sig = _private_key.sign(msg)
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }

# ====================== SETTINGS ======================
BASE = "https://api.elections.kalshi.com/trade-api/v2"

MAX_CONTRACTS = 4
MAX_NOTIONAL = 18.0
MIN_EDGE = 0.037
MIN_SECS_LEFT = 65
MAX_SECS_LEFT = 700

TAKE_PROFIT = 0.17
STOP_LOSS = -0.11
FORCE_CLOSE_SECS = 38

AUTO_TRADE = True

ASSETS = {
    "BTC": ("KXBTC15M", "BTC-USD"),
    "ETH": ("KXETH15M", "ETH-USD"),
    "SOL": ("KXSOL15M", "SOL-USD"),
}

# ====================== DATABASE ======================
DB_PATH = os.environ.get("DB_DIR", "/data") + "/trades.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    asset TEXT,
    side TEXT,
    entry_price REAL,
    exit_price REAL,
    count INTEGER,
    pnl REAL,
    status TEXT,
    created_at TEXT,
    closed_at TEXT
)
""")
db.commit()

if "traded_tickers" not in st.session_state:
    st.session_state.traded_tickers = set()
if "entry_data" not in st.session_state:
    st.session_state.entry_data = {}  # ticker → {side, entry, count}

# ====================== HELPERS ======================
def cdf(x, df=5):
    if _t is not None:
        return float(_t.cdf(x * math.sqrt(df/(df-2)), df))
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def fee(p): return 0.07 * p * (1 - p)

def ema(values, n):
    if not values: return [0]
    k = 2/(n+1)
    out = [values[0]]
    for x in values[1:]:
        out.append(x*k + out[-1]*(1-k))
    return out

def rsi(closes, n=14):
    if len(closes) < n+1: return 50.0
    gains = [max(b-a, 0) for a,b in zip(closes, closes[1:])][-n:]
    losses = [max(a-b, 0) for a,b in zip(closes, closes[1:])][-n:]
    ag = sum(gains)/n if gains else 0
    al = sum(losses)/n if losses else 1e-9
    return 100 - 100/(1 + ag/al)

def sigma_ps(closes):
    if len(closes) < 30: return 0.001
    rets = [math.log(b/a) for a,b in zip(closes[-61:], closes[-60:])]
    var = rets[0]**2
    for r in rets[1:]:
        var = 0.94*var + 0.06*r*r
    slow = math.sqrt(sum(r*r for r in rets)/len(rets))
    return max((0.55*math.sqrt(var) + 0.45*slow)/math.sqrt(60), 1e-7)

def better_settle_stat(spot, secs, ticks, close_ts):
    L = 60.0
    if secs > L + 25:
        return spot, secs * 0.62
    elif secs > L:
        return spot, (secs - L) + L/3.2
    else:
        recent = [p for t,p in ticks if t >= close_ts - L]
        if recent:
            avg = sum(recent)/len(recent)
            w = secs / L
            return w*spot + (1-w)*avg, max(secs*0.32, 0.65)
        return spot, max(secs*0.4, 0.65)

@st.cache_data(ttl=11)
def get_candles(product, granularity=60, limit=100):
    try:
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={"granularity": granularity},
            timeout=7
        )
        r.raise_for_status()
        return [x[4] for x in reversed(r.json()[:limit])]
    except:
        return []

def live_price(product):
    try:
        return float(requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/ticker",
            timeout=5
        ).json()["price"])
    except:
        return None

def market(series):
    path = "/markets"
    r = requests.get(f"{BASE}{path}",
                     params={"series_ticker": series, "status": "open", "limit": 8},
                     headers=kalshi_headers("GET", f"/trade-api/v2{path}"), timeout=8)
    r.raise_for_status()
    ms = r.json().get("markets", [])
    now = datetime.now(timezone.utc)
    open_ms = [m for m in ms if datetime.fromisoformat(m["close_time"].replace("Z","+00:00")) > now]
    return min(open_ms, key=lambda m: m["close_time"]) if open_ms else None

def dollars(m, key):
    v = m.get(f"{key}_dollars")
    if v not in (None, ""): return float(v)
    v = m.get(key)
    return float(v)/100 if v not in (None, "") else None

def multi_tf_score(product):
    tfs = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800}
    weights = {"1m": 0.18, "5m": 0.32, "15m": 0.32, "30m": 0.18}
    total = 0.0
    count = 0
    for name, gran in tfs.items():
        closes = get_candles(product, gran, 40)
        if len(closes) < 16: continue
        r = rsi(closes)
        e9 = ema(closes, 9)[-1]
        e21 = ema(closes, 21)[-1]
        mom = (closes[-1] / closes[-3] - 1) if len(closes) > 3 else 0
        score = 0
        score += 0.42 if r > 56 else -0.42 if r < 44 else 0
        score += 0.33 if e9 > e21 else -0.33
        score += 0.25 if mom > 0.0012 else -0.25 if mom < -0.0012 else 0
        total += score * weights[name]
        count += 1
    return total / count if count else 0.0

# ====================== ANALYSIS ======================
def analyse(asset):
    series, product = ASSETS[asset]
    m = market(series)
    if not m:
        return {"asset": asset, "err": "No open market"}

    strike = float(m["floor_strike"])
    ticker = m["ticker"]
    close_time = m["close_time"]
    closes = get_candles(product, 60, 120)
    spot = live_price(product) or (closes[-1] if closes else None)
    if not spot:
        return {"asset": asset, "err": "No price data"}

    tb = st.session_state.setdefault("ticks", {}).setdefault(asset, [])
    tb.append((time.time(), spot))
    del tb[:-450]

    close_ts = datetime.fromisoformat(close_time.replace("Z","+00:00")).timestamp()
    secs = max(close_ts - time.time(), 1)

    sig = sigma_ps(closes)
    mu, vf = better_settle_stat(spot, secs, tb, close_ts)
    z = math.log(mu / strike) / math.sqrt(sig*sig*vf + 0.00018**2)
    p_stat = cdf(z)

    tf = multi_tf_score(product)
    p_tech = 0.5 + tf * 0.41

    w = 0.51 + 0.29 * max(0, (190 - secs)/190)
    up = max(0.035, min(0.965, w * p_stat + (1 - w) * p_tech))

    ya = dollars(m, "yes_ask")
    yb = dollars(m, "yes_bid")
    mid = (ya + yb)/2 if ya and yb else (ya or yb or 0.5)

    edge_yes = up - mid - fee(mid)
    edge_no  = (1 - up) - (1 - mid) - fee(1 - mid)
    best = max(edge_yes, edge_no)
    side = "yes" if edge_yes > edge_no else "no"

    should_trade = (
        best >= MIN_EDGE and
        MIN_SECS_LEFT < secs < MAX_SECS_LEFT and
        ticker not in st.session_state.traded_tickers
    )

    return {
        "asset": asset, "ticker": ticker, "spot": spot, "strike": strike,
        "secs": secs, "up": up, "mid": mid, "edge": best, "side": side,
        "should_trade": should_trade, "yes_ask": ya, "yes_bid": yb,
        "close_ts": close_ts
    }

# ====================== TRADING ======================
def get_balance():
    path = "/portfolio/balance"
    r = requests.get(BASE + path, headers=kalshi_headers("GET", f"/trade-api/v2{path}"), timeout=8)
    r.raise_for_status()
    return r.json()

def get_positions():
    path = "/portfolio/positions"
    r = requests.get(BASE + path, headers=kalshi_headers("GET", f"/trade-api/v2{path}"), timeout=8)
    r.raise_for_status()
    return r.json().get("market_positions", [])

def place_order(ticker, side, count, price_cents):
    if count > MAX_CONTRACTS or count * (price_cents / 100) > MAX_NOTIONAL:
        raise ValueError("Risk limit hit")

    if side == "yes":
        v2_side, price = "bid", f"{price_cents/100:.4f}"
    else:
        v2_side, price = "ask", f"{(100 - price_cents)/100:.4f}"

    path = "/portfolio/events/orders"
    body = {
        "ticker": ticker,
        "side": v2_side,
        "count": str(count),
        "price": price,
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": str(uuid.uuid4())
    }
    r = requests.post(BASE + path, json=body, headers=kalshi_headers("POST", f"/trade-api/v2{path}"), timeout=12)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{r.status_code}: {r.text}")
    return r.json()

def close_position(ticker, yes_count, no_count, yes_bid, yes_ask):
    if yes_count > 0:
        price_cents = max(1, int(yes_bid * 100) - 1)
        return place_order(ticker, "yes", int(yes_count), price_cents)
    elif no_count > 0:
        price_cents = min(99, int((1 - yes_ask) * 100) + 1)
        return place_order(ticker, "no", int(no_count), price_cents)
    raise ValueError("No position")

def log_trade(ticker, asset, side, entry, exit_p, count, pnl, status):
    db.execute(
        "INSERT INTO trades (ticker, asset, side, entry_price, exit_price, count, pnl, status, created_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ticker, asset, side, entry, exit_p, count, pnl, status,
         datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat())
    )
    db.commit()

# ====================== UI ======================
st.title("Kalshi Full Auto Bot")
st.caption(f"AUTO={AUTO_TRADE} | TP={TAKE_PROFIT*100:.0f}% | SL={STOP_LOSS*100:.0f}% | Max {MAX_CONTRACTS} contracts")

try:
    bal = get_balance()
    balance = bal.get("balance", 0) / 100
    st.metric("Available Balance", f"${balance:.2f}")
except Exception as e:
    st.error(f"Balance error: {e}")
    balance = 0

@st.fragment(run_every=7)
def main_loop():
    # ===== 1. Auto Cash-out =====
    try:
        positions = get_positions()
        for p in positions:
            ticker = p.get("ticker")
            yes_c = int(float(p.get("yes_count", 0) or 0))
            no_c = int(float(p.get("no_count", 0) or 0))
            if yes_c == 0 and no_c == 0:
                continue

            try:
                series = ticker[:8] if len(ticker) > 8 else ticker
                m = market(series)
                if not m: continue

                yb = dollars(m, "yes_bid") or 0.4
                ya = dollars(m, "yes_ask") or 0.6
                secs_left = max(0, datetime.fromisoformat(m["close_time"].replace("Z","+00:00")).timestamp() - time.time())

                entry_info = st.session_state.entry_data.get(ticker, {})
                entry = entry_info.get("entry", 0.5)
                side = entry_info.get("side", "yes")
                count = entry_info.get("count", yes_c or no_c)

                if yes_c > 0:
                    current = yb
                    pnl_pct = (current - entry) / entry if entry > 0 else 0
                else:
                    current = 1 - ya
                    pnl_pct = (current - (1 - entry)) / (1 - entry) if entry < 1 else 0

                st.write(f"**{ticker}** | PnL: `{pnl_pct*100:+.1f}%` | {secs_left:.0f}s left")

                if pnl_pct >= TAKE_PROFIT or pnl_pct <= STOP_LOSS or secs_left < FORCE_CLOSE_SECS:
                    res = close_position(ticker, yes_c, no_c, yb, ya)
                    status = "TP" if pnl_pct >= TAKE_PROFIT else "SL" if pnl_pct <= STOP_LOSS else "TIME"
                    log_trade(ticker, entry_info.get("asset", "?"), side, entry, current, count, pnl_pct, status)
                    st.success(f"AUTO CASHOUT ({status}) {ticker} → {pnl_pct*100:+.1f}%")
                    if ticker in st.session_state.entry_data:
                        del st.session_state.entry_data[ticker]
            except Exception as e:
                st.warning(f"Cashout error {ticker}: {e}")
    except Exception as e:
        st.warning(f"Position check: {e}")

    st.divider()

    # ===== 2. Auto Entry =====
    cols = st.columns(len(ASSETS))
    for i, (asset, _) in enumerate(ASSETS.items()):
        with cols[i]:
            try:
                d = analyse(asset)
            except Exception as e:
                st.error(f"{asset}: {e}")
                continue

            if "err" in d:
                st.warning(d["err"])
                continue

            st.subheader(asset)
            st.metric("Spot", f"${d['spot']:,.2f}", f"{d['secs']:.0f}s")
            st.progress(int(d["up"] * 100), text=f"P(UP) {d['up']*100:.1f}%")
            st.write(f"Edge: **{d['edge']*100:.1f}%** → **{d['side'].upper()}**")

            if AUTO_TRADE and d["should_trade"] and balance > 10:
                price_cents = int(round((d["yes_ask"] or 0.5)*100)) if d["side"] == "yes" else int(round((1 - (d["yes_bid"] or 0.5))*100))
                price_cents = max(5, min(95, price_cents))
                size = max(1, min(MAX_CONTRACTS, int(min(balance * 0.10, MAX_NOTIONAL) / (price_cents / 100))))

                try:
                    res = place_order(d["ticker"], d["side"], size, price_cents)
                    st.session_state.traded_tickers.add(d["ticker"])
                    st.session_state.entry_data[d["ticker"]] = {
                        "side": d["side"],
                        "entry": price_cents / 100,
                        "count": size,
                        "asset": asset
                    }
                    st.success(f"AUTO ENTRY → {d['side'].upper()} ×{size} @ {price_cents}¢")
                except Exception as e:
                    st.error(f"Entry failed: {e}")

main_loop()

# ===== Trade History =====
st.divider()
st.subheader("Trade History (Last 15)")
try:
    rows = db.execute("SELECT ticker, side, entry_price, exit_price, count, pnl, status, closed_at FROM trades ORDER BY id DESC LIMIT 15").fetchall()
    if rows:
        for r in rows:
            color = "green" if r[5] and r[5] > 0 else "red"
            st.markdown(f"`{r[0]}` | {r[1].upper()} | Entry {r[2]:.2f} → Exit {r[3]:.2f} | "
                        f"PnL: :{color}[{r[5]*100:+.1f}%] | {r[6]} | {r[7][11:19] if r[7] else ''}")
    else:
        st.write("No trades yet")
except Exception as e:
    st.write(f"History error: {e}")

st.caption("Full Auto • Multi-TF • TP/SL • Trade Logging")
