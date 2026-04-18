import os
import asyncio
import json
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv

from scrapers import FootballData
from analysis import calculate_poisson_probs, calculate_kelly
from ai_module import generate_bet_post
from database import init_db

load_dotenv()

# Инициализация бота
bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()
collector = FootballData()

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        "⚽ **xG Master Bot v2** запущен!\n\n"
        "Команды:\n"
        "/scan — Найти валуйные ставки (xG + AI)\n"
        "/bank — Проверить банкролл"
    )

@dp.message(Command("scan"))
async def scan_matches(message: types.Message):
    await message.answer("📡 Начинаю сканирование топ-лиг... Это займет около 30 секунд.")
    
    matches = collector.get_upcoming_matches()
    if not matches:
        await message.answer("❌ Матчей на сегодня не найдено.")
        return

    found_bets = 0
    for m in matches[:5]: # Проверяем первые 5 матчей
        home = m['homeTeam']['name']
        away = m['awayTeam']['name']
        
        # 1. Получаем xG
        xg_h = collector.get_understat_xg(home)
        xg_a = collector.get_understat_xg(away)
        
        # 2. Математический расчет
        probs = calculate_poisson_probs(xg_h, xg_a)
        odds = collector.get_real_odds(home, away)
        
        # Проверяем перевес (edge) на Победу Хозяев (П1)
        edge = probs['1'] - (1 / odds['1'])
        
        if edge > 0.05: # Если перевес больше 5%
            found_bets += 1
            stake = calculate_kelly(probs['1'], odds['1'], 10000)
            
            # 3. Генерация красивого поста через Gemini AI
            match_data = {'home': home, 'away': away}
            analysis_results = {'bet': 'П1', 'odds': odds['1'], 'xg_h': xg_h, 'xg_a': xg_a}
            
            try:
                ai_post = generate_bet_post(match_data, analysis_results)
                await message.answer(f"✅ **Найдена ставка!**\n\n{ai_post}")
            except Exception as e:
                await message.answer(f"✅ **Найдена ставка!**\n⚽ {home}-{away}\n📈 Прогноз: П1 за {odds['1']}\n💰 Стейк: {stake}₽")
        
        await asyncio.sleep(1) # Пауза между матчами

    if found_bets == 0:
        await message.answer("📉 Сканирование завершено. Валуйных ставок с перевесом >5% сейчас нет.")

async def main():
    init_db()
    print("🚀 Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")