import numpy as np
from scipy.stats import poisson

def calculate_poisson_probs(home_xg, away_xg):
    """Рассчитывает вероятности 1-X-2 на основе среднего xG"""
    max_goals = 10
    home_matrix = [poisson.pmf(i, home_xg) for i in range(max_goals)]
    away_matrix = [poisson.pmf(i, away_xg) for i in range(max_goals)]
    
    m = np.outer(home_matrix, away_matrix)
    
    prob_home = np.sum(np.tril(m, -1)) # П1
    prob_draw = np.sum(np.diag(m))     # Ничья
    prob_away = np.sum(np.triu(m, 1))  # П2
    
    return {"1": prob_home, "X": prob_draw, "2": prob_away}

def calculate_kelly(prob, odds, bank, fraction=0.25):
    """Считает рекомендуемую ставку (дробный Келли)"""
    if odds <= 1: return 0
    edge = (prob * odds) - 1
    if edge <= 0: return 0
    
    stake_percent = edge / (odds - 1)
    recommended_stake = bank * stake_percent * fraction
    
    # Ограничения из ТЗ: от 100р до 5% банка
    final_stake = max(100, min(recommended_stake, bank * 0.05))
    return round(final_stake, 0)