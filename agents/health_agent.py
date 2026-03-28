#!/usr/bin/env python3
"""
Health Agent ("Doctor") — deep diagnostics + interactive Telegram fixes.

Runs every 15 min in TMUX session `health`.
Checks 6 categories (25+ checks), calculates health score 0-100.
Sends fix proposals to CEO via Telegram inline keyboards.
CEO approves → auto-fix. CEO rejects → pause 2hr.

Uses Claude Sonnet for complex issues (rate-limited to 1x per 30 min).
"""

import os
import sys
import json
import time
import sqlite3
import subprocess
import hashlib
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(BASE_DIR, "db", "experiments.db")
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
STATE_FILE = os.path.join(RUNTIME_DIR, "health_state.json")
LOG_FILE = os.path.join(BASE_DIR, "results", "health.log")
VENV = os.path.join(BASE_DIR, "venv", "bin", "activate")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL = 900  # 15 min
CALLBACK_POLL_INTERVAL = 30  # poll TG every 30s during sleep
SUPPRESS_DURATION = 7200  # 2hr after reject
CLAUDE_COOLDOWN = 1800  # 30 min between Claude calls

CRITICAL_FILES = [
    "agents/orchestrator_v2.py",
    "agents/backtest_agent.py",
    "agents/optimizer_agent.py",
    "strategy/base_strategy.py",
]

EXPECTED_TABLES = [
    "experiments", "instrument_metrics", "trade_log",
    "suggestions", "holdout_results", "analyst_reports",
]

TMUX_SESSIONS = ["backtest", "orchestrator", "monitor", "impulse", "dashboard"]

# ═══════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [Doctor] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# Telegram helpers (JSON API — supports inline keyboards)
# ═══════════════════════════════════════════════════════════

import urllib.request
import urllib.parse


def _tg_request(method, payload):
    """Generic Telegram Bot API call via JSON body."""
    if not TG_TOKEN or not TG_CHAT:
        return None
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except Exception as e:
        log(f"TG {method} error: {e}")
        return None


def send_tg(text, keyboard=None):
    """Send message, optionally with inline keyboard. Returns message_id."""
    payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    result = _tg_request("sendMessage", payload)
    if result and result.get("ok"):
        return result["result"]["message_id"]
    return None


def edit_tg(message_id, new_text):
    """Edit existing message (remove buttons after decision)."""
    payload = {
        "chat_id": TG_CHAT,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "HTML",
    }
    _tg_request("editMessageText", payload)


def answer_callback(callback_query_id, text=""):
    """Answer callback query (removes loading spinner)."""
    _tg_request("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
    })


def poll_callbacks(last_update_id=0):
    """Poll for callback_query updates. Returns (updates, new_last_id)."""
    payload = {
        "offset": last_update_id + 1,
        "timeout": 1,
        "allowed_updates": ["callback_query"],
    }
    result = _tg_request("getUpdates", payload)
    if not result or not result.get("ok"):
        return [], last_update_id
    updates = result.get("result", [])
    new_id = last_update_id
    for u in updates:
        if u["update_id"] > new_id:
            new_id = u["update_id"]
    return updates, new_id


def clear_webhook():
    """Ensure no webhook is set (would block getUpdates)."""
    result = _tg_request("getWebhookInfo", {})
    if result and result.get("ok"):
        url = result["result"].get("url", "")
        if url:
            log(f"Webhook found ({url}), deleting...")
            _tg_request("deleteWebhook", {"drop_pending_updates": False})


# ═══════════════════════════════════════════════════════════
# State management
# ═══════════════════════════════════════════════════════════

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "last_run": None,
        "last_update_id": 0,
        "health_score": 100,
        "check_scores": {},
        "pending_fixes": {},
        "fix_history": [],
        "suppressed_checks": {},
        "claude_last_invoked": 0,
        "webhook_cleared": False,
        "cycle_count": 0,
    }


def save_state(state):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.rename(tmp, STATE_FILE)


# ═══════════════════════════════════════════════════════════
# Utility
# ═══════════════════════════════════════════════════════════

def run_cmd(cmd, timeout=10):
    """Run shell command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def tmux_alive(name):
    rc, _, _ = run_cmd(f"tmux has-session -t {name} 2>/dev/null")
    return rc == 0


LOG_FILES = {
    "orchestrator": "results/orchestrator.log",
    "backtest": "results/orchestrator.log",  # backtest output goes to orchestrator log via tee
}

def agent_has_recent_activity(name, max_age=1800):
    """Check if agent has recent activity via log file mtime (more reliable than tmux pane)."""
    # Primary: check log file freshness
    log_rel = LOG_FILES.get(name)
    if log_rel:
        log_path = os.path.join(BASE_DIR, log_rel)
        if os.path.exists(log_path):
            return file_age_seconds(log_path) < max_age
    # Fallback: check if tmux pane has any content via capture-pane with scrollback
    rc, out, _ = run_cmd(f"tmux capture-pane -t {name} -p 2>/dev/null")
    return bool(out.strip()) if rc == 0 else False


def file_age_seconds(path):
    """How many seconds since file was last modified."""
    try:
        return time.time() - os.path.getmtime(path)
    except Exception:
        return float("inf")


def file_size_mb(path):
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except Exception:
        return 0


def make_fix_id(check_id):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"fix_{ts}_{check_id}"


# ═══════════════════════════════════════════════════════════
# HEALTH CHECKS
# ═══════════════════════════════════════════════════════════

def check_A_agent_vitality(state):
    """Category A: Agent process health (weight 0.20)."""
    issues = []
    score = 100
    deductions = 0

    # A1: TMUX sessions alive + recent output
    for name in TMUX_SESSIONS:
        if not tmux_alive(name):
            deductions += 15
            issues.append({
                "check_id": f"A1_{name}",
                "severity": "critical" if name in ("orchestrator", "backtest") else "warning",
                "title": f"TMUX session '{name}' is dead",
                "description": f"Session {name} not found in tmux. Agent is not running.",
                "fix_action": "restart_agent",
                "fix_detail": {"agent": name},
                "risk": "medium",
                "reversible": True,
            })
        else:
            # Only check activity freshness for core agents
            if name in ("orchestrator", "backtest"):
                if not agent_has_recent_activity(name, max_age=1800):
                    deductions += 5
                    issues.append({
                        "check_id": f"A1_{name}_silent",
                        "severity": "warning",
                        "title": f"Agent '{name}' no activity for 30+ min",
                        "description": f"Log file for {name} not updated recently — might be stuck.",
                        "fix_action": "restart_agent",
                        "fix_detail": {"agent": name},
                        "risk": "medium",
                        "reversible": True,
                    })

    # A2: Process resource usage
    rc, out, _ = run_cmd("ps aux | grep python3 | grep -v grep")
    if rc == 0:
        py_procs = [l for l in out.split("\n") if l.strip()]
        total_rss = 0
        for line in py_procs:
            parts = line.split()
            if len(parts) >= 6:
                try:
                    total_rss += int(parts[5])  # RSS in KB
                except (ValueError, IndexError):
                    pass
        total_rss_mb = total_rss / 1024
        if total_rss_mb > 3000:
            deductions += 10
            issues.append({
                "check_id": "A2_memory",
                "severity": "warning",
                "title": f"High memory usage: {total_rss_mb:.0f}MB",
                "description": "Total Python RSS > 3GB. Possible memory leak.",
                "fix_action": "notify_only",
                "risk": "medium",
            })

    # A3: Log error rate (last 50 lines of orchestrator log)
    log_path = os.path.join(BASE_DIR, "results", "orchestrator.log")
    if os.path.exists(log_path):
        rc, out, _ = run_cmd(f"tail -50 {log_path}")
        if rc == 0:
            error_count = sum(1 for line in out.split("\n")
                              if any(kw in line.lower() for kw in ["error", "traceback", "exception"]))
            if error_count > 15:
                deductions += 15
                issues.append({
                    "check_id": "A3_error_rate",
                    "severity": "warning",
                    "title": f"High error rate in orchestrator log ({error_count}/50 lines)",
                    "description": "Many errors in recent orchestrator output. May need investigation.",
                    "fix_action": "claude_diagnose",
                    "risk": "low",
                })

    # A4: Stale backtest request
    req_file = os.path.join(RUNTIME_DIR, "backtest_request.json")
    done_file = os.path.join(RUNTIME_DIR, "backtest_done.json")
    if os.path.exists(req_file) and not os.path.exists(done_file):
        age = file_age_seconds(req_file)
        if age > 2700:  # 45 min
            deductions += 15
            issues.append({
                "check_id": "A4_stale_request",
                "severity": "critical",
                "title": f"Backtest request stuck ({age/60:.0f} min)",
                "description": "backtest_request.json exists without done file for > 45 min.",
                "fix_action": "clear_stale_request",
                "risk": "medium",
                "reversible": True,
            })

    score = max(0, score - deductions)
    return score, issues


def check_B_database(state):
    """Category B: Database integrity (weight 0.20)."""
    issues = []
    score = 100
    deductions = 0

    if not os.path.exists(DB_PATH):
        return 0, [{"check_id": "B0", "severity": "critical",
                     "title": "Database not found", "description": f"{DB_PATH} missing",
                     "fix_action": "notify_only", "risk": "high"}]

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)

        # B1: Tables exist
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        missing = [t for t in EXPECTED_TABLES if t not in tables]
        if missing:
            deductions += 20
            issues.append({
                "check_id": "B1_missing_tables",
                "severity": "critical",
                "title": f"Missing DB tables: {', '.join(missing)}",
                "description": "Expected tables not found. DB may need re-initialization.",
                "fix_action": "notify_only",
                "risk": "high",
            })

        # B2: Integrity check
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result and result[0] != "ok":
            deductions += 30
            issues.append({
                "check_id": "B2_integrity",
                "severity": "critical",
                "title": "Database integrity check failed",
                "description": f"PRAGMA integrity_check returned: {result[0][:100]}",
                "fix_action": "notify_only",
                "risk": "high",
            })

        # B3: Row count sanity
        if "experiments" in tables:
            count = conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
            prev_count = state.get("prev_experiment_count", 0)
            if count < prev_count and prev_count > 0:
                deductions += 15
                issues.append({
                    "check_id": "B3_row_decrease",
                    "severity": "critical",
                    "title": f"Experiment count decreased: {prev_count} → {count}",
                    "description": "Row count dropped. Possible data corruption or unauthorized DELETE.",
                    "fix_action": "notify_only",
                    "risk": "high",
                })
            state["prev_experiment_count"] = count

        # B4: Type mismatches in recent rows
        if "experiments" in tables:
            rows = conn.execute(
                "SELECT typeof(avg_score), typeof(old_value), typeof(new_value) "
                "FROM experiments ORDER BY id DESC LIMIT 20"
            ).fetchall()
            type_issues = 0
            for avg_type, old_type, new_type in rows:
                if avg_type not in ("real", "integer", "null"):
                    type_issues += 1
            if type_issues > 5:
                deductions += 10
                issues.append({
                    "check_id": "B4_type_mismatch",
                    "severity": "warning",
                    "title": f"Type mismatches in experiments ({type_issues}/20 rows)",
                    "description": "avg_score stored as text in multiple rows. May cause formatting errors.",
                    "fix_action": "notify_only",
                    "risk": "medium",
                })

        # B5: WAL file size
        wal_path = DB_PATH + "-wal"
        wal_mb = file_size_mb(wal_path)
        if wal_mb > 50:
            deductions += 10
            issues.append({
                "check_id": "B5_wal_large",
                "severity": "warning",
                "title": f"WAL file large: {wal_mb:.0f}MB",
                "description": "WAL file > 50MB. Checkpointing may help performance.",
                "fix_action": "wal_checkpoint",
                "risk": "low",
                "reversible": True,
            })

        # B6: DB file size
        db_mb = file_size_mb(DB_PATH)
        if db_mb > 500:
            deductions += 5
            issues.append({
                "check_id": "B6_db_size",
                "severity": "info",
                "title": f"Database large: {db_mb:.0f}MB",
                "description": "Consider archiving old experiment data.",
                "fix_action": "notify_only",
                "risk": "low",
            })

        conn.close()
    except Exception as e:
        deductions += 25
        issues.append({
            "check_id": "B_error",
            "severity": "critical",
            "title": f"DB check error: {str(e)[:80]}",
            "description": str(e),
            "fix_action": "notify_only",
            "risk": "high",
        })

    score = max(0, score - deductions)
    return score, issues


def check_C_runtime_files(state):
    """Category C: Runtime file health (weight 0.15)."""
    issues = []
    score = 100
    deductions = 0

    # C1: Stale request without done (handled in A4, skip here)

    # C2: Orphan done file
    done_file = os.path.join(RUNTIME_DIR, "backtest_done.json")
    req_file = os.path.join(RUNTIME_DIR, "backtest_request.json")
    if os.path.exists(done_file) and not os.path.exists(req_file):
        age = file_age_seconds(done_file)
        if age > 600:  # 10 min old orphan
            deductions += 5
            issues.append({
                "check_id": "C2_orphan_done",
                "severity": "info",
                "title": "Orphan backtest_done.json",
                "description": f"Done file exists without request ({age/60:.0f} min old). Harmless but messy.",
                "fix_action": "cleanup_runtime",
                "fix_detail": {"file": "backtest_done.json"},
                "risk": "low",
                "reversible": True,
            })

    # C3: JSON validity
    if os.path.isdir(RUNTIME_DIR):
        for fname in os.listdir(RUNTIME_DIR):
            if fname.endswith(".json") and fname != "health_state.json":
                fpath = os.path.join(RUNTIME_DIR, fname)
                try:
                    with open(fpath) as f:
                        json.load(f)
                except (json.JSONDecodeError, ValueError) as e:
                    deductions += 10
                    issues.append({
                        "check_id": f"C3_invalid_{fname}",
                        "severity": "warning",
                        "title": f"Invalid JSON: {fname}",
                        "description": f"Cannot parse runtime/{fname}: {str(e)[:80]}",
                        "fix_action": "cleanup_runtime",
                        "fix_detail": {"file": fname},
                        "risk": "low",
                        "reversible": False,
                    })

    # C4: Watchdog state freshness
    watchdog_state = os.path.join(RUNTIME_DIR, "watchdog_state.json")
    if os.path.exists(watchdog_state):
        age = file_age_seconds(watchdog_state)
        if age > 2100:  # 35 min (cron is */30)
            deductions += 10
            severity = "critical" if age > 7200 else "warning"  # critical if > 2hr
            issues.append({
                "check_id": "C4_watchdog_stale",
                "severity": severity,
                "title": f"Watchdog cron not running ({age/60:.0f} min stale)",
                "description": "Watchdog cron (*/30) hasn't updated state. Check: crontab -l",
                "fix_action": "notify_only",
                "risk": "medium",
            })

    # C5: params.json validity
    params_path = os.path.join(BASE_DIR, "strategy", "params.json")
    if os.path.exists(params_path):
        try:
            with open(params_path) as f:
                params = json.load(f)
            if not isinstance(params, dict) or len(params) < 3:
                deductions += 15
                issues.append({
                    "check_id": "C5_params_invalid",
                    "severity": "critical",
                    "title": "params.json looks corrupted",
                    "description": f"Expected dict with 3+ keys, got {type(params).__name__} with {len(params) if hasattr(params, '__len__') else '?'} items",
                    "fix_action": "notify_only",
                    "risk": "high",
                })
        except Exception as e:
            deductions += 20
            issues.append({
                "check_id": "C5_params_error",
                "severity": "critical",
                "title": f"params.json parse error: {str(e)[:60]}",
                "description": str(e),
                "fix_action": "notify_only",
                "risk": "high",
            })

    score = max(0, score - deductions)
    return score, issues


def check_D_performance(state):
    """Category D: Performance trends (weight 0.20)."""
    issues = []
    score = 100
    deductions = 0

    if not os.path.exists(DB_PATH):
        return score, issues

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)

        # D1: Error/crash rate in last 20 experiments
        rows = conn.execute(
            "SELECT action FROM experiments ORDER BY id DESC LIMIT 20"
        ).fetchall()
        if rows:
            error_count = sum(1 for r in rows if r[0] in ("error", "crash", "anomaly"))
            error_rate = error_count / len(rows)
            if error_rate > 0.3:
                deductions += 20
                issues.append({
                    "check_id": "D1_error_rate",
                    "severity": "critical" if error_rate > 0.5 else "warning",
                    "title": f"High error rate: {error_rate:.0%} in last {len(rows)} experiments",
                    "description": f"{error_count} errors/anomalies out of {len(rows)} recent experiments.",
                    "fix_action": "claude_diagnose",
                    "risk": "low",
                })

        # D2: Iteration speed (compare last 10 vs previous 10 by timestamp)
        ts_rows = conn.execute(
            "SELECT timestamp FROM experiments ORDER BY id DESC LIMIT 20"
        ).fetchall()
        if len(ts_rows) >= 15:
            try:
                recent = [datetime.fromisoformat(r[0]) for r in ts_rows[:10] if r[0]]
                older = [datetime.fromisoformat(r[0]) for r in ts_rows[10:20] if r[0]]
                if len(recent) >= 2 and len(older) >= 2:
                    recent_avg = (recent[0] - recent[-1]).total_seconds() / len(recent)
                    older_avg = (older[0] - older[-1]).total_seconds() / len(older)
                    if older_avg > 0 and recent_avg > older_avg * 2:
                        deductions += 10
                        issues.append({
                            "check_id": "D2_slowdown",
                            "severity": "warning",
                            "title": f"Backtest slowing: {recent_avg/60:.0f}min vs {older_avg/60:.0f}min avg",
                            "description": "Recent iterations are 2x+ slower than before.",
                            "fix_action": "notify_only",
                            "risk": "low",
                        })
            except Exception:
                pass

        # D3: Score trajectory (last 20 keeps/baselines)
        score_rows = conn.execute(
            "SELECT avg_score FROM experiments WHERE action IN ('keep', 'baseline') "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
        if len(score_rows) >= 5:
            scores = [float(r[0]) for r in score_rows if r[0] is not None]
            if len(scores) >= 5:
                # Simple: compare first half vs second half
                mid = len(scores) // 2
                recent_avg = sum(scores[:mid]) / mid
                older_avg = sum(scores[mid:]) / (len(scores) - mid)
                if older_avg > 0 and recent_avg < older_avg * 0.8:
                    deductions += 10
                    issues.append({
                        "check_id": "D3_score_decline",
                        "severity": "warning",
                        "title": f"Score declining: {recent_avg:.3f} vs {older_avg:.3f}",
                        "description": "Recent keep scores are 20%+ lower than earlier ones.",
                        "fix_action": "notify_only",
                        "risk": "low",
                    })

        # D4: Zero-trade streak
        trade_rows = conn.execute(
            "SELECT total_trades FROM experiments ORDER BY id DESC LIMIT 10"
        ).fetchall()
        zero_streak = 0
        for r in trade_rows:
            if r[0] is not None and int(r[0]) == 0:
                zero_streak += 1
            else:
                break
        if zero_streak >= 3:
            deductions += 15
            issues.append({
                "check_id": "D4_zero_trades",
                "severity": "critical",
                "title": f"Zero-trade streak: {zero_streak} iterations",
                "description": "Multiple consecutive experiments produced 0 trades.",
                "fix_action": "restart_agent",
                "fix_detail": {"agent": "backtest"},
                "risk": "medium",
                "reversible": True,
            })

        # D5: Revert ratio
        action_rows = conn.execute(
            "SELECT action FROM experiments WHERE action IN ('keep', 'revert') "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
        if len(action_rows) >= 10:
            reverts = sum(1 for r in action_rows if r[0] == "revert")
            ratio = reverts / len(action_rows)
            if ratio > 0.8:
                deductions += 10
                issues.append({
                    "check_id": "D5_revert_ratio",
                    "severity": "warning",
                    "title": f"High revert ratio: {ratio:.0%} in last {len(action_rows)}",
                    "description": "System is stuck — most changes get reverted.",
                    "fix_action": "notify_only",
                    "risk": "low",
                })

        conn.close()
    except Exception as e:
        log(f"D check error: {e}")

    score = max(0, score - deductions)
    return score, issues


def check_E_resources(state):
    """Category E: System resources (weight 0.10)."""
    issues = []
    score = 100
    deductions = 0

    # E1: Disk space
    rc, out, _ = run_cmd("df -BG / | tail -1")
    if rc == 0:
        parts = out.split()
        if len(parts) >= 4:
            try:
                avail_gb = int(parts[3].replace("G", ""))
                if avail_gb < 2:
                    deductions += 25
                    issues.append({
                        "check_id": "E1_disk",
                        "severity": "critical",
                        "title": f"Low disk space: {avail_gb}GB free",
                        "description": "Less than 2GB free. System may fail on next write.",
                        "fix_action": "notify_only",
                        "risk": "high",
                    })
            except ValueError:
                pass

    # E2: Python process count
    rc, out, _ = run_cmd("pgrep -c python3 2>/dev/null || echo 0")
    if rc == 0:
        try:
            proc_count = int(out.strip())
            if proc_count > 20:
                deductions += 15
                issues.append({
                    "check_id": "E2_zombies",
                    "severity": "warning",
                    "title": f"Too many python3 processes: {proc_count}",
                    "description": "Likely zombie/leaked workers.",
                    "fix_action": "kill_zombies",
                    "risk": "low",
                    "reversible": False,
                })
        except ValueError:
            pass

    score = max(0, score - deductions)
    return score, issues


def check_F_code_integrity(state):
    """Category F: Code integrity (weight 0.15)."""
    issues = []
    score = 100
    deductions = 0

    # F1: Syntax check on critical files
    for relpath in CRITICAL_FILES:
        fpath = os.path.join(BASE_DIR, relpath)
        if not os.path.exists(fpath):
            deductions += 10
            issues.append({
                "check_id": f"F1_{os.path.basename(relpath)}",
                "severity": "critical",
                "title": f"Missing file: {relpath}",
                "description": f"Critical file {relpath} does not exist.",
                "fix_action": "notify_only",
                "risk": "high",
            })
            continue
        rc, _, err = run_cmd(f"python3 -m py_compile {fpath}")
        if rc != 0:
            deductions += 20
            issues.append({
                "check_id": f"F1_{os.path.basename(relpath)}",
                "severity": "critical",
                "title": f"Syntax error in {relpath}",
                "description": f"py_compile failed: {err[:120]}",
                "fix_action": "notify_only",
                "risk": "high",
            })

    # F2: Dashboard responding
    rc, out, _ = run_cmd("curl -s -o /dev/null -w '%{http_code}' -u '111blackjack111:qwertrewq123454321' http://localhost:8080/ --max-time 5")
    if out.strip() != "200":
        deductions += 10
        issues.append({
            "check_id": "F2_dashboard",
            "severity": "warning",
            "title": f"Dashboard not responding (HTTP {out.strip() or 'timeout'})",
            "description": "Dashboard at :8080 returned non-200 or timed out.",
            "fix_action": "restart_agent",
            "fix_detail": {"agent": "dashboard"},
            "risk": "low",
            "reversible": True,
        })

    score = max(0, score - deductions)
    return score, issues


# ═══════════════════════════════════════════════════════════
# Health score calculation
# ═══════════════════════════════════════════════════════════

WEIGHTS = {"A": 0.20, "B": 0.20, "C": 0.15, "D": 0.20, "E": 0.10, "F": 0.15}

def calculate_health_score(check_scores):
    total = 0
    for cat, weight in WEIGHTS.items():
        total += check_scores.get(cat, 100) * weight
    return round(total, 1)


# ═══════════════════════════════════════════════════════════
# Fix proposal & execution
# ═══════════════════════════════════════════════════════════

def is_suppressed(check_id, state):
    """Check if this check is suppressed (CEO rejected recently)."""
    suppressed = state.get("suppressed_checks", {})
    until = suppressed.get(check_id)
    if until:
        try:
            if datetime.now().isoformat() < until:
                return True
        except Exception:
            pass
        # Expired — clean up
        del suppressed[check_id]
    return False


def filter_issues(all_issues, state):
    """Filter to only Telegram-worthy issues: actionable fixes or critical alerts."""
    result = []
    for i in all_issues:
        if is_suppressed(i["check_id"], state):
            continue
        if i["severity"] == "info":
            continue
        # notify_only warnings are logged but NOT sent to Telegram (avoid spam)
        # Only send: actionable fixes (with buttons) or critical notify_only
        if i["fix_action"] == "notify_only" and i["severity"] != "critical":
            continue
        result.append(i)
    return result


def send_fix_proposal(issue, state):
    """Send issue to Telegram with approve/reject buttons."""
    fix_id = make_fix_id(issue["check_id"])

    severity_emoji = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(issue["severity"], "⚪")
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(issue.get("risk", "low"), "⚪")

    text = (
        f"🩺 <b>Doctor Agent</b>\n\n"
        f"{severity_emoji} <b>{issue['title']}</b>\n"
        f"{issue['description']}\n\n"
        f"<b>Fix:</b> {issue['fix_action']}\n"
        f"<b>Risk:</b> {risk_emoji} {issue.get('risk', 'unknown')}\n"
    )

    if issue["fix_action"] == "notify_only":
        # No buttons for notify-only — just send
        send_tg(text + "\n<i>Требует ручного вмешательства</i>")
        return

    keyboard = [[
        {"text": "✅ Принять", "callback_data": f"approve_{fix_id}"},
        {"text": "❌ Отклонить", "callback_data": f"reject_{fix_id}"},
    ]]

    msg_id = send_tg(text, keyboard=keyboard)

    if msg_id:
        state.setdefault("pending_fixes", {})[fix_id] = {
            "id": fix_id,
            "check_id": issue["check_id"],
            "issue": issue,
            "message_id": msg_id,
            "status": "pending_approval",
            "created_at": datetime.now().isoformat(),
        }
        log(f"Sent fix proposal {fix_id} (msg {msg_id})")


def execute_fix(fix_data):
    """Execute an approved fix. Returns (success, message)."""
    issue = fix_data["issue"]
    action = issue["fix_action"]
    detail = issue.get("fix_detail", {})

    try:
        if action == "restart_agent":
            agent = detail.get("agent", "")
            if not agent:
                return False, "No agent specified"
            # Get restart command from watchdog pattern
            cmds = {
                "backtest": f"cd {BASE_DIR} && source {VENV} && python3 agents/backtest_agent.py --mode watch",
                "orchestrator": f"cd {BASE_DIR} && source {VENV} && export TELEGRAM_BOT_TOKEN={TG_TOKEN} && export TELEGRAM_CHAT_ID={TG_CHAT} && python3 -u agents/orchestrator_v2.py --iterations 100 --skip-data 2>&1 | tee results/orchestrator.log",
                "monitor": f"cd {BASE_DIR} && source {VENV} && export TELEGRAM_BOT_TOKEN={TG_TOKEN} && export TELEGRAM_CHAT_ID={TG_CHAT} && python3 -u agents/monitor_agent.py 2>&1 | tee results/monitor.log",
                "dashboard": f"cd {BASE_DIR} && source {VENV} && python3 -u dashboard_v5.py 2>&1 | tee results/dashboard.log",
                "impulse": f"cd {BASE_DIR} && source {VENV} && while true; do python3 agents/impulse_agent.py --mode scan --days 7 2>&1 | tee -a results/impulse.log; sleep 3600; done",
            }
            cmd = cmds.get(agent)
            if not cmd:
                return False, f"Unknown agent: {agent}"
            run_cmd(f"tmux kill-session -t {agent} 2>/dev/null", timeout=5)
            time.sleep(1)
            run_cmd(f"tmux new-session -d -s {agent} '{cmd}'", timeout=5)
            time.sleep(2)
            if tmux_alive(agent):
                return True, f"Agent '{agent}' restarted successfully"
            return False, f"Agent '{agent}' failed to restart"

        elif action == "wal_checkpoint":
            conn = sqlite3.connect(DB_PATH, timeout=10)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            wal_mb = file_size_mb(DB_PATH + "-wal")
            return True, f"WAL checkpoint done. New WAL size: {wal_mb:.1f}MB"

        elif action == "cleanup_runtime":
            fname = detail.get("file", "")
            if not fname or "/" in fname or ".." in fname:
                return False, "Invalid filename"
            fpath = os.path.join(RUNTIME_DIR, fname)
            if os.path.exists(fpath):
                os.remove(fpath)
                return True, f"Removed runtime/{fname}"
            return True, f"File already gone: runtime/{fname}"

        elif action == "kill_zombies":
            rc, out, _ = run_cmd(
                "pgrep -f 'multiprocessing.spawn' | head -20 | xargs -r kill -9 2>/dev/null; echo done",
                timeout=10,
            )
            return True, f"Zombie cleanup: {out}"

        elif action == "clear_stale_request":
            req_file = os.path.join(RUNTIME_DIR, "backtest_request.json")
            if os.path.exists(req_file):
                os.remove(req_file)
                return True, "Removed stale backtest_request.json"
            return True, "Request file already gone"

        elif action == "claude_diagnose":
            return invoke_claude_diagnosis(fix_data)

        else:
            return False, f"Unknown action: {action}"

    except Exception as e:
        return False, f"Exception: {str(e)[:150]}"


def invoke_claude_diagnosis(fix_data):
    """Use Claude Sonnet for deeper issue analysis."""
    issue = fix_data["issue"]
    # Gather context
    rc, orch_log, _ = run_cmd(f"tail -30 {BASE_DIR}/results/orchestrator.log 2>/dev/null")
    rc2, db_info, _ = run_cmd(
        f"sqlite3 {DB_PATH} \"SELECT action, COUNT(*) FROM experiments GROUP BY action ORDER BY COUNT(*) DESC LIMIT 10\" 2>/dev/null"
    )

    prompt = f"""You are a trading system doctor. Diagnose this issue:

Issue: {issue['title']}
Details: {issue['description']}

Recent orchestrator log:
{orch_log[:1500]}

Experiment action counts:
{db_info[:500]}

Provide:
1. Root cause (1-2 sentences)
2. Recommended fix (specific)
3. Risk level

Keep response under 200 words. Be specific."""

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--model", "sonnet"],
            input=prompt, capture_output=True, text=True, timeout=120,
            cwd=BASE_DIR,
        )
        if result.returncode == 0 and result.stdout.strip():
            diagnosis = result.stdout.strip()[:1000]
            send_tg(f"🩺 <b>Claude Diagnosis</b>\n\n<pre>{diagnosis}</pre>")
            return True, "Diagnosis sent to Telegram"
        return False, f"Claude error: {result.stderr[:100]}"
    except Exception as e:
        return False, f"Claude invocation failed: {str(e)[:100]}"


# ═══════════════════════════════════════════════════════════
# Callback processing
# ═══════════════════════════════════════════════════════════

def process_callbacks(state):
    """Poll Telegram for callback responses and handle them."""
    updates, new_id = poll_callbacks(state.get("last_update_id", 0))
    state["last_update_id"] = new_id

    for update in updates:
        cb = update.get("callback_query")
        if not cb:
            continue

        cb_id = cb["id"]
        cb_data = cb.get("data", "")
        msg_id = cb.get("message", {}).get("message_id")

        # Parse: approve_fix_XXXXX or reject_fix_XXXXX
        if cb_data.startswith("approve_"):
            fix_id = cb_data[len("approve_"):]
            handle_approve(fix_id, cb_id, msg_id, state)
        elif cb_data.startswith("reject_"):
            fix_id = cb_data[len("reject_"):]
            handle_reject(fix_id, cb_id, msg_id, state)
        else:
            answer_callback(cb_id, "Unknown action")


def handle_approve(fix_id, cb_id, msg_id, state):
    """CEO approved a fix — execute it."""
    pending = state.get("pending_fixes", {})
    fix_data = pending.get(fix_id)

    if not fix_data:
        answer_callback(cb_id, "Fix expired or not found")
        return

    answer_callback(cb_id, "Принято! Выполняю...")
    log(f"Fix {fix_id} APPROVED by CEO")

    # Update message
    edit_tg(msg_id, f"🩺 Fix {fix_id}\n\n✅ <b>APPROVED</b>\nВыполняю...")

    # Execute
    success, result_msg = execute_fix(fix_data)

    # Update message with result
    status = "✅ Готово" if success else "❌ Ошибка"
    edit_tg(msg_id, f"🩺 Fix {fix_id}\n\n{status}\n{result_msg}")

    # Move to history
    fix_data["status"] = "done" if success else "failed"
    fix_data["executed_at"] = datetime.now().isoformat()
    fix_data["result"] = result_msg
    state.setdefault("fix_history", []).append(fix_data)
    del pending[fix_id]


def handle_reject(fix_id, cb_id, msg_id, state):
    """CEO rejected a fix — suppress for 2 hours."""
    pending = state.get("pending_fixes", {})
    fix_data = pending.get(fix_id)

    if not fix_data:
        answer_callback(cb_id, "Fix expired or not found")
        return

    answer_callback(cb_id, "Отклонено. CEO берёт контроль.")
    log(f"Fix {fix_id} REJECTED by CEO")

    # Suppress this check for 2 hours
    check_id = fix_data.get("check_id", "")
    until = (datetime.now() + timedelta(seconds=SUPPRESS_DURATION)).isoformat()
    state.setdefault("suppressed_checks", {})[check_id] = until

    # Update message
    edit_tg(msg_id, f"🩺 Fix {fix_id}\n\n❌ <b>REJECTED</b>\nCEO берёт контроль.\nCheck suppressed до {until[:16]}")

    # Move to history
    fix_data["status"] = "rejected"
    fix_data["decided_at"] = datetime.now().isoformat()
    state.setdefault("fix_history", []).append(fix_data)
    del pending[fix_id]


# ═══════════════════════════════════════════════════════════
# Summary report
# ═══════════════════════════════════════════════════════════

def send_health_summary(health_score, check_scores, issues_count):
    """Periodic summary (every ~6 cycles = ~90 min)."""
    score_emoji = "🟢" if health_score >= 80 else "🟡" if health_score >= 50 else "🔴"

    cats = []
    for cat, name in [("A", "Agents"), ("B", "Database"), ("C", "Files"),
                       ("D", "Performance"), ("E", "Resources"), ("F", "Code")]:
        s = check_scores.get(cat, 100)
        e = "🟢" if s >= 80 else "🟡" if s >= 50 else "🔴"
        cats.append(f"  {e} {name}: {s}/100")

    text = (
        f"🩺 <b>Health Report</b>\n\n"
        f"{score_emoji} <b>Overall: {health_score}/100</b>\n\n"
        + "\n".join(cats)
        + f"\n\nIssues found: {issues_count}"
    )
    send_tg(text)


# ═══════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════

def run_checks(state):
    """Run all health checks, return (check_scores, all_issues)."""
    all_issues = []
    check_scores = {}

    checks = [
        ("A", check_A_agent_vitality),
        ("B", check_B_database),
        ("C", check_C_runtime_files),
        ("D", check_D_performance),
        ("E", check_E_resources),
        ("F", check_F_code_integrity),
    ]

    for cat, check_fn in checks:
        try:
            score, issues = check_fn(state)
            check_scores[cat] = score
            all_issues.extend(issues)
            log(f"  {cat}: {score}/100 ({len(issues)} issues)")
        except Exception as e:
            log(f"  {cat}: ERROR — {e}")
            check_scores[cat] = 50
            all_issues.append({
                "check_id": f"{cat}_error",
                "severity": "warning",
                "title": f"Check {cat} failed: {str(e)[:60]}",
                "description": str(e),
                "fix_action": "notify_only",
                "risk": "low",
            })

    return check_scores, all_issues


def main():
    log("Doctor Agent starting...")
    state = load_state()

    # Clear webhook if needed
    if not state.get("webhook_cleared"):
        clear_webhook()
        state["webhook_cleared"] = True

    send_tg("🩺 <b>Doctor Agent started</b>\nMonitoring system health every 15 min.")
    log("Startup complete. Entering main loop.")

    while True:
        cycle_start = time.time()
        state["cycle_count"] = state.get("cycle_count", 0) + 1
        log(f"\n{'='*50}")
        log(f"Health check cycle #{state['cycle_count']}")

        # Phase 1: Process pending callbacks
        process_callbacks(state)

        # Phase 2: Execute any approved fixes that haven't run yet
        # (callbacks processed above already execute, but just in case)

        # Phase 3: Run all health checks
        check_scores, all_issues = run_checks(state)
        health_score = calculate_health_score(check_scores)

        state["check_scores"] = check_scores
        state["health_score"] = health_score
        state["last_run"] = datetime.now().isoformat()

        log(f"Health score: {health_score}/100 | Issues: {len(all_issues)}")

        # Phase 4: Filter (remove suppressed, info-only)
        actionable = filter_issues(all_issues, state)

        # Phase 5: Send fix proposals for new issues
        # Deduplicate: don't re-send for issues that already have pending fixes
        pending_checks = {f["check_id"] for f in state.get("pending_fixes", {}).values()}
        new_issues = [i for i in actionable if i["check_id"] not in pending_checks]

        for issue in new_issues:
            send_fix_proposal(issue, state)

        # Phase 6: Claude Sonnet for complex issues (rate-limited)
        if health_score < 60 and any(i["severity"] == "critical" for i in actionable):
            last_claude = state.get("claude_last_invoked", 0)
            if time.time() - last_claude > CLAUDE_COOLDOWN:
                log("Health score critical — invoking Claude Sonnet...")
                critical = [i for i in actionable if i["severity"] == "critical"]
                # Create a synthetic fix for Claude diagnosis
                diag_issue = {
                    "check_id": "claude_auto",
                    "severity": "critical",
                    "title": f"System health critical ({health_score}/100)",
                    "description": "; ".join(i["title"] for i in critical[:5]),
                    "fix_action": "claude_diagnose",
                    "risk": "low",
                }
                invoke_claude_diagnosis({"issue": diag_issue})
                state["claude_last_invoked"] = time.time()

        # Phase 7: Periodic summary (every 6 cycles ≈ 90 min)
        if state["cycle_count"] % 6 == 0:
            send_health_summary(health_score, check_scores, len(all_issues))

        # Save state
        save_state(state)

        # Phase 8: Sleep with callback polling
        elapsed = time.time() - cycle_start
        remaining = max(60, CHECK_INTERVAL - elapsed)
        log(f"Next check in {remaining/60:.1f} min. Polling callbacks...")

        poll_end = time.time() + remaining
        while time.time() < poll_end:
            try:
                process_callbacks(state)
                save_state(state)  # persist after each callback
            except Exception as e:
                log(f"Callback poll error: {e}")
            time.sleep(CALLBACK_POLL_INTERVAL)

        log("Cycle complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Shutting down...")
    except Exception as e:
        log(f"FATAL: {e}")
        # Try to notify CEO
        try:
            send_tg(f"🔴 <b>Doctor Agent CRASHED</b>\n\n{str(e)[:300]}")
        except Exception:
            pass
        raise
