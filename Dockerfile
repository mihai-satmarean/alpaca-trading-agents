FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml ./
RUN uv pip install --system -e ".[dev]"

COPY . .

ENV PYTHONUNBUFFERED=1
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "from src.core.alpaca_client import AlpacaClient; c = AlpacaClient(); print(c.get_account().equity)" || exit 1

CMD ["python", "scripts/run_live.py"]
