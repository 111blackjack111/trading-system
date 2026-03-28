#!/usr/bin/env python3
"""
Watchdog v2 — автоматический мониторинг, починка и Telegram-уведомления.
Cron: */30 * * * *
Telegram: ТОЛЬКО если что-то чинит/находит проблему.
"""

import os
import sys
import json
import time
import sqlite3
import subprocess
import signal
from datetime import datetime, timezone

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(BASE_DIR, "db", "experiments.db")
CSV_DIR = os.path.join(BASE_DIR, "data", "csv")
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
STATE_FILE = os.path.join(RUNTIME_DIR, "watchdog_state.json")
VENV = os.path.join(BASE_DIR, "venv", "bin", "activate")
LOG_FILE = os.path.join(BASE_DIR, "results", "watchdog.log")

TG_TOKEN = "8588577391:AAE5poxdFXYDFVlf8fkCe3kZXOGCHRqVFfI"
TG_CHAT = "438218324"

# --- Helpers ---

def send_tg(message):
    import urllib.request, urllib.parse
    try:
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT, "text": message, "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log(f"TG error: {e}")
        return False

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def db_query(sql):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

def tmux_alive(name):
    try:
        r = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

def tmux_restart(name, cmd):
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, timeout=5)
    time.sleep(1)
    subprocess.run(["tmux", "new-session", "-d", "-s", name, cmd], capture_output=True, timeout=5)

def pgrep(pattern):
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
        return [int(p) for p in r.stdout.strip().split() if p.isdigit()] if r.returncode == 0 else []
    except Exception:
        return []

# --- Checks ---

def check_1_tmux_sessions():
    """Проверка и перезапуск мёртвых tmux сессий."""
    actions = []
    sessions = {
        "backtest": f"cd {BASE_DIR} && source {VENV} && python3 agents/backtest_agent.py --mode watch",
        "orchestrator": f"cd {BASE_DIR} && source {VENV} && python3 agents/orchestrator_v2.py --iterations 100 --skip-data 2>&1 | tee results/orchestrator.log",
        "monitor": f"cd {BASE_DIR} && source {VENV} && export TELEGRAM_BOT_TOKEN={TG_TOKEN} && export TELEGRAM_CHAT_ID={TG_CHAT} && python3 -u agents/monitor_agent.py 2>&1 | tee results/monitor.log",
        "impulse": f"cd {BASE_DIR} && source {VENV} && while true; do python3 agents/impulse_agent.py --mode scan --days 7 2>&1 | tee -a results/impulse.log; sleep 3600; done",
        "health": f"cd {BASE_DIR} && source {VENV} && export TELEGRAM_BOT_TOKEN={TG_TOKEN} && export TELEGRAM_CHAT_ID={TG_CHAT} && python3 -u agents/health_agent.py 2>&1 | tee results/health.log",
    }
    for name, cmd in sessions.items():
        if not tmux_alive(name):
            log(f"Session {name} DEAD → restarting")
            tmux_restart(name, cmd)
            actions.append(f"🔄 <b>{name}</b> — перезапущен (сессия умерла)")
    return actions

def check_2_csv_integrity():
    """Фикс двойных заголовков и битых CSV."""
    actions = []
    if not os.path.exists(CSV_DIR):
        return actions
    for fname in os.listdir(CSV_DIR):
        if not fname.endswith(".csv"):
            continue
        fpath = os.path.join(CSV_DIR, fname)
        try:
            with open(fpath) as f:
                line1 = f.readline().strip()
                line2 = f.readline().strip()
            if line1 == line2 and "timestamp" in line1:
                subprocess.run(["sed", "-i", "2{/^timestamp/d}", fpath], capture_output=True, timeout=10)
                log(f"Fixed double header: {fname}")
                actions.append(f"🔧 <b>{fname}</b> — удалён дубль заголовка")
            if os.path.getsize(fpath) < 100:
                actions.append(f"⚠️ <b>{fname}</b> — подозрительно мал ({os.path.getsize(fpath)}B)")
        except Exception:
            pass
    return actions

def check_3_zero_trades(state):
    """Детекция серии итераций с 0 trades → перезапуск backtest."""
    actions = []
    rows = db_query("SELECT iteration, total_trades, action FROM experiments ORDER BY id DESC LIMIT 5")
    if not rows:
        return actions

    zero_count = 0
    for r in rows:
        if (r.get("total_trades") or 0) == 0 and r.get("action") not in ("baseline", "error"):
            zero_count += 1
        else:
            break

    cooldown = time.time() - state.get("last_zero_fix", 0) > 600
    if zero_count >= 3 and cooldown:
        log(f"{zero_count} iters with 0 trades → restarting backtest")
        # Kill ALL old backtest workers
        for pid in pgrep("backtest_agent"):
            try: os.kill(pid, signal.SIGTERM)
            except: pass
        for pid in pgrep("multiprocessing.spawn"):
            try: os.kill(pid, signal.SIGTERM)
            except: pass
        time.sleep(2)
        # Clean runtime
        for f in ["backtest_request.json", "backtest_done.json"]:
            fp = os.path.join(RUNTIME_DIR, f)
            if os.path.exists(fp):
                os.remove(fp)
        # Restart
        cmd = f"cd {BASE_DIR} && source {VENV} && python3 agents/backtest_agent.py --mode watch"
        tmux_restart("backtest", cmd)
        state["last_zero_fix"] = time.time()
        actions.append(f"🔄 <b>backtest</b> — перезапущен ({zero_count} итераций с 0 trades)")
    return actions

def check_4_orchestrator_hang(state):
    """Детекция зависания orchestrator."""
    actions = []
    rows = db_query("SELECT id, timestamp FROM experiments ORDER BY id DESC LIMIT 1")
    if not rows:
        return actions

    last_id = rows[0]["id"]
    prev_id = state.get("last_db_id", last_id)
    prev_time = state.get("last_db_change_time", time.time())

    if last_id != prev_id:
        state["last_db_id"] = last_id
        state["last_db_change_time"] = time.time()
        return actions

    age = time.time() - prev_time
    cooldown = time.time() - state.get("last_hang_fix", 0) > 1800

    # 75 min без новой итерации (night backtests can take up to 60 min)
    if age > 4500 and cooldown:
        orch_pids = pgrep("orchestrator_v2.py")
        bt_pids = pgrep("backtest_agent.py")

        if not orch_pids:
            log("Orchestrator process dead → full restart via restart.sh")
            subprocess.Popen(["nohup", "bash", f"{BASE_DIR}/restart.sh"], 
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            state["last_hang_fix"] = time.time()
            actions.append(f"🔄 <b>orchestrator+backtest</b> — полный перезапуск (процесс мёртв, {int(age)}с)")
        elif not bt_pids:
            log("Backtest agent dead while orchestrator waits → restarting backtest")
            for f in ["backtest_request.json", "backtest_done.json"]:
                fp = os.path.join(RUNTIME_DIR, f)
                if os.path.exists(fp):
                    os.remove(fp)
            cmd = f"cd {BASE_DIR} && source {VENV} && python3 agents/backtest_agent.py --mode watch"
            tmux_restart("backtest", cmd)
            state["last_hang_fix"] = time.time()
            actions.append(f"🔄 <b>backtest</b> — перезапущен (orchestrator ждёт {int(age)}с, backtest мёртв)")
        else:
            # Оба живы — проверяем не завис ли backtest (workers active?)
            workers = pgrep("multiprocessing.spawn")
            if not workers and age > 4800:
                log("No active workers for 60+ min → full restart")
                for pid in pgrep("backtest_agent"):
                    try: os.kill(pid, signal.SIGTERM)
                    except: pass
                time.sleep(2)
                for f in ["backtest_request.json", "backtest_done.json"]:
                    fp = os.path.join(RUNTIME_DIR, f)
                    if os.path.exists(fp):
                        os.remove(fp)
                cmd = f"cd {BASE_DIR} && source {VENV} && python3 agents/backtest_agent.py --mode watch"
                tmux_restart("backtest", cmd)
                state["last_hang_fix"] = time.time()
                actions.append(f"🔄 <b>backtest</b> — перезапущен (нет воркеров {int(age)}с)")
            elif age > 4800:
                actions.append(f"⚠️ Система работает но медленно: {int(age)}с с последней итерации")

    state["last_db_id"] = last_id
    return actions

def check_5_zombie_workers():
    """Убиваем zombie multiprocessing workers от предыдущих запусков."""
    actions = []
    try:
        # Получаем PID текущего backtest_agent
        bt_pids = pgrep("backtest_agent.py")
        if not bt_pids:
            return actions

        current_bt_pid = bt_pids[0]

        # Получаем всех spawn workers
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,etimes,comm"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 4 and "python3" in parts[3]:
                pid, ppid, elapsed = int(parts[0]), int(parts[1]), int(parts[2])
                # Worker старше 30 мин, родитель не текущий backtest
                if elapsed > 1800 and ppid != current_bt_pid and ppid not in bt_pids:
                    # Проверяем что это spawn worker
                    try:
                        cmdline = subprocess.run(
                            ["ps", "-p", str(pid), "-o", "args="],
                            capture_output=True, text=True, timeout=5
                        ).stdout.strip()
                        if "multiprocessing" in cmdline or "spawn_main" in cmdline:
                            os.kill(pid, signal.SIGTERM)
                            log(f"Killed zombie worker {pid} (age: {elapsed}s)")
                            actions.append(f"💀 Убит zombie worker PID {pid} (возраст {elapsed//60}м)")
                    except Exception:
                        pass
    except Exception as e:
        log(f"Zombie check error: {e}")
    return actions

def check_6_dashboard():
    """Перезапуск dashboard если умер."""
    actions = []
    if not pgrep("dashboard.py"):
        log("Dashboard dead → restarting")
        subprocess.Popen(
            ["bash", "-c", f"cd {BASE_DIR} && source {VENV} && nohup python3 dashboard.py &"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        actions.append("🔄 <b>dashboard</b> — перезапущен")
    return actions

def check_7_score_anomaly(state):
    """Алерт при аномальном score."""
    actions = []
    rows = db_query("SELECT iteration, avg_score, total_trades, action FROM experiments ORDER BY id DESC LIMIT 3")
    alerted = set(state.get("alerted_anomalies", []))
    for r in rows:
        it = r["iteration"]
        score = r.get("avg_score") or 0
        trades = r.get("total_trades") or 0
        if it not in alerted and score < -50 and trades > 0:
            actions.append(f"🚨 <b>Аномалия iter {it}</b>: score {score:.1f}, trades {trades}")
            alerted.add(it)
    state["alerted_anomalies"] = list(alerted)[-50:]
    return actions


# --- Main ---

def main():
    log("=== Watchdog v2 check ===")
    state = load_state()
    all_actions = []

    all_actions.extend(check_1_tmux_sessions())
    all_actions.extend(check_2_csv_integrity())
    all_actions.extend(check_3_zero_trades(state))
    all_actions.extend(check_4_orchestrator_hang(state))
    all_actions.extend(check_5_zombie_workers())
    all_actions.extend(check_6_dashboard())
    all_actions.extend(check_7_score_anomaly(state))

    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    if all_actions:
        ts_now = datetime.now().strftime("%H:%M:%S %d.%m")
        msg = "🐕 <b>Watchdog</b>\n\n" + "\n\n".join(all_actions) + "\n\n<i>" + ts_now + "</i>"
        send_tg(msg)
        log(f"Sent {len(all_actions)} action(s) to Telegram")
    else:
        log("All OK ✓")

if __name__ == "__main__":
    main()
