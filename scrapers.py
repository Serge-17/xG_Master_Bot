import requests
from bs4 import BeautifulSoup
import json
import re
import os
import datetime
from dotenv import load_dotenv

load_dotenv()

class FootballData:
    def __init__(self):
        self.fd_key = os.getenv("FOOTBALL_DATA_API_KEY")
        self.odds_key = os.getenv("ODDS_API_KEY")

    def get_upcoming_matches(self):
        """Получаем матчи на ближайшие 2 дня"""
        today = datetime.datetime.now().date()
        tomorrow = today + datetime.timedelta(days=2)
        
        url = "https://api.football-data.org/v4/matches"
        headers = {'X-Auth-Token': self.fd_key}
        # Для бесплатного тарифа лучше не указывать много лиг сразу, если не уверен в кодах
        params = {
            'dateFrom': today.strftime('%Y-%m-%d'),
            'dateTo': tomorrow.strftime('%Y-%m-%d')
        }
        
        try:
            response = requests.get(url, headers=headers, params=params)
            if response.status_code == 200:
                return response.json().get('matches', [])
            print(f"Ошибка API Football-Data: {response.status_code}")
            return []
        except Exception as e:
            print(f"Ошибка запроса: {e}")
            return []

    def get_understat_xg(self, team_name):
        """Скрапинг xG с Understat"""
        formatted_name = team_name.replace(' ', '_')
        url = f"https://understat.com/team/{formatted_name}/2024" # Текущий сезон
        
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200: return 1.2
            soup = BeautifulSoup(res.content, 'html.parser')
            scripts = soup.find_all('script')
            
            for s in scripts:
                if 'datesData' in s.text:
                    data_str = re.search(r"JSON\.parse\('(.+?)'\)", s.text).group(1)
                    data_json = json.loads(data_str.encode('utf-8').decode('unicode_escape'))
                    past_matches = [m for m in data_json if m['isResult'] is True][-5:]
                    if not past_matches: return 1.2
                    
                    total_xg = 0
                    for m in past_matches:
                        if m['h']['title'] == team_name:
                            total_xg += float(m['xG']['h'])
                        else:
                            total_xg += float(m['xG']['a'])
                    return round(total_xg / len(past_matches), 2)
        except:
            return 1.2
        return 1.2

    def get_real_odds(self, home_team, away_team):
        """Заглушка для Odds API"""
        return {"1": 2.10, "X": 3.40, "2": 3.20}