"""
TradeAnalyst — глубокий анализ отдельных сделок через Claude.

Вызывается orchestrator каждые 15 итераций (или по запросу).
Анализирует паттерны в проигрышных И выигрышных сделках.
Предлагает конкретные фильтры или изменения стратегии.

Использует Claude CLI (Max подписка, $0).
"""

import os
import sys
import json
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.base_strategy import load_params
from db.db_manager import get_latest_trade_log as db_get_trade_log

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")


def get_trade_data():
    """Получает обогащённый trade_log."""
    result = db_get_trade_log()
    if result:
        return result["data"]
    # Fallback: read from file
    path = os.path.join(RUNTIME_DIR, "trade_log.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def build_prompt(trade_data, params):
    """Строит промпт для анализа сделок."""
    return f"""You are a senior quantitative trading analyst. Analyze individual trade data from an SMC (Smart Money Concepts) strategy and find actionable patterns.

## Strategy Overview
- Entry: H1 BOS (Break of Structure) → FVG (Fair Value Gap) on M3 with confirmation candle
- SL: Behind FVG + ATR buffer
- TP: Fixed RR ratio with partial TP at 1.0R (50%), rest at full TP
- BE: Move SL to entry when price reaches be_trigger_rr
- Sessions: London (06:00-11:00 UTC), NY for USD_JPY only (12:00-14:00 UTC)

## Current Parameters
```json
{json.dumps(params, indent=2)}
```

## Trade Statistics
Total trades: {trade_data.get('total_trades', 0)}
Overall WR: {trade_data.get('overall_winrate', 0):.1%}

## Exit Reason Breakdown (with avg MFE/MAE)
```json
{json.dumps(trade_data.get('exit_reason_breakdown', {}), indent=2)}
```

## MFE/MAE Summary
```json
{json.dumps(trade_data.get('mfe_mae_summary', {}), indent=2)}
```

## Win Rate by Instrument
```json
{json.dumps(trade_data.get('win_by_instrument', {}), indent=2)}
```

## Win Rate by Session
```json
{json.dumps(trade_data.get('win_by_session', {}), indent=2)}
```

## Win Rate by Hour (UTC)
```json
{json.dumps(trade_data.get('win_by_hour_utc', {}), indent=2)}
```

## Last 20 Losing Trades (most recent)
```json
{json.dumps(trade_data.get('losing_trades', []), indent=2)}
```

## Top 10 Winning Trades (by pnl_r)
```json
{json.dumps(trade_data.get('winning_trades', []), indent=2)}
```

## Your Task
1. Find PATTERNS in losing trades that distinguish them from winners
2. Identify specific hours, instruments, or conditions that consistently lose
3. Check if MFE data reveals missed opportunities or bad SL placement
4. Propose concrete, testable improvements

## Response Format (JSON only)
{{
    "patterns_found": [
        {{
            "pattern": "Description of the pattern",
            "evidence": "Specific data that supports this",
            "affected_trades_pct": 0.0-1.0,
            "impact_estimate_r": -5.0
        }}
    ],
    "recommendations": [
        {{
            "type": "param_change" | "filter" | "code_change",
            "description": "What to do",
            "param": "parameter name (if param_change)",
            "value": "suggested value",
            "expected_impact": "What should improve and by how much",
            "confidence": 0.0-1.0
        }}
    ],
    "summary_ru": "2-3 предложения на русском: что нашли и что предлагают"
}}

Rules:
- Focus on ACTIONABLE insights, not obvious observations
- Each pattern must have specific evidence from the data
- Recommendations must be testable (param change or concrete filter)
- Maximum 5 patterns and 3 recommendations
- summary_ru MUST be in Russian (Cyrillic)
"""


def run_analysis():
    """Запускает анализ сделок через Claude CLI."""
    trade_data = get_trade_data()
    if not trade_data or trade_data.get("total_trades", 0) < 20:
        print("  [TradeAnalyst] Not enough trades for analysis")
        return None

    params = load_params()
    prompt = build_prompt(trade_data, params)

    # Use Opus for deep analysis
    print("  [TradeAnalyst] Analyzing trades with Claude Opus...")
    result = subprocess.run(
        ["claude", "-p", "--output-format", "text", "--model", "opus"],
        input=prompt,
        capture_output=True, text=True, timeout=300,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
    )

    if result.returncode != 0:
        print(f"  [TradeAnalyst] Error: {result.stderr[:300]}")
        return None

    text = result.stdout.strip()
    if not text:
        print("  [TradeAnalyst] Empty response")
        return None

    # Parse JSON
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    try:
        analysis = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  [TradeAnalyst] JSON parse error: {e}")
        print(f"  Raw response: {text[:500]}")
        return None

    # Save to runtime
    output_path = os.path.join(RUNTIME_DIR, "trade_analysis.json")
    with open(output_path, "w") as f:
        json.dump(analysis, f, indent=2, ensure_ascii=False)

    # Print summary
    print(f"\n  [TradeAnalyst] Analysis complete:")
    print(f"  Patterns found: {len(analysis.get('patterns_found', []))}")
    print(f"  Recommendations: {len(analysis.get('recommendations', []))}")
    if analysis.get("summary_ru"):
        print(f"  Summary: {analysis['summary_ru']}")

    return analysis


if __name__ == "__main__":
    analysis = run_analysis()
    if analysis:
        print(json.dumps(analysis, indent=2, ensure_ascii=False))
