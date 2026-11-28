# =====================================================================
#  Kalshi 15-minute crypto bot
#  - ENGINE (top half): plain Python, runs in a background thread on the
#    server, so alerts / exits / logging keep working with the page closed.
#  - UI (bottom half): sliders + dashboard. Sliders are saved to the database
#    and the engine reads the saved values every cycle.
# =====================================================================
import math, time, os, json, base64, uuid, sqlite3, threading, zlib, hashlib
from datetime import datetime, timezone, timedelta
import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

try:
    from scipy.stats import t as _t
except Exception:
    _t = None

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------
BASE = os.environ.get("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
API_PREFIX = "/trade-api/v2"
DB_PATH = os.path.join(os.environ.get("DB_DIR", "/data"), "trades.db")

# series ticker, Coinbase product, TradingView symbol
# KXXRP15M is confirmed. KXDOGE15M is my best guess; if DOGE says "No open market",
# check the real series ticker on kalshi.com and fix it here.
ASSETS = {
    "BTC":  ("KXBTC15M",  "BTC-USD",  "COINBASE:BTCUSD"),
    "ETH":  ("KXETH15M",  "ETH-USD",  "COINBASE:ETHUSD"),
    "SOL":  ("KXSOL15M",  "SOL-USD",  "COINBASE:SOLUSD"),
    "XRP":  ("KXXRP15M",  "XRP-USD",  "COINBASE:XRPUSD"),
    "DOGE": ("KXDOGE15M", "DOGE-USD", "COINBASE:DOGEUSD"),
}

MIN_PRICE = 0.12          # never buy contracts cheaper than 12% (lottery tickets)
SCALP_MAX_PRICE = 0.92    # never buy anything above this
MAX_SECS = 680            # don't enter too early
TICK_SECONDS = 10         # engine cycle
LOG_WINDOW = (60, 300)    # a market is logged when it has between these many seconds left

DEFAULTS = {
    "auto_trade": False,
    # entry
    "min_edge": 2, "min_secs_left": 91, "safe_max_price": 85,
    "scalp_on": False, "scalp_tp": 6, "scalp_sl": 6,
    # sizing
    "bankroll": 20.0, "kelly": 0.15, "max_stake_pct": 5,
    "bet_min": 1.0, "bet_max": 3.0, "fixed_bet": False, "max_contracts": 3,
    "daily_loss_cap": 3.0,
    # safety / model
    "pause_on_losses": True, "follow_kalshi": True, "never_against": 74,
    "shrink": 70, "max_edge": 8, "tech_weight": 15, "proven_min": 40,
    # exits
    "take_profit": 20, "stop_loss": -20, "force_close": 40,
    "flip_exit": 31, "profit_lock": 45, "min_hold": 20,
    # assets / display
    "assets": ["BTC", "ETH", "SOL"], "show_tv": False,
    # discord
    "discord_on": True, "discord_webhook": "",
    "alert_buy": True, "alert_scalp": True, "alert_wait": True,
    "alert_hold": True, "alert_cash": True, "alert_cut": True, "hold_every": 180,
}

# ---------------------------------------------------------------------
# Database (one connection per code generation, guarded by a lock)
# ---------------------------------------------------------------------
_conn = None
_lock = threading.RLock()


def _init_schema(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT, asset TEXT, side TEXT,
        entry_price REAL, exit_price REAL, count INTEGER,
        pnl REAL, status TEXT, created_at TEXT, closed_at TEXT);
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT UNIQUE, asset TEXT,
        model_up REAL, adj_up REAL, mid REAL,
        side TEXT, cost REAL, edge REAL, would_trade INTEGER,
        secs REAL, logged_at TEXT, result TEXT);
    CREATE TABLE IF NOT EXISTS blocked (ticker TEXT PRIMARY KEY, reason TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS alerts (k TEXT PRIMARY KEY, at REAL);
    CREATE TABLE IF NOT EXISTS held (
        ticker TEXT PRIMARY KEY, asset TEXT, side TEXT, count INTEGER,
        entry REAL, mode TEXT, last_pnl REAL, last_cur REAL,
        opened_at REAL, seen INTEGER, exit_reason TEXT, attempts INTEGER);
    CREATE TABLE IF NOT EXISTS snap (k TEXT PRIMARY KEY, v TEXT, at REAL);
    CREATE TABLE IF NOT EXISTS elog (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, src TEXT, msg TEXT);
    """)
    c.commit()


def dbq(sql, args=(), commit=False):
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
            try:
                _conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            _init_schema(_conn)
        cur = _conn.execute(sql, args)
        rows = cur.fetchall() if cur.description else []
        if commit:
            _conn.commit()
        return rows


def elog(src, err):
    """Keep the last ~100 errors so problems are visible on the page."""
    try:
        dbq("INSERT INTO elog (at, src, msg) VALUES (?,?,?)",
            (time.time(), src, str(err)[:300]), commit=True)
        dbq("DELETE FROM elog WHERE id < (SELECT MAX(id) FROM elog) - 100", commit=True)
    except Exception:
        pass


def meta_get(k, default=None):
    rows = dbq("SELECT v FROM meta WHERE k=?", (k,))
    return rows[0][0] if rows else default


def meta_set(k, v):
    dbq("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (k, str(v)), commit=True)


def put_snap(k, obj):
    dbq("INSERT OR REPLACE INTO snap (k, v, at) VALUES (?,?,?)",
        (k, json.dumps(obj), time.time()), commit=True)


def get_snap(k):
    rows = dbq("SELECT v, at FROM snap WHERE k=?", (k,))
    if not rows:
        return None, None
    try:
        return json.loads(rows[0][0]), rows[0][1]
    except Exception:
        return None, None


def get_settings():
    S = dict(DEFAULTS)
    raw = meta_get("settings")
    if raw:
        try:
            S.update(json.loads(raw))
        except Exception:
            pass
    return S


def save_settings(S):
    new = json.dumps(S, sort_keys=True)
    if meta_get("settings") != new:
        meta_set("settings", new)


# ---------------------------------------------------------------------
# Persistence without a Fly volume (Upstash Redis over HTTPS) + engine lock
# ---------------------------------------------------------------------
MACHINE_ID = os.environ.get("FLY_MACHINE_ID", "local")
ENGINE_ID = f"{MACHINE_ID}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
STATE_KEY = os.environ.get("STATE_KEY", "kalshi_bot_state_v1")
LOCK_KEY = STATE_KEY + ":engine_lock"
STATE_TABLES = ["trades", "signals", "blocked", "meta", "alerts", "held"]
VOLATILE_META = {"last_tick", "engine_started", "balance", "resolve_note", "last_backup",
                 "engine_owner", "engine_hb"}
_remote_ready = False
_last_push_hash, _last_push_at, _last_claim_ok = None, 0.0, 0.0


def remote_on():
    return bool(os.environ.get("UPSTASH_REDIS_REST_URL") and os.environ.get("UPSTASH_REDIS_REST_TOKEN"))


def remote_cmd(cmd):
    r = requests.post(os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/"), json=cmd,
                      headers={"Authorization": "Bearer " + os.environ["UPSTASH_REDIS_REST_TOKEN"]},
                      timeout=10)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data.get("result") if isinstance(data, dict) else data


def dump_state():
    out = {}
    for t in STATE_TABLES:
        cols = [r[1] for r in dbq(f"PRAGMA table_info({t})")]
        rows = dbq(f"SELECT {','.join(cols)} FROM {t}")
        if t == "meta":
            rows = [r for r in rows if r[0] not in VOLATILE_META]
        out[t] = {"cols": cols, "rows": rows}
    return json.dumps(out, separators=(",", ":"), sort_keys=True)


def load_state(blob):
    data = json.loads(zlib.decompress(base64.b64decode(blob)).decode())
    for t in STATE_TABLES:
        if t not in data:
            continue
        cols = data[t]["cols"]
        dbq(f"DELETE FROM {t}")
        for row in data[t]["rows"]:
            dbq(f"INSERT OR REPLACE INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(row))
    dbq("SELECT 1", commit=True)


def remote_pull():
    """Load the latest saved state from Upstash. Marks the remote as safe to write to."""
    global _remote_ready
    blob = remote_cmd(["GET", STATE_KEY])
    if blob:
        load_state(blob)
    _remote_ready = True


def remote_sync_start():
    """Used by the page at startup: restore saved settings into an empty database."""
    global _remote_ready
    if not remote_on() or _remote_ready:
        return
    try:
        fresh = not (dbq("SELECT 1 FROM meta WHERE k='settings'") or dbq("SELECT 1 FROM trades LIMIT 1")
                     or dbq("SELECT 1 FROM signals LIMIT 1"))
        if fresh:
            remote_pull()
        else:
            remote_cmd(["PING"])
            _remote_ready = True
    except Exception as e:
        elog("restore", e)


def maybe_push_backup(force=False):
    """Save everything to Upstash whenever it changes (at most every 30s).
    Never pushes before the remote state is known, so a failed restore cannot wipe the backup."""
    global _last_push_hash, _last_push_at
    if not remote_on() or not _remote_ready:
        return
    now = time.time()
    if not force and now - _last_push_at < 30:
        return
    _last_push_at = now
    state = dump_state()
    h = hashlib.md5(state.encode()).hexdigest()
    if h == _last_push_hash:
        return
    blob = base64.b64encode(zlib.compress(state.encode(), 9)).decode()
    if len(blob) > 900_000:
        elog("backup", "saved state is too large to back up")
        return
    remote_cmd(["SET", STATE_KEY, blob])
    _last_push_hash = h
    meta_set("last_backup", now)


def claim_engine():
    """Only one engine may run. With Upstash this holds across machines; without it, per machine."""
    global _last_claim_ok
    now = time.time()
    if remote_on():
        try:
            got = remote_cmd(["SET", LOCK_KEY, ENGINE_ID, "NX", "EX", 30])
            if got == "OK":
                try:
                    remote_pull()                   # take over with the latest saved state
                except Exception as e:
                    elog("takeover", e)
                    remote_cmd(["DEL", LOCK_KEY])
                    return False
            else:
                if remote_cmd(["GET", LOCK_KEY]) != ENGINE_ID:
                    return False
                remote_cmd(["SET", LOCK_KEY, ENGINE_ID, "EX", 30])
            _last_claim_ok = now
            return True
        except Exception as e:
            elog("lock", e)
            return (now - _last_claim_ok) < 60      # brief outage: keep running if we were the owner
    owner, hb = meta_get("engine_owner"), float(meta_get("engine_hb", 0) or 0)
    if owner and owner != ENGINE_ID and now - hb < 30:
        return False
    meta_set("engine_owner", ENGINE_ID)
    meta_set("engine_hb", now)
    return True


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------
_cache = {}


def cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def block_ticker(ticker, reason):
    dbq("INSERT OR IGNORE INTO blocked (ticker, reason, at) VALUES (?,?,?)",
        (ticker, reason, datetime.now(timezone.utc).isoformat()), commit=True)


def is_blocked(ticker):
    return bool(dbq("SELECT 1 FROM blocked WHERE ticker=?", (ticker,)))


def should_alert(key, cooldown):
    now = time.time()
    rows = dbq("SELECT at FROM alerts WHERE k=?", (key,))
    if rows and now - rows[0][0] < cooldown:
        return False
    dbq("INSERT OR REPLACE INTO alerts (k, at) VALUES (?,?)", (key, now), commit=True)
    return True


def fee_pc(p, n=1):
    """Kalshi taker fee per contract: 0.07*C*P*(1-P) rounded UP to the next cent per order."""
    n = max(1, int(n))
    return math.ceil(0.07 * n * p * (1 - p) * 100 - 1e-9) / 100.0 / n


def cdf(x, df=5):
    if _t is not None:
        return float(_t.cdf(x * math.sqrt(df / (df - 2)), df))
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


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


def settle_stat(spot, secs, ticks, close_ts):
    """Kalshi settles on the average of the final 60 seconds vs the market's target
    (floor_strike). Estimate that average and its uncertainty."""
    L = 60.0
    if secs > L + 25:
        return spot, secs * 0.62
    elif secs > L:
        return spot, (secs - L) + L / 3.2
    recent = [p for t, p in ticks if t >= close_ts - L]
    if recent:
        avg = sum(recent) / len(recent)
        w = secs / L
        return w * spot + (1 - w) * avg, max(secs * 0.32, 0.65)
    return spot, max(secs * 0.4, 0.65)


def dollars(m, key):
    v = m.get(f"{key}_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(key)
    return float(v) / 100 if v not in (None, "") else None


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def asset_from_ticker(ticker):
    return next((a for a, v in ASSETS.items() if ticker.startswith(v[0])), None)


def fmt_secs(s):
    s = int(max(0, s))
    return f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------------
# Kalshi API
# ---------------------------------------------------------------------
_key = None


def _private_key():
    global _key
    if _key is None:
        pem = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "").replace("\\n", "\n")
        _key = serialization.load_pem_private_key(pem.encode(), password=None)
    return _key


def _sign(msg):
    k = _private_key()
    if isinstance(k, rsa.RSAPrivateKey):
        return k.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                       salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return k.sign(msg)


def kalshi_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path.split('?')[0]}".encode()
    return {
        "KALSHI-ACCESS-KEY": os.environ.get("KALSHI_KEY_ID", ""),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(_sign(msg)).decode(),
        "Content-Type": "application/json",
    }


def kget(path, params=None):
    r = requests.get(BASE + path, params=params,
                     headers=kalshi_headers("GET", API_PREFIX + path), timeout=8)
    r.raise_for_status()
    return r.json()


def get_open_market(series):
    def f():
        ms = kget("/markets", {"series_ticker": series, "status": "open", "limit": 8}).get("markets", [])
        now = time.time()
        open_ms = [m for m in ms if parse_ts(m["close_time"]) > now]
        return min(open_ms, key=lambda m: m["close_time"]) if open_ms else None
    return cached(("mk", series), 4, f)


def get_market(ticker, fresh=False):
    def f():
        return kget(f"/markets/{ticker}").get("market", {}) or {}
    if fresh:
        return f()
    return cached(("mt", ticker), 4, f)


def fetch_balance():
    raw = cached("bal", 15, lambda: kget("/portfolio/balance"))
    if raw.get("balance") is not None:
        return float(raw["balance"]) / 100.0
    return float(raw.get("balance_dollars", 0) or 0)


def fetch_positions():
    """Open positions. Kalshi returns position_fp: positive = YES contracts, negative = NO."""
    data = cached("pos", 4, lambda: kget("/portfolio/positions",
                                         {"count_filter": "position", "limit": 200}))
    out = []
    for p in data.get("market_positions", []) or []:
        q = p.get("position_fp", p.get("position"))
        try:
            q = float(q)
        except (TypeError, ValueError):
            q = 0.0
        if abs(q) < 0.5:
            continue
        n = int(round(abs(q)))
        try:
            expo = float(p.get("market_exposure_dollars") or 0)
        except (TypeError, ValueError):
            expo = 0.0
        out.append({"ticker": p.get("ticker"), "side": "yes" if q > 0 else "no",
                    "count": n, "entry": (expo / n) if (n and expo > 0) else None})
    return out


def place_order(ticker, side, count, price, tif, reduce_only=False):
    """V2 order. side 'bid' = buy YES, 'ask' = sell YES (selling YES == buying NO at 1-price)."""
    path = "/portfolio/events/orders"
    body = {
        "ticker": ticker, "side": side, "count": f"{count:.2f}", "price": f"{price:.4f}",
        "time_in_force": tif, "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": str(uuid.uuid4()),
    }
    if reduce_only:
        body["reduce_only"] = True
    r = requests.post(BASE + path, json=body,
                      headers=kalshi_headers("POST", API_PREFIX + path), timeout=12)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
    return r.json()


def enter_order(ticker, side, n, ya, yb):
    """Buy with an immediate-or-cancel order so nothing is left resting."""
    if side == "yes":
        return place_order(ticker, "bid", n, min(0.99, round((ya or 0.5) + 0.01, 4)), "immediate_or_cancel")
    return place_order(ticker, "ask", n, max(0.01, round((yb or 0.5) - 0.01, 4)), "immediate_or_cancel")


def close_order(ticker, side, n, yb, ya, slip):
    """Sell by crossing the book, reduce-only so it can never flip the position."""
    if side == "yes":
        if yb is None:
            raise ValueError("no yes_bid to sell into")
        return place_order(ticker, "ask", n, max(0.01, round(yb - slip, 4)),
                           "immediate_or_cancel", reduce_only=True)
    if ya is None:
        raise ValueError("no yes_ask to buy back")
    return place_order(ticker, "bid", n, min(0.99, round(ya + slip, 4)),
                       "immediate_or_cancel", reduce_only=True)


# ---------------------------------------------------------------------
# Coinbase prices
# ---------------------------------------------------------------------
def cb_candles(product, gran):
    def f():
        r = requests.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                         params={"granularity": gran}, timeout=7)
        r.raise_for_status()
        return [x[4] for x in reversed(r.json()[:120])]
    try:
        return cached(("cb", product, gran), 25 if gran == 60 else 120, f)
    except Exception as e:
        elog(f"coinbase candles {product}", e)
        return []


def cb_price(product):
    def f():
        r = requests.get(f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    try:
        return cached(("px", product), 3, f)
    except Exception as e:
        elog(f"coinbase price {product}", e)
        return None


def multi_tf_score(product):
    tfs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
    weights = {"1m": 0.18, "5m": 0.32, "15m": 0.32, "1h": 0.18}
    total, count = 0.0, 0
    for name, gran in tfs.items():
        closes = cb_candles(product, gran)
        if len(closes) < 16:
            continue
        r = rsi(closes)
        e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
        mom = (closes[-1] / closes[-3] - 1) if len(closes) > 3 else 0
        score = 0
        score += 0.42 if r > 56 else -0.42 if r < 44 else 0
        score += 0.33 if e9 > e21 else -0.33
        score += 0.25 if mom > 0.0012 else -0.25 if mom < -0.0012 else 0
        total += score * weights[name]
        count += 1
    return total / count if count else 0.0


# ---------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------
_ticks = {}


def bot_proven(S):
    rows = dbq("SELECT model_up, mid, result FROM signals WHERE result IS NOT NULL")
    n = len(rows)
    if n < S["proven_min"]:
        return False, n
    bm = sum((r[0] - (1.0 if r[2] == "yes" else 0.0)) ** 2 for r in rows) / n
    bk = sum((r[1] - (1.0 if r[2] == "yes" else 0.0)) ** 2 for r in rows) / n
    return bm < bk, n


def losing_streak():
    reset_id = int(meta_get("pause_reset_id", 0) or 0)
    rows = dbq("SELECT pnl FROM trades WHERE id > ? ORDER BY id DESC LIMIT 3", (reset_id,))
    return len(rows) == 3 and all((r[0] or 0) < 0 for r in rows)


def daily_pnl_usd():
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = dbq("SELECT COALESCE(SUM(pnl*entry_price*count),0) FROM trades WHERE closed_at >= ?", (start,))
    return float(rows[0][0] or 0.0)


def analyse(asset, S):
    series, product, _tv = ASSETS[asset]
    m = get_open_market(series)
    if not m:
        return {"asset": asset, "err": "No open market"}

    strike = float(m["floor_strike"])
    ticker = m["ticker"]
    close_ts = parse_ts(m["close_time"])
    closes = cb_candles(product, 60)
    spot = cb_price(product) or (closes[-1] if closes else None)
    if not spot:
        return {"asset": asset, "err": "No price"}

    tb = _ticks.setdefault(asset, [])
    tb.append((time.time(), spot))
    del tb[:-450]

    secs = max(close_ts - time.time(), 1)
    sig = sigma_ps(closes)
    mu, vf = settle_stat(spot, secs, tb, close_ts)
    z = math.log(mu / strike) / math.sqrt(sig * sig * vf + 0.00018 ** 2)
    p_stat = cdf(z)

    tw = S["tech_weight"] / 100.0
    p_tech = 0.5 + multi_tf_score(product) * 0.41
    up = max(0.035, min(0.965, (1 - tw) * p_stat + tw * p_tech))

    ya, yb = dollars(m, "yes_ask"), dollars(m, "yes_bid")
    mid = (ya + yb) / 2 if ya and yb else (ya or yb or 0.5)

    shrink = S["shrink"] / 100.0
    adj = (1 - shrink) * up + shrink * mid

    cost_yes = ya if ya else mid
    cost_no = (1 - yb) if yb else (1 - mid)
    n_est = max(1, int(S["max_contracts"]))
    edge_yes = adj - cost_yes - fee_pc(cost_yes, n_est)
    edge_no = (1 - adj) - cost_no - fee_pc(cost_no, n_est)
    best = max(edge_yes, edge_no)
    side = "yes" if edge_yes > edge_no else "no"
    cost = cost_yes if side == "yes" else cost_no

    why = []
    min_secs = max(S["min_secs_left"], S["force_close"] + 15)
    if best < S["min_edge"] / 100.0:
        why.append(f"edge {best * 100:.1f}% is under the {S['min_edge']}% minimum")
    if best > S["max_edge"] / 100.0:
        why.append("edge looks too big to be real")
    if secs < min_secs:
        why.append("too close to expiry")
    if secs > MAX_SECS:
        why.append("too early in the window")
    if cost < MIN_PRICE:
        why.append("price too low (lottery ticket)")

    mode = None
    if cost <= S["safe_max_price"] / 100.0:
        mode = "SAFE"
    elif S["scalp_on"] and cost <= SCALP_MAX_PRICE:
        mode = "SCALP"
    else:
        why.append("price above SAFE max" + ("" if S["scalp_on"] else " (scalps off)"))

    favored = "yes" if mid >= 0.5 else "no"
    conf = max(mid, 1 - mid)
    if side != favored and conf >= S["never_against"] / 100.0:
        why.append(f"Kalshi is {conf * 100:.0f}% sure the other way")
    proven, n_settled = bot_proven(S)
    if S["follow_kalshi"] and not proven and side != favored:
        why.append(f"bot unproven ({n_settled}/{S['proven_min']} settled), only trading with Kalshi")
    if is_blocked(ticker):
        why.append("already traded or closed this market")
    if S["pause_on_losses"] and losing_streak():
        why.append("paused after 3 losses in a row")
    if daily_pnl_usd() <= -abs(S["daily_loss_cap"]):
        why.append("daily loss cap reached")

    return {
        "asset": asset, "ticker": ticker, "spot": spot, "strike": strike, "close_ts": close_ts,
        "secs": secs, "up": up, "adj": adj, "mid": mid, "edge": best, "side": side,
        "cost": cost, "mode": mode, "why": why,
        "should_trade": (not why) and mode is not None, "yes_ask": ya, "yes_bid": yb,
    }


def size_for(S, d, bal):
    """Fractional Kelly, then clamped into your min/max bet and max contracts."""
    c = d["cost"]
    p = d["adj"] if d["side"] == "yes" else 1 - d["adj"]
    f = max((p - c - fee_pc(c, 1)) / (1 - c), 0) if c < 1 else 0
    cash_cap = max(bal or 0, 0) * 0.9
    if S["fixed_bet"]:
        wanted = S["bet_max"]
    else:
        wanted = min(S["kelly"] * f * S["bankroll"], S["max_stake_pct"] / 100.0 * S["bankroll"])
    stake = min(max(wanted, S["bet_min"]), S["bet_max"], cash_cap)
    n = int(stake // c) if c > 0 else 0
    if n == 0 and 0 < c <= min(S["bet_max"], cash_cap):
        n = 1
    n = max(0, min(int(S["max_contracts"]), n))
    return n, n * c


# ---------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------
def webhook_url(S):
    return (os.environ.get("DISCORD_WEBHOOK_URL") or S.get("discord_webhook") or "").strip()


def send_discord(S, text):
    url = webhook_url(S)
    if not url or not S.get("discord_on", True):
        return False
    try:
        r = requests.post(url, json={"content": text[:1900], "username": "Kalshi Bot"}, timeout=6)
        if r.status_code not in (200, 204):
            elog("discord", f"{r.status_code}: {r.text[:120]}")
            return False
        return True
    except Exception as e:
        elog("discord", e)
        return False


def side_label(side):
    return "YES (price UP)" if side == "yes" else "NO (price DOWN)"


def msg_buy(S, d, n):
    auto = S["auto_trade"]
    return (f"🟢 **BUY {side_label(d['side'])} · {d['asset']}** at **{d['cost'] * 100:.0f}%**\n"
            f"Model {d['adj'] * 100:.0f}% vs Kalshi {d['mid'] * 100:.0f}% · edge {d['edge'] * 100:.1f}% · "
            f"{fmt_secs(d['secs'])} left\n"
            f"Suggested size: {n} contract(s) (~${n * d['cost']:.2f})\n"
            f"Plan: cash out at +{S['take_profit']}% · cut losses at {S['stop_loss']}%\n"
            + ("🤖 Auto-trade is placing this order." if auto else "👤 Alert only: place it yourself on Kalshi."))


def msg_scalp(S, d, n):
    c = d["cost"] * 100
    return (f"⚡ **SCALP {side_label(d['side'])} · {d['asset']}**\n"
            f"Buy at **{c:.0f}%** → sell at **{c + S['scalp_tp']:.0f}%** · "
            f"cut losses at **{c - S['scalp_sl']:.0f}%**\n"
            f"Edge {d['edge'] * 100:.1f}% · {fmt_secs(d['secs'])} left · ~{n} contract(s)\n"
            + ("🤖 Auto-trade is placing this order." if S["auto_trade"] else "👤 Alert only."))


def msg_wait(S, d):
    reason = "; ".join(d["why"]) if d["why"] else "no edge"
    return (f"⏳ **WAIT · {d['asset']}**: {reason}\n"
            f"Model {d['adj'] * 100:.0f}% vs Kalshi {d['mid'] * 100:.0f}% · {fmt_secs(d['secs'])} left")


REASONS = {
    "TP": "take-profit target hit", "LOCK": "model now says it ends against you, lock the profit",
    "TIME": "market is about to close", "SL": "stop-loss hit",
    "FLIP": "model flipped against your side", "SCALP-TP": "scalp target hit",
    "SCALP-SL": "scalp stop hit",
}


def exit_kind(status, pnl):
    if status in ("TP", "LOCK", "SCALP-TP"):
        return "CASH"
    if status in ("SL", "FLIP", "SCALP-SL"):
        return "CUT"
    return "CASH" if pnl >= 0 else "CUT"


def msg_exit(S, kind, pos):
    head = "💰 **CASH OUT" if kind == "CASH" else "🔴 **CUT LOSSES"
    tail = ("🤖 Selling automatically." if S["auto_trade"]
            else "👤 Alert only: sell it yourself on Kalshi.")
    return (f"{head} {pos['side'].upper()} · {pos['asset']}** at ~{pos['current'] * 100:.0f}% "
            f"({pos['pnl'] * 100:+.1f}%)\n"
            f"Reason: {REASONS.get(pos['status'], pos['status'])} · {fmt_secs(pos['secs_left'])} left\n{tail}")


def msg_hold(S, pos):
    wc = f" · win chance {pos['my_prob'] * 100:.0f}%" if pos.get("my_prob") is not None else ""
    ent = f" (bought at {pos['entry'] * 100:.0f}%)" if pos.get("entry") else ""
    return (f"✋ **HOLD {pos['side'].upper()} · {pos['asset']}**: {pos['pnl'] * 100:+.1f}% "
            f"at {pos['current'] * 100:.0f}%{ent}{wc} · {fmt_secs(pos['secs_left'])} left")


# ---------------------------------------------------------------------
# Positions: exits, alerts, locks, trade log
# ---------------------------------------------------------------------
HELD_COLS = ["ticker", "asset", "side", "count", "entry", "mode", "last_pnl", "last_cur",
             "opened_at", "seen", "exit_reason", "attempts"]


def held_rows():
    rows = dbq(f"SELECT {', '.join(HELD_COLS)} FROM held")
    return {r[0]: dict(zip(HELD_COLS, r)) for r in rows}


def upsert_held(row):
    dbq(f"INSERT OR REPLACE INTO held ({', '.join(HELD_COLS)}) VALUES ({', '.join('?' * len(HELD_COLS))})",
        tuple(row.get(c) for c in HELD_COLS), commit=True)


def log_trade(ticker, asset, side, entry, exit_p, count, pnl, status):
    now = datetime.now(timezone.utc).isoformat()
    dbq("INSERT INTO trades (ticker, asset, side, entry_price, exit_price, count, pnl, status, created_at, closed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ticker, asset, side, entry, exit_p, count, pnl, status, now, now), commit=True)


def decide_exit(S, pos, secs_left, mode, held_for, my_prob):
    pnl, diff = pos["pnl"], pos["diff"]
    have_entry = pos["entry"] is not None
    if secs_left < S["force_close"]:
        return "TIME"
    if not have_entry:
        return None
    if mode == "SCALP":
        if diff >= S["scalp_tp"] / 100.0:
            return "SCALP-TP"
        if diff <= -S["scalp_sl"] / 100.0:
            return "SCALP-SL"
        return None
    if pnl >= S["take_profit"] / 100.0:
        return "TP"
    if pnl <= S["stop_loss"] / 100.0:
        return "SL"
    if my_prob is not None and held_for >= S["min_hold"]:
        if pnl > 0 and my_prob < S["profit_lock"] / 100.0:
            return "LOCK"
        if pnl <= 0 and my_prob < S["flip_exit"] / 100.0:
            return "FLIP"
    return None


def handle_positions(S, positions, A):
    now = time.time()
    held = held_rows()
    seen_now, snap_positions = set(), []

    for p in positions:
        t = p["ticker"]
        seen_now.add(t)
        block_ticker(t, "held")                      # never add to a market we already hold
        row = held.get(t)
        try:
            m = get_market(t)
        except Exception as e:
            elog(f"market {t}", e)
            continue
        yb, ya = dollars(m, "yes_bid"), dollars(m, "yes_ask")
        side, n = p["side"], p["count"]
        current = yb if side == "yes" else ((1 - ya) if ya is not None else None)
        if current is None:
            continue
        secs_left = max(0.0, parse_ts(m["close_time"]) - now) if m.get("close_time") else 0.0

        entry = p["entry"] or (row["entry"] if row and row["entry"] else None)
        mode = row["mode"] if row else "SAFE"
        opened = row["opened_at"] if row else now
        asset = (row["asset"] if row else None) or asset_from_ticker(t) or "?"
        pnl = (current - entry) / entry if entry else 0.0
        diff = (current - entry) if entry else 0.0

        d = A.get(asset)
        my_prob = None
        if d and d.get("ticker") == t:
            my_prob = d["up"] if side == "yes" else 1 - d["up"]

        pos = {"ticker": t, "asset": asset, "side": side, "count": n, "entry": entry,
               "current": current, "pnl": pnl, "diff": diff, "my_prob": my_prob,
               "secs_left": secs_left, "mode": mode, "status": None}
        status = decide_exit(S, pos, secs_left, mode, now - opened, my_prob)
        pos["status"] = status

        attempts = row["attempts"] if row else 0
        if status and S["auto_trade"]:
            try:
                close_order(t, side, n, yb, ya, min(0.20, 0.02 + 0.03 * attempts))
                attempts += 1
            except Exception as e:
                elog(f"close {t}", e)

        upsert_held({"ticker": t, "asset": asset, "side": side, "count": n, "entry": entry or 0.0,
                     "mode": mode, "last_pnl": pnl, "last_cur": current, "opened_at": opened,
                     "seen": 1, "exit_reason": status, "attempts": attempts})

        if status:
            kind = exit_kind(status, pnl)
            if S["alert_cash" if kind == "CASH" else "alert_cut"] and should_alert(f"{t}|{kind}", 90):
                send_discord(S, msg_exit(S, kind, pos))
        elif S["alert_hold"] and should_alert(f"{t}|HOLD", S["hold_every"]):
            send_discord(S, msg_hold(S, pos))
        snap_positions.append(pos)

    # Positions that disappeared: closed by the bot, closed by you, or settled.
    for t, row in held.items():
        if t in seen_now:
            continue
        if not row["seen"]:
            if now - row["opened_at"] > 25:          # entry order never filled
                dbq("DELETE FROM held WHERE ticker=?", (t,), commit=True)
                elog("entry", f"{t}: order did not fill")
                if S["alert_buy"]:
                    send_discord(S, f"⚠️ The buy on `{t}` did not fill (price moved). No position was opened.")
            continue
        entry = row["entry"] or 0.0
        result, closed_ts = None, 0
        try:
            mk = get_market(t, fresh=True)
            result = mk.get("result")
            closed_ts = parse_ts(mk["close_time"]) if mk.get("close_time") else 0
        except Exception as e:
            elog(f"finalize {t}", e)
        if row["exit_reason"]:
            status, exit_p, pnl = row["exit_reason"], row["last_cur"], row["last_pnl"]
        elif result in ("yes", "no"):
            exit_p = 1.0 if row["side"] == result else 0.0
            pnl = ((exit_p - entry) / entry) if entry else 0.0
            status = "SETTLED"
        elif closed_ts and closed_ts <= now and now - closed_ts < 600:
            continue                                 # waiting for Kalshi to publish the result
        else:
            status, exit_p, pnl = "MANUAL", row["last_cur"], row["last_pnl"]
        log_trade(t, row["asset"], row["side"], entry, exit_p, row["count"], pnl or 0.0, status)
        block_ticker(t, "closed")                    # never buy back into a market we left
        dbq("DELETE FROM held WHERE ticker=?", (t,), commit=True)

    put_snap("positions", snap_positions)


# ---------------------------------------------------------------------
# Signals log
# ---------------------------------------------------------------------
def log_signal(d):
    dbq("INSERT OR IGNORE INTO signals (ticker, asset, model_up, adj_up, mid, side, cost, edge, would_trade, secs, logged_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (d["ticker"], d["asset"], d["up"], d["adj"], d["mid"], d["side"], d["cost"],
         d["edge"], int(d["should_trade"]), d["secs"], datetime.now(timezone.utc).isoformat()),
        commit=True)


_last_resolve = 0.0


def resolve_signals():
    global _last_resolve
    if time.time() - _last_resolve < 60:
        return
    _last_resolve = time.time()
    for (tk,) in dbq("SELECT ticker FROM signals WHERE result IS NULL ORDER BY id LIMIT 6"):
        try:
            mk = get_market(tk, fresh=True)
            res = mk.get("result")
            meta_set("resolve_note", f"{tk}: status={mk.get('status')}, result={res!r}")
            if res in ("yes", "no"):
                dbq("UPDATE signals SET result=? WHERE ticker=?", (res, tk), commit=True)
        except Exception as e:
            meta_set("resolve_note", f"{tk}: {e}")
            elog(f"resolve {tk}", e)


# ---------------------------------------------------------------------
# One engine cycle
# ---------------------------------------------------------------------
def try_enter(S, d, bal):
    n, stake = size_for(S, d, bal)
    if n <= 0 or (bal or 0) < 2:
        return False
    t = d["ticker"]
    block_ticker(t, "entered")                       # one attempt per market, win or lose
    try:
        enter_order(t, d["side"], n, d["yes_ask"], d["yes_bid"])
    except Exception as e:
        elog(f"entry {t}", e)
        if S["alert_buy"]:
            send_discord(S, f"⚠️ Buy failed on `{t}`: {str(e)[:150]}")
        return False
    upsert_held({"ticker": t, "asset": d["asset"], "side": d["side"], "count": n, "entry": d["cost"],
                 "mode": d["mode"], "last_pnl": 0.0, "last_cur": d["cost"], "opened_at": time.time(),
                 "seen": 0, "exit_reason": None, "attempts": 0})
    return True


def engine_tick():
    S = get_settings()
    try:
        bal = fetch_balance()
        meta_set("balance", bal)
    except Exception as e:
        elog("balance", e)
        bal = float(meta_get("balance", 0) or 0)

    A = {}
    for asset in S["assets"]:
        try:
            d = analyse(asset, S)
        except Exception as e:
            elog(f"analyse {asset}", e)
            continue
        A[asset] = d
        put_snap(f"asset:{asset}", d)
        if "err" not in d and LOG_WINDOW[0] < d["secs"] < LOG_WINDOW[1]:
            try:
                log_signal(d)
            except Exception as e:
                elog("log_signal", e)

    try:
        positions = fetch_positions()
    except Exception as e:
        elog("positions", e)
        positions = None
    if positions is not None:                        # never treat a failed fetch as "all closed"
        try:
            handle_positions(S, positions, A)
        except Exception as e:
            elog("handle_positions", e)

    for asset, d in A.items():
        if "err" in d:
            continue
        t = d["ticker"]
        try:
            if is_blocked(t):
                continue                              # we hold or already traded this market
            if d["should_trade"]:
                n, _ = size_for(S, d, bal)
                kind = "SCALP" if d["mode"] == "SCALP" else "BUY"
                if S["alert_scalp" if kind == "SCALP" else "alert_buy"] and should_alert(f"{t}|{kind}", 10 ** 9):
                    send_discord(S, msg_scalp(S, d, n) if kind == "SCALP" else msg_buy(S, d, n))
                if S["auto_trade"]:
                    try_enter(S, d, bal)
            elif LOG_WINDOW[0] < d["secs"] < LOG_WINDOW[1] and S["alert_wait"] \
                    and should_alert(f"{t}|WAIT", 10 ** 9):
                send_discord(S, msg_wait(S, d))
        except Exception as e:
            elog(f"entry/alerts {asset}", e)

    resolve_signals()
    meta_set("last_tick", time.time())


def worker_loop():
    if not claim_engine():
        return                                      # another engine owns it: stay on standby
    time.sleep(2)
    if not claim_engine():
        return                                      # lost a start-up race
    meta_set("engine_started", time.time())
    while True:
        t0 = time.time()
        if not claim_engine():
            return
        try:
            engine_tick()
        except Exception as e:
            elog("tick", e)
        try:
            maybe_push_backup()
        except Exception as e:
            elog("backup", e)
        time.sleep(max(1.0, TICK_SECONDS - (time.time() - t0)))


def engine_forever():
    """Run the engine; if another copy owns it, wait and take over if that one dies."""
    while True:
        try:
            worker_loop()
        except Exception as e:
            elog("engine", e)
        time.sleep(15)


# Started from start.sh as a separate process so the engine runs even if nobody opens the page.
if __name__ == "__main__" and os.environ.get("RUN_ENGINE_ONLY") == "1":
    engine_forever()

# ===== UI =====
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Kalshi Full Auto Bot", page_icon="⚡", layout="wide")

if not os.environ.get("KALSHI_KEY_ID") or not os.environ.get("KALSHI_PRIVATE_KEY_PEM"):
    st.error("Missing secrets: set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PEM with `fly secrets set`.")
    st.stop()
try:
    _private_key()
except Exception as e:
    st.error(f"Could not read KALSHI_PRIVATE_KEY_PEM: {e}")
    st.stop()


@st.cache_resource
def start_worker():
    t = threading.Thread(target=engine_forever, daemon=True, name="kalshi-engine")
    t.start()
    return t


start_worker()
remote_sync_start()

SV = get_settings()


def _i(k):
    return int(SV[k])


# ---------------------------- sidebar ----------------------------
sb = st.sidebar
sb.title("Settings")
S = {}
S["auto_trade"] = sb.toggle("Auto Trade (OFF = alerts only)", value=bool(SV["auto_trade"]))

sb.subheader("Entry rules")
S["min_edge"] = sb.slider("Minimum edge after fees (%)", 1, 15, _i("min_edge"))
S["min_secs_left"] = sb.slider("No new entries with less than (sec) left", 30, 300, _i("min_secs_left"))
S["safe_max_price"] = sb.slider("Max contract price for SAFE buys (%)", 55, 90, _i("safe_max_price"))
S["scalp_on"] = sb.toggle("Enable SCALP signals", value=bool(SV["scalp_on"]))
S["scalp_tp"] = sb.slider("Scalp profit target (%)", 3, 15, _i("scalp_tp"))
S["scalp_sl"] = sb.slider("Scalp stop loss (%)", 4, 12, _i("scalp_sl"))

sb.subheader("Sizing")
S["bankroll"] = sb.number_input("Bankroll for stake suggestions ($)", 5.0, 10000.0,
                                float(SV["bankroll"]), step=5.0)
S["kelly"] = sb.slider("Kelly fraction (smaller = safer)", 0.05, 0.50, float(SV["kelly"]), step=0.05)
S["max_stake_pct"] = sb.slider("Max stake per trade (% of bankroll)", 1, 10, _i("max_stake_pct"))
S["bet_min"], S["bet_max"] = sb.slider("Price per bet range: min / max ($)", 0.5, 30.0,
                                       (float(SV["bet_min"]), float(SV["bet_max"])), step=0.5)
S["fixed_bet"] = sb.toggle("Always bet the max amount (skip Kelly)", value=bool(SV["fixed_bet"]))
S["max_contracts"] = sb.slider("Max contracts per trade", 1, 8, _i("max_contracts"))
S["daily_loss_cap"] = sb.slider("Stop new entries after losing ($) today", 1.0, 50.0,
                                float(SV["daily_loss_cap"]), step=0.5)

sb.subheader("Safety and model")
S["pause_on_losses"] = sb.toggle("Pause new calls after 3 losses in a row", value=bool(SV["pause_on_losses"]))
S["follow_kalshi"] = sb.toggle("Follow Kalshi unless the bot has proven it is more accurate",
                               value=bool(SV["follow_kalshi"]))
S["never_against"] = sb.slider("Never go against Kalshi when it is this sure (%)", 60, 95, _i("never_against"))
S["shrink"] = sb.slider("Trust market over model %", 0, 100, _i("shrink"))
S["max_edge"] = sb.slider("Skip if edge above (%) - likely model error", 6, 30, _i("max_edge"))
S["tech_weight"] = sb.slider("Weight on RSI/EMA indicators (%)", 0, 50, _i("tech_weight"))
S["proven_min"] = sb.slider("Settled markets needed to count as proven", 20, 200, _i("proven_min"))

sb.subheader("Exits")
S["take_profit"] = sb.slider("Take Profit %", 8, 30, _i("take_profit"))
S["stop_loss"] = sb.slider("Stop Loss %", -20, -5, _i("stop_loss"))
S["force_close"] = sb.slider("Force close under (seconds)", 20, 60, _i("force_close"))
S["flip_exit"] = sb.slider("Exit if my side's win chance drops below %", 25, 50, _i("flip_exit"))
S["profit_lock"] = sb.slider("Lock profit if my side's win chance below %", 40, 60, _i("profit_lock"))
S["min_hold"] = sb.slider("Min hold before flip exit (sec)", 0, 90, _i("min_hold"))

sb.subheader("Discord alerts")
S["discord_on"] = sb.toggle("Send Discord alerts", value=bool(SV["discord_on"]))
S["discord_webhook"] = sb.text_input("Webhook URL (or set the DISCORD_WEBHOOK_URL secret)",
                                     value=SV["discord_webhook"], type="password")
S["alert_buy"] = sb.toggle("BUY alerts", value=bool(SV["alert_buy"]))
S["alert_scalp"] = sb.toggle("SCALP alerts (buy at x%, sell at x%)", value=bool(SV["alert_scalp"]))
S["alert_wait"] = sb.toggle("WAIT alerts (once per market)", value=bool(SV["alert_wait"]))
S["alert_hold"] = sb.toggle("HOLD updates", value=bool(SV["alert_hold"]))
S["alert_cash"] = sb.toggle("CASH OUT alerts", value=bool(SV["alert_cash"]))
S["alert_cut"] = sb.toggle("CUT LOSSES alerts", value=bool(SV["alert_cut"]))
S["hold_every"] = sb.slider("HOLD update every (sec)", 60, 600, _i("hold_every"), step=30)
if sb.button("Send test alert"):
    if send_discord(S, "✅ Test alert from your Kalshi bot. Alerts are working."):
        sb.success("Sent. Check your Discord channel.")
    else:
        sb.error("Could not send. Check the webhook URL and that alerts are on.")

sb.subheader("Assets")
S["assets"] = sb.multiselect("Assets", list(ASSETS.keys()),
                             default=[a for a in SV["assets"] if a in ASSETS])
S["show_tv"] = sb.toggle("TradingView charts", value=bool(SV["show_tv"]))
sb.markdown("---")
sb.caption("Settings are saved automatically. The engine runs on the server, so alerts, "
           "exits and logging keep going when this page is closed. A market you trade or "
           "cash out of is locked: the bot will not buy back in.")

save_settings(S)

# ---------------------------- dashboard ----------------------------
st.title("Kalshi Full Auto Bot")


@st.fragment(run_every=5)
def dashboard():
    now = time.time()
    last_tick = float(meta_get("last_tick", 0) or 0)
    age = now - last_tick if last_tick else None
    if age is None:
        st.info("Engine is starting up. The first cycle takes a few seconds.")
    elif age > 60:
        st.error(f"Engine has not run for {age:.0f}s. Check `fly logs`.")
    else:
        st.caption(f"Engine running · last cycle {age:.0f}s ago · "
                   f"Discord {'on' if webhook_url(S) and S['discord_on'] else 'off'}")

    if S["auto_trade"]:
        st.warning("AUTO TRADE IS ON: the bot is placing and closing real orders with your Kalshi money, "
                   "even when this page is closed.")
    else:
        st.caption("Auto Trade is off: the bot only sends alerts and logs signals.")

    bal = float(meta_get("balance", 0) or 0)
    st.metric("Available Balance", f"${bal:.2f}")

    if remote_on():
        lb = float(meta_get("last_backup", 0) or 0)
        st.caption("☁️ Settings and logs are backed up to Upstash"
                   + (f" · last backup {now - lb:.0f}s ago" if lb else " · first backup pending"))
        try:
            owner = cached("lockowner", 20, lambda: remote_cmd(["GET", LOCK_KEY]))
            if owner and not str(owner).startswith(MACHINE_ID + ":"):
                st.warning("The engine is running on a DIFFERENT Fly machine than this page. You have more "
                           "than one machine: delete the extras in the Fly dashboard (Machines) so settings "
                           "you change here reach the engine.")
        except Exception:
            pass
    elif os.path.ismount(os.path.dirname(DB_PATH)):
        st.caption("💾 Settings and logs are saved on the Fly volume")
    else:
        st.warning("Nothing is saved between restarts yet: settings and logs reset when the machine "
                   "restarts. Add the UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN secrets "
                   "(free Upstash database) to fix this.")

    if S["pause_on_losses"] and losing_streak():
        st.warning("Paused: the last 3 closed trades were losses.")
    pnl_today = daily_pnl_usd()
    st.caption(f"Realized today (bot-closed trades): ${pnl_today:+.2f} · cap -${S['daily_loss_cap']:.2f}")

    pos, _ = get_snap("positions")
    if pos:
        st.subheader("Open positions")
        for p in pos:
            tag = f"`{p['status']}`" if p.get("status") else "hold"
            wc = f" · win chance {p['my_prob'] * 100:.0f}%" if p.get("my_prob") is not None else ""
            ent = f"{p['entry'] * 100:.0f}%" if p.get("entry") else "?"
            st.write(f"**{p['asset']} {p['side'].upper()}** ×{p['count']} · bought {ent} → now "
                     f"{p['current'] * 100:.0f}% · PnL **{p['pnl'] * 100:+.1f}%**{wc} · "
                     f"{fmt_secs(max(0, p['secs_left']))} left · {tag}")
        st.divider()

    n_ok = len([a for a in S["assets"]])
    cols = st.columns(n_ok or 1)
    for i, asset in enumerate(S["assets"]):
        with cols[i]:
            st.subheader(asset)
            d, at = get_snap(f"asset:{asset}")
            if not d:
                st.caption("Waiting for the first cycle...")
                continue
            if "err" in d:
                st.warning(d["err"])
                continue
            secs = max(0, d["close_ts"] - now)
            st.metric("Spot", f"${d['spot']:,.4f}" if d["spot"] < 10 else f"${d['spot']:,.2f}",
                      f"{secs:.0f}s left")
            st.progress(int(d["up"] * 100), text=f"P(UP) {d['up'] * 100:.1f}%")
            st.write(f"Model {d['up'] * 100:.0f}% → adj {d['adj'] * 100:.0f}% vs Kalshi {d['mid'] * 100:.0f}%")
            st.write(f"Edge **{d['edge'] * 100:.1f}%** → **{d['side'].upper()}** @ {d['cost'] * 100:.0f}%")
            if d["should_trade"]:
                st.write(f"Signal: `{d['mode']}` → **{d['side'].upper()}**")
            else:
                st.write("Signal: `wait`")
                if d["why"]:
                    st.caption("; ".join(d["why"]))
            logged = dbq("SELECT 1 FROM signals WHERE ticker=?", (d["ticker"],))
            if logged:
                st.caption("Logged for accuracy test ✓")
            elif secs >= LOG_WINDOW[1]:
                st.caption(f"Will be logged in {secs - LOG_WINDOW[1]:.0f}s")
            else:
                st.caption("Not logged (engine did not see this market in its window)")

    errs = dbq("SELECT at, src, msg FROM elog ORDER BY id DESC LIMIT 5")
    if errs:
        with st.expander(f"Recent engine errors ({len(errs)})"):
            for at, src, msg in errs:
                st.caption(f"{datetime.fromtimestamp(at).strftime('%H:%M:%S')} · {src}: {msg}")


dashboard()


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


if S["show_tv"] and S["assets"]:
    st.divider()
    st.subheader("TradingView")
    for tab, a in zip(st.tabs(S["assets"]), S["assets"]):
        with tab:
            tradingview(ASSETS[a][2])

st.divider()
st.subheader("Trade History")
if st.button("Resume trading after a pause"):
    meta_set("pause_reset_id", dbq("SELECT COALESCE(MAX(id),0) FROM trades")[0][0])
    st.rerun()
rows = dbq("SELECT ticker, side, entry_price, exit_price, pnl, status FROM trades ORDER BY id DESC LIMIT 12")
if rows:
    for r in rows:
        color = "green" if (r[4] or 0) > 0 else "red"
        st.markdown(f"`{r[0]}` {r[1].upper()} | {r[2]:.2f} → {r[3]:.2f} | "
                    f":{color}[{(r[4] or 0) * 100:+.1f}%] | {r[5]}")
else:
    st.write("No trades yet")

st.divider()


@st.fragment(run_every=30)
def accuracy_panel():
    st.subheader("Is the model actually accurate?")
    total = dbq("SELECT COUNT(*) FROM signals")[0][0]
    settled = dbq("SELECT COUNT(*) FROM signals WHERE result IS NOT NULL")[0][0]
    need = S["proven_min"]
    proven, _ = bot_proven(S)
    st.progress(min(settled / need, 1.0), text=f"{settled} / {need} markets settled")
    c1, c2, c3 = st.columns(3)
    c1.metric("Logged", total)
    c2.metric(f"Settled (counts toward {need})", settled)
    c3.metric("Waiting to settle", total - settled)
    st.caption(f"A market is logged when it has {LOG_WINDOW[0]}-{LOG_WINDOW[1]} seconds left. The engine "
               f"does this on the server, so the page does not need to be open. "
               f"Status: {'PROVEN' if proven else 'not proven yet'}")

    last = dbq("SELECT ticker, logged_at FROM signals ORDER BY id DESC LIMIT 1")
    st.caption(f"Last market logged: {last[0][0]} at {last[0][1][:19]} UTC" if last
               else "Nothing logged yet. The first market is logged within about 15 minutes.")

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    stale = dbq("SELECT COUNT(*) FROM signals WHERE result IS NULL AND logged_at < ?", (cutoff,))[0][0]
    if stale:
        st.warning(f"{stale} logged market(s) should have settled by now but Kalshi has not returned a result.")
    note = meta_get("resolve_note")
    if note:
        st.caption(f"Last settlement check: {note}")

    rows = dbq("SELECT model_up, mid, side, cost, would_trade, result FROM signals WHERE result IS NOT NULL")
    if not rows:
        return
    bm = sum((r[0] - (1.0 if r[5] == "yes" else 0.0)) ** 2 for r in rows) / len(rows)
    bk = sum((r[1] - (1.0 if r[5] == "yes" else 0.0)) ** 2 for r in rows) / len(rows)
    d1, d2 = st.columns(2)
    d1.metric("Model error (Brier, lower=better)", f"{bm:.3f}")
    d2.metric("Kalshi price error (Brier)", f"{bk:.3f}",
              "model beats market" if bm < bk else "market beats model",
              delta_color="normal" if bm < bk else "inverse")
    hyp = [r for r in rows if r[4]]
    if hyp:
        pnl, wins = 0.0, 0
        for r in hyp:
            won = (r[2] == r[5])
            wins += won
            pnl += ((1 - r[3]) if won else -r[3]) - fee_pc(r[3], 1)
        st.write(f"Signals the bot would have traded: {len(hyp)} | win rate {wins / len(hyp) * 100:.0f}% "
                 f"| hypothetical PnL per 1 contract each: **${pnl:+.2f}**")


accuracy_panel()
st.caption("Engine + Discord alerts + Locks + Kelly + SAFE/SCALP + Calibration")
