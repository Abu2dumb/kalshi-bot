import streamlit as st
import requests
import time
import base64
import os
import pandas as pd
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization
import numpy as np
import scipy.stats as stats

# ==========================================
# CONFIGURATION & AUTHENTICATION
# ==========================================
KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID")
PEM = os.environ.get("KALSHI_PEM")
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

if not KALSHI_KEY_ID or not PEM:
    st.error("Missing KALSHI_KEY_ID or KALSHI_PEM environment variables.")
    st.stop()

key = serialization.load_pem_private_key(PEM.encode(), password=None)

def get_headers(method, path):
    """Generate Kalshi V2 authentication headers."""
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path}".encode()
    sig = key.sign(msg)
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json"
    }

@st.cache_data(ttl=2)
def get_cf_benchmarks_60s_avg():
    """
    Fetch the official CF Benchmarks 60-second average (TWAP).
    This is the exact data source Kalshi uses for settlement.
    """
    path = "/trade-api/v2/cfbenchmarks"
    params = {"id": "BRTI", "maxResolution": "PER_SECOND"} 
    headers = get_headers("GET", path)
    
    try:
        res = requests.get(f"{BASE_URL}/cfbenchmarks", params=params, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            values = [float(v["value"]) for v in data.get("data", {}).get("values", [])]
            if values:
                return sum(values) / len(values)
        return None
    except Exception as e:
        st.error(f"CF Index Error: {e}")
        return None

@st.cache_data(ttl=5)
def get_active_market(series_ticker="KXBTC15M"):
    """Get the currently active 15-minute market."""
    path = "/trade-api/v2/markets"
    params = {"series_ticker": series_ticker, "status": "open", "limit": 5}
    try:
        res = requests.get(f"{BASE_URL}/markets", params=params, timeout=5)
        markets = res.json().get("markets", [])
        if markets:
            return markets[0]
    except:
        pass
    return None

def place_order(ticker, side, count, price_cents, tif="immediate_or_cancel"):
    """Place a V2 order using the required payload structure."""
    path = "/trade-api/v2/portfolio/events/orders"
    headers = get_headers("POST", path)
    
    # V2 API requires price in dollars (e.g. 0.56) and specific fields
    payload = {
        "ticker": ticker,
        "client_order_id": f"bot-{int(time.time()*1000)}",
        "side": side,
        "action": "buy",
        "count": str(int(count)),
        "price": f"{price_cents / 100:.4f}",
        "time_in_force": tif,
        "self_trade_prevention_type": "taker_at_cross" 
    }
    
    res = requests.post(f"{BASE_URL}/portfolio/events/orders", json=payload, headers=headers, timeout=5)
    return res.status_code, res.text

# ==========================================
# UI & DASHBOARD
# ==========================================
st.set_page_config(page_title="Kalshi BRTI Bot", page_icon="⚡", layout="wide")
st.title("⚡ Kalshi 15-Min BRTI Oracle Bot")

# Sidebar Settings
st.sidebar.header("Strategy Settings")
max_spend = st.sidebar.slider("Max $ per trade", 1.0, 50.0, 5.0, step=1.0)
auto_trade = st.sidebar.toggle("Enable Auto-Trading", value=False)
st.sidebar.info("⚠️ Bot uses CF Benchmarks BRTI (the actual settlement source), not Coinbase spot.")

# Main Dashboard
col1, col2, col3 = st.columns(3)
index_price = get_cf_benchmarks_60s_avg()
market = get_active_market()

if index_price:
    col1.metric("CF Benchmarks BRTI (60s TWAP)", f"${index_price:,.2f}")
else:
    col1.metric("CF Benchmarks BRTI", "Fetching...")

if market:
    ticker = market["ticker"]
    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)
    
    col2.metric("Active Market", ticker)
    col3.metric("Yes Ask / Bid", f"{yes_ask}¢ / {yes_bid}¢")
    
    st.markdown("---")
    
    # Strategy Logic
    st.subheader("Strategy Evaluation")
    
    price_to_pay = yes_ask / 100.0 if yes_ask else 0.55
    contracts_to_buy = int(max_spend / price_to_pay) if price_to_pay > 0 else 0
    
    st.write(f"Calculated stake: **${max_spend:.2f}** for **{contracts_to_buy}** contracts.")
    st.write(f"Target Limit Price: **{int(price_to_pay * 100)}¢**")
    
    if st.button("Execute Trade (Buy YES)"):
        if not auto_trade:
            st.warning("Please enable Auto-Trading in the sidebar first.")
        elif contracts_to_buy < 1:
            st.error("Max spend is too low to buy 1 contract.")
        else:
            with st.spinner("Placing order..."):
                status, response = place_order(ticker, "yes", contracts_to_buy, int(price_to_pay * 100))
                if status == 201:
                    st.success(f"✅ Order Placed Successfully!")
                    st.json(response)
                else:
                    st.error(f"❌ Failed: {status} - {response}")
else:
    st.info("Waiting for active 15-minute markets...")
