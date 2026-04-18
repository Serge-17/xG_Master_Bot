from scrapers import FootballData
from database import init_db

# 1. Инициализируем БД
init_db()

# 2. Проверяем парсер
collector = FootballData()
print("📡 Запрос списка матчей...")
matches = collector.get_upcoming_matches()

if matches:
    first_match = matches[0]
    home = first_match['homeTeam']['name']
    away = first_match['awayTeam']['name']
    print(f"✅ Найден матч: {home} vs {away}")
    
    print(f"📊 Сбор xG для {home}...")
    xg = collector.get_understat_xg(home)
    print(f"Результат: средний xG за 5 игр = {xg}")
else:
    print("❌ Матчи не найдены. Проверь API ключ или лимиты.")