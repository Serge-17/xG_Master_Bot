from __future__ import annotations

import re


def _normalize(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", " ", (value or "").lower()).strip()


LEAGUE_ALIASES: dict[str, tuple[str, str]] = {
    "premier league": ("premier league", "Премьер-лига"),
    "epl": ("premier league", "Премьер-лига"),
    "англия": ("premier league", "Премьер-лига"),
    "апл": ("premier league", "Премьер-лига"),
    "премьер лига": ("premier league", "Премьер-лига"),
    "премьер лига англии": ("premier league", "Премьер-лига"),
    "e0": ("premier league", "Премьер-лига"),
    "la liga": ("la liga", "Ла Лига"),
    "ла лига": ("la liga", "Ла Лига"),
    "испания": ("la liga", "Ла Лига"),
    "sp1": ("la liga", "Ла Лига"),
    "serie a": ("serie a", "Серия А"),
    "серия а": ("serie a", "Серия А"),
    "италия": ("serie a", "Серия А"),
    "i1": ("serie a", "Серия А"),
    "bundesliga": ("bundesliga", "Бундеслига"),
    "бундеслига": ("bundesliga", "Бундеслига"),
    "германия": ("bundesliga", "Бундеслига"),
    "d1": ("bundesliga", "Бундеслига"),
    "ligue 1": ("ligue 1", "Лига 1"),
    "лига 1": ("ligue 1", "Лига 1"),
    "франция": ("ligue 1", "Лига 1"),
    "f1": ("ligue 1", "Лига 1"),
    "champions league": ("champions league", "Лига чемпионов"),
    "ucl": ("champions league", "Лига чемпионов"),
    "лига чемпионов": ("champions league", "Лига чемпионов"),
    "cl": ("champions league", "Лига чемпионов"),
    "championship": ("championship", "Чемпионшип"),
    "чемпионшип": ("championship", "Чемпионшип"),
    "e1": ("championship", "Чемпионшип"),
    "eredivisie": ("eredivisie", "Эредивизи"),
    "n1": ("eredivisie", "Эредивизи"),
    "primeira liga": ("primeira liga", "Примейра"),
    "p1": ("primeira liga", "Примейра"),
}


TEAM_ALIASES: dict[str, tuple[str, str]] = {
    "arsenal": ("Arsenal", "Арсенал"),
    "арсенал": ("Arsenal", "Арсенал"),
    "chelsea": ("Chelsea", "Челси"),
    "челси": ("Chelsea", "Челси"),
    "liverpool": ("Liverpool", "Ливерпуль"),
    "ливерпуль": ("Liverpool", "Ливерпуль"),
    "manchester city": ("Manchester City", "Манчестер Сити"),
    "man city": ("Manchester City", "Манчестер Сити"),
    "манчестер сити": ("Manchester City", "Манчестер Сити"),
    "manchester united": ("Manchester United", "Манчестер Юнайтед"),
    "man united": ("Manchester United", "Манчестер Юнайтед"),
    "манчестер юнайтед": ("Manchester United", "Манчестер Юнайтед"),
    "tottenham": ("Tottenham", "Тоттенхэм"),
    "spurs": ("Tottenham", "Тоттенхэм"),
    "тоттенхэм": ("Tottenham", "Тоттенхэм"),
    "newcastle": ("Newcastle", "Ньюкасл"),
    "ньюкасл": ("Newcastle", "Ньюкасл"),
    "aston villa": ("Aston Villa", "Астон Вилла"),
    "астон вилла": ("Aston Villa", "Астон Вилла"),
    "barcelona": ("Barcelona", "Барселона"),
    "барселона": ("Barcelona", "Барселона"),
    "real madrid": ("Real Madrid", "Реал Мадрид"),
    "реал мадрид": ("Real Madrid", "Реал Мадрид"),
    "atletico madrid": ("Atletico Madrid", "Атлетико Мадрид"),
    "atl madrid": ("Atletico Madrid", "Атлетико Мадрид"),
    "atletico": ("Atletico Madrid", "Атлетико Мадрид"),
    "атлетико мадрид": ("Atletico Madrid", "Атлетико Мадрид"),
    "атлетико": ("Atletico Madrid", "Атлетико Мадрид"),
    "sevilla": ("Sevilla", "Севилья"),
    "севилья": ("Sevilla", "Севилья"),
    "valencia": ("Valencia", "Валенсия"),
    "валенсия": ("Valencia", "Валенсия"),
    "real sociedad": ("Real Sociedad", "Реал Сосьедад"),
    "реал сосьедад": ("Real Sociedad", "Реал Сосьедад"),
    "girona": ("Girona", "Жирона"),
    "жирона": ("Girona", "Жирона"),
    "bayern munich": ("Bayern Munich", "Бавария"),
    "bayern": ("Bayern Munich", "Бавария"),
    "бавария": ("Bayern Munich", "Бавария"),
    "borussia dortmund": ("Borussia Dortmund", "Боруссия Дортмунд"),
    "dortmund": ("Borussia Dortmund", "Боруссия Дортмунд"),
    "боруссия дортмунд": ("Borussia Dortmund", "Боруссия Дортмунд"),
    "bayer leverkusen": ("Bayer Leverkusen", "Байер"),
    "байер": ("Bayer Leverkusen", "Байер"),
    "rb leipzig": ("RB Leipzig", "РБ Лейпциг"),
    "лейпциг": ("RB Leipzig", "РБ Лейпциг"),
    "psg": ("Paris Saint Germain", "ПСЖ"),
    "paris saint germain": ("Paris Saint Germain", "ПСЖ"),
    "псж": ("Paris Saint Germain", "ПСЖ"),
    "marseille": ("Marseille", "Марсель"),
    "марсель": ("Marseille", "Марсель"),
    "monaco": ("Monaco", "Монако"),
    "монако": ("Monaco", "Монако"),
    "lyon": ("Lyon", "Лион"),
    "лион": ("Lyon", "Лион"),
    "juventus": ("Juventus", "Ювентус"),
    "ювентус": ("Juventus", "Ювентус"),
    "inter": ("Inter", "Интер"),
    "интер": ("Inter", "Интер"),
    "milan": ("Milan", "Милан"),
    "милан": ("Milan", "Милан"),
    "napoli": ("Napoli", "Наполи"),
    "наполи": ("Napoli", "Наполи"),
    "roma": ("Roma", "Рома"),
    "рома": ("Roma", "Рома"),
    "lazio": ("Lazio", "Лацио"),
    "лацио": ("Lazio", "Лацио"),
    "atalanta": ("Atalanta", "Аталанта"),
    "аталанта": ("Atalanta", "Аталанта"),
    "benfica": ("Benfica", "Бенфика"),
    "бенфика": ("Benfica", "Бенфика"),
    "porto": ("Porto", "Порту"),
    "порту": ("Porto", "Порту"),
    "sporting": ("Sporting CP", "Спортинг"),
    "sporting cp": ("Sporting CP", "Спортинг"),
    "спортинг": ("Sporting CP", "Спортинг"),
    "ajax": ("Ajax", "Аякс"),
    "аякс": ("Ajax", "Аякс"),
    "psv": ("PSV", "ПСВ"),
    "псв": ("PSV", "ПСВ"),
    "feyenoord": ("Feyenoord", "Фейеноорд"),
    "фейеноорд": ("Feyenoord", "Фейеноорд"),
}


MARKET_LABELS = {
    "P1": "П1",
    "P2": "П2",
    "X": "Ничья",
    "BTTS": "Обе забьют",
    "ОЗ": "Обе забьют",
    "BTTS YES": "Обе забьют",
    "OVER 2.5": "ТБ 2.5",
    "TБ2.5": "ТБ 2.5",
    "ТБ2.5": "ТБ 2.5",
    "ТБ 2.5": "ТБ 2.5",
    "OTHER": "Другая ставка",
}


OUTCOME_LABELS = {
    "Pending": "Ожидает расчёта",
    "Win": "Выигрыш",
    "Loss": "Проигрыш",
    "Refund": "Возврат",
}


def resolve_league_name(name: str) -> tuple[str, str]:
    normalized = _normalize(name)
    if normalized in LEAGUE_ALIASES:
        return LEAGUE_ALIASES[normalized]
    return name.strip(), name.strip()


def resolve_team_name(name: str) -> tuple[str, str]:
    normalized = _normalize(name)
    if normalized in TEAM_ALIASES:
        return TEAM_ALIASES[normalized]
    return name.strip(), name.strip()


def parse_matchup(raw: str) -> tuple[str, str] | None:
    value = re.sub(r"\s+", " ", (raw or "").strip())
    separators = [r"\s+vs\s+", r"\s+v\s+", r"\s+-\s+", r"\s+—\s+", r"\s+против\s+"]
    for separator in separators:
        parts = re.split(separator, value, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    return None


def translate_market(value: str) -> str:
    normalized = (value or "").strip().upper()
    return MARKET_LABELS.get(normalized, value or "Ставка не определена")


def translate_outcome(value: str) -> str:
    return OUTCOME_LABELS.get(value or "", value or "")


def translate_sentiment(value: str) -> str:
    mapping = {
        "Positive": "Позитивно",
        "Negative": "Негативно",
        "Neutral": "Нейтрально",
        "Позитивно": "Позитивно",
        "Негативно": "Негативно",
        "Нейтрально": "Нейтрально",
    }
    return mapping.get(value or "", value or "Нейтрально")
