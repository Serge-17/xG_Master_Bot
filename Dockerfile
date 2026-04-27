FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*

# HF Spaces требует non-root пользователя
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR /app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .

ENV PYTHONUNBUFFERED=1

EXPOSE 7860

CMD ["sh", "-c", "python -m uvicorn webapp:app --host 0.0.0.0 --port ${PORT:-7860}"]
