import math, time, os, base64, uuid, sqlite3
import requests, streamlit as st
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization

try:
    from scipy.stats import t as _t
except Exception:
    _t = None

st.set_page_config(page_title="Kalshi Full Auto Bot", page_icon="⚡", layout="wide")

# ====================== AUTH ======================
KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
KALSHI_PEM = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "")

if not KALSHI_KEY_ID or not KALSHI_PEM:
    st.error("Missing secrets")
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

ASSETS = {
    "BTC": ("KXBTC15M", "BTC-USD"),
    "ETH": ("KXETH15M", "ETH-USD"),
    "SOL": ("KXSOL15M", "SOL-USD"),
}

# ====================== SIDEBAR CONTROLS ======================
st.sidebar.title("Settings")

AUTO_TRADE = st.sidebar.toggle("Auto Trade (OFF = log signals only)", value=False)

st.sidebar.subheader("Entry")
MIN_EDGE = st.sidebar.slider("Min Edge %", 2.0, 12.0, 4.5) / 100
MAX_CONTRACTS = st.sidebar.slider("Max Contracts", 1, 8, 3)
MAX_NOTIONAL = st.sidebar.slider("Max $ per trade", 5.0, 30.0, 12.0)
SHRINK = st.sidebar.slider("Trust market over model %", 0, 100, 60) / 100
MAX_EDGE = st.sidebar.slider("Skip if edge above % (likely model error)", 6.0, 30.0, 10.0) / 100
PRICE_LO, PRICE_HI = st.sidebar.slider("Only buy contracts priced between (cents)", 5, 95, (15, 85))

st.sidebar.subheader("Exit")
TAKE_PROFIT = st.sidebar.slider("Take Profit %", 8, 30, 16) / 100
STOP_LOSS = st.sidebar.slider("Stop Loss %", -20, -5, -10) / 100
FORCE_CLOSE_SECS = st.sidebar.slider("Force close under (seconds)", 20, 60, 35)

st.sidebar.subheader("Prediction-flip exits")
FLIP_EXIT = st.sidebar.slider(
    "Exit if my side's win chance drops below %", 25, 50, 42) / 100
PROFIT_LOCK = st.sidebar.slider(
    "Lock profit if my side's win chance below %", 40, 60, 50) / 100
MIN_HOLD = st.sidebar.slider("Min hold before flip exit (sec)", 0, 90, 25)

picked = st.sidebar.multiselect(
    "Assets", list(ASSETS.keys()), default=["BTC", "ETH", "SOL"])

st.sidebar.markdown("---")
st.sidebar.caption("Higher Min Edge = fewer but better trades")
st.sidebar.caption("Flip exits use the model's updated probability for the side you hold")

# ====================== DATABASE ======================
DB_PATH = os.environ.get("DB_DIR", "/data") + "/trades.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT, asset TEXT, side TEXT,
    entry_price REAL, exit_price REAL, count INTEGER,
    pnl REAL, status TEXT, created_at TEXT, closed_at TEXT
)""")
db.execute("""
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT UNIQUE, asset TEXT,
    model_up REAL, adj_up REAL, mid REAL,
    side TEXT, cost REAL, edge REAL, would_trade INTEGER,
    secs REAL, logged_at TEXT, result TEXT
)""")
db.commit()

if "traded_tickers" not in st.session_state:
    st.session_state.traded_tickers = set()
if "entry_data" not in st.session_state:
    st.session_state.entry_data = {}


# ====================== HELPERS ======================
def cdf(x, df=5):
    if _t is not None:
        return float(_t.cdf(x * math.sqrt(df / (df - 2)), df))
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fee(p):
    return 0.07 * p * (1 - p)


def ema(values, n):
    if not values:
        return [0]
    k = 2 / (n + 1)
    out = [values[0]]
    for x in values[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return 50.0
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])][-n:]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])][-n:]
    ag = sum(gains) / n if gains else 0
    al = sum(losses) / n if losses else 1e-9
    return 100 - 100 / (1 + ag / al)


def sigma_ps(closes):
    if len(closes) < 30:
        return 0.001
    rets = [math.log(b / a) for a, b in zip(closes[-61:], closes[-60:])]
    var = rets[0] ** 2
    for r in rets[1:]:
        var = 0.94 * var + 0.06 * r * r
    slow = math.sqrt(sum(r * r for r in rets) / len(rets))
    return max((0.55 * math.sqrt(var) + 0.45 * slow) / math.sqrt(60), 1e-7)


def better_settle_stat(spot, secs, ticks, close_ts):
    L = 60.0
    if secs > L + 25:
        return spot, secs * 0.62
    elif secs > L:
        return spot, (secs - L) + L / 3.2
    else:
        recent = [p for t, p in ticks if t >= close_ts - L]
        if recent:
            avg = sum(recent) / len(recent)
            w = secs / L
            return w * spot + (1 - w) * avg, max(secs * 0.32, 0.65)
        return spot, max(secs * 0.4, 0.65)


@st.cache_data(ttl=11)
def get_candles(product, granularity=60, limit=100):
    try:
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={"granularity": granularity}, timeout=7)
        r.raise_for_status()
        return [x[4] for x in reversed(r.json()[:limit])]
    except Exception:
        return []


def live_price(product):
    try:
        return float(requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/ticker",
            timeout=5).json()["price"])
    except Exception:
        return None


def market(series):
    path = "/markets"
    r = requests.get(
        f"{BASE}{path}",
        params={"series_ticker": series, "status": "open", "limit": 8},
        headers=kalshi_headers("GET", f"/trade-api/v2{path}"), timeout=8)
    r.raise_for_status()
    ms = r.json().get("markets", [])
    now = datetime.now(timezone.utc)
    open_ms = [m for m in ms
               if datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) > now]
    return min(open_ms, key=lambda m: m["close_time"]) if open_ms else None


def dollars(m, key):
    v = m.get(f"{key}_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(key)
    return float(v) / 100 if v not in (None, "") else None


def multi_tf_score(product):
    tfs = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800}
    weights = {"1m": 0.18, "5m": 0.32, "15m": 0.32, "30m": 0.18}
    total = 0.0
    count = 0
    for name, gran in tfs.items():
        closes = get_candles(product, gran, 40)
        if len(closes) < 16:
            continue
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
        return {"asset": asset, "err": "No price"}

    tb = st.session_state.setdefault("ticks", {}).setdefault(asset, [])
    tb.append((time.time(), spot))
    del tb[:-450]

    close_ts = datetime.fromisoformat(close_time.replace("Z", "+00:00")).timestamp()
    secs = max(close_ts - time.time(), 1)

    sig = sigma_ps(closes)
    mu, vf = better_settle_stat(spot, secs, tb, close_ts)
    z = math.log(mu / strike) / math.sqrt(sig * sig * vf + 0.00018 ** 2)
    p_stat = cdf(z)

    tf = multi_tf_score(product)
    p_tech = 0.5 + tf * 0.41
    w = 0.51 + 0.29 * max(0, (190 - secs) / 190)
    up = max(0.035, min(0.965, w * p_stat + (1 - w) * p_tech))

    ya = dollars(m, "yes_ask")
    yb = dollars(m, "yes_bid")
    mid = (ya + yb) / 2 if ya and yb else (ya or yb or 0.5)

    # Shrink the model toward the market: the market is usually right.
    adj = (1 - SHRINK) * up + SHRINK * mid

    # Edge against the price you would actually pay (ask / 1-bid), plus fee
    cost_yes = ya if ya else mid
    cost_no = (1 - yb) if yb else (1 - mid)
    edge_yes = adj - cost_yes - fee(cost_yes)
    edge_no = (1 - adj) - cost_no - fee(cost_no)
    best = max(edge_yes, edge_no)
    side = "yes" if edge_yes > edge_no else "no"
    cost = cost_yes if side == "yes" else cost_no

    should_trade = (
        MIN_EDGE <= best <= MAX_EDGE and
        PRICE_LO / 100 <= cost <= PRICE_HI / 100 and
        60 < secs < 680 and
        ticker not in st.session_state.traded_tickers
    )

    return {
        "asset": asset, "ticker": ticker, "spot": spot, "strike": strike,
        "secs": secs, "up": up, "adj": adj, "mid": mid, "edge": best,
        "side": side, "cost": cost, "should_trade": should_trade,
        "yes_ask": ya, "yes_bid": yb
    }


def get_balance():
    path = "/portfolio/balance"
    r = requests.get(BASE + path,
                     headers=kalshi_headers("GET", f"/trade-api/v2{path}"), timeout=8)
    r.raise_for_status()
    return r.json()


def get_positions():
    path = "/portfolio/positions"
    r = requests.get(BASE + path,
                     headers=kalshi_headers("GET", f"/trade-api/v2{path}"), timeout=8)
    r.raise_for_status()
    return r.json().get("market_positions", [])


def place_order(ticker, side, count, price_cents):
    if count > MAX_CONTRACTS or count * (price_cents / 100) > MAX_NOTIONAL:
        raise ValueError("Risk limit")
    if side == "yes":
        v2_side, price = "bid", f"{price_cents / 100:.4f}"
    else:
        v2_side, price = "ask", f"{(100 - price_cents) / 100:.4f}"
    path = "/portfolio/events/orders"
    body = {
        "ticker": ticker, "side": v2_side, "count": str(count), "price": price,
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": str(uuid.uuid4())
    }
    r = requests.post(BASE + path, json=body,
                      headers=kalshi_headers("POST", f"/trade-api/v2{path}"), timeout=12)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{r.status_code}: {r.text}")
    return r.json()


def close_position(ticker, yes_count, no_count, yes_bid, yes_ask):
    """Close by doing the opposite of what is held.
    Holding YES -> sell YES (ask at/below the bid so it crosses).
    Holding NO  -> buy YES back (bid at/above the ask so it crosses).
    No risk-limit check here: closing should never be blocked."""
    if yes_count > 0:
        side, count = "ask", int(yes_count)
        price = max(0.01, yes_bid - 0.01)
    elif no_count > 0:
        side, count = "bid", int(no_count)
        price = min(0.99, yes_ask + 0.01)
    else:
        raise ValueError("Nothing to close")
    path = "/portfolio/events/orders"
    body = {
        "ticker": ticker, "side": side, "count": str(count),
        "price": f"{price:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": str(uuid.uuid4())
    }
    r = requests.post(BASE + path, json=body,
                      headers=kalshi_headers("POST", f"/trade-api/v2{path}"), timeout=12)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{r.status_code}: {r.text}")
    return r.json()


def log_trade(ticker, asset, side, entry, exit_p, count, pnl, status):
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO trades (ticker, asset, side, entry_price, exit_price, count, pnl, status, created_at, closed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ticker, asset, side, entry, exit_p, count, pnl, status, now, now)
    )
    db.commit()


def log_signal(d):
    """One row per market, logged once when 90-240s remain, whether or not we trade."""
    db.execute(
        "INSERT OR IGNORE INTO signals (ticker, asset, model_up, adj_up, mid, side, cost, edge, would_trade, secs, logged_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (d["ticker"], d["asset"], d["up"], d["adj"], d["mid"], d["side"], d["cost"],
         d["edge"], int(d["should_trade"]), d["secs"], datetime.now(timezone.utc).isoformat()))
    db.commit()


def resolve_signals():
    """Fill in the real outcome of logged markets once Kalshi has settled them."""
    if time.time() - st.session_state.get("last_resolve", 0) < 60:
        return
    st.session_state["last_resolve"] = time.time()
    rows = db.execute("SELECT ticker FROM signals WHERE result IS NULL ORDER BY id LIMIT 15").fetchall()
    for (tk,) in rows:
        try:
            path = f"/markets/{tk}"
            r = requests.get(BASE + path,
                             headers=kalshi_headers("GET", f"/trade-api/v2{path}"), timeout=8)
            r.raise_for_status()
            res = (r.json().get("market", {}) or {}).get("result")
            if res in ("yes", "no"):
                db.execute("UPDATE signals SET result=? WHERE ticker=?", (res, tk))
        except Exception:
            pass
    db.commit()


def calibration_panel():
    st.subheader("Is the model actually accurate?")
    resolve_signals()
    rows = db.execute(
        "SELECT model_up, adj_up, mid, side, cost, would_trade, result FROM signals "
        "WHERE result IS NOT NULL").fetchall()
    total = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    st.caption(f"{total} markets logged, {len(rows)} settled. Needs 50+ settled before it means anything.")
    if not rows:
        return
    brier_model = sum((r[0] - (r[6] == "yes")) ** 2 for r in rows) / len(rows)
    brier_mkt = sum((r[2] - (r[6] == "yes")) ** 2 for r in rows) / len(rows)
    c1, c2 = st.columns(2)
    c1.metric("Model error (Brier, lower=better)", f"{brier_model:.3f}")
    c2.metric("Kalshi price error (Brier)", f"{brier_mkt:.3f}",
              "model beats market" if brier_model < brier_mkt else "market beats model",
              delta_color="normal" if brier_model < brier_mkt else "inverse")
    hyp = [r for r in rows if r[5]]
    if hyp:
        pnl, wins = 0.0, 0
        for r in hyp:
            won = (r[3] == r[6])
            wins += won
            pnl += ((1 - r[4]) if won else -r[4]) - fee(r[4])
        st.write(f"Signals the bot would have traded: {len(hyp)} | win rate {wins/len(hyp)*100:.0f}% "
                 f"| hypothetical PnL per 1 contract each: **${pnl:+.2f}**")


# ====================== MAIN UI ======================
st.title("Kalshi Full Auto Bot")
st.caption(f"AUTO={AUTO_TRADE} | Min Edge={MIN_EDGE * 100:.1f}% | Max {MAX_CONTRACTS} contracts")

try:
    bal = get_balance()
    balance = bal.get("balance", 0) / 100
    st.metric("Available Balance", f"${balance:.2f}")
except Exception as e:
    st.error(f"Balance error: {e}")
    balance = 0


@st.fragment(run_every=7)
def main_loop():
    # ---------------- Auto Cashout ----------------
    try:
        positions = get_positions()
        for p in positions:
            ticker = p.get("ticker")
            yes_c = int(float(p.get("yes_count", 0) or 0))
            no_c = int(float(p.get("no_count", 0) or 0))
            if yes_c == 0 and no_c == 0:
                continue

            try:
                series = ticker[:8]
                m = market(series)
                if not m:
                    continue
                yb = dollars(m, "yes_bid") or 0.4
                ya = dollars(m, "yes_ask") or 0.6
                secs_left = max(0, datetime.fromisoformat(
                    m["close_time"].replace("Z", "+00:00")).timestamp() - time.time())

                entry_info = st.session_state.entry_data.get(ticker, {})
                entry = entry_info.get("entry", 0.5)  # price paid for the side held
                count = entry_info.get("count", yes_c or no_c)
                held_for = time.time() - entry_info.get("t", 0)
                asset = entry_info.get("asset") or next(
                    (a for a, (s, _) in ASSETS.items() if ticker.startswith(s)), None)

                if yes_c > 0:
                    side, current = "yes", yb
                else:
                    side, current = "no", 1 - ya
                pnl_pct = (current - entry) / entry if entry > 0 else 0

                # Re-run the model on this market for the side we hold
                my_prob = None
                if asset:
                    try:
                        d = analyse(asset)
                        if "err" not in d and d["ticker"] == ticker:
                            my_prob = d["up"] if side == "yes" else 1 - d["up"]
                    except Exception:
                        pass

                prob_txt = f" | win chance {my_prob * 100:.0f}%" if my_prob is not None else ""
                st.write(
                    f"**{ticker}** {side.upper()} | PnL `{pnl_pct * 100:+.1f}%`"
                    f"{prob_txt} | {secs_left:.0f}s left")

                status = None
                if pnl_pct >= TAKE_PROFIT:
                    status = "TP"
                elif pnl_pct <= STOP_LOSS:
                    status = "SL"
                elif secs_left < FORCE_CLOSE_SECS:
                    status = "TIME"
                elif my_prob is not None and held_for >= MIN_HOLD:
                    if pnl_pct > 0 and my_prob < PROFIT_LOCK:
                        status = "LOCK"   # in profit, model now says it ends against us
                    elif pnl_pct <= 0 and my_prob < FLIP_EXIT:
                        status = "FLIP"   # model says we were wrong

                if status:
                    close_position(ticker, yes_c, no_c, yb, ya)
                    log_trade(ticker, asset or "?", side, entry, current, count,
                              pnl_pct, status)
                    st.success(f"AUTO CASHOUT ({status}) → {pnl_pct * 100:+.1f}%")
                    st.session_state.entry_data.pop(ticker, None)
            except Exception as e:
                st.warning(f"Cashout {ticker}: {e}")
    except Exception as e:
        st.warning(f"Positions: {e}")

    st.divider()

    # ---------------- Auto Entry ----------------
    cols = st.columns(len(picked) or 1)
    for i, asset in enumerate(picked):
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
            st.progress(int(d["up"] * 100), text=f"P(UP) {d['up'] * 100:.1f}%")
            st.write(f"Edge **{d['edge'] * 100:.1f}%** → **{d['side'].upper()}** "
                     f"(model {d['up'] * 100:.0f}% → adj {d['adj'] * 100:.0f}% vs market {d['mid'] * 100:.0f}%)")
            if 90 < d["secs"] < 240:
                try:
                    log_signal(d)
                except Exception:
                    pass

            if AUTO_TRADE and d["should_trade"] and balance > 8:
                if d["side"] == "yes":
                    price_cents = int(round((d["yes_ask"] or 0.5) * 100))
                else:
                    price_cents = int(round((1 - (d["yes_bid"] or 0.5)) * 100))
                price_cents = max(5, min(95, price_cents))
                size = max(1, min(
                    MAX_CONTRACTS,
                    int(min(balance * 0.09, MAX_NOTIONAL) / (price_cents / 100))))

                try:
                    place_order(d["ticker"], d["side"], size, price_cents)
                    st.session_state.traded_tickers.add(d["ticker"])
                    st.session_state.entry_data[d["ticker"]] = {
                        "side": d["side"], "entry": price_cents / 100,
                        "count": size, "asset": asset, "t": time.time()
                    }
                    st.success(f"AUTO ENTRY → {d['side'].upper()} ×{size}")
                except Exception as e:
                    st.error(f"Entry failed: {e}")


main_loop()

# ====================== TRADE HISTORY ======================
st.divider()
st.subheader("Trade History")
try:
    rows = db.execute(
        "SELECT ticker, side, entry_price, exit_price, pnl, status, closed_at "
        "FROM trades ORDER BY id DESC LIMIT 12").fetchall()
    if rows:
        for r in rows:
            color = "green" if r[4] and r[4] > 0 else "red"
            st.markdown(
                f"`{r[0]}` {r[1].upper()} | {r[2]:.2f} → {r[3]:.2f} | "
                f":{color}[{r[4] * 100:+.1f}%] | {r[5]}")
    else:
        st.write("No trades yet")
except Exception:
    st.write("No history yet")

st.divider()
calibration_panel()

st.caption("Full Auto + Sidebar Controls + TP/SL + Prediction-Flip Exits + Logging")
