import math, time, os, base64, uuid
import requests, streamlit as st
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

try:
    from scipy.stats import t as _t
except:
    _t = None

st.set_page_config(page_title="Kalshi 15-Min LIVE", page_icon="⚡", layout="wide")

# ====================== AUTH ======================
KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
KALSHI_PEM = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "")

if not KALSHI_KEY_ID or not KALSHI_PEM:
    st.error("Missing secrets: KALSHI_KEY_ID or KALSHI_PRIVATE_KEY_PEM")
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

# ====================== CONFIG ======================
BASE = "https://api.elections.kalshi.com/trade-api/v2"   # LIVE
MAX_CONTRACTS = 5
MAX_NOTIONAL = 25.0

ASSETS = {
    "BTC":  ("KXBTC15M", "BTC-USD"),
    "ETH":  ("KXETH15M", "ETH-USD"),
    "SOL":  ("KXSOL15M", "SOL-USD"),
    "XRP":  ("KXXRP15M", "XRP-USD"),
    "DOGE": ("KXDOGE15M", "DOGE-USD"),
}

# ====================== HELPERS ======================
def cdf(x, df=5):
    if _t is not None:
        return float(_t.cdf(x * math.sqrt(df / (df - 2)), df))
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def fee(p):
    return 0.07 * p * (1 - p)

def ema(values, n):
    k = 2 / (n + 1)
    out = [values[0]]
    for x in values[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out

def rsi(closes, n=14):
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])][-n:]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])][-n:]
    avg_gain = sum(gains) / n if gains else 0
    avg_loss = sum(losses) / n if losses else 1e-9
    return 100 - 100 / (1 + avg_gain / avg_loss)

def sigma_ps(closes):
    if len(closes) < 30:
        return 0.001
    rets = [math.log(b / a) for a, b in zip(closes[-61:], closes[-60:])]
    var = rets[0] ** 2
    for r in rets[1:]:
        var = 0.94 * var + 0.06 * r * r
    slow = math.sqrt(sum(r * r for r in rets) / len(rets))
    return max((0.55 * math.sqrt(var) + 0.45 * slow) / math.sqrt(60), 1e-7)

def better_settle_stat(spot, secs_left, ticks, close_ts):
    L = 60.0
    if secs_left > L + 30:
        return spot, secs_left * 0.7
    elif secs_left > L:
        return spot, (secs_left - L) + L / 4
    else:
        recent = [p for t, p in ticks if t >= close_ts - L]
        if recent:
            avg = sum(recent) / len(recent)
            weight = secs_left / L
            return weight * spot + (1 - weight) * avg, max(secs_left * 0.4, 0.8)
        return spot, max(secs_left * 0.5, 0.8)

def market(series):
    path = "/markets"
    r = requests.get(
        f"{BASE}{path}",
        params={"series_ticker": series, "status": "open", "limit": 15},
        headers=kalshi_headers("GET", f"/trade-api/v2{path}"),
        timeout=10
    )
    r.raise_for_status()
    ms = r.json().get("markets", [])
    now = datetime.now(timezone.utc)
    open_ms = [m for m in ms if datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) > now]
    return min(open_ms, key=lambda m: m["close_time"]) if open_ms else None

def dollars(m, key):
    v = m.get(f"{key}_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(key)
    return float(v) / 100 if v not in (None, "") else None

@st.cache_data(ttl=12)
def candles(product):
    r = requests.get(
        f"https://api.exchange.coinbase.com/products/{product}/candles",
        params={"granularity": 60},
        timeout=10
    )
    r.raise_for_status()
    return [x[4] for x in reversed(r.json()[:150])]

@st.cache_data(ttl=5)
def live_price(product):
    try:
        return float(requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/ticker",
            timeout=6
        ).json()["price"])
    except:
        return None

# ====================== ANALYSIS ======================
def analyse(asset):
    series, product = ASSETS[asset]
    m = market(series)
    if not m:
        return {"asset": asset, "err": "No open market"}

    strike = float(m["floor_strike"])
    ticker = m["ticker"]
    close_time = m["close_time"]
    closes = candles(product)
    spot = live_price(product) or closes[-1]

    tb = st.session_state.setdefault("ticks", {}).setdefault(asset, [])
    tb.append((time.time(), spot))
    del tb[:-300]

    close_ts = datetime.fromisoformat(close_time.replace("Z", "+00:00")).timestamp()
    secs = max(close_ts - time.time(), 1)

    sig = sigma_ps(closes)
    mu, vf = better_settle_stat(spot, secs, tb, close_ts)
    z = math.log(mu / strike) / math.sqrt(sig * sig * vf + 0.00025 ** 2)
    p_stat = cdf(z)

    r = rsi(closes)
    e9 = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    mom = (closes[-1] / closes[-6] - 1) if len(closes) > 6 else 0

    tech = 0.5
    tech += 0.12 if r > 55 else -0.12 if r < 45 else 0
    tech += 0.10 if e9 > e21 else -0.10
    tech += 0.08 if mom > 0.001 else -0.08 if mom < -0.001 else 0
    tech = max(0.15, min(0.85, tech))

    w_stat = 0.55 + 0.25 * max(0, (180 - secs) / 180)
    up = max(0.03, min(0.97, w_stat * p_stat + (1 - w_stat) * tech))

    ya = dollars(m, "yes_ask")
    yb = dollars(m, "yes_bid")
    mid = (ya + yb) / 2 if ya and yb else (ya or yb or 0.5)

    edge_yes = up - mid - fee(mid)
    edge_no = (1 - up) - (1 - mid) - fee(1 - mid)
    best_edge = max(edge_yes, edge_no)

    mode = "wait"
    side = "yes" if up >= 0.5 else "no"
    if best_edge > 0.025 and secs > 45:
        mode = "SAFE" if best_edge > 0.05 else "SCALP"
        side = "yes" if edge_yes > edge_no else "no"

    return {
        "asset": asset,
        "ticker": ticker,
        "spot": spot,
        "strike": strike,
        "secs": secs,
        "up": up,
        "mid": mid,
        "edge": best_edge,
        "mode": mode,
        "side": side,
        "yes_ask": ya,
        "yes_bid": yb,
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
    if count > MAX_CONTRACTS:
        raise ValueError(f"Max contracts is {MAX_CONTRACTS}")
    notional = count * (price_cents / 100)
    if notional > MAX_NOTIONAL:
        raise ValueError(f"Max notional is ${MAX_NOTIONAL}")

    if side == "yes":
        v2_side = "bid"
        price = f"{price_cents / 100:.4f}"
    else:
        v2_side = "ask"
        price = f"{(100 - price_cents) / 100:.4f}"

    path = "/portfolio/events/orders"
    body = {
        "ticker": ticker,
        "side": v2_side,
        "count": str(count),
        "price": price,
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": str(uuid.uuid4()),
    }
    r = requests.post(BASE + path, json=body, headers=kalshi_headers("POST", f"/trade-api/v2{path}"), timeout=12)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{r.status_code}: {r.text}")
    return r.json()

def close_position(ticker, yes_count, no_count, yes_bid, yes_ask):
    if yes_count > 0:
        # Sell YES
        price_cents = max(1, int(yes_bid * 100) - 1)
        return place_order(ticker, "yes", int(yes_count), price_cents)
    elif no_count > 0:
        # Sell NO
        price_cents = min(99, int((1 - yes_ask) * 100) + 1)
        return place_order(ticker, "no", int(no_count), price_cents)
    else:
        raise ValueError("No position to close")

# ====================== UI ======================
st.title("Kalshi 15-Min Desk — LIVE")
st.warning("LIVE TRADING • Real money")

# Account Overview
with st.expander("Account & Positions", expanded=True):
    try:
        bal = get_balance()
        st.metric("Available Balance", f"${bal.get('balance', 0) / 100:.2f}")
    except Exception as e:
        st.error(f"Balance error: {e}")

    try:
        positions = get_positions()
        if positions:
            for p in positions:
                ticker = p.get("ticker")
                yes_c = int(float(p.get("yes_count", 0) or 0))
                no_c = int(float(p.get("no_count", 0) or 0))
                if yes_c == 0 and no_c == 0:
                    continue

                st.write(f"**{ticker}** → YES: `{yes_c}` | NO: `{no_c}`")
                if st.button(f"Cash Out {ticker}", key=f"close_{ticker}"):
                    try:
                        # Using conservative prices for closing
                        res = close_position(ticker, yes_c, no_c, 0.35, 0.65)
                        st.success(f"Close order sent → {res.get('order_id')}")
                    except Exception as e:
                        st.error(str(e))
        else:
            st.write("No open positions")
    except Exception as e:
        st.error(f"Positions error: {e}")

# Controls
c1, c2, c3 = st.columns(3)
with c1:
    MIN_EDGE = st.slider("Min edge %", 1.0, 12.0, 3.5) / 100
with c2:
    BANKROLL = st.number_input("Bankroll $", 20, 50000, 100)
with c3:
    picked = st.multiselect("Assets", list(ASSETS), default=["BTC", "ETH", "SOL"])

st.divider()

@st.fragment(run_every=6)
def dashboard():
    cols = st.columns(len(picked) or 1)
    for i, asset in enumerate(picked):
        with cols[i]:
            try:
                d = analyse(asset)
            except Exception as e:
                st.error(f"{asset}: {str(e)[:90]}")
                continue

            if "err" in d:
                st.warning(d["err"])
                continue

            st.subheader(f"{asset} · {int(d['secs'])}s left")
            st.metric("Spot", f"${d['spot']:,.2f}", f"{(d['spot']/d['strike']-1)*100:+.2f}%")
            st.progress(int(d["up"] * 100), text=f"Model UP {d['up']*100:.1f}%")
            st.write(f"Kalshi mid: **{d['mid']*100:.1f}%** | Edge: **{d['edge']*100:.1f}%**")
            st.write(f"Signal: `{d['mode']}` → **{d['side'].upper()}**")

            if d["mode"] in ("SAFE", "SCALP") and d["secs"] > 40:
                price_cents = int(round((d["yes_ask"] or 0.5) * 100)) if d["side"] == "yes" else int(round((1 - (d["yes_bid"] or 0.5)) * 100))
                price_cents = max(1, min(99, price_cents))
                count = max(1, min(MAX_CONTRACTS, int(BANKROLL * 0.03 / (price_cents / 100))))

                if st.button(f"BUY {d['side'].upper()} ×{count} @ {price_cents}¢", key=f"buy_{asset}_{d['ticker']}"):
                    try:
                        res = place_order(d["ticker"], d["side"], count, price_cents)
                        st.success(f"Order placed → {res.get('order_id')}")
                    except Exception as e:
                        st.error(str(e))

dashboard()

st.caption("Live mode • Risk limits active • Always double-check before trading")
