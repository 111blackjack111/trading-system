"""
Алгоритмический оптимизатор — замена Claude API.
Использует Bayesian-like подход: анализирует историю экспериментов
и выбирает следующее изменение на основе того, что работало.

Бесплатный, не требует API ключей.
"""

import os
import sys
import json
import random
import sqlite3
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategy.base_strategy import load_params

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "experiments.db")

# Диапазоны и шаги
PARAM_CONFIG = {
    "fvg_min_size_multiplier": {"min": 0.1, "max": 1.0, "step": 0.05, "type": "float"},
    "fvg_entry_depth":         {"min": 0.3, "max": 0.7, "step": 0.05, "type": "float"},
    "ob_lookback":             {"min": 5,   "max": 30,  "step": 1,    "type": "int"},
    "bos_swing_length":        {"min": 5,   "max": 25,  "step": 1,    "type": "int"},
    "sl_atr_multiplier":       {"min": 1.0, "max": 3.0, "step": 0.1,  "type": "float"},
    "be_trigger_rr":           {"min": 0.3, "max": 0.7, "step": 0.05, "type": "float"},
    "tp_rr_ratio":             {"min": 1.5, "max": 3.0, "step": 0.1,  "type": "float"},
    "min_atr_percentile":      {"min": 20,  "max": 60,  "step": 5,    "type": "int"},
}


def get_experiment_history(limit=50):
    """Читает историю экспериментов."""
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT param_changed, old_value, new_value, avg_score, action "
            "FROM experiments ORDER BY iteration DESC LIMIT ?", (limit,)
        ).fetchall()]
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def analyze_history(history):
    """
    Анализирует что работало и что нет.
    Возвращает dict: param -> {"up_good": int, "down_good": int, "up_bad": int, "down_bad": int}
    """
    stats = {}
    for h in history:
        param = h.get("param_changed")
        if not param or param == "baseline" or param not in PARAM_CONFIG:
            continue

        if param not in stats:
            stats[param] = {"up_good": 0, "down_good": 0, "up_bad": 0, "down_bad": 0, "tried": 0}

        stats[param]["tried"] += 1
        old_val = h.get("old_value", 0)
        new_val = h.get("new_value", 0)
        direction = "up" if new_val > old_val else "down"
        outcome = "good" if h.get("action") == "keep" else "bad"
        stats[param][f"{direction}_{outcome}"] += 1

    return stats


def suggest_change(params=None):
    """
    Предлагает одно изменение параметра.

    Стратегия выбора:
    1. Если мало истории — случайный параметр, случайное направление
    2. Если есть история — предпочитаем параметры которые ещё не пробовали
       или которые давали улучшения
    3. Размер шага уменьшается со временем (simulated annealing)
    """
    if params is None:
        params = load_params()

    history = get_experiment_history()
    stats = analyze_history(history)
    iteration = len(history)

    # Температура (уменьшается со временем)
    temperature = max(0.3, 1.0 - iteration * 0.01)

    # Выбираем параметр
    param = choose_param(params, stats, temperature)
    cfg = PARAM_CONFIG[param]
    current_val = params.get(param, (cfg["min"] + cfg["max"]) / 2)

    # Выбираем направление
    direction = choose_direction(param, stats, temperature)

    # Размер шага (1-3 шага, уменьшается с температурой)
    num_steps = random.randint(1, max(1, int(3 * temperature)))
    delta = cfg["step"] * num_steps * direction

    new_val = current_val + delta

    # Клэмпим в диапазон
    new_val = max(cfg["min"], min(cfg["max"], new_val))
    if cfg["type"] == "int":
        new_val = int(round(new_val))
    else:
        new_val = round(new_val, 4)

    # Если не изменилось — сдвигаем в противоположную сторону
    if new_val == current_val:
        new_val = current_val - delta
        new_val = max(cfg["min"], min(cfg["max"], new_val))
        if cfg["type"] == "int":
            new_val = int(round(new_val))
        else:
            new_val = round(new_val, 4)

    reasoning = build_reasoning(param, current_val, new_val, stats, temperature)

    suggestion = {
        "param": param,
        "old_value": current_val,
        "new_value": new_val,
        "reasoning": reasoning,
    }

    # Сохраняем
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(os.path.join(RUNTIME_DIR, "suggestion.json"), "w") as f:
        json.dump(suggestion, f, indent=2)

    print(f"  Suggestion: {param} {current_val} -> {new_val}")
    print(f"  Reasoning: {reasoning}")

    return suggestion


def choose_param(params, stats, temperature):
    """Выбирает параметр для изменения."""
    weights = {}

    for param in PARAM_CONFIG:
        if param not in stats:
            # Не пробовали — высокий приоритет
            weights[param] = 3.0
        else:
            s = stats[param]
            total_good = s["up_good"] + s["down_good"]
            total_tried = s["tried"]

            if total_tried == 0:
                weights[param] = 3.0
            elif total_good / total_tried > 0.3:
                # Параметр давал улучшения — пробуем ещё
                weights[param] = 2.0
            else:
                # Параметр редко помогал — низкий приоритет
                weights[param] = 0.5

    # Добавляем случайность через температуру
    params_list = list(weights.keys())
    w = [weights[p] * temperature + random.random() * temperature for p in params_list]
    total_w = sum(w)
    w = [x / total_w for x in w]

    return random.choices(params_list, weights=w, k=1)[0]


def choose_direction(param, stats, temperature):
    """Выбирает направление изменения: +1 или -1."""
    if param not in stats:
        return random.choice([1, -1])

    s = stats[param]

    # Если одно направление работало лучше — предпочитаем его
    up_score = s["up_good"] - s["up_bad"] * 0.5
    down_score = s["down_good"] - s["down_bad"] * 0.5

    if up_score > down_score + 1:
        return 1 if random.random() > temperature * 0.3 else -1
    elif down_score > up_score + 1:
        return -1 if random.random() > temperature * 0.3 else 1
    else:
        return random.choice([1, -1])


def build_reasoning(param, old_val, new_val, stats, temperature):
    """Строит объяснение."""
    direction = "increase" if new_val > old_val else "decrease"

    if param not in stats:
        return f"First time testing {param}: {direction} from {old_val} to {new_val}"

    s = stats[param]
    total_good = s["up_good"] + s["down_good"]
    total_tried = s["tried"]
    success_rate = total_good / total_tried if total_tried > 0 else 0

    return (f"{direction} {param} from {old_val} to {new_val}. "
            f"History: {total_good}/{total_tried} improvements ({success_rate:.0%}). "
            f"Temperature: {temperature:.2f}")


if __name__ == "__main__":
    suggestion = suggest_change()
    print(json.dumps(suggestion, indent=2))
