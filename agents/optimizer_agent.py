"""
OptimizerAgent — использует Claude API для предложения изменений параметров.
Читает текущие params.json и историю экспериментов.
Предлагает ОДНО изменение одного параметра за итерацию.
"""

import os
import sys
import json
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from strategy.base_strategy import load_params

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "experiments.db")

# Диапазоны параметров для оптимизации
PARAM_RANGES = {
    "fvg_min_size_multiplier": (0.1, 1.0),
    "fvg_entry_depth": (0.3, 0.7),
    "ob_lookback": (5, 30),
    "bos_swing_length": (5, 25),
    "sl_atr_multiplier": (1.0, 3.0),
    "be_trigger_rr": (0.3, 0.7),
    "tp_rr_ratio": (1.5, 3.0),
    "min_atr_percentile": (20, 60),
}


def get_experiment_history(limit=20):
    """Читает последние эксперименты из SQLite."""
    if not os.path.exists(DB_PATH):
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT iteration, param_changed, old_value, new_value,
                   avg_score, best_score, action, notes
            FROM experiments
            ORDER BY iteration DESC
            LIMIT ?
        """, (limit,))
        rows = [dict(r) for r in cursor.fetchall()]
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()

    return rows


def get_current_metrics():
    """Читает текущие метрики из runtime/."""
    metrics = {}
    if not os.path.exists(RUNTIME_DIR):
        return metrics

    for f in os.listdir(RUNTIME_DIR):
        if f.startswith("metrics_") and f.endswith(".json"):
            instrument = f.replace("metrics_", "").replace(".json", "")
            with open(os.path.join(RUNTIME_DIR, f)) as fh:
                metrics[instrument] = json.load(fh)

    return metrics


def build_prompt(params, history, metrics):
    """Строит промпт для Claude."""
    prompt = f"""You are an AI trading strategy optimizer. Your job is to suggest ONE parameter change
to improve the SMC (Smart Money Concepts) trading strategy.

## Current Parameters
```json
{json.dumps(params, indent=2)}
```

## Parameter Ranges
```json
{json.dumps(PARAM_RANGES, indent=2)}
```

## Current Backtest Metrics (by instrument)
```json
{json.dumps(metrics, indent=2)}
```

## Recent Experiment History (most recent first)
{json.dumps(history, indent=2) if history else "No previous experiments yet."}

## Score Formula
score = sharpe * 0.4 + profit_factor * 0.3 - max_drawdown * 0.2 + winrate * 0.1
Penalties (score=0): <30 trades, max_drawdown>0.10, winrate<0.40

## Rules
1. Suggest EXACTLY ONE parameter change
2. Stay within the allowed ranges
3. Consider what worked and what didn't in history
4. If many recent changes were reverted, try a different direction
5. Think about WHY a change might help based on SMC logic

## Response Format (JSON only)
{{
    "param": "parameter_name",
    "old_value": current_value,
    "new_value": suggested_value,
    "reasoning": "Brief explanation of why this change should improve the score"
}}"""

    return prompt


def suggest_change(params=None):
    """Вызывает Claude API и получает предложение."""
    import anthropic

    if not config.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set")

    if params is None:
        params = load_params()

    history = get_experiment_history()
    metrics = get_current_metrics()

    prompt = build_prompt(params, history, metrics)

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-4-sonnet-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    # Парсим JSON из ответа
    text = response.content[0].text.strip()

    # Извлекаем JSON если обёрнут в markdown
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    suggestion = json.loads(text)

    # Валидация
    param = suggestion["param"]
    if param not in PARAM_RANGES:
        raise ValueError(f"Unknown parameter: {param}")

    low, high = PARAM_RANGES[param]
    new_val = suggestion["new_value"]
    if not (low <= new_val <= high):
        raise ValueError(f"{param}={new_val} out of range [{low}, {high}]")

    # Сохраняем предложение
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    suggestion_path = os.path.join(RUNTIME_DIR, "suggestion.json")
    with open(suggestion_path, "w") as f:
        json.dump(suggestion, f, indent=2)

    print(f"  Suggestion: {param} {suggestion['old_value']} -> {suggestion['new_value']}")
    print(f"  Reasoning: {suggestion['reasoning']}")

    return suggestion


if __name__ == "__main__":
    suggestion = suggest_change()
    print(json.dumps(suggestion, indent=2))
