FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && apt-get update && apt-get install -y supervisor
COPY . .

RUN echo "[supervisord]\nnodaemon=true\n\n[program:streamlit]\ncommand=streamlit run app.py --server.port=8501 --server.address=0.0.0.0\n\n[program:worker]\ncommand=python worker.py\nstdout_logfile=/dev/stdout\nstdout_logfile_maxbytes=0" > /etc/supervisor/conf.d/supervisord.conf

EXPOSE 8501
CMD ["/usr/bin/supervisord"]
