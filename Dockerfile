FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user

USER user

ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .

CMD ["sh", "-c", "uvicorn xG_Master_Bot.webapp:app --host 0.0.0.0 --port ${PORT:-7860}"]
