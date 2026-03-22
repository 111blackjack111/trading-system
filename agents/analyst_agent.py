"""
AnalystAgent — мета-аналитик системы.

Запускается каждые 10 итераций. Анализирует тренды, находит паттерны,
даёт рекомендации. Думает на уровне цикла, не итерации.

Работает через Claude CLI (подписка Max, $0).
"""

import os
import sys
import json
import sqlite3
import subprocess
import urllib.request
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "experiments.db")
PARAMS_PATH = os.path.join(os.path.dirname(__file__), "..", "strategy", "params.json")
REPORT_PATH = os.path.join(RUNTIME_DIR, "analyst_report.json")


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def get_last_experiments(n=10):
    """Последние N экспериментов."""
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT iteration, param_changed, old_value, new_value,
                   avg_score, avg_winrate, avg_pf, total_trades, action
            FROM experiments
            ORDER BY id DESC LIMIT ?
        """, (n,))
        return [dict(r) for r in cursor.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def get_all_keeps():
    """Все keep эксперименты — что работало."""
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT param_changed, old_value, new_value, avg_score, avg_winrate
            FROM experiments
            WHERE action = 'keep'
            ORDER BY avg_score DESC
        """)
        return [dict(r) for r in cursor.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def get_trade_log():
    path = os.path.join(RUNTIME_DIR, "trade_log.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def get_params():
    with open(PARAMS_PATH) as f:
        return json.load(f)


def build_analyst_prompt(experiments, keeps, trade_log, params, blacklist_info, consecutive_reverts):
    """Промпт для мета-анализа."""
    return f"""You are a senior trading strategy analyst reviewing an automated optimization system.
Your job: analyze the last 10 iterations, find patterns, and give strategic recommendations.

## Current Parameters
```json
{json.dumps(params, indent=2)}
```

## Last 10 Experiments (most recent first)
```json
{json.dumps(experiments, indent=2)}
```

## All Successful Changes (keeps, sorted by score)
```json
{json.dumps(keeps, indent=2)}
```

## Current Trade Log
Total trades: {trade_log.get('total_trades', 0)}
Overall WR: {trade_log.get('overall_winrate', 0):.1%}

Instruments:
{json.dumps(trade_log.get('win_by_instrument', {}), indent=2)}

Exit breakdown:
{json.dumps(trade_log.get('exit_reason_breakdown', {}), indent=2)}

Sessions:
{json.dumps(trade_log.get('win_by_session', {}), indent=2)}

## System State
Consecutive reverts: {consecutive_reverts}
Blacklisted params: {blacklist_info}

## Your Task
Analyze everything and respond with JSON:
{{
    "diagnosis": "2-3 sentences: why is the system stuck or progressing?",
    "trend": "improving" | "stuck" | "degrading",
    "recommendations": [
        {{
            "action": "expand_range" | "add_param" | "change_metric" | "code_change" | "exclude_instrument" | "change_prompt",
            "target": "specific parameter or component",
            "details": "what exactly to do",
            "expected_impact": "what should improve",
            "confidence": 0.0-1.0
        }}
    ],
    "param_adjustments": {{
        "param_name": [new_low, new_high]
    }},
    "summary": "1-2 sentence Telegram summary for CEO"
}}

Rules:
- Maximum 3 recommendations
- confidence > 0.8 = apply automatically, < 0.8 = send to Telegram for CEO approval
- Be specific: "expand be_trigger_rr to (0.5, 2.5)" not "try wider ranges"
- If system is improving — say so, don't change what works
- If stuck — identify the bottleneck and propose a concrete fix
- IMPORTANT: Write "diagnosis" and "summary" fields in RUSSIAN (Cyrillic). CEO reads Telegram in Russian.
  Example: "diagnosis": "Система улучшается — score вырос с 0.33 до 1.59 за счёт forex overrides."
  Example: "summary": "Score растёт. Рекомендую убрать USD/JPY и расширить BE для форекса."
- "expected_impact" and "details" can stay in English
"""


def run_analysis(consecutive_reverts=0, blacklist_info="none"):
    """Запускает мета-анализ через Claude CLI."""
    experiments = get_last_experiments(10)
    keeps = get_all_keeps()
    trade_log = get_trade_log()
    params = get_params()

    if not experiments:
        print("[Analyst] No experiments to analyze")
        return None

    prompt = build_analyst_prompt(experiments, keeps, trade_log, params, blacklist_info, consecutive_reverts)

    print("[Analyst] Running meta-analysis via Claude CLI...")

    result = subprocess.run(
        ["claude", "-p", "--output-format", "text"],
        input=prompt,
        capture_output=True, text=True, timeout=180,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
    )

    if result.returncode != 0:
        print(f"[Analyst] Claude CLI error: {result.stderr[:200]}")
        return None

    text = result.stdout.strip()

    # Extract JSON
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    try:
        report = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[Analyst] JSON parse error: {e}")
        print(f"[Analyst] Raw text: {text[:300]}")
        return None

    # Save report
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)

    print(f"[Analyst] Diagnosis: {report.get('diagnosis', 'N/A')}")
    print(f"[Analyst] Trend: {report.get('trend', 'N/A')}")
    print(f"[Analyst] Recommendations: {len(report.get('recommendations', []))}")

    # Full Telegram report
    trend_emoji = {"improving": "📈", "stuck": "⚠️", "degrading": "📉"}.get(report.get("trend"), "🔍")

    # Build instrument stats
    inst_lines = []
    for inst, data in sorted(trade_log.get("win_by_instrument", {}).items(), key=lambda x: -x[1].get("total_r", 0)):
        wr = data.get("winrate", 0)
        total_r = data.get("total_r", 0)
        trades = data.get("total_trades", 0)
        emoji = "✅" if total_r > 0 else "❌"
        inst_lines.append(f"  {emoji} {inst}: WR {wr:.0%}, {total_r:+.0f}R ({trades})")

    # Build keeps info
    keeps_count = len(keeps)
    reverts_in_last10 = sum(1 for e in experiments if e.get("action") == "revert")
    keeps_in_last10 = sum(1 for e in experiments if e.get("action") == "keep")

    # Best score from keeps
    best_keep = keeps[0] if keeps else {}
    best_param = best_keep.get("param_changed", "N/A")
    best_wr = trade_log.get("overall_winrate", 0)

    # Exit breakdown
    exits = trade_log.get("exit_reason_breakdown", {})
    exit_lines = []
    total_trades = trade_log.get("total_trades", 1)
    for reason, data in exits.items():
        pct = data.get("count", 0) / total_trades * 100
        exit_lines.append(f"  {reason}: {pct:.0f}%")

    # Recommendations
    rec_lines = []
    for r in report.get("recommendations", []):
        conf = r.get("confidence", 0)
        conf_emoji = "🟢" if conf >= 0.8 else "🟡" if conf >= 0.5 else "🔴"
        status = "авто" if conf >= 0.8 else "ждёт CEO"
        rec_lines.append(f"  {conf_emoji} {r.get('action')}: {r.get('target')} ({status})")

    msg = (
        f"🧠 <b>Orchestrator Decision</b>\n"
        f"\n"
        f"📊 Проанализировано {len(experiments)} итераций\n"
        f"🏆 Лучший: score={best_keep.get('avg_score', 0):.2f} "
        f"(WR={best_wr:.1%}, {total_trades} trades)\n"
        f"\n"
        f"{trend_emoji} <b>Статус:</b> {report.get('trend', 'unknown')}\n"
        f"✅ Keep: {keeps_in_last10} | ❌ Revert: {reverts_in_last10}\n"
        f"\n"
        f"📈 <b>По инструментам:</b>\n"
        f"{chr(10).join(inst_lines)}\n"
        f"\n"
        f"🎯 <b>Выходы:</b> {' | '.join(exit_lines)}\n"
        f"\n"
        f"💡 <b>Диагноз:</b>\n"
        f"{report.get('diagnosis', 'N/A')}\n"
        f"\n"
        f"🚀 <b>Рекомендации:</b>\n"
        f"{chr(10).join(rec_lines) if rec_lines else '  Нет'}\n"
        f"\n"
        f"🎯 {report.get('summary', '')}"
    )
    send_telegram(msg)

    return report


def apply_recommendations(report, param_ranges):
    """
    Применяет рекомендации с confidence > 0.8 автоматически.
    Остальные отправляет в Telegram для подтверждения CEO.

    Returns:
        list of applied actions
    """
    if not report:
        return []

    applied = []

    # 1. Apply param_adjustments (range expansions)
    adjustments = report.get("param_adjustments", {})
    for param, new_range in adjustments.items():
        if isinstance(new_range, list) and len(new_range) == 2:
            if param in param_ranges:
                old_range = param_ranges[param]
                param_ranges[param] = (new_range[0], new_range[1])
                applied.append(f"Range {param}: {old_range} → ({new_range[0]}, {new_range[1]})")
                print(f"  [Analyst] Applied range: {param} → {new_range}")

    # 2. Process recommendations
    for rec in report.get("recommendations", []):
        confidence = rec.get("confidence", 0)
        action = rec.get("action", "")
        target = rec.get("target", "")
        details = rec.get("details", "")

        if confidence >= 0.8:
            # Auto-apply
            if action == "expand_range" and target in param_ranges:
                # Already handled by param_adjustments
                pass
            elif action == "exclude_instrument":
                applied.append(f"EXCLUDE {target} (confidence {confidence})")
                print(f"  [Analyst] Auto-exclude: {target}")
            else:
                applied.append(f"AUTO: {action} {target} — {details}")
                print(f"  [Analyst] Auto-applied: {action} {target}")
        else:
            # Send to Telegram for CEO
            send_telegram(
                f"🔍 <b>Analyst рекомендует</b> (confidence: {confidence:.0%})\n\n"
                f"<b>{action}</b>: {target}\n"
                f"{details}\n"
                f"Expected: {rec.get('expected_impact', 'N/A')}\n\n"
                f"Применить? Ответь в чате с Claude Code"
            )

    return applied


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--reverts", type=int, default=0)
    parser.add_argument("--blacklist", type=str, default="none")
    args = parser.parse_args()

    report = run_analysis(args.reverts, args.blacklist)
    if report:
        print(json.dumps(report, indent=2))
