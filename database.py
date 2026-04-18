import sqlite3

def init_db():
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    
    # Таблица матчей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            league TEXT,
            home_team TEXT,
            away_team TEXT,
            kick_off DATETIME,
            xg_home REAL,
            xg_away REAL,
            odds_json TEXT,
            analyzed_at DATETIME
        )
    ''')
    
    # Таблица банка
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bankroll (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 10000
        )
    ''')

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()