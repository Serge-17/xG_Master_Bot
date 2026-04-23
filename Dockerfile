# Dockerfile для HuggingFace Spaces
# Spaces требует: порт 7860, пользователь без root (user 1000)

FROM python:3.11-slim

# Системные зависимости (для psycopg2 и Pillow/OCR)
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    libffi-dev \
    libssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория
WORKDIR /app

# Сначала копируем только requirements — слой кешируется
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# HuggingFace запускает контейнер от непривилегированного пользователя
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER 1000

# HuggingFace Spaces требует порт 7860
EXPOSE 7860

# Переменная окружения порта
ENV PORT=7860

# Точка входа — запускаем FastAPI через uvicorn
CMD ["python", "-m", "uvicorn", "webapp:app", "--host", "0.0.0.0", "--port", "7860"]