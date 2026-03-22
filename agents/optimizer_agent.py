"""
OptimizerAgent v3 — использует Claude API для предложения изменений.

Cycle 3: Читает trade_log.json с аналитикой проигрышных сделок.
Если WR < 25% — может предлагать изменения в base_strategy.py (не только params).
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
STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "..", "strategy", "base_strategy.py")

# Диапазоны параметров для оптимизации
# Общие параметры (применяются ко всем инструментам)
PARAM_RANGES = {
    # fvg_min_size_multiplier BLACKLISTED — даёт аномальные scores (-200/-400)
    "fvg_entry_depth": (0.2, 0.8),
    "ob_lookback": (5, 30),
    "bos_swing_length": (5, 25),
    "sl_atr_multiplier": (0.8, 3.5),
    "be_trigger_rr": (0.5, 2.0),
    "tp_rr_ratio": (1.5, 4.0),
    "min_atr_percentile": (15, 70),
    "fvg_max_age_bars": (5, 50),
    # Группа-специфичные (crypto_overrides.X, forex_overrides.X)
    "crypto_overrides.be_trigger_rr": (0.5, 1.5),
    "crypto_overrides.sl_atr_multiplier": (1.0, 3.0),
    "crypto_overrides.tp_rr_ratio": (1.5, 3.0),
    "crypto_overrides.min_atr_percentile": (20, 60),
    "forex_overrides.be_trigger_rr": (0.3, 1.2),
    "forex_overrides.sl_atr_multiplier": (1.0, 3.0),
    "forex_overrides.tp_rr_ratio": (1.5, 3.0),
    "forex_overrides.min_atr_percentile": (20, 60),
}


def get_experiment_history():
    """Читает top-5 лучших + последние 3 эксперимента (экономия токенов)."""
    if not os.path.exists(DB_PATH):
        return {"top_5": [], "last_3": []}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    result = {"top_5": [], "last_3": []}

    try:
        # Top 5 by avg_score
        cursor.execute("""
            SELECT iteration, param_changed, old_value, new_value,
                   avg_score, best_score, action
            FROM experiments
            WHERE action = 'keep' OR action = 'baseline'
            ORDER BY avg_score DESC
            LIMIT 5
        """)
        result["top_5"] = [dict(r) for r in cursor.fetchall()]

        # Last 3
        cursor.execute("""
            SELECT iteration, param_changed, old_value, new_value,
                   avg_score, best_score, action
            FROM experiments
            ORDER BY iteration DESC
            LIMIT 3
        """)
        result["last_3"] = [dict(r) for r in cursor.fetchall()]
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    return result


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


def get_trade_log():
    """Читает trade_log.json и возвращает компактную версию (экономия токенов)."""
    path = os.path.join(RUNTIME_DIR, "trade_log.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        full = json.load(f)

    # Компактная версия: только ключевые метрики + 5 последних losers
    summary = {
        "total_trades": full.get("total_trades"),
        "overall_winrate": full.get("overall_winrate"),
        "win_by_session": full.get("win_by_session"),
        "win_by_instrument": full.get("win_by_instrument"),
        "avg_bars_to_stop": full.get("avg_bars_to_stop"),
        "exit_reason_breakdown": full.get("exit_reason_breakdown"),
        "recent_losers_5": full.get("losing_trades", [])[-5:],
    }
    return summary


def get_strategy_code():
    """Читает текущий код стратегии (для code-change mode)."""
    if not os.path.exists(STRATEGY_PATH):
        return ""
    with open(STRATEGY_PATH) as f:
        return f.read()


def compute_avg_winrate(metrics):
    """Считает средний WR по всем инструментам."""
    winrates = [m.get("winrate", 0) for m in metrics.values() if m]
    return sum(winrates) / len(winrates) if winrates else 0


def compute_param_priority(history):
    """Вычисляет приоритет параметров по историческому impact."""
    if not history:
        return "No history yet — explore freely."

    # Собираем impact по параметрам из top_5 keeps
    param_impact = {}
    for exp in history.get("top_5", []):
        param = exp.get("param_changed", "")
        score = exp.get("avg_score", 0)
        action = exp.get("action", "")
        if action == "keep" and param and param != "baseline":
            if param not in param_impact or score > param_impact[param]:
                param_impact[param] = score

    # Считаем reverts из last_3
    recent_reverts = set()
    for exp in history.get("last_3", []):
        if exp.get("action") == "revert":
            recent_reverts.add(exp.get("param_changed", ""))

    if not param_impact:
        return "No keeps yet — explore: be_trigger_rr, bos_swing_length, fvg_entry_depth"

    # Сортируем по impact
    sorted_params = sorted(param_impact.items(), key=lambda x: -x[1])
    lines = []
    for i, (param, score) in enumerate(sorted_params, 1):
        status = "⚠️ recently reverted" if param in recent_reverts else "✅ available"
        lines.append(f"{i}. {param} (best score: {score:.2f}) — {status}")

    # Добавляем неисследованные
    explored = set(param_impact.keys())
    unexplored = [p for p in PARAM_RANGES if p not in explored]
    if unexplored:
        lines.append(f"\nUnexplored parameters: {', '.join(unexplored[:5])}")

    return "\n".join(lines)


def build_prompt(params, history, metrics, trade_log, allow_code_changes=False):
    """Строит промпт для Claude."""

    trade_log_section = ""
    if trade_log:
        trade_log_section = f"""
## Trade Analysis (trade_log.json)
Overall WR: {trade_log.get('overall_winrate', 0):.1%} ({trade_log.get('total_trades', 0)} trades)

### Win Rate by Session
```json
{json.dumps(trade_log.get('win_by_session', {}), indent=2)}
```

### Win Rate by Instrument
```json
{json.dumps(trade_log.get('win_by_instrument', {}), indent=2)}
```

### Average Bars to Stop-Loss: {trade_log.get('avg_bars_to_stop', 'N/A')}

### Exit Reason Breakdown
```json
{json.dumps(trade_log.get('exit_reason_breakdown', {}), indent=2)}
```

### Recent Losing Trades (last 5)
```json
{json.dumps(trade_log.get('recent_losers_5', []), indent=2)}
```
"""

    code_change_section = ""
    if allow_code_changes:
        strategy_code = get_strategy_code()
        code_change_section = f"""
## CODE CHANGE MODE (WR < 25%)
Win rate is critically low. Parameter changes alone are insufficient.
You MAY suggest a code change to strategy/base_strategy.py instead of a parameter change.

### Current Strategy Code
```python
{strategy_code}
```

### Allowed Code Changes
You can suggest ONE of these types of changes:
1. **Add confirmation candle** — require a bullish/bearish close after FVG touch before entry
2. **Add multi-timeframe filter** — e.g., require M15 trend alignment
3. **Add time-of-day filter** — block specific hours that lose consistently
4. **Modify entry logic** — e.g., wait for price to close back inside FVG (not just touch)
5. **Modify SL/TP logic** — e.g., trail stop, partial TP, dynamic RR
6. **Add instrument-specific filter** — disable instruments with WR < 10%

### Code Change Response Format
If you suggest a code change, respond with:
```json
{{
    "type": "code_change",
    "change_description": "Brief description of what to change",
    "function_name": "name of function to modify",
    "old_code": "exact code to replace (copy-paste from above)",
    "new_code": "replacement code",
    "reasoning": "Why this should improve WR"
}}
```
"""

    prompt = f"""You are an AI trading strategy optimizer using autoresearch methodology.
Your job is to improve an SMC (Smart Money Concepts) trading strategy.

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
{trade_log_section}
## Best Experiments (top 5)
{json.dumps(history.get("top_5", []), indent=2) if history else "None yet"}

## Last 3 Experiments
{json.dumps(history.get("last_3", []), indent=2) if history else "None yet"}

## Score Formula
score = sharpe * 0.35 + (winrate * profit_factor) * 0.35 - max_drawdown * 0.2 + 0.1
{code_change_section}
## Parameter Priority (by historical impact — start with highest)
{compute_param_priority(history)}

## Rules
1. START with highest-priority parameters that haven't been exhausted
2. Analyze the trade_log data — find PATTERNS in losses
3. Stay within allowed ranges
4. If a parameter was reverted 2+ times — SKIP IT, try something else
5. Think about WHY a change might help based on SMC logic
6. Try crypto_overrides and forex_overrides separately — they behave differently
7. Small steps (10-20% change) are better than large jumps

## Response Format (JSON only)
For parameter changes:
{{
    "type": "param_change",
    "param": "parameter_name",
    "old_value": current_value,
    "new_value": suggested_value,
    "reasoning": "Brief explanation based on trade_log analysis"
}}"""

    return prompt


def suggest_change(params=None):
    """Вызывает Claude CLI (подписка Max) для предложения изменений. Расход API = $0."""
    import subprocess

    if params is None:
        params = load_params()

    history = get_experiment_history()
    metrics = get_current_metrics()
    trade_log = get_trade_log()

    allow_code_changes = False

    prompt = build_prompt(params, history, metrics, trade_log, allow_code_changes)

    # Вызываем Claude CLI через подписку Max (бесплатно)
    # Передаём промпт через stdin чтобы избежать лимита длины аргументов
    result = subprocess.run(
        ["claude", "-p", "--output-format", "text"],
        input=prompt,
        capture_output=True, text=True, timeout=180,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
    )

    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI error (rc={result.returncode}): {result.stderr[:500]}")

    text = result.stdout.strip()
    if not text:
        raise RuntimeError(f"Claude CLI returned empty response. stderr: {result.stderr[:500]}")

    # Извлекаем JSON если обёрнут в markdown
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    suggestion = json.loads(text)

    # Обработка по типу
    change_type = suggestion.get("type", "param_change")

    if change_type == "code_change":
        # Код стратегии
        print(f"  CODE CHANGE: {suggestion.get('change_description', 'N/A')}")
        print(f"  Function: {suggestion.get('function_name', 'N/A')}")
        print(f"  Reasoning: {suggestion.get('reasoning', 'N/A')}")

        # Сохраняем предложение
        suggestion["param"] = "code_change"
        suggestion["old_value"] = 0
        suggestion["new_value"] = 0

    else:
        # Параметр
        param = suggestion.get("param")
        if not param or param not in PARAM_RANGES:
            raise ValueError(f"Unknown parameter: {param}")

        low, high = PARAM_RANGES[param]
        new_val = suggestion["new_value"]
        if not (low <= new_val <= high):
            raise ValueError(f"{param}={new_val} out of range [{low}, {high}]")

        print(f"  Suggestion: {param} {suggestion.get('old_value')} -> {suggestion['new_value']}")
        print(f"  Reasoning: {suggestion.get('reasoning', 'N/A')}")

    # Сохраняем
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    suggestion_path = os.path.join(RUNTIME_DIR, "suggestion.json")
    with open(suggestion_path, "w") as f:
        json.dump(suggestion, f, indent=2)

    return suggestion


if __name__ == "__main__":
    suggestion = suggest_change()
    print(json.dumps(suggestion, indent=2))
