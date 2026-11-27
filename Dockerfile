FROM python:3.11-slim

WORKDIR /app

# System deps (ca-certificates is required for outbound HTTPS calls to Kalshi/Coinbase)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY start.sh .
RUN chmod +x start.sh

# Do not hardcode PORT; Fly injects it at runtime
EXPOSE 8501

CMD ["./start.sh"]
