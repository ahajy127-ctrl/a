FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot files
COPY hyperliquid_bot.py .
COPY .env.example .env
COPY setup_hyperliquid.py .

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/')" || exit 1

EXPOSE 8080

CMD ["python3", "hyperliquid_bot.py"]