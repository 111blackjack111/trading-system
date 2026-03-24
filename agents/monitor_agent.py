"""
MonitorAgent — следит за здоровьем системы и шлёт отчёты в Telegram.

1. Каждые 10 итераций — отчёт в Telegram с прогрессом
2. Если агент завис (нет записей 15 мин) — перезапуск + алерт
3. Если 10 итераций без улучшения — алерт "система застряла"
"""

import os
import sys
import json
import time
import subprocess
from datetime import datetime, timezone

# Unbuffered output for tee/logs
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from db.db_manager import get_latest_trade_log as db_get_trade_log

BASE_DIR = os.path.join(os.path.dirname(__file__), "..")
DB_PATH = os.path.join(BASE_DIR, "db", "experiments.db")

CHECK_INTERVAL = 30          # проверяем каждые 30 секунд
REPORT_EVERY_N = 10          # отчёт каждые N итераций
STALL_THRESHOLD = 10         # N итераций без улучшения = застрял
HANG_TIMEOUT = 1800          # 30 минут без новых записей = завис (baseline ~20 мин)


def send_telegram(message):
    """Шлёт сообщение в Telegram."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        print(f"[Monitor] Telegram not configured. Message:\n{message}")
        return False

    import urllib.request
    import urllib.parse

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }).encode()

    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print(f"[Monitor] Telegram sent OK")
                return True
            else:
                print(f"[Monitor] Telegram error: {result}")
                return False
    except Exception as e:
        print(f"[Monitor] Telegram failed: {e}")
        return False


def read_results_tsv():
    """Читает эксперименты из БД (замена results.tsv)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT iteration, timestamp, param_changed as param, "
            "old_value as old_val, new_value as new_val, "
            "round(avg_score,4) as avg_score, round(best_score,4) as best_score, "
            "best_instrument as best_inst, total_trades as trades, "
            "round(avg_winrate,4) as winrate, round(avg_pf,4) as pf, action "
            "FROM experiments ORDER BY id"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def read_trade_log():
    """Читает trade_log из БД."""
    result = db_get_trade_log()
    if result:
        return result["data"]
    return None


def check_api_health():
    """Проверяет работает ли Anthropic API."""
    try:
        import anthropic
        api_key = getattr(config, 'ANTHROPIC_API_KEY', '')
        if not api_key:
            return False
        client = anthropic.Anthropic(api_key=api_key)
        client.messages.create(
            model="claude-4-sonnet-20250514",
            max_tokens=5,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True
    except Exception as e:
        err = str(e)
        if "credit" in err.lower() or "limit" in err.lower() or "balance" in err.lower():
            print(f"[Monitor] API credit/limit error: {err[:100]}")
            return False
        # Other errors (network etc) - don't alert
        return True


def check_tmux_session(name):
    """Проверяет, жива ли tmux сессия."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def restart_tmux_session(name):
    """Перезапускает tmux сессию агента."""
    base = os.path.abspath(BASE_DIR)
    venv = os.path.join(base, "venv", "bin", "activate")
    api_key = getattr(config, 'ANTHROPIC_API_KEY', '') or ""

    commands = {
        "backtest": f"cd {base} && source {venv} && export ANTHROPIC_API_KEY='{api_key}' && python3 agents/backtest_agent.py --mode watch",
        "orchestrator": f"cd {base} && source {venv} && export ANTHROPIC_API_KEY='{api_key}' && python3 agents/orchestrator_v2.py --iterations 100 --skip-data 2>&1 | tee results/orchestrator.log",
    }

    if name not in commands:
        print(f"[Monitor] Unknown session: {name}")
        return False

    try:
        subprocess.run(["tmux", "kill-session", "-t", name],
                       capture_output=True, timeout=5)
        time.sleep(1)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, commands[name]],
            capture_output=True, timeout=5
        )
        print(f"[Monitor] Restarted session: {name}")
        return True
    except Exception as e:
        print(f"[Monitor] Failed to restart {name}: {e}")
        return False


def format_report(rows, trade_log, start_iter, end_iter):
    """Формирует отчёт для Telegram."""
    # Фильтруем строки за период
    period_rows = []
    for r in rows:
        try:
            it = int(r.get("iteration", -1))
            if start_iter <= it <= end_iter:
                period_rows.append(r)
        except ValueError:
            continue

    if not period_rows:
        return None

    # Score начало и конец
    scores = []
    for r in period_rows:
        try:
            scores.append(float(r.get("avg_score", 0)))
        except ValueError:
            scores.append(0)

    first_score = scores[0] if scores else 0
    last_score = scores[-1] if scores else 0

    # Keep / Revert
    kept = sum(1 for r in period_rows if r.get("action") == "keep")
    reverted = sum(1 for r in period_rows if r.get("action") == "revert")

    # Лучший инструмент
    best_inst = ""
    best_score_val = -999
    for r in period_rows:
        try:
            s = float(r.get("best_score", -999))
            if s > best_score_val:
                best_score_val = s
                best_inst = r.get("best_inst", "")
        except ValueError:
            continue

    # WR из trade_log
    wr_str = "N/A"
    if trade_log:
        wr = trade_log.get("overall_winrate", 0)
        wr_str = f"{wr:.1%}"
        total = trade_log.get("total_trades", 0)
        wr_str += f" ({total} trades)"

    # Code changes
    code_changes = sum(1 for r in period_rows
                       if r.get("param", "") == "code_change")

    # Формируем текст
    score_direction = "📈" if last_score > first_score else "📉" if last_score < first_score else "➡️"

    msg = f"""<b>🤖 Trading Autoresearch</b>

<b>Итерации {start_iter} — {end_iter}</b>

{score_direction} Score: <b>{first_score:.4f} → {last_score:.4f}</b>
📊 WR: {wr_str}
✅ Kept: {kept} | ❌ Reverted: {reverted}
🏆 Лучший: {best_inst} ({best_score_val:.4f})"""

    if code_changes:
        msg += f"\n🔧 Code changes: {code_changes}"

    # Session breakdown
    if trade_log and "win_by_session" in trade_log:
        msg += "\n\n<b>WR по сессиям:</b>"
        for session, data in trade_log["win_by_session"].items():
            wr = data.get("winrate", 0)
            n = data.get("total_trades", 0)
            msg += f"\n  {session}: {wr:.1%} ({n})"

    msg += "\n\n⏳ Следующие итерации запущены"

    return msg


def run():
    """Главный цикл мониторинга."""
    print("=" * 50)
    print("[Monitor] Starting MonitorAgent")
    print(f"  Report every: {REPORT_EVERY_N} iterations")
    print(f"  Hang timeout: {HANG_TIMEOUT}s")
    print(f"  Stall threshold: {STALL_THRESHOLD} iterations")
    print(f"  Telegram: {'configured' if config.TELEGRAM_BOT_TOKEN else 'NOT configured'}")
    print("=" * 50)

    last_reported_iter = -1
    last_tsv_modified = 0
    last_activity_time = time.time()
    hang_alerted = False
    stall_alerted = False

    # Стартовое сообщение
    send_telegram("🚀 <b>MonitorAgent запущен</b>\nСлежу за оптимизацией 24/7")

    while True:
        try:
            rows = read_results_tsv()
            trade_log = read_trade_log()

            # Текущая итерация
            current_iter = 0
            if rows:
                try:
                    current_iter = max(int(r.get("iteration", 0)) for r in rows)
                except ValueError:
                    current_iter = 0

            # --- 1. Проверка активности (зависание) ---
            # Check latest experiment timestamp from DB
            try:
                conn = sqlite3.connect(DB_PATH)
                row = conn.execute("SELECT MAX(id) FROM experiments").fetchone()
                conn.close()
                latest_id = row[0] if row else 0
            except Exception:
                latest_id = 0

            if latest_id > last_tsv_modified:
                last_tsv_modified = latest_id
                last_activity_time = time.time()
                hang_alerted = False

            time_since_activity = time.time() - last_activity_time

            if time_since_activity > HANG_TIMEOUT and not hang_alerted:
                print(f"[Monitor] HANG detected! No activity for {time_since_activity:.0f}s")

                # Проверяем tmux сессии
                sessions_status = {}
                for name in ["backtest", "orchestrator"]:
                    alive = check_tmux_session(name)
                    sessions_status[name] = alive
                    if not alive:
                        print(f"[Monitor] Session {name} is DEAD, restarting...")
                        restart_tmux_session(name)

                status_str = ", ".join(
                    f"{k}: {'✅' if v else '💀 перезапущен'}"
                    for k, v in sessions_status.items()
                )

                send_telegram(
                    f"⚠️ <b>Система зависла!</b>\n"
                    f"Нет активности {int(time_since_activity)}с\n"
                    f"Сессии: {status_str}\n"
                    f"Последняя итерация: {current_iter}"
                )
                hang_alerted = True

            # --- 2. Отчёт каждые N итераций ---
            if current_iter > 0 and current_iter > last_reported_iter:
                # Определяем, пора ли слать отчёт
                next_report_at = ((last_reported_iter // REPORT_EVERY_N) + 1) * REPORT_EVERY_N
                if next_report_at <= 0:
                    next_report_at = REPORT_EVERY_N

                if current_iter >= next_report_at:
                    start = max(0, next_report_at - REPORT_EVERY_N)
                    end = current_iter

                    report = format_report(rows, trade_log, start, end)
                    if report:
                        send_telegram(report)
                        print(f"[Monitor] Report sent for iterations {start}-{end}")

                    last_reported_iter = current_iter

            # --- 2.5. Проверка API (раз в 5 минут) ---
            if not hasattr(run, '_last_api_check'):
                run._last_api_check = 0
                run._api_alert_sent = False

            if time.time() - run._last_api_check > 300:
                run._last_api_check = time.time()
                api_ok = check_api_health()
                if not api_ok and not run._api_alert_sent:
                    send_telegram(
                        "🔴 <b>API не работает!</b>\n"
                        "Кредиты закончились или лимит достигнут.\n"
                        "Оптимизация остановлена.\n\n"
                        "Действие: пополни кредиты на console.anthropic.com"
                    )
                    run._api_alert_sent = True
                elif api_ok and run._api_alert_sent:
                    send_telegram("🟢 <b>API восстановлен!</b>\nОптимизация продолжается.")
                    run._api_alert_sent = False

            # --- 3. Проверка стагнации ---
            if len(rows) >= STALL_THRESHOLD:
                recent = rows[-STALL_THRESHOLD:]
                all_reverted = all(r.get("action") == "revert" for r in recent)

                if all_reverted and not stall_alerted:
                    send_telegram(
                        f"🔴 <b>Система застряла!</b>\n"
                        f"Последние {STALL_THRESHOLD} итераций — все revert.\n"
                        f"Score не улучшается.\n"
                        f"Текущий score: {rows[-1].get('avg_score', '?')}\n\n"
                        f"Нужно вмешательство:\n"
                        f"• Расширить диапазоны параметров\n"
                        f"• Изменить стратегию входа\n"
                        f"• Добавить новые данные"
                    )
                    stall_alerted = True
                    print(f"[Monitor] STALL alert sent")

                elif not all_reverted:
                    stall_alerted = False

        except Exception as e:
            print(f"[Monitor] Error in main loop: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
