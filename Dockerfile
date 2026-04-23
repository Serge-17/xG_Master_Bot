# HuggingFace Spaces — порт 7860, user 1000
FROM python:3.11-slim

# gcc нужен для сборки numpy/scipy; остальное — минимально
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER 1000

EXPOSE 7860
ENV PORT=7860 PYTHONUNBUFFERED=1

CMD ["python", "-m", "uvicorn", "webapp:app", "--host", "0.0.0.0", "--port", "7860"]
