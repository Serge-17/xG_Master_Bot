# ai_module.py
import google.generativeai as genai

def generate_bet_post(match_data, analysis_results):
    model = genai.GenerativeModel('gemini-1.5-flash') # Gemini 2.5 пока в будущем, используем доступную
    
    prompt = f"""
    Ты — эксперт в ставках. Сгенерируй пост для ТГ-канала на основе данных:
    Матч: {match_data['home']} vs {match_data['away']}
    Прогноз: {analysis_results['bet']}
    Коэффициент: {analysis_results['odds']}
    xG Home: {analysis_results['xg_h']}, xG Away: {analysis_results['xg_a']}
    
    Верни JSON с полями: title, bet, odds, stake_rub, confidence, reasoning, risk.
    Язык: Русский.
    """
    
    response = model.generate_content(prompt)
    return response.text # Тут парсим JSON  