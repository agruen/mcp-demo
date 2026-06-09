# Multi-arch friendly: python:3.11-slim publishes images for amd64 and arm64,
# so the same Dockerfile builds on a laptop or a (64-bit) Raspberry Pi.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-download tiktoken encoding so first request isn't slow
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" 2>/dev/null || true

# Log directory — surfaced to the host via docker volume
RUN mkdir -p /var/log/mcp

ENV PORT=8080
ENV PRIVACY_POLICY_JSON_PATH=/app/data/openai-us-privacy-policy.json
ENV MCP_LOG_DIR=/var/log/mcp

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"

CMD ["sh", "-c", "echo PORT=$PORT; exec gunicorn server:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180 --forwarded-allow-ips='*' --access-logfile - --error-logfile -"]
