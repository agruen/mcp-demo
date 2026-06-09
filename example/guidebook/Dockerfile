FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-download tiktoken encoding so first request isn't slow
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" 2>/dev/null || true

# Log directory — shared with dashboard sidecar via docker volume
RUN mkdir -p /var/log/mcp

ENV PORT=8080
ENV CPS_GUIDEBOOK_JSON_PATH=/app/data/cps-ai-guidebook.json
ENV MCP_LOG_DIR=/var/log/mcp

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"

CMD ["sh", "-c", "echo PORT=$PORT; exec gunicorn server:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180 --forwarded-allow-ips='*' --access-logfile - --error-logfile -"]
