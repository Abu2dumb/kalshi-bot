import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timezone
import requests

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
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL)")

def get_setting(key, default):
    res = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return float(res[0]) if res else default

def save_setting(key, val):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
    db.commit()

# ==========================================
# SIDEBAR SETTINGS
# ==========================================
st.sidebar.header("⚙️ Strategy Settings")
min_edge = st.sidebar.slider("Minimum edge after fees (%)", -10.0, 15.0, get_setting("min_edge", -5.0), step=0.5)
price_cap = st.sidebar.slider("Max contract price for SAFE buys (%)", 50.0, 99.0, get_setting("price_cap", 90.0), step=1.0)

st.sidebar.header("💰 Auto-Sell Settings")
profit_target = st.sidebar.slider("Profit Target (%)", 5.0, 100.0, get_setting("profit_target", 35.0), step=1.0)
stop_loss = st.sidebar.slider("Stop Loss (%)", -50.0, -5.0, get_setting("stop_loss", -20.0), step=1.0)
sell_conf = st.sidebar.slider("Sell if confidence drops below (%)", 10.0, 90.0, get_setting("sell_conf", 50.0), step=1.0)

# Save to database so worker reads them
save_setting("min_edge", min_edge / 100.0)
save_setting("price_cap", price_cap / 100.0)
save_setting("profit_target", profit_target / 100.0)
save_setting("stop_loss", stop_loss / 100.0)
save_setting("sell_conf", sell_conf / 100.0)

if st.sidebar.button("Reset History"):
    db.execute("DELETE FROM pos"); db.execute("DELETE FROM preds"); db.commit()
    st.rerun()

# ==========================================
# DASHBOARD STATS
# ==========================================
n, wins, avg = db.execute("SELECT COUNT(*), COALESCE(SUM(win),0), COALESCE(AVG(pnl),0) FROM pos WHERE status!='open'").fetchone()
op = db.execute("SELECT COUNT(*) FROM pos WHERE status='open'").fetchone()[0]
wr = f"{wins/n*100:.1f}%" if n else "—"
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
        <div class="stat"><div class="l">Record</div><div class="v">{int(wins)} / {n-int(wins)}</div></div>
        <div class="stat"><div class="l">Avg result</div><div class="v">{avg:+.1f}%</div></div>
        <div class="stat"><div class="l">Open pos</div><div class="v">{op}</div></div>
        <div class="stat"><div class="l">Engine</div><div class="v" style="color:#22c55e">ONLINE</div></div>
    </div>
    """,
    unsafe_allow_html=True
)

st.subheader("Recent Trades")
df = pd.read_sql("SELECT asset, side, count, entry, status, pnl FROM pos ORDER BY close_time DESC LIMIT 20", db)
st.dataframe(df, use_container_width=True)
