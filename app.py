import math, time, os, base64, uuid, sqlite3
import requests, streamlit as st
import streamlit.components.v1 as components
from datetime import datetime, timezone, timedelta
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


# ====================== ASSETS ======================
BASE = "https://api.elections.kalshi.com/trade-api/v2"

# series ticker, Coinbase product, TradingView symbol
# NOTE: KXXRP15M is confirmed on Kalshi. KXDOGE15M is my best guess at the
# DOGE series name; if DOGE shows "No open market", check the real ticker on kalshi.com.
ASSETS = {
    "BTC": ("KXBTC15M", "BTC-USD", "COINBASE:BTCUSD"),
    "ETH": ("KXETH15M", "ETH-USD", "COINBASE:ETHUSD"),
    "SOL": ("KXSOL15M", "SOL-USD", "COINBASE:SOLUSD"),
    "XRP": ("KXXRP15M", "XRP-USD", "COINBASE:XRPUSD"),
    "DOGE": ("KXDOGE15M", "DOGE-USD", "COINBASE:DOGEUSD"),
}

MIN_PRICE = 0.12        # never buy contracts cheaper than 12 cents (lottery tickets)
SCALP_MAX_PRICE = 0.92  # never buy anything above this
MAX_SECS = 680          # don't enter too early either
PROVEN_MIN_SAMPLES = 40  # settled signals needed before the bot counts as "proven"

# ====================== SIDEBAR CONTROLS ======================
st.sidebar.title("Settings")

AUTO_TRADE = st.sidebar.toggle("Auto Trade (OFF = log signals only)", value=False)

st.sidebar.subheader("Entry rules")
MIN_EDGE = st.sidebar.slider("Minimum edge after fees (%)", 1, 15, 4) / 100
MIN_SECS_LEFT = st.sidebar.slider("No new entries with less than (sec) left", 30, 300, 120)
SAFE_MAX_PRICE = st.sidebar.slider("Max contract price for SAFE buys (%)", 55, 90, 72) / 100
SCALP_ON = st.sidebar.toggle("Enable SCALP signals", value=False)
SCALP_TP = st.sidebar.slider("Scalp profit target (%)", 3, 15, 6) / 100
SCALP_SL = st.sidebar.slider("Scalp stop loss (%)", 4, 12, 6) / 100

st.sidebar.subheader("Sizing")
BANKROLL = st.sidebar.number_input("Bankroll for stake suggestions ($)", 5.0, 10000.0, 20.0, step=5.0)
KELLY = st.sidebar.slider("Kelly fraction (smaller = safer)", 0.05, 0.50, 0.15, step=0.05)
MAX_STAKE_PCT = st.sidebar.slider("Max stake per trade (% of bankroll)", 1, 10, 5) / 100
BET_MIN, BET_MAX = st.sidebar.slider("Price per bet range: min / max ($)", 0.5, 30.0, (1.0, 2.0), step=0.5)
FIXED_BET = st.sidebar.toggle("Always bet the max amount (skip Kelly)", value=False)
MAX_CONTRACTS = st.sidebar.slider("Max contracts per trade", 1, 8, 3)

st.sidebar.subheader("Safety")
PAUSE_ON_LOSSES = st.sidebar.toggle("Pause new calls after 3 losses in a row", value=True)
FOLLOW_KALSHI = st.sidebar.toggle(
    "Follow Kalshi unless the bot has proven it is more accurate", value=True)
NEVER_AGAINST = st.sidebar.slider(
    "Never go against Kalshi when it is this sure (%)", 60, 95, 70) / 100
SHRINK = st.sidebar.slider("Trust market over model %", 0, 100, 70) / 100
MAX_EDGE = st.sidebar.slider("Skip if edge above (%) - likely model error", 6, 30, 8) / 100

st.sidebar.subheader("Exits for SAFE trades")
TAKE_PROFIT = st.sidebar.slider("Take Profit %", 8, 30, 20) / 100
STOP_LOSS = st.sidebar.slider("Stop Loss %", -20, -5, -20) / 100
FORCE_CLOSE_SECS = st.sidebar.slider("Force close under (seconds)", 20, 60, 40)
FLIP_EXIT = st.sidebar.slider(
    "Exit if my side's win chance drops below %", 25, 50, 40) / 100
PROFIT_LOCK = st.sidebar.slider(
    "Lock profit if my side's win chance below %", 40, 60, 50) / 100
MIN_HOLD = st.sidebar.slider("Min hold before flip exit (sec)", 0, 90, 30)

st.sidebar.subheader("Assets")
picked = st.sidebar.multiselect(
    "Assets", list(ASSETS.keys()), default=["BTC", "ETH", "SOL"])
SHOW_TV = st.sidebar.toggle("TradingView charts", value=False)

st.sidebar.markdown("---")
st.sidebar.caption("A market you trade or cash out of is locked: the bot will not buy back in.")

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
# Persistent lock: any market in this table can never be entered again.
db.execute("""
CREATE TABLE IF NOT EXISTS blocked (
    ticker TEXT PRIMARY KEY, reason TEXT, at TEXT
)""")
db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
db.commit()

if "entry_data" not in st.session_state:
    st.session_state.entry_data = {}
if "held" not in st.session_state:
    st.session_state.held = set()


# ====================== LOCKS / PAUSE ======================
def block_ticker(ticker, reason):
    db.execute("INSERT OR IGNORE INTO blocked (ticker, reason, at) VALUES (?,?,?)",
               (ticker, reason, datetime.now(timezone.utc).isoformat()))
    db.commit()


def is_blocked(ticker):
    return db.execute("SELECT 1 FROM blocked WHERE ticker=?", (ticker,)).fetchone() is not None


def meta_get(k, default=0):
    row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return int(row[0]) if row else default


def meta_set(k, v):
    db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (k, str(v)))
    db.commit()


def losing_streak():
    reset_id = meta_get("pause_reset_id", 0)
    rows = db.execute(
        "SELECT pnl FROM trades WHERE id > ? ORDER BY id DESC LIMIT 3", (reset_id,)).fetchall()
    return len(rows) == 3 and all((r[0] or 0) < 0 for r in rows)


def bot_proven():
    rows = db.execute(
        "SELECT model_up, mid, result FROM signals WHERE result IS NOT NULL").fetchall()
    n = len(rows)
    if n < PROVEN_MIN_SAMPLES:
        return False, n
    bm = sum((r[0] - (r[2] == "yes")) ** 2 for r in rows) / n
    bk = sum((r[1] - (r[2] == "yes")) ** 2 for r in rows) / n
    return bm < bk, n


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


@st.cache_data(ttl=4, show_spinner=False)
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
    series, product, _tv = ASSETS[asset]
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

    # ---------- gates ----------
    why = []
    min_secs = max(MIN_SECS_LEFT, FORCE_CLOSE_SECS + 15)
    if best < MIN_EDGE:
        why.append(f"edge {best * 100:.1f}% is under the {MIN_EDGE * 100:.0f}% minimum")
    if best > MAX_EDGE:
        why.append("edge looks too big to be real")
    if secs < min_secs:
        why.append("too close to expiry")
    if secs > MAX_SECS:
        why.append("too early in the window")
    if cost < MIN_PRICE:
        why.append("price too low (lottery ticket)")

    mode = None
    if cost <= SAFE_MAX_PRICE:
        mode = "SAFE"
    elif SCALP_ON and cost <= SCALP_MAX_PRICE:
        mode = "SCALP"
    else:
        why.append("price above SAFE max" + ("" if SCALP_ON else " (scalps off)"))

    favored = "yes" if mid >= 0.5 else "no"
    market_conf = max(mid, 1 - mid)
    if side != favored and market_conf >= NEVER_AGAINST:
        why.append(f"Kalshi is {market_conf * 100:.0f}% sure the other way")
    proven, n_settled = bot_proven()
    if FOLLOW_KALSHI and not proven and side != favored:
        why.append(f"bot unproven ({n_settled}/{PROVEN_MIN_SAMPLES} settled), only trading with Kalshi")
    if is_blocked(ticker):
        why.append("already traded or closed this market")
    if PAUSE_ON_LOSSES and losing_streak():
        why.append("paused after 3 losses in a row")

    should_trade = (not why) and mode is not None

    return {
        "asset": asset, "ticker": ticker, "spot": spot, "strike": strike,
        "secs": secs, "up": up, "adj": adj, "mid": mid, "edge": best,
        "side": side, "cost": cost, "mode": mode, "why": why,
        "should_trade": should_trade, "yes_ask": ya, "yes_bid": yb
    }


def size_for(d, balance):
    """Fractional Kelly for a binary contract, capped by stake % and max contracts."""
    c = d["cost"]
    p = d["adj"] if d["side"] == "yes" else 1 - d["adj"]
    f = (p - c - fee(c)) / (1 - c) if c < 1 else 0
    f = max(f, 0)
    cash_cap = max(balance, 0) * 0.9
    if FIXED_BET:
        wanted = BET_MAX
    else:
        wanted = min(KELLY * f * BANKROLL, MAX_STAKE_PCT * BANKROLL)
    # Keep every bet between your min and max (the min overrides Kelly / stake %)
    stake = min(max(wanted, BET_MIN), BET_MAX, cash_cap)
    n = int(stake // c) if c > 0 else 0
    if n == 0 and 0 < c <= min(BET_MAX, cash_cap):
        n = 1   # one contract still fits inside your max bet
    n = max(0, min(MAX_CONTRACTS, n))
    return n, n * c


@st.cache_data(ttl=15, show_spinner=False)
def get_balance():
    path = "/portfolio/balance"
    r = requests.get(BASE + path,
                     headers=kalshi_headers("GET", f"/trade-api/v2{path}"), timeout=8)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=4, show_spinner=False)
def get_positions():
    path = "/portfolio/positions"
    r = requests.get(BASE + path,
                     headers=kalshi_headers("GET", f"/trade-api/v2{path}"), timeout=8)
    r.raise_for_status()
    return r.json().get("market_positions", [])


def place_order(ticker, side, count, price_cents):
    if count > MAX_CONTRACTS:
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
    Holding NO  -> buy YES back (bid at/above the ask so it crosses)."""
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
    rows = db.execute("SELECT ticker FROM signals WHERE result IS NULL ORDER BY id LIMIT 5").fetchall()
    for (tk,) in rows:
        try:
            path = f"/markets/{tk}"
            r = requests.get(BASE + path,
                             headers=kalshi_headers("GET", f"/trade-api/v2{path}"), timeout=8)
            r.raise_for_status()
            mk = r.json().get("market", {}) or {}
            res = mk.get("result")
            st.session_state["resolve_note"] = f"{tk}: status={mk.get('status')}, result={res!r}"
            if res in ("yes", "no"):
                db.execute("UPDATE signals SET result=? WHERE ticker=?", (res, tk))
        except Exception as e:
            st.session_state["resolve_note"] = f"{tk}: {e}"
    db.commit()


def proof_counts():
    total = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    settled = db.execute("SELECT COUNT(*) FROM signals WHERE result IS NOT NULL").fetchone()[0]
    return total, settled, total - settled


@st.fragment(run_every=30)
def calibration_panel():
    st.subheader("Is the model actually accurate?")
    resolve_signals()
    total, settled, pending = proof_counts()
    proven, _ = bot_proven()

    st.progress(min(settled / PROVEN_MIN_SAMPLES, 1.0),
                text=f"{settled} / {PROVEN_MIN_SAMPLES} markets settled")
    c1, c2, c3 = st.columns(3)
    c1.metric("Logged", total)
    c2.metric(f"Settled (counts toward {PROVEN_MIN_SAMPLES})", settled)
    c3.metric("Waiting to settle", pending)
    st.caption("A market is logged when it has 90-240 seconds left, and only while this page is open. "
               "It counts toward the total after Kalshi settles it. "
               f"Status: {'PROVEN' if proven else 'not proven yet'}")

    last = db.execute("SELECT ticker, logged_at FROM signals ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        st.caption(f"Last market logged: {last[0]} at {last[1][:19]} UTC")
    else:
        st.caption("Nothing logged yet. Keep this page open and a market will be logged within 15 minutes.")

    data_dir = os.path.dirname(DB_PATH)
    if not os.path.ismount(data_dir):
        st.warning(f"{data_dir} is not a mounted Fly volume, so this log can be wiped on every deploy or "
                   "restart and the count would start over. Check the [mounts] section of fly.toml.")

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    stale = db.execute(
        "SELECT COUNT(*) FROM signals WHERE result IS NULL AND logged_at < ?", (cutoff,)).fetchone()[0]
    if stale:
        st.warning(f"{stale} logged market(s) should have settled by now, but Kalshi hasn't returned a "
                   "result for them. The settlement lookup may not be working.")
    note = st.session_state.get("resolve_note")
    if note:
        st.caption(f"Last settlement check: {note}")

    rows = db.execute(
        "SELECT model_up, adj_up, mid, side, cost, would_trade, result FROM signals "
        "WHERE result IS NOT NULL").fetchall()
    if not rows:
        return
    brier_model = sum((r[0] - (r[6] == "yes")) ** 2 for r in rows) / len(rows)
    brier_mkt = sum((r[2] - (r[6] == "yes")) ** 2 for r in rows) / len(rows)
    d1, d2 = st.columns(2)
    d1.metric("Model error (Brier, lower=better)", f"{brier_model:.3f}")
    d2.metric("Kalshi price error (Brier)", f"{brier_mkt:.3f}",
              "model beats market" if brier_model < brier_mkt else "market beats model",
              delta_color="normal" if brier_model < brier_mkt else "inverse")
    hyp = [r for r in rows if r[5]]
    if hyp:
        pnl, wins = 0.0, 0
        for r in hyp:
            won = (r[3] == r[6])
            wins += won
            pnl += ((1 - r[4]) if won else -r[4]) - fee(r[4])
        st.write(f"Signals the bot would have traded: {len(hyp)} | win rate {wins / len(hyp) * 100:.0f}% "
                 f"| hypothetical PnL per 1 contract each: **${pnl:+.2f}**")


def tradingview(symbol, height=380):
    components.html(f"""
    <div class="tradingview-widget-container">
      <div id="tv_{abs(hash(symbol))}"></div>
      <script src="https://s3.tradingview.com/tv.js"></script>
      <script>
        new TradingView.widget({{
          "width": "100%", "height": {height}, "symbol": "{symbol}",
          "interval": "1", "timezone": "Etc/UTC", "theme": "dark", "style": "1",
          "locale": "en", "enable_publishing": false, "hide_top_toolbar": false,
          "allow_symbol_change": false, "container_id": "tv_{abs(hash(symbol))}"
        }});
      </script>
    </div>""", height=height + 10)


# ====================== MAIN UI ======================
st.title("Kalshi Full Auto Bot")
st.caption(f"AUTO={AUTO_TRADE} | Min edge {MIN_EDGE * 100:.0f}% | SAFE ≤ {SAFE_MAX_PRICE * 100:.0f}% | "
           f"SCALP={'on' if SCALP_ON else 'off'} | Kelly {KELLY:.2f}")

try:
    bal = get_balance()
    balance = bal.get("balance", 0) / 100
    st.session_state["last_balance"] = balance
    st.metric("Available Balance", f"${balance:.2f}")
except Exception as e:
    balance = st.session_state.get("last_balance", 0)
    st.metric("Available Balance", f"${balance:.2f}")
    st.caption(f"Balance temporarily unavailable ({e}). Showing last known value.")

if PAUSE_ON_LOSSES and losing_streak():
    st.warning("Paused: the last 3 closed trades were losses. No new entries until you resume.")
    if st.button("Resume trading"):
        last_id = db.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0]
        meta_set("pause_reset_id", last_id)
        st.rerun()


@st.fragment(run_every=10)
def main_loop():
    try:
        bal_now = get_balance().get("balance", 0) / 100
        st.session_state["last_balance"] = bal_now
    except Exception:
        bal_now = st.session_state.get("last_balance", balance)

    # ---------------- Positions: lock markets + auto cashout ----------------
    try:
        positions = get_positions()
        now_held = set()
        for p in positions:
            tk = p.get("ticker")
            yc = int(float(p.get("yes_count", 0) or 0))
            nc = int(float(p.get("no_count", 0) or 0))
            if tk and (yc > 0 or nc > 0):
                now_held.add(tk)
                block_ticker(tk, "held")          # never add to a market we're already in

        # Anything we held last cycle that is gone now was cashed out (by you on
        # Kalshi, or by the bot). Lock it so the bot can't buy back in.
        for tk in st.session_state.held - now_held:
            block_ticker(tk, "closed")
            st.session_state.entry_data.pop(tk, None)
        st.session_state.held = now_held

        for p in positions:
            ticker = p.get("ticker")
            yes_c = int(float(p.get("yes_count", 0) or 0))
            no_c = int(float(p.get("no_count", 0) or 0))
            if yes_c == 0 and no_c == 0:
                continue

            try:
                series = ticker.split("-")[0]
                m = market(series)
                if not m or m["ticker"] != ticker:
                    continue
                yb = dollars(m, "yes_bid") or 0.4
                ya = dollars(m, "yes_ask") or 0.6
                secs_left = max(0, datetime.fromisoformat(
                    m["close_time"].replace("Z", "+00:00")).timestamp() - time.time())

                entry_info = st.session_state.entry_data.get(ticker, {})
                entry = entry_info.get("entry", 0.5)  # price paid for the side held
                count = entry_info.get("count", yes_c or no_c)
                mode = entry_info.get("mode", "SAFE")
                held_for = time.time() - entry_info.get("t", 0)
                asset = entry_info.get("asset") or next(
                    (a for a, v in ASSETS.items() if ticker.startswith(v[0])), None)

                if yes_c > 0:
                    side, current = "yes", yb
                else:
                    side, current = "no", 1 - ya
                pnl_pct = (current - entry) / entry if entry > 0 else 0
                diff = current - entry

                my_prob = None
                if asset and mode == "SAFE":
                    try:
                        d = analyse(asset)
                        if "err" not in d and d["ticker"] == ticker:
                            my_prob = d["up"] if side == "yes" else 1 - d["up"]
                    except Exception:
                        pass

                prob_txt = f" | win chance {my_prob * 100:.0f}%" if my_prob is not None else ""
                st.write(
                    f"**{ticker}** {mode} {side.upper()} | PnL `{pnl_pct * 100:+.1f}%` "
                    f"({diff * 100:+.1f}%){prob_txt} | {secs_left:.0f}s left")

                status = None
                if secs_left < FORCE_CLOSE_SECS:
                    status = "TIME"
                elif mode == "SCALP":
                    if diff >= SCALP_TP:
                        status = "SCALP-TP"
                    elif diff <= -SCALP_SL:
                        status = "SCALP-SL"
                else:
                    if pnl_pct >= TAKE_PROFIT:
                        status = "TP"
                    elif pnl_pct <= STOP_LOSS:
                        status = "SL"
                    elif my_prob is not None and held_for >= MIN_HOLD:
                        if pnl_pct > 0 and my_prob < PROFIT_LOCK:
                            status = "LOCK"
                        elif pnl_pct <= 0 and my_prob < FLIP_EXIT:
                            status = "FLIP"

                if status:
                    close_position(ticker, yes_c, no_c, yb, ya)
                    block_ticker(ticker, "closed")   # no buying back in
                    log_trade(ticker, asset or "?", side, entry, current, count,
                              pnl_pct, status)
                    st.success(f"AUTO CASHOUT ({status}) → {pnl_pct * 100:+.1f}%")
                    st.session_state.entry_data.pop(ticker, None)
            except Exception as e:
                st.warning(f"Cashout {ticker}: {e}")
    except Exception as e:
        st.warning(f"Positions: {e}")

    st.divider()

    # ---------------- Entries ----------------
    t_all, t_settled, t_pending = proof_counts()
    st.caption(f"Proof progress: {t_settled}/{PROVEN_MIN_SAMPLES} markets settled "
               f"({t_pending} waiting to settle, {t_all} logged)")
    cols = st.columns(len(picked) or 1)
    for i, asset in enumerate(picked):
        with cols[i]:
            try:
                d = analyse(asset)
            except Exception as e:
                st.error(f"{asset}: {e}")
                continue
            if "err" in d:
                st.subheader(asset)
                st.warning(d["err"])
                continue

            st.subheader(asset)
            st.metric("Spot", f"${d['spot']:,.4f}" if d["spot"] < 10 else f"${d['spot']:,.2f}",
                      f"{d['secs']:.0f}s")
            st.progress(int(d["up"] * 100), text=f"P(UP) {d['up'] * 100:.1f}%")
            st.write(f"Model {d['up'] * 100:.0f}% → adj {d['adj'] * 100:.0f}% vs Kalshi {d['mid'] * 100:.0f}%")
            st.write(f"Edge **{d['edge'] * 100:.1f}%** → **{d['side'].upper()}** @ {d['cost'] * 100:.0f}%")

            if 90 < d["secs"] < 240:
                try:
                    log_signal(d)
                except Exception:
                    pass

            if d["should_trade"]:
                n, stake = size_for(d, bal_now)
                st.write(f"Signal: `{d['mode']}` → **{d['side'].upper()}**")
                st.caption(f"Kelly stake ${stake:.2f} → {n} contract(s)")
                if AUTO_TRADE and n > 0 and bal_now > 2 and not is_blocked(d["ticker"]):
                    price_cents = max(5, min(95, int(round(d["cost"] * 100))))
                    try:
                        place_order(d["ticker"], d["side"], n, price_cents)
                        block_ticker(d["ticker"], "entered")   # one trade per market
                        st.session_state.entry_data[d["ticker"]] = {
                            "side": d["side"], "entry": price_cents / 100,
                            "count": n, "asset": asset, "mode": d["mode"], "t": time.time()
                        }
                        st.success(f"AUTO ENTRY {d['mode']} → {d['side'].upper()} ×{n}")
                    except Exception as e:
                        st.error(f"Entry failed: {e}")
                elif AUTO_TRADE and n == 0:
                    st.caption("Stake too small for 1 contract at this price. Raise bankroll or stake %.")
            else:
                st.write("Signal: `wait`")
                if d["why"]:
                    st.caption("; ".join(d["why"]))


main_loop()

# ====================== CHARTS ======================
if SHOW_TV and picked:
    st.divider()
    st.subheader("TradingView")
    tabs = st.tabs(picked)
    for tab, a in zip(tabs, picked):
        with tab:
            tradingview(ASSETS[a][2])

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

st.caption("Full Auto + Locks + Kelly + SAFE/SCALP + Calibration")
