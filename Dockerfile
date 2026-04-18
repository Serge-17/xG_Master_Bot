# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем рабочую папку
WORKDIR /app

# Копируем список зависимостей
COPY requirements.txt .

# Устанавливаем библиотеки
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все остальные файлы проекта
COPY . .

# Запускаем бота
CMD ["python", "app.py"]