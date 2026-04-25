# Railway-optimised Dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=8000

EXPOSE $PORT

CMD ["sh", "-c", "python -m uvicorn webapp:app --host 0.0.0.0 --port ${PORT:-8000}"]
