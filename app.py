import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timezone

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
</style>""", unsafe_allow_html=True)

DB_FILE = "calls3.db"
# ADDED timeout=30 to prevent crashes when worker is writing at the same time
db = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)

# Ensure tables exist just in case the dashboard loads first
db.execute("""CREATE TABLE IF NOT EXISTS pos(key TEXT PRIMARY KEY, ticker TEXT, asset TEXT, kind TEXT, side TEXT,
count INTEGER, entry REAL, peak REAL, close_time TEXT, status TEXT, exit REAL, pnl REAL, win INTEGER)""")
db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL)")
db.execute("""CREATE TABLE IF NOT EXISTS preds(ticker TEXT PRIMARY KEY, asset TEXT, model REAL, kalshi REAL,
final REAL, close_time TEXT, result TEXT)""")
db.commit()

def get_s(key, default):
    res = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return float(res[0]) if res else default

def save_s(key, val):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
    db.commit()

# ==========================================
# SIDEBAR SETTINGS
# ==========================================
st.sidebar.header("⚙️ Strategy Settings")
min_edge = st.sidebar.slider("Minimum edge after fees (%)", -10.0, 15.0, get_s("min_edge", -5.0) * 100, step=0.5) / 100
min_secs = st.sidebar.slider("No new entries with less than (sec) left", 30.0, 300.0, get_s("min_secs", 60.0), step=10.0)
price_cap = st.sidebar.slider("Max contract price for SAFE buys (%)", 50.0, 99.0, get_s("price_cap", 90.0) * 100, step=1.0) / 100

st.sidebar.header("⚡ Scalp Settings")
scalp_on = st.sidebar.checkbox("Enable SCALP signals", value=(get_s("scalp_on", 1.0) == 1.0))
scalp_sl = st.sidebar.slider("Scalp stop loss (%)", -20.0, -1.0, get_s("scalp_sl", -6.0) * 100, step=1.0) / 100

st.sidebar.header("💵 Auto-Trading")
auto_safe = st.sidebar.checkbox("Auto-Trade SAFE signals", value=(get_s("auto_safe", 1.0) == 1.0))
auto_scalp = st.sidebar.checkbox("Auto-Trade SCALP signals", value=(get_s("auto_scalp", 1.0) == 1.0))
max_trade_usd = st.sidebar.number_input("Max $ per trade", 1.0, 50.0, get_s("max_trade_usd", 2.0), step=1.0)

st.sidebar.header("💰 Auto-Sell Settings")
auto_sell = st.sidebar.checkbox("Enable Auto-Selling", value=(get_s("auto_sell", 1.0) == 1.0))
profit_target = st.sidebar.slider("Standard Trade Profit Target (%)", 5.0, 100.0, get_s("profit_target", 35.0) * 100, step=1.0) / 100
sell_conf = st.sidebar.slider("Sell standard if confidence drops below (%)", 10.0, 90.0, get_s("sell_conf", 50.0) * 100, step=1.0) / 100
stop_loss = st.sidebar.slider("Standard Stop Loss (%)", -50.0, -5.0, get_s("stop_loss", -20.0) * 100, step=1.0) / 100

st.sidebar.header("🛡️ Safety")
pause_on = st.sidebar.checkbox("Pause new calls after 3 losses in a row", value=(get_s("pause_on", 1.0) == 1.0))

if st.sidebar.button("Reset history"):
    try:
        db.execute("DELETE FROM pos"); db.execute("DELETE FROM preds"); db.commit()
    except: pass
    st.rerun()

# Save to DB
save_s("min_edge", min_edge)
save_s("min_secs", min_secs)
save_s("price_cap", price_cap)
save_s("scalp_on", 1.0 if scalp_on else 0.0)
save_s("scalp_sl", scalp_sl)
save_s("auto_safe", 1.0 if auto_safe else 0.0)
save_s("auto_scalp", 1.0 if auto_scalp else 0.0)
save_s("max_trade_usd", max_trade_usd)
save_s("auto_sell", 1.0 if auto_sell else 0.0)
save_s("profit_target", profit_target)
save_s("sell_conf", sell_conf)
save_s("stop_loss", stop_loss)
save_s("pause_on", 1.0 if pause_on else 0.0)

# ==========================================
# DASHBOARD STATS
# ==========================================
try:
    n, wins, avg = db.execute("SELECT COUNT(*), COALESCE(SUM(win),0), COALESCE(AVG(pnl),0) FROM pos WHERE status!='open'").fetchone()
    op = db.execute("SELECT COUNT(*) FROM pos WHERE status='open'").fetchone()[0]
    wr = f"{wins/n*100:.1f}%" if n else "—"
except:
    n, wins, avg, op, wr = 0, 0, 0, 0, "—"

now = datetime.now(timezone.utc)

st.markdown(
    f"""
    <div class="top">
        <div>
            <div class="title">⚡ Kalshi 15-Min Desk</div>
            <div class="sub">24/7 Background Worker Active · Settings sync automatically</div>
        </div>
        <div class="live">● LIVE · {now.strftime("%H:%M:%S")} UTC<br><span class="sub">running in background</span></div>
    </div>
    <div class="stats">
        <div class="stat"><div class="l">Win rate</div><div class="v">{wr}</div></div>
        <div class="stat"><div class="l">Record</div><div class="v">{int(wins)} / {int(n-wins)}</div></div>
        <div class="stat"><div class="l">Avg result</div><div class="v">{avg:+.1f}%</div></div>
        <div class="stat"><div class="l">Open pos</div><div class="v">{op}</div></div>
        <div class="stat"><div class="l">Engine</div><div class="v" style="color:#22c55e">ONLINE</div></div>
    </div>
    """,
    unsafe_allow_html=True
)

st.subheader("Recent Trades")
try:
    df = pd.read_sql("SELECT asset, kind, side, count, entry, status, pnl FROM pos ORDER BY close_time DESC LIMIT 20", db)
    st.dataframe(df, use_container_width=True)
except:
    st.info("No trades yet.")
