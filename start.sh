#!/bin/bash
set -e

# The engine (alerts, exits, logging, backups) runs in its own process,
# so it starts at boot even if nobody opens the page.
RUN_ENGINE_ONLY=1 python app.py &

# Fly injects PORT, Streamlit must listen on 0.0.0.0
exec streamlit run app.py \
  --server.port=${PORT:-8501} \
  --server.address=0.0.0.0 \
  --server.headless=true \
  --browser.serverAddress=0.0.0.0
