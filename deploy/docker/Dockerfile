FROM python:3.11-slim

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir -e ".[dev]"

CMD ["robot-core", "smoke-surveillance", "--output", "logs/smoke_surveillance_trace.jsonl"]
