#!/bin/bash
set -e

# Fly injects PORT, Streamlit must listen on 0.0.0.0
exec streamlit run app.py \
  --server.port=${PORT:-8501} \
  --server.address=0.0.0.0 \
  --server.headless=true \
  --browser.serverAddress=0.0.0.0
