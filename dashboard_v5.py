"""
Modern Trading System Dashboard v5.
http://server:8080
Auto-refreshes every 30s via JS fetch (no page reload).
Endpoints: GET / (HTML), GET /api/data (JSON).

v5 additions:
  - All data sources cached with 30s TTL
  - ThreadingHTTPServer for non-blocking
  - Single DB query for scores + equity curve
  - Iteration speed & ETA
  - Keep/Revert ratio (last 10 / last 50)
  - Per-instrument scores in table
  - MFE/MAE section (from exit_reason_breakdown avg_pnl)
  - Night/Day mode indicator
  - Last change detail card
"""

import os
import json
import sqlite3
import subprocess
import base64
import time as _time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

def _sf(val, default=0.0):
    """Safe float — handles None, strings, dicts."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "db", "experiments.db")
IMPULSE_DB_PATH = os.path.join(BASE_DIR, "db", "impulse_patterns.db")
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
RESULTS_TSV = os.path.join(BASE_DIR, "results", "results.tsv")
PARAMS_PATH = os.path.join(BASE_DIR, "strategy", "params.json")
ORCH_LOG = os.path.join(BASE_DIR, "results", "orchestrator.log")
MONITOR_LOG = os.path.join(BASE_DIR, "results", "monitor.log")
HOLDOUT_PATH = os.path.join(RUNTIME_DIR, "holdout_results.json")

TIME_RANGE_MAP = {
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "24h": timedelta(hours=24),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
}

# --------------- universal cache ---------------
_cache = {}
_CACHE_TTL = 30  # seconds


def _cached(key, fn):
    now = _time.time()
    if key in _cache and now - _cache[key][1] < _CACHE_TTL:
        return _cache[key][0]
    try:
        result = fn()
    except Exception:
        return _cache.get(key, [({}, 0)])[0]
    _cache[key] = (result, now)
    return result


def get_last_experiment_time():
    """Return the timestamp of the most recent experiment, or None."""
    def _inner():
        if not os.path.exists(DB_PATH):
            return None
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT timestamp FROM experiments ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[0]:
            ts = row[0].replace("T", " ")
            if "." in ts:
                return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
            return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return None
    return _cached("last_experiment_time", _inner)


def get_cutoff_timestamp(time_range):
    if not time_range or time_range == "all":
        return None
    delta = TIME_RANGE_MAP.get(time_range)
    if not delta:
        return None
    ref = get_last_experiment_time() or datetime.now()
    cutoff = ref - delta
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


# --------------- data fetching ---------------

def get_tmux_sessions():
    return _cached("tmux_sessions", _get_tmux_sessions)


def _get_tmux_sessions():
    try:
        out = subprocess.check_output(
            ["tmux", "list-sessions"], stderr=subprocess.DEVNULL, text=True, timeout=5
        )
        sessions = {}
        for line in out.strip().split("\n"):
            name = line.split(":")[0]
            sessions[name] = True
        return sessions
    except Exception:
        return {}


def get_agent_activity(name, lines=5):
    return _cached(f"activity_{name}", lambda: _get_agent_activity(name, lines))


def _get_agent_activity(name, lines=5):
    try:
        out = subprocess.check_output(
            ["tmux", "capture-pane", "-t", name, "-p", "-S", f"-{lines}"],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
        result = [l for l in out.strip().split("\n") if l.strip()]
        return result if result else ["idle"]
    except Exception:
        return ["not running"]


def get_process_info():
    return _cached("process_info", _get_process_info)


def _get_process_info():
    try:
        out = subprocess.check_output(
            ["ps", "aux"], stderr=subprocess.DEVNULL, text=True, timeout=5
        )
        procs = {}
        for line in out.split("\n"):
            if "python3" not in line or "grep" in line:
                continue
            if "orchestrator" in line:
                parts = line.split()
                procs["orchestrator"] = {"cpu": parts[2], "mem": parts[3]}
            elif "backtest_agent" in line and "spawn" not in line and "tmux" not in line:
                parts = line.split()
                procs["backtest"] = {"cpu": parts[2], "mem": parts[3]}
            elif "monitor_agent" in line:
                parts = line.split()
                procs["monitor"] = {"cpu": parts[2], "mem": parts[3]}
            elif "impulse_agent" in line:
                parts = line.split()
                procs["impulse"] = {"cpu": parts[2], "mem": parts[3]}
            elif "spawn" in line:
                parts = line.split()
                cpu = float(parts[2])
                if "workers" not in procs:
                    procs["workers"] = {"count": 0, "total_cpu": 0}
                procs["workers"]["count"] += 1
                procs["workers"]["total_cpu"] += cpu
            elif "dashboard" in line:
                parts = line.split()
                procs["dashboard"] = {"cpu": parts[2], "mem": parts[3]}
        return procs
    except Exception:
        return {}


def get_experiments(limit=15, cutoff=None):
    cache_key = f"experiments_{limit}_{cutoff}"
    return _cached(cache_key, lambda: _get_experiments(limit, cutoff))


def _get_experiments(limit=15, cutoff=None):
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        if cutoff:
            rows = conn.execute(
                "SELECT iteration, param_changed, old_value, new_value, "
                "round(avg_score,4) as avg_score, round(avg_winrate,4) as avg_winrate, "
                "total_trades, best_instrument, action, timestamp, notes "
                "FROM experiments WHERE timestamp >= ? ORDER BY id DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT iteration, param_changed, old_value, new_value, "
                "round(avg_score,4) as avg_score, round(avg_winrate,4) as avg_winrate, "
                "total_trades, best_instrument, action, timestamp, notes "
                "FROM experiments ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_all_scores_and_equity(cutoff=None):
    """Single DB query for both score history and equity curve data."""
    cache_key = f"scores_equity_{cutoff}"
    return _cached(cache_key, lambda: _get_all_scores_and_equity(cutoff))


def _get_all_scores_and_equity(cutoff=None):
    if not os.path.exists(DB_PATH):
        return {"scores": [], "equity": []}
    try:
        conn = sqlite3.connect(DB_PATH)
        if cutoff:
            rows = conn.execute(
                "SELECT iteration, round(avg_score,4), action FROM experiments "
                "WHERE timestamp >= ? ORDER BY id",
                (cutoff,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT iteration, round(avg_score,4), action FROM experiments ORDER BY id"
            ).fetchall()
        conn.close()

        scores = [{"iter": r[0], "score": r[1] or 0, "action": r[2]} for r in rows]

        # Build equity curve from same data
        curve = []
        cumulative_r = 0.0
        prev_score = 0.0
        for r in rows:
            iteration = r[0]
            score = r[1] or 0.0
            action = r[2]
            if action == "keep":
                delta = score - prev_score if prev_score else score * 0.1
                cumulative_r += max(delta, 0.01)
            elif action == "revert":
                cumulative_r -= 0.02
            prev_score = score
            curve.append({"iter": iteration, "cumR": round(cumulative_r, 4)})

        return {"scores": scores, "equity": curve}
    except Exception:
        return {"scores": [], "equity": []}


def get_experiment_stats():
    return _cached("experiment_stats", _get_experiment_stats)


def _get_experiment_stats():
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN action='keep' THEN 1 ELSE 0 END) as kept,
                   SUM(CASE WHEN action='revert' THEN 1 ELSE 0 END) as reverted,
                   SUM(CASE WHEN action='error' THEN 1 ELSE 0 END) as errors,
                   SUM(CASE WHEN action='baseline' THEN 1 ELSE 0 END) as baselines
            FROM experiments
        """).fetchone()
        conn.close()
        return {
            "total": row[0] or 0,
            "kept": row[1] or 0,
            "reverted": row[2] or 0,
            "errors": row[3] or 0,
            "baselines": row[4] or 0,
        }
    except Exception:
        return {}


def get_trade_log():
    return _cached("trade_log", _get_trade_log)


def _get_trade_log():
    try:
        from db.db_manager import get_latest_trade_log
        result = get_latest_trade_log()
        if result:
            data = result["data"]
            if result.get("timestamp"):
                ts = datetime.strptime(result["timestamp"], "%Y-%m-%d %H:%M:%S")
                data["_age_seconds"] = int((datetime.now() - ts).total_seconds())
            return data
    except Exception:
        pass
    return None


def get_suggestion():
    return _cached("suggestion", _get_suggestion)


def _get_suggestion():
    try:
        from db.db_manager import get_latest_suggestion
        result = get_latest_suggestion()
        if result:
            if result.get("timestamp"):
                ts = datetime.strptime(result["timestamp"], "%Y-%m-%d %H:%M:%S")
                result["_age_seconds"] = int((datetime.now() - ts).total_seconds())
            return result
    except Exception:
        pass
    return None


def get_params():
    return _cached("params", _get_params)


def _get_params():
    if not os.path.exists(PARAMS_PATH):
        return {}
    try:
        with open(PARAMS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def get_last_log_lines(path, n=5):
    return _cached(f"log_{path}_{n}", lambda: _get_last_log_lines(path, n))


def _get_last_log_lines(path, n=5):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4000))
            lines = f.read().decode("utf-8", errors="replace").split("\n")
        return [l for l in lines if l.strip()][-n:]
    except Exception:
        return []


def get_runtime_files():
    return _cached("runtime_files", _get_runtime_files)


def _get_runtime_files():
    """Get latest instrument metrics from DB."""
    try:
        from db.db_manager import get_latest_instrument_metrics
        metrics = get_latest_instrument_metrics()
        result = {}
        for m in metrics:
            key = f"metrics_{m['instrument']}"
            result[key] = {
                "instrument": m["instrument"],
                "score": m["score"],
                "winrate": m["winrate"],
                "total_trades": m["total_trades"],
                "profit_factor": m["profit_factor"],
                "sharpe": m["sharpe"],
                "max_drawdown": m["max_drawdown"],
                "iteration": m["iteration"],
            }
        return result
    except Exception:
        return {}


def format_age(seconds):
    if seconds < 60:
        return f"{seconds}s ago"
    elif seconds < 3600:
        return f"{seconds // 60}m ago"
    elif seconds < 86400:
        return f"{seconds // 3600}h ago"
    else:
        return f"{seconds // 86400}d ago"


# --------------- holdout status ---------------

def get_holdout_status():
    return _cached("holdout_status", _get_holdout_status)


def _get_holdout_status():
    try:
        from db.db_manager import get_latest_holdout
        result = get_latest_holdout()
        if result:
            return {"status": "completed", "results": result["data"]}
    except Exception:
        pass
    return {"status": "reserved", "message": "\u0417\u0430\u0440\u0435\u0437\u0435\u0440\u0432\u0438\u0440\u043e\u0432\u0430\u043d (\u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 2 \u043c\u0435\u0441)"}


# --------------- consecutive reverts ---------------

def get_consecutive_reverts():
    return _cached("consecutive_reverts", _get_consecutive_reverts)


def _get_consecutive_reverts():
    if not os.path.exists(DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT action FROM experiments ORDER BY id DESC LIMIT 50"
        ).fetchall()
        conn.close()
        count = 0
        for r in rows:
            if r[0] == "revert":
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


# --------------- next iteration timing ---------------

def get_next_iteration_info():
    return _cached("next_iteration_info", _get_next_iteration_info)


def _get_next_iteration_info():
    if not os.path.exists(DB_PATH):
        return {"last_ts": None, "avg_duration": 720, "seconds_until": None}
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT timestamp FROM experiments ORDER BY id DESC LIMIT 10"
        ).fetchall()
        conn.close()
        if not rows:
            return {"last_ts": None, "avg_duration": 720, "seconds_until": None}

        last_ts_str = rows[0][0]
        try:
            last_dt = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                last_dt = datetime.fromisoformat(last_ts_str)
            except Exception:
                return {"last_ts": last_ts_str, "avg_duration": 720, "seconds_until": None}

        avg_duration = 720
        if len(rows) >= 2:
            try:
                timestamps = []
                for r in rows:
                    try:
                        timestamps.append(datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S"))
                    except Exception:
                        try:
                            timestamps.append(datetime.fromisoformat(r[0]))
                        except Exception:
                            pass
                if len(timestamps) >= 2:
                    deltas = []
                    for i in range(len(timestamps) - 1):
                        d = (timestamps[i] - timestamps[i + 1]).total_seconds()
                        if 60 < d < 7200:
                            deltas.append(d)
                    if deltas:
                        avg_duration = int(sum(deltas) / len(deltas))
            except Exception:
                pass

        elapsed = (datetime.now() - last_dt).total_seconds()
        seconds_until = max(0, int(avg_duration - elapsed))

        return {
            "last_ts": last_ts_str,
            "avg_duration": avg_duration,
            "seconds_until": seconds_until,
        }
    except Exception:
        return {"last_ts": None, "avg_duration": 720, "seconds_until": None}


# --------------- iteration speed & ETA ---------------

def get_iteration_speed():
    return _cached("iteration_speed", _get_iteration_speed)


def _get_iteration_speed():
    if not os.path.exists(DB_PATH):
        return {"iters_per_hour": 0, "eta_hours": None, "total": 0, "target": 100}
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT timestamp FROM experiments ORDER BY id DESC LIMIT 10"
        ).fetchall()
        total_row = conn.execute("SELECT COUNT(*) FROM experiments").fetchone()
        conn.close()

        total = total_row[0] if total_row else 0
        target = 100

        if len(rows) < 2:
            return {"iters_per_hour": 0, "eta_hours": None, "total": total, "target": target}

        timestamps = []
        for r in rows:
            try:
                timestamps.append(datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S"))
            except Exception:
                try:
                    timestamps.append(datetime.fromisoformat(r[0]))
                except Exception:
                    pass

        if len(timestamps) < 2:
            return {"iters_per_hour": 0, "eta_hours": None, "total": total, "target": target}

        # Time span between oldest and newest of last 10
        span_seconds = (timestamps[0] - timestamps[-1]).total_seconds()
        if span_seconds <= 0:
            return {"iters_per_hour": 0, "eta_hours": None, "total": total, "target": target}

        iters_per_hour = round((len(timestamps) - 1) / (span_seconds / 3600), 1)
        remaining = max(0, target - total)
        eta_hours = round(remaining / iters_per_hour, 1) if iters_per_hour > 0 else None

        return {
            "iters_per_hour": iters_per_hour,
            "eta_hours": eta_hours,
            "total": total,
            "target": target,
        }
    except Exception:
        return {"iters_per_hour": 0, "eta_hours": None, "total": 0, "target": 100}


# --------------- keep/revert ratio ---------------

def get_keep_revert_ratio():
    return _cached("keep_revert_ratio", _get_keep_revert_ratio)


def _get_keep_revert_ratio():
    if not os.path.exists(DB_PATH):
        return {"last10_keep_pct": 0, "last50_keep_pct": 0}
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT action FROM experiments ORDER BY id DESC LIMIT 50"
        ).fetchall()
        conn.close()

        actions = [r[0] for r in rows]
        last10 = actions[:10]
        last50 = actions[:50]

        def keep_pct(lst):
            relevant = [a for a in lst if a in ("keep", "revert")]
            if not relevant:
                return 0
            return round(sum(1 for a in relevant if a == "keep") / len(relevant) * 100, 1)

        return {
            "last10_keep_pct": keep_pct(last10),
            "last50_keep_pct": keep_pct(last50),
        }
    except Exception:
        return {"last10_keep_pct": 0, "last50_keep_pct": 0}


# --------------- last change detail ---------------

def get_last_change_detail():
    return _cached("last_change_detail", _get_last_change_detail)


def _get_last_change_detail():
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT iteration, param_changed, old_value, new_value, action, notes, "
            "round(avg_score,4) as avg_score, timestamp "
            "FROM experiments ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return None
        notes = row[5] or ""
        if len(notes) > 150:
            notes = notes[:147] + "..."
        return {
            "iteration": row[0],
            "param": row[1] or "baseline",
            "old_value": row[2],
            "new_value": row[3],
            "action": row[4],
            "notes": notes,
            "score": row[6] or 0,
            "timestamp": row[7],
        }
    except Exception:
        return None


# --------------- MFE/MAE from exit breakdown ---------------

def get_mfe_mae_data():
    """Extract MFE/MAE-like data from exit_reason_breakdown avg_pnl."""
    return _cached("mfe_mae_data", _get_mfe_mae_data)


def _get_mfe_mae_data():
    trade_log = get_trade_log()
    if not trade_log:
        return None
    exit_bd = trade_log.get("exit_reason_breakdown", {})
    if not exit_bd:
        return None

    tp_data = exit_bd.get("tp", {})
    be_data = exit_bd.get("be", {})
    sl_data = exit_bd.get("sl", {})

    return {
        "tp_avg_pnl": round(_sf(tp_data.get("avg_pnl", 0)), 3),
        "tp_count": tp_data.get("count", 0),
        "be_avg_pnl": round(_sf(be_data.get("avg_pnl", 0)), 3),
        "be_count": be_data.get("count", 0),
        "sl_avg_pnl": round(_sf(sl_data.get("avg_pnl", 0)), 3),
        "sl_count": sl_data.get("count", 0),
        "has_data": True,
    }


# --------------- night/day mode ---------------

def get_day_night_mode():
    """Kyiv = UTC+2. Night = 00:00-08:00 Kyiv time."""
    kyiv_tz = timezone(timedelta(hours=2))
    kyiv_now = datetime.now(kyiv_tz)
    hour = kyiv_now.hour
    is_night = 0 <= hour < 8
    return {
        "is_night": is_night,
        "kyiv_time": kyiv_now.strftime("%H:%M"),
        "label": "Night / Opus" if is_night else "Day / Sonnet",
    }


# --------------- impulse agent progress ---------------

def get_impulse_progress():
    return _cached("impulse_progress", _get_impulse_progress)


def _get_impulse_progress():
    result = {
        "coins_analyzed": 0,
        "patterns_found": 0,
        "last_alert": None,
        "status": "no_data",
    }

    if os.path.exists(IMPULSE_DB_PATH):
        try:
            conn = sqlite3.connect(IMPULSE_DB_PATH)
            try:
                row = conn.execute("SELECT COUNT(DISTINCT symbol) FROM impulse_events").fetchone()
                result["coins_analyzed"] = row[0] if row else 0
            except Exception:
                pass
            try:
                row = conn.execute("SELECT COUNT(*) FROM impulse_events").fetchone()
                result["patterns_found"] = row[0] if row else 0
            except Exception:
                pass
            try:
                row = conn.execute(
                    "SELECT symbol, timestamp FROM impulse_events ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                if row:
                    result["last_alert"] = f"{row[0]} @ {row[1]}"
            except Exception:
                pass
            conn.close()
            result["status"] = "active"
        except Exception:
            pass

    if os.path.exists(RUNTIME_DIR):
        for f in os.listdir(RUNTIME_DIR):
            if "impulse" in f.lower():
                result["status"] = "active"
                try:
                    path = os.path.join(RUNTIME_DIR, f)
                    with open(path) as fh:
                        data = json.load(fh)
                    if isinstance(data, dict):
                        if "coins_analyzed" in data:
                            result["coins_analyzed"] = max(result["coins_analyzed"], data["coins_analyzed"])
                        if "patterns_found" in data:
                            result["patterns_found"] = max(result["patterns_found"], data["patterns_found"])
                        if "last_alert" in data and data["last_alert"]:
                            result["last_alert"] = data["last_alert"]
                except Exception:
                    pass

    return result


# --------------- build JSON payload ---------------

def build_api_data(time_range="all"):
    cutoff = get_cutoff_timestamp(time_range)
    sessions = get_tmux_sessions()
    experiments = get_experiments(15, cutoff=cutoff)
    exp_stats = get_experiment_stats()
    trade_log = get_trade_log()
    suggestion = get_suggestion()
    params = get_params()
    procs = get_process_info()
    runtime_files = get_runtime_files()
    orch_lines = get_last_log_lines(ORCH_LOG, 5)

    # Single query for scores + equity
    scores_equity = get_all_scores_and_equity(cutoff=cutoff)
    all_scores = scores_equity["scores"]
    equity_curve = scores_equity["equity"]

    holdout = get_holdout_status()
    consecutive_reverts = get_consecutive_reverts()
    next_iter = get_next_iteration_info()
    impulse_progress = get_impulse_progress()

    # NEW v5
    iteration_speed = get_iteration_speed()
    keep_revert_ratio = get_keep_revert_ratio()
    last_change = get_last_change_detail()
    mfe_mae = get_mfe_mae_data()
    day_night = get_day_night_mode()

    agents = []
    for name, label in [
        ("orchestrator", "Orchestrator"),
        ("backtest", "BacktestAgent"),
        ("monitor", "MonitorAgent"),
        ("impulse", "ImpulseAgent"),
        ("dashboard", "Dashboard"),
    ]:
        alive = name in sessions or name == "dashboard"
        proc = procs.get(name, {})
        last_line = ""
        if alive and name != "dashboard":
            activity = get_agent_activity(name, 3)
            last_line = activity[-1] if activity else "idle"
            if len(last_line) > 80:
                last_line = last_line[:77] + "..."
        agents.append({
            "id": name,
            "label": label,
            "alive": alive,
            "cpu": proc.get("cpu", ""),
            "mem": proc.get("mem", ""),
            "last_line": last_line,
        })

    workers = procs.get("workers", {})

    total = exp_stats.get("total", 0)
    kept = exp_stats.get("kept", 0)
    reverted = exp_stats.get("reverted", 0)
    errors = exp_stats.get("errors", 0)

    current_score = 0
    best_score = 0
    current_wr = 0
    total_trades = 0
    best_instrument = "N/A"

    if experiments:
        last_exp = experiments[0]
        current_score = last_exp.get("avg_score") or 0
        current_wr = last_exp.get("avg_winrate") or 0
        total_trades = last_exp.get("total_trades") or 0
        best_instrument = last_exp.get("best_instrument") or "N/A"
        if all_scores:
            best_score = max(s["score"] for s in all_scores)

    # Trade log derived data
    instruments_list = []
    sessions_list = []
    exits_list = []
    tl_wr = 0
    tl_total = 0

    # Build instrument score map from runtime_files
    inst_score_map = {}
    for key, val in runtime_files.items():
        if key.startswith("metrics_") and isinstance(val, dict):
            inst_name = val.get("instrument", key.replace("metrics_", ""))
            inst_score_map[inst_name] = val.get("score", None)

    if trade_log:
        tl_wr = trade_log.get("overall_winrate", 0)
        tl_total = trade_log.get("total_trades", 0)

        for inst, data in sorted(
            trade_log.get("win_by_instrument", {}).items(),
            key=lambda x: -x[1]["winrate"],
        ):
            instruments_list.append({
                "name": inst,
                "winrate": data["winrate"],
                "total_r": data["total_r"],
                "trades": data["total_trades"],
                "score": inst_score_map.get(inst),
            })

        for sess, data in sorted(
            trade_log.get("win_by_session", {}).items(),
            key=lambda x: -x[1]["winrate"],
        ):
            sessions_list.append({
                "name": sess,
                "winrate": data["winrate"],
                "trades": data["total_trades"],
            })

        exit_bd = trade_log.get("exit_reason_breakdown", {})
        for reason in ["tp", "be", "sl", "time_exit"]:
            if reason in exit_bd:
                d = exit_bd[reason]
                exits_list.append({
                    "reason": reason,
                    "count": d["count"],
                    "pct": d["count"] / tl_total * 100 if tl_total else 0,
                })

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stats": {
            "score": round(_sf(current_score), 4),
            "best_score": round(_sf(best_score), 4),
            "winrate": round(_sf(current_wr), 4),
            "total_trades": total_trades,
            "best_instrument": best_instrument,
            "total_experiments": total,
            "kept": kept,
            "reverted": reverted,
            "errors": errors,
        },
        "agents": agents,
        "workers": {
            "count": workers.get("count", 0),
            "total_cpu": workers.get("total_cpu", 0),
        },
        "score_history": all_scores,
        "experiments": experiments[:10],
        "instruments": instruments_list,
        "sessions": sessions_list,
        "exits": exits_list,
        "trade_log_wr": round(_sf(tl_wr), 4),
        "trade_log_trades": tl_total,
        "suggestion": suggestion,
        "params": params,
        "orch_log": orch_lines,
        "runtime_files": runtime_files,
        # v4 fields
        "holdout": holdout,
        "equity_curve": equity_curve,
        "consecutive_reverts": consecutive_reverts,
        "next_iteration": next_iter,
        "impulse_progress": impulse_progress,
        # v5 fields
        "iteration_speed": iteration_speed,
        "keep_revert_ratio": keep_revert_ratio,
        "last_change": last_change,
        "mfe_mae": mfe_mae,
        "day_night": day_night,
    }


# --------------- HTML page ---------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading System Dashboard</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--card:#161b22;--border:#30363d;
  --accent:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#d29922;--orange:#db6d28;
  --text:#c9d1d9;--text-dim:#8b949e;--text-bright:#f0f6fc;
  --radius:12px;--card-shadow:0 1px 3px rgba(0,0,0,.4);
}
html{font-size:14px}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;min-height:100vh}
a{color:var(--accent);text-decoration:none}

.shell{max-width:1360px;margin:0 auto;padding:16px}

/* ---- header ---- */
.header{display:flex;align-items:center;justify-content:space-between;padding:12px 0 20px;border-bottom:1px solid var(--border);margin-bottom:20px;flex-wrap:wrap;gap:8px}
.header h1{font-size:1.35rem;font-weight:600;color:var(--text-bright);display:flex;align-items:center;gap:10px}
.header h1 .dot{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block;animation:pulse 2s infinite}
.header-right{display:flex;align-items:center;gap:16px;color:var(--text-dim);font-size:.85rem}
.mode-badge{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:8px;font-size:.8rem;font-weight:600}
.mode-badge.night{background:rgba(88,166,255,.12);color:var(--accent);border:1px solid rgba(88,166,255,.25)}
.mode-badge.day{background:rgba(210,153,34,.12);color:var(--yellow);border:1px solid rgba(210,153,34,.25)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}

/* ---- stat cards ---- */
.stat-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px;box-shadow:var(--card-shadow);transition:border-color .2s}
.stat-card:hover{border-color:var(--accent)}
.stat-card .label{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text-dim);margin-bottom:6px}
.stat-card .value{font-size:1.65rem;font-weight:700;color:var(--text-bright)}
.stat-card .sub{font-size:.78rem;color:var(--text-dim);margin-top:4px}
.stat-card .value.green{color:var(--green)}.stat-card .value.red{color:var(--red)}.stat-card .value.accent{color:var(--accent)}.stat-card .value.yellow{color:var(--yellow)}

/* ---- last change detail card ---- */
.last-change-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px 20px;box-shadow:var(--card-shadow);margin-bottom:20px}
.last-change-header{display:flex;align-items:center;gap:12px;margin-bottom:8px;flex-wrap:wrap}
.last-change-param{font-size:1.05rem;font-weight:700;color:var(--accent)}
.last-change-change{font-size:.9rem;color:var(--text-bright)}
.last-change-notes{font-size:.82rem;color:var(--text-dim);margin-top:6px;line-height:1.5;font-style:italic}

/* ---- mini stat row ---- */
.mini-stat-row{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:20px}
.mini-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px 18px;box-shadow:var(--card-shadow);display:flex;align-items:center;gap:14px}
.mini-card .mini-icon{font-size:1.8rem;line-height:1}
.mini-card .mini-content{flex:1}
.mini-card .mini-label{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text-dim);margin-bottom:3px}
.mini-card .mini-value{font-size:1.1rem;font-weight:700;color:var(--text-bright)}
.mini-card .mini-sub{font-size:.75rem;color:var(--text-dim);margin-top:2px}

/* ---- grid layout ---- */
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.grid-3{display:grid;grid-template-columns:2fr 1fr 1fr;gap:14px;margin-bottom:14px}
.grid-2-1{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:14px}
.full-w{margin-bottom:14px}

/* ---- panel (card) ---- */
.panel{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--card-shadow);overflow:hidden}
.panel-head{padding:14px 18px 10px;font-size:.82rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-dim);border-bottom:1px solid var(--border)}
.panel-body{padding:14px 18px 18px}

/* ---- agents ---- */
.agent-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid rgba(48,54,61,.5)}
.agent-row:last-child{border-bottom:none}
.agent-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.agent-dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}
.agent-dot.off{background:var(--red);box-shadow:0 0 6px var(--red)}
.agent-label{font-weight:600;color:var(--text-bright);min-width:110px}
.agent-meta{color:var(--text-dim);font-size:.8rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---- SVG chart ---- */
.chart-wrap{width:100%;overflow-x:auto}
.chart-wrap svg{display:block;width:100%;height:200px}

/* ---- tables ---- */
.tbl{width:100%;border-collapse:collapse;font-size:.85rem}
.tbl th{text-align:left;color:var(--text-dim);font-weight:500;padding:8px 10px;border-bottom:1px solid var(--border);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
.tbl td{padding:8px 10px;border-bottom:1px solid rgba(48,54,61,.4);vertical-align:middle}
.tbl tbody tr{transition:background .15s}
.tbl tbody tr:hover{background:rgba(88,166,255,.06)}

/* ---- WR bar ---- */
.wr-bar-bg{height:7px;background:var(--border);border-radius:4px;overflow:hidden;min-width:80px}
.wr-bar-fill{height:100%;border-radius:4px;transition:width .6s ease}
.wr-bar-fill.g{background:var(--green)}.wr-bar-fill.y{background:var(--yellow)}.wr-bar-fill.r{background:var(--red)}

/* ---- badges ---- */
.badge{display:inline-block;font-size:.72rem;font-weight:600;padding:2px 8px;border-radius:10px;text-transform:uppercase;letter-spacing:.03em}
.badge.keep{background:rgba(63,185,80,.15);color:var(--green);border:1px solid rgba(63,185,80,.3)}
.badge.revert{background:rgba(248,81,73,.12);color:var(--red);border:1px solid rgba(248,81,73,.25)}
.badge.error{background:rgba(210,153,34,.12);color:var(--yellow);border:1px solid rgba(210,153,34,.25)}
.badge.baseline{background:rgba(88,166,255,.12);color:var(--accent);border:1px solid rgba(88,166,255,.25)}

/* ---- exit bars (horizontal stacked) ---- */
.exit-bar-wrap{display:flex;height:28px;border-radius:6px;overflow:hidden;margin-bottom:10px}
.exit-seg{display:flex;align-items:center;justify-content:center;font-size:.72rem;font-weight:600;color:#fff;transition:width .5s ease;min-width:24px}
.exit-seg.tp{background:var(--green)}.exit-seg.sl{background:var(--red)}.exit-seg.be{background:var(--yellow)}.exit-seg.time_exit{background:var(--orange)}
.exit-legend{display:flex;gap:16px;flex-wrap:wrap;font-size:.8rem;color:var(--text-dim)}
.exit-legend span::before{content:'';display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:middle}
.exit-legend .l-tp::before{background:var(--green)}.exit-legend .l-sl::before{background:var(--red)}
.exit-legend .l-be::before{background:var(--yellow)}.exit-legend .l-time::before{background:var(--orange)}

/* ---- params grid ---- */
.params-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:6px}
.param-item{display:flex;justify-content:space-between;padding:5px 8px;border-radius:6px;background:rgba(88,166,255,.05);font-size:.82rem}
.param-item .pk{color:var(--text-dim)}.param-item .pv{color:var(--accent);font-weight:600}

/* ---- log lines ---- */
.log-line{font-family:'SF Mono',Menlo,Consolas,monospace;font-size:.78rem;padding:2px 0;color:var(--text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.log-line.err{color:var(--red)}.log-line.keep{color:var(--green)}.log-line.rev{color:var(--red)}

/* ---- MFE/MAE cards ---- */
.mfe-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.mfe-item{text-align:center;padding:12px 8px;border-radius:8px;background:rgba(88,166,255,.04)}
.mfe-item .mfe-val{font-size:1.3rem;font-weight:700}
.mfe-item .mfe-label{font-size:.72rem;text-transform:uppercase;color:var(--text-dim);margin-top:4px}
.mfe-item .mfe-sub{font-size:.72rem;color:var(--text-dim);margin-top:2px}

/* ---- responsive ---- */
@media(max-width:900px){.stat-row{grid-template-columns:repeat(2,1fr)}.grid-2,.grid-3,.grid-2-1,.mini-stat-row{grid-template-columns:1fr}}
@media(max-width:500px){.stat-row{grid-template-columns:1fr}.header{flex-direction:column;align-items:flex-start}}

/* ---- time range filter bar ---- */
.filter-bar{display:flex;gap:6px;margin-bottom:20px;flex-wrap:wrap}
.filter-btn{
  background:#21262d;border:1px solid #30363d;color:#c9d1d9;
  padding:6px 16px;border-radius:8px;font-size:.82rem;font-weight:500;
  cursor:pointer;transition:all .2s;outline:none;font-family:inherit;
}
.filter-btn:hover{border-color:#58a6ff;color:#f0f6fc}
.filter-btn.active{background:#58a6ff;color:#0d1117;border-color:#58a6ff;font-weight:600}

/* fade-in for data refresh */
.fade-update{animation:fadeIn .35s ease}
@keyframes fadeIn{from{opacity:.5}to{opacity:1}}
</style>
</head>
<body>
<div class="shell">

<!-- header -->
<div class="header">
  <h1><span class="dot"></span> Trading System <span id="mode-badge" class="mode-badge day"></span></h1>
  <div class="header-right">
    <span id="hdr-time">--</span>
    <span id="hdr-iter">-- experiments</span>
  </div>
</div>

<!-- time range filter -->
<div class="filter-bar" id="filter-bar">
  <button class="filter-btn" data-range="6h">6&#x447;</button>
  <button class="filter-btn" data-range="12h">12&#x447;</button>
  <button class="filter-btn" data-range="24h">24&#x447;</button>
  <button class="filter-btn" data-range="3d">3&#x434;</button>
  <button class="filter-btn" data-range="7d">&#x41D;&#x435;&#x434;&#x435;&#x43B;&#x44F;</button>
  <button class="filter-btn active" data-range="all">&#x412;&#x441;&#x435;</button>
</div>

<!-- stat cards -->
<div class="stat-row" id="stat-cards">
  <div class="stat-card"><div class="label">Current Score</div><div class="value accent" id="sc-score">--</div><div class="sub" id="sc-score-sub">best: --</div></div>
  <div class="stat-card"><div class="label">Win Rate</div><div class="value green" id="sc-wr">--</div><div class="sub" id="sc-wr-sub">-- trades</div></div>
  <div class="stat-card"><div class="label">Experiments</div><div class="value" id="sc-exp">--</div><div class="sub" id="sc-exp-sub">--</div></div>
  <div class="stat-card"><div class="label">Best Instrument</div><div class="value accent" id="sc-best">--</div><div class="sub" id="sc-best-sub">&nbsp;</div></div>
</div>

<!-- last change detail -->
<div class="last-change-card" id="last-change-card" style="display:none">
  <div class="last-change-header">
    <span style="font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text-dim)">Last Change</span>
    <span class="badge" id="lc-badge">--</span>
    <span class="last-change-param" id="lc-param">--</span>
    <span class="last-change-change" id="lc-change">--</span>
    <span style="font-size:.78rem;color:var(--text-dim)" id="lc-score">--</span>
  </div>
  <div class="last-change-notes" id="lc-notes"></div>
</div>

<!-- mini stat row: reverts + next iter + holdout + speed + keep ratio -->
<div class="mini-stat-row">
  <div class="mini-card" id="reverts-card">
    <div class="mini-icon" id="reverts-icon">&#x21BA;</div>
    <div class="mini-content">
      <div class="mini-label">Consecutive Reverts</div>
      <div class="mini-value" id="reverts-value">--</div>
      <div class="mini-sub" id="reverts-sub">&nbsp;</div>
    </div>
  </div>
  <div class="mini-card" id="next-iter-card">
    <div class="mini-icon">&#x23F1;</div>
    <div class="mini-content">
      <div class="mini-label">Next Iteration</div>
      <div class="mini-value" id="next-iter-value">--</div>
      <div class="mini-sub" id="next-iter-sub">&nbsp;</div>
    </div>
  </div>
  <div class="mini-card" id="speed-card">
    <div class="mini-icon">&#x26A1;</div>
    <div class="mini-content">
      <div class="mini-label">Iteration Speed</div>
      <div class="mini-value" id="speed-value">--</div>
      <div class="mini-sub" id="speed-sub">&nbsp;</div>
    </div>
  </div>
  <div class="mini-card" id="keep-ratio-card">
    <div class="mini-icon">&#x2705;</div>
    <div class="mini-content">
      <div class="mini-label">Keep Ratio</div>
      <div class="mini-value" id="keep-ratio-value">--</div>
      <div class="mini-sub" id="keep-ratio-sub">&nbsp;</div>
    </div>
  </div>
  <div class="mini-card" id="holdout-card">
    <div class="mini-icon">&#x1F9EA;</div>
    <div class="mini-content">
      <div class="mini-label">Out-of-Sample Test</div>
      <div class="mini-value" id="holdout-value">--</div>
      <div class="mini-sub" id="holdout-sub">&nbsp;</div>
    </div>
  </div>
</div>

<!-- chart + equity curve -->
<div class="grid-2">
  <div class="panel"><div class="panel-head">Score History</div><div class="panel-body"><div class="chart-wrap" id="chart-area"></div></div></div>
  <div class="panel"><div class="panel-head">Equity Curve (Cumulative R)</div><div class="panel-body"><div class="chart-wrap" id="equity-chart-area"></div></div></div>
</div>

<!-- agents + impulse progress -->
<div class="grid-2">
  <div class="panel"><div class="panel-head">Agents</div><div class="panel-body" id="agents-area"></div></div>
  <div class="panel"><div class="panel-head">ImpulseAgent Progress</div><div class="panel-body" id="impulse-area"></div></div>
</div>

<!-- instruments + sessions + exits -->
<div class="grid-3">
  <div class="panel"><div class="panel-head">Instruments</div><div class="panel-body" id="instruments-area"></div></div>
  <div class="panel"><div class="panel-head">Sessions</div><div class="panel-body" id="sessions-area"></div></div>
  <div class="panel">
    <div class="panel-head">Exit Breakdown</div>
    <div class="panel-body">
      <div id="exits-area"></div>
      <div id="mfe-area" style="margin-top:14px"></div>
    </div>
  </div>
</div>

<!-- experiments table -->
<div class="full-w">
  <div class="panel"><div class="panel-head">Recent Experiments</div><div class="panel-body" id="exp-area"></div></div>
</div>

<!-- params + log -->
<div class="grid-2">
  <div class="panel"><div class="panel-head">Current Parameters</div><div class="panel-body" id="params-area"></div></div>
  <div class="panel"><div class="panel-head">Orchestrator Log</div><div class="panel-body" id="log-area"></div></div>
</div>

</div><!-- /shell -->

<script>
(function(){
"use strict";

const $ = s => document.getElementById(s);
let prev = null;
let nextIterCountdown = null;
let currentTimeRange = 'all';

// Time range filter buttons
document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', function(){
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    currentTimeRange = this.getAttribute('data-range');
    fetchData();
  });
});

function esc(s){if(s==null)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function pct(v){return (v*100).toFixed(1)+'%';}
function wrClass(v){return v>=0.5?'g':v>=0.35?'y':'r';}

function renderChart(data){
  const pts = data.score_history || [];
  if(!pts.length) return '<div style="color:var(--text-dim);text-align:center;padding:60px 0">No data</div>';
  const W=600,H=180,PAD=40,PADR=20,PADT=10,PADB=30;
  const n=pts.length;
  const scores=pts.map(p=>p.score);
  let mn=Math.min(...scores), mx=Math.max(...scores);
  if(mn===mx){mn-=1;mx+=1;}
  const rng=mx-mn;
  const xStep=(W-PAD-PADR)/Math.max(n-1,1);

  function sx(i){return PAD+i*xStep;}
  function sy(v){return PADT+(1-(v-mn)/rng)*(H-PADT-PADB);}

  let path='M';
  let areaPath='M';
  const dots=[];
  pts.forEach((p,i)=>{
    const x=sx(i),y=sy(p.score);
    path+=(i?'L':'')+x.toFixed(1)+','+y.toFixed(1);
    if(i===0) areaPath+=x.toFixed(1)+','+(H-PADB);
    areaPath+=' L'+x.toFixed(1)+','+y.toFixed(1);
    const clr=p.action==='keep'?'var(--green)':p.action==='revert'?'var(--red)':'var(--accent)';
    dots.push('<circle cx="'+x.toFixed(1)+'" cy="'+y.toFixed(1)+'" r="3" fill="'+clr+'" opacity=".85"><title>#'+p.iter+' score='+p.score+'</title></circle>');
  });
  areaPath+=' L'+sx(n-1).toFixed(1)+','+(H-PADB)+' Z';

  let yLabels='';
  for(let i=0;i<=4;i++){
    const v=mn+rng*(i/4);
    const y=sy(v);
    yLabels+='<text x="'+(PAD-6)+'" y="'+(y+4)+'" text-anchor="end" fill="var(--text-dim)" font-size="10">'+v.toFixed(2)+'</text>';
    yLabels+='<line x1="'+PAD+'" x2="'+(W-PADR)+'" y1="'+y+'" y2="'+y+'" stroke="var(--border)" stroke-dasharray="3,3"/>';
  }

  let xLabels='';
  const step=Math.max(1,Math.floor(n/6));
  for(let i=0;i<n;i+=step){
    xLabels+='<text x="'+sx(i)+'" y="'+(H-PADB+16)+'" text-anchor="middle" fill="var(--text-dim)" font-size="10">#'+pts[i].iter+'</text>';
  }
  if(n>1){
    xLabels+='<text x="'+sx(n-1)+'" y="'+(H-PADB+16)+'" text-anchor="middle" fill="var(--text-dim)" font-size="10">#'+pts[n-1].iter+'</text>';
  }

  return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
    yLabels+xLabels+
    '<path d="'+areaPath+'" fill="url(#areaGrad)" opacity=".25"/>'+
    '<path d="'+path+'" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>'+
    dots.join('')+
    '<defs><linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="var(--accent)"/><stop offset="100%" stop-color="transparent"/></linearGradient></defs>'+
  '</svg>';
}

function renderEquityCurve(data){
  const pts = data.equity_curve || [];
  if(!pts.length) return '<div style="color:var(--text-dim);text-align:center;padding:60px 0">No data</div>';
  const W=600,H=180,PAD=40,PADR=20,PADT=10,PADB=30;
  const n=pts.length;
  const vals=pts.map(p=>p.cumR);
  let mn=Math.min(...vals,0), mx=Math.max(...vals);
  if(mn===mx){mn-=1;mx+=1;}
  const rng=mx-mn;
  const xStep=(W-PAD-PADR)/Math.max(n-1,1);

  function sx(i){return PAD+i*xStep;}
  function sy(v){return PADT+(1-(v-mn)/rng)*(H-PADT-PADB);}

  const zeroY=sy(0);

  let linePath='M';
  pts.forEach((p,i)=>{
    const x=sx(i),y=sy(p.cumR);
    linePath+=(i?'L':'')+x.toFixed(1)+','+y.toFixed(1);
  });

  let areaPath='M'+sx(0).toFixed(1)+','+zeroY.toFixed(1);
  pts.forEach((p,i)=>{
    areaPath+=' L'+sx(i).toFixed(1)+','+sy(p.cumR).toFixed(1);
  });
  areaPath+=' L'+sx(n-1).toFixed(1)+','+zeroY.toFixed(1)+' Z';

  let yLabels='';
  for(let i=0;i<=4;i++){
    const v=mn+rng*(i/4);
    const y=sy(v);
    yLabels+='<text x="'+(PAD-6)+'" y="'+(y+4)+'" text-anchor="end" fill="var(--text-dim)" font-size="10">'+v.toFixed(2)+'R</text>';
    yLabels+='<line x1="'+PAD+'" x2="'+(W-PADR)+'" y1="'+y+'" y2="'+y+'" stroke="var(--border)" stroke-dasharray="3,3"/>';
  }

  let zeroLine='';
  if(mn<0 && mx>0){
    zeroLine='<line x1="'+PAD+'" x2="'+(W-PADR)+'" y1="'+zeroY.toFixed(1)+'" y2="'+zeroY.toFixed(1)+'" stroke="var(--text-dim)" stroke-width="1" stroke-dasharray="4,2" opacity=".6"/>';
  }

  let xLabels='';
  const step=Math.max(1,Math.floor(n/6));
  for(let i=0;i<n;i+=step){
    xLabels+='<text x="'+sx(i)+'" y="'+(H-PADB+16)+'" text-anchor="middle" fill="var(--text-dim)" font-size="10">#'+pts[i].iter+'</text>';
  }
  if(n>1){
    xLabels+='<text x="'+sx(n-1)+'" y="'+(H-PADB+16)+'" text-anchor="middle" fill="var(--text-dim)" font-size="10">#'+pts[n-1].iter+'</text>';
  }

  const lastR=pts[n-1].cumR;
  const rColor=lastR>=0?'var(--green)':'var(--red)';
  const rLabel='<text x="'+(W-PADR+2)+'" y="'+(sy(lastR)+4)+'" fill="'+rColor+'" font-size="11" font-weight="700">'+(lastR>=0?'+':'')+lastR.toFixed(2)+'R</text>';

  return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
    '<defs>'+
      '<clipPath id="clipPos"><rect x="0" y="0" width="'+W+'" height="'+zeroY.toFixed(1)+'"/></clipPath>'+
      '<clipPath id="clipNeg"><rect x="0" y="'+zeroY.toFixed(1)+'" width="'+W+'" height="'+(H-zeroY)+'"/></clipPath>'+
    '</defs>'+
    yLabels+xLabels+zeroLine+
    '<path d="'+areaPath+'" fill="var(--green)" opacity=".18" clip-path="url(#clipPos)"/>'+
    '<path d="'+areaPath+'" fill="var(--red)" opacity=".18" clip-path="url(#clipNeg)"/>'+
    '<path d="'+linePath+'" fill="none" stroke="'+rColor+'" stroke-width="2" stroke-linejoin="round"/>'+
    rLabel+
  '</svg>';
}

function renderAgents(data){
  const agents=data.agents||[];
  let h='';
  agents.forEach(a=>{
    const cls=a.alive?'on':'off';
    const meta=a.alive?(a.cpu?'CPU '+a.cpu+'% MEM '+a.mem+'%':'running'):'not running';
    const extra=a.last_line?' &mdash; <span style="opacity:.65">'+esc(a.last_line)+'</span>':'';
    h+='<div class="agent-row"><span class="agent-dot '+cls+'"></span><span class="agent-label">'+esc(a.label)+'</span><span class="agent-meta">'+meta+extra+'</span></div>';
  });
  const w=data.workers||{};
  if(w.count>0){
    h+='<div class="agent-row"><span class="agent-dot on"></span><span class="agent-label">Workers</span><span class="agent-meta">'+w.count+' processes, CPU '+w.total_cpu.toFixed(0)+'%</span></div>';
  }
  return h;
}

function renderImpulse(data){
  const imp=data.impulse_progress||{};
  if(imp.status==='no_data'){
    return '<div style="display:flex;align-items:center;gap:12px;padding:20px 0">'+
      '<div style="font-size:1.8rem">&#x1F50D;</div>'+
      '<div>'+
        '<div style="color:var(--text-bright);font-weight:600;margin-bottom:4px">ImpulseAgent: \u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0435\u0442...</div>'+
        '<div style="color:var(--text-dim);font-size:.82rem">\u041e\u0436\u0438\u0434\u0430\u043d\u0438\u0435 \u0434\u0430\u043d\u043d\u044b\u0445 \u0430\u043d\u0430\u043b\u0438\u0437\u0430</div>'+
      '</div>'+
    '</div>';
  }
  const coins=imp.coins_analyzed||0;
  const patterns=imp.patterns_found||0;
  const alert=imp.last_alert||'\u043d\u0435\u0442';
  return '<div style="padding:10px 0">'+
    '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:14px">'+
      '<div style="text-align:center">'+
        '<div style="font-size:1.6rem;font-weight:700;color:var(--accent)">'+coins+'</div>'+
        '<div style="font-size:.75rem;color:var(--text-dim);text-transform:uppercase">\u041c\u043e\u043d\u0435\u0442</div>'+
      '</div>'+
      '<div style="text-align:center">'+
        '<div style="font-size:1.6rem;font-weight:700;color:'+(patterns>0?'var(--green)':'var(--text-dim)')+'">'+patterns+'</div>'+
        '<div style="font-size:.75rem;color:var(--text-dim);text-transform:uppercase">\u041f\u0430\u0442\u0442\u0435\u0440\u043d\u043e\u0432</div>'+
      '</div>'+
      '<div style="text-align:center">'+
        '<div style="font-size:.9rem;font-weight:600;color:var(--text-bright);margin-top:6px">'+esc(alert)+'</div>'+
        '<div style="font-size:.75rem;color:var(--text-dim);text-transform:uppercase">\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 \u0430\u043b\u0435\u0440\u0442</div>'+
      '</div>'+
    '</div>'+
  '</div>';
}

function renderInstruments(data){
  const list=data.instruments||[];
  if(!list.length) return '<div style="color:var(--text-dim)">No data</div>';
  let h='<table class="tbl"><thead><tr><th>Instrument</th><th>WR</th><th style="min-width:90px"></th><th>R</th><th>Score</th><th>Trades</th></tr></thead><tbody>';
  list.forEach(i=>{
    const w=i.winrate;
    const rc=i.total_r>=0?'color:var(--green)':'color:var(--red)';
    let scoreHtml='<span style="color:var(--text-dim)">--</span>';
    if(i.score!=null && i.score!==-999){
      const sc=i.score;
      const scColor=sc>0?'var(--green)':sc<0?'var(--red)':'var(--text-dim)';
      scoreHtml='<span style="color:'+scColor+';font-weight:600">'+(sc>0?'+':'')+sc.toFixed(2)+'</span>';
    }
    h+='<tr><td style="font-weight:600">'+esc(i.name)+'</td><td>'+pct(w)+'</td><td><div class="wr-bar-bg"><div class="wr-bar-fill '+wrClass(w)+'" style="width:'+(w*100).toFixed(0)+'%"></div></div></td><td style="'+rc+';font-weight:600">'+(i.total_r>=0?'+':'')+i.total_r.toFixed(0)+'R</td><td>'+scoreHtml+'</td><td style="color:var(--text-dim)">'+i.trades+'</td></tr>';
  });
  h+='</tbody></table>';
  return h;
}

function renderSessions(data){
  const list=data.sessions||[];
  if(!list.length) return '<div style="color:var(--text-dim)">No data</div>';
  let h='<table class="tbl"><thead><tr><th>Session</th><th>WR</th><th></th><th>Trades</th></tr></thead><tbody>';
  list.forEach(s=>{
    const w=s.winrate;
    h+='<tr><td style="font-weight:600">'+esc(s.name)+'</td><td>'+pct(w)+'</td><td><div class="wr-bar-bg"><div class="wr-bar-fill '+wrClass(w)+'" style="width:'+(w*100).toFixed(0)+'%"></div></div></td><td style="color:var(--text-dim)">'+s.trades+'</td></tr>';
  });
  h+='</tbody></table>';
  return h;
}

function renderExits(data){
  const list=data.exits||[];
  if(!list.length) return '<div style="color:var(--text-dim)">No data</div>';
  const total=list.reduce((a,e)=>a+e.count,0);
  let barH='<div class="exit-bar-wrap">';
  list.forEach(e=>{
    const w=(e.count/total*100).toFixed(1);
    barH+='<div class="exit-seg '+e.reason+'" style="width:'+w+'%">'+(w>6?e.reason.toUpperCase()+' '+e.count:e.count)+'</div>';
  });
  barH+='</div>';
  const labels={'tp':'Take Profit','sl':'Stop Loss','be':'Break Even','time_exit':'Time Exit'};
  barH+='<div class="exit-legend">';
  list.forEach(e=>{
    const cls='l-'+(e.reason==='time_exit'?'time':e.reason);
    barH+='<span class="'+cls+'">'+(labels[e.reason]||e.reason)+': '+e.count+' ('+e.pct.toFixed(1)+'%)</span>';
  });
  barH+='</div>';
  return barH;
}

function renderMfeMae(data){
  const m=data.mfe_mae;
  if(!m||!m.has_data) return '';
  return '<div style="font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text-dim);margin-bottom:8px">Avg PnL by Exit Type</div>'+
    '<div class="mfe-grid">'+
      '<div class="mfe-item"><div class="mfe-val" style="color:var(--green)">+'+m.tp_avg_pnl.toFixed(2)+'R</div><div class="mfe-label">TP Avg</div><div class="mfe-sub">'+m.tp_count+' trades</div></div>'+
      '<div class="mfe-item"><div class="mfe-val" style="color:var(--yellow)">'+m.be_avg_pnl.toFixed(2)+'R</div><div class="mfe-label">BE Avg</div><div class="mfe-sub">'+m.be_count+' trades</div></div>'+
      '<div class="mfe-item"><div class="mfe-val" style="color:var(--red)">'+m.sl_avg_pnl.toFixed(2)+'R</div><div class="mfe-label">SL Avg</div><div class="mfe-sub">'+m.sl_count+' trades</div></div>'+
    '</div>';
}

function renderExperiments(data){
  const list=data.experiments||[];
  if(!list.length) return '<div style="color:var(--text-dim)">No experiments yet</div>';
  let h='<table class="tbl"><thead><tr><th>#</th><th>Parameter</th><th>Change</th><th>Score</th><th>WR</th><th>Trades</th><th>Result</th></tr></thead><tbody>';
  list.forEach(e=>{
    const act=e.action||'?';
    const bcls=act==='keep'?'keep':act==='revert'?'revert':act==='error'?'error':'baseline';
    const chg=(e.old_value!=null&&e.new_value!=null)?esc(e.old_value)+' &rarr; '+esc(e.new_value):'&mdash;';
    h+='<tr>'+
      '<td style="font-weight:600;color:var(--text-dim)">'+e.iteration+'</td>'+
      '<td style="color:var(--accent)">'+esc(e.param_changed||'baseline')+'</td>'+
      '<td style="font-size:.82rem">'+chg+'</td>'+
      '<td style="font-weight:600">'+parseFloat(e.avg_score||0).toFixed(4)+'</td>'+
      '<td>'+pct(parseFloat(e.avg_winrate||0))+'</td>'+
      '<td style="color:var(--text-dim)">'+(e.total_trades||0)+'</td>'+
      '<td><span class="badge '+bcls+'">'+act+'</span></td>'+
    '</tr>';
  });
  h+='</tbody></table>';
  return h;
}

function renderParams(data){
  const p=data.params||{};
  const keys=Object.keys(p);
  if(!keys.length) return '<div style="color:var(--text-dim)">No params</div>';
  let h='';
  const groups=[{title:'Global',obj:p,skip:['crypto_overrides','forex_overrides']},{title:'Crypto Overrides',obj:p.crypto_overrides||{}},{title:'Forex Overrides',obj:p.forex_overrides||{}}];
  groups.forEach(g=>{
    const entries=Object.entries(g.obj).filter(([k])=>!(g.skip||[]).includes(k)&&typeof g.obj[k]!=='object');
    if(!entries.length) return;
    h+='<div style="font-size:.75rem;text-transform:uppercase;color:var(--text-dim);margin:10px 0 6px;letter-spacing:.04em">'+g.title+'</div>';
    h+='<div class="params-grid">';
    entries.forEach(([k,v])=>{
      h+='<div class="param-item"><span class="pk">'+esc(k)+'</span><span class="pv">'+esc(String(v))+'</span></div>';
    });
    h+='</div>';
  });
  return h;
}

function renderLog(data){
  const lines=data.orch_log||[];
  if(!lines.length) return '<div style="color:var(--text-dim)">Empty</div>';
  let h='';
  lines.forEach(l=>{
    let cls='';
    const ll=l.toLowerCase();
    if(ll.includes('error')||ll.includes('fail')) cls='err';
    else if(l.includes('KEEP')) cls='keep';
    else if(l.includes('REVERT')) cls='rev';
    h+='<div class="log-line '+cls+'" title="'+esc(l)+'">'+esc(l)+'</div>';
  });
  return h;
}

function formatCountdown(sec){
  if(sec==null) return '--';
  if(sec<=0) return '\u0421\u0447\u0438\u0442\u0430\u0435\u0442\u0441\u044f...';
  const m=Math.floor(sec/60);
  const s=sec%60;
  if(m>0) return '~'+m+' \u043c\u0438\u043d '+s+' \u0441\u0435\u043a';
  return s+' \u0441\u0435\u043a';
}

function update(data){
  // header
  $('hdr-time').textContent=data.timestamp||'--';
  $('hdr-iter').textContent=(data.stats.total_experiments||0)+' experiments';

  // day/night mode
  const dn=data.day_night||{};
  const modeBadge=$('mode-badge');
  if(dn.is_night){
    modeBadge.className='mode-badge night';
    modeBadge.innerHTML='\uD83C\uDF19 '+esc(dn.label||'Night')+' <span style="font-weight:400;opacity:.7">'+esc(dn.kyiv_time||'')+'</span>';
  } else {
    modeBadge.className='mode-badge day';
    modeBadge.innerHTML='\u2600\uFE0F '+esc(dn.label||'Day')+' <span style="font-weight:400;opacity:.7">'+esc(dn.kyiv_time||'')+'</span>';
  }

  // stat cards
  const s=data.stats;
  $('sc-score').textContent=parseFloat(s.score||0).toFixed(2);
  $('sc-score-sub').textContent='best: '+parseFloat(s.best_score||0).toFixed(2);
  $('sc-wr').textContent=pct(parseFloat(s.winrate||0));
  $('sc-wr-sub').textContent=(s.total_trades||0)+' trades';
  $('sc-wr').className='value '+(s.winrate>=0.45?'green':s.winrate>=0.35?'':'red');
  $('sc-exp').textContent=s.total_experiments;
  $('sc-exp-sub').innerHTML='<span style="color:var(--green)">'+s.kept+' kept</span> &middot; <span style="color:var(--red)">'+s.reverted+' rev</span>'+(s.errors?' &middot; <span style="color:var(--yellow)">'+s.errors+' err</span>':'');
  $('sc-best').textContent=s.best_instrument;

  // last change detail
  const lc=data.last_change;
  const lcCard=$('last-change-card');
  if(lc){
    lcCard.style.display='block';
    const act=lc.action||'?';
    const bcls=act==='keep'?'keep':act==='revert'?'revert':act==='error'?'error':'baseline';
    $('lc-badge').className='badge '+bcls;
    $('lc-badge').textContent=act;
    $('lc-param').textContent=lc.param||'--';
    const chg=(lc.old_value!=null&&lc.new_value!=null)?esc(lc.old_value)+' \u2192 '+esc(lc.new_value):'';
    $('lc-change').innerHTML=chg;
    $('lc-score').textContent='score: '+parseFloat(lc.score||0).toFixed(4);
    $('lc-notes').textContent=lc.notes||'';
  } else {
    lcCard.style.display='none';
  }

  // consecutive reverts
  const cr=data.consecutive_reverts||0;
  const crColor=cr<=3?'var(--green)':cr<=6?'var(--yellow)':'var(--red)';
  $('reverts-value').textContent=cr+' \u0440\u0435\u0432\u0435\u0440\u0442\u043e\u0432 \u043f\u043e\u0434\u0440\u044f\u0434';
  $('reverts-value').style.color=crColor;
  $('reverts-icon').style.color=crColor;
  if(cr>=7){
    $('reverts-sub').textContent='\u041e\u043f\u0442\u0438\u043c\u0438\u0437\u0430\u0446\u0438\u044f \u0437\u0430\u0441\u0442\u0440\u044f\u043b\u0430!';
    $('reverts-sub').style.color='var(--red)';
    $('reverts-card').style.borderColor='var(--red)';
  } else if(cr>=4){
    $('reverts-sub').textContent='\u0412\u043d\u0438\u043c\u0430\u043d\u0438\u0435: \u043c\u043d\u043e\u0433\u043e \u043e\u0442\u043a\u0430\u0442\u043e\u0432';
    $('reverts-sub').style.color='var(--yellow)';
    $('reverts-card').style.borderColor='var(--yellow)';
  } else {
    $('reverts-sub').textContent='\u041d\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u044b\u0439 \u0440\u0435\u0436\u0438\u043c';
    $('reverts-sub').style.color='var(--text-dim)';
    $('reverts-card').style.borderColor='var(--border)';
  }

  // next iteration countdown
  const ni=data.next_iteration||{};
  nextIterCountdown=ni.seconds_until;
  updateCountdownDisplay();
  if(ni.avg_duration){
    $('next-iter-sub').textContent='\u0421\u0440\u0435\u0434\u043d\u044f\u044f \u0438\u0442\u0435\u0440\u0430\u0446\u0438\u044f: '+Math.round(ni.avg_duration/60)+' \u043c\u0438\u043d';
  }

  // iteration speed & ETA
  const spd=data.iteration_speed||{};
  $('speed-value').textContent=(spd.iters_per_hour||0).toFixed(1)+'/\u0447';
  let speedSub=spd.total+'/'+spd.target+' \u0438\u0442\u0435\u0440\u0430\u0446\u0438\u0439';
  if(spd.eta_hours!=null){
    if(spd.eta_hours<1){
      speedSub+=' \u2022 ETA: '+Math.round(spd.eta_hours*60)+' \u043c\u0438\u043d';
    } else {
      speedSub+=' \u2022 ETA: '+spd.eta_hours.toFixed(1)+' \u0447';
    }
  }
  $('speed-sub').textContent=speedSub;

  // keep/revert ratio
  const kr=data.keep_revert_ratio||{};
  const kr10=kr.last10_keep_pct||0;
  const kr50=kr.last50_keep_pct||0;
  $('keep-ratio-value').textContent=kr10.toFixed(0)+'% / '+kr50.toFixed(0)+'%';
  const krColor=kr10>20?'var(--green)':kr10>=10?'var(--yellow)':'var(--red)';
  $('keep-ratio-value').style.color=krColor;
  $('keep-ratio-sub').textContent='last 10 / last 50';

  // holdout status
  const ho=data.holdout||{};
  if(ho.status==='completed'&&ho.results){
    const r=ho.results;
    const hoScore=r.score||r.avg_score||0;
    const hoWr=r.winrate||r.avg_winrate||0;
    $('holdout-value').textContent='\u0421\u043a\u043e\u0440: '+(typeof hoScore==='number'?hoScore.toFixed(2):hoScore);
    $('holdout-value').style.color=hoScore>0?'var(--green)':'var(--red)';
    $('holdout-sub').textContent='WR: '+(typeof hoWr==='number'?pct(hoWr):hoWr)+' | '+(r.total_trades||'?')+' trades';
  } else if(ho.status==='reserved'){
    $('holdout-value').textContent='\u041d\u0435 \u0437\u0430\u043f\u0443\u0449\u0435\u043d';
    $('holdout-value').style.color='var(--text-dim)';
    $('holdout-sub').textContent=ho.message||'\u0417\u0430\u0440\u0435\u0437\u0435\u0440\u0432\u0438\u0440\u043e\u0432\u0430\u043d (\u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 2 \u043c\u0435\u0441)';
  } else {
    $('holdout-value').textContent='\u041e\u0448\u0438\u0431\u043a\u0430';
    $('holdout-value').style.color='var(--yellow)';
    $('holdout-sub').textContent=ho.message||'';
  }

  // score chart
  $('chart-area').innerHTML=renderChart(data);

  // equity curve chart
  $('equity-chart-area').innerHTML=renderEquityCurve(data);

  // agents
  $('agents-area').innerHTML=renderAgents(data);

  // impulse progress
  $('impulse-area').innerHTML=renderImpulse(data);

  // instruments, sessions, exits
  $('instruments-area').innerHTML=renderInstruments(data);
  $('sessions-area').innerHTML=renderSessions(data);
  $('exits-area').innerHTML=renderExits(data);
  $('mfe-area').innerHTML=renderMfeMae(data);

  // experiments
  $('exp-area').innerHTML=renderExperiments(data);

  // params
  $('params-area').innerHTML=renderParams(data);

  // log
  $('log-area').innerHTML=renderLog(data);
}

function updateCountdownDisplay(){
  if(nextIterCountdown==null){
    $('next-iter-value').textContent='--';
    return;
  }
  $('next-iter-value').textContent=formatCountdown(nextIterCountdown);
  if(nextIterCountdown<=0){
    $('next-iter-value').style.color='var(--green)';
  } else {
    $('next-iter-value').style.color='var(--text-bright)';
  }
}

// Tick the countdown every second
setInterval(function(){
  if(nextIterCountdown!=null && nextIterCountdown>0){
    nextIterCountdown--;
    updateCountdownDisplay();
  }
},1000);

function fetchData(){
  fetch('/api/data?timeRange='+encodeURIComponent(currentTimeRange))
    .then(r=>r.json())
    .then(d=>{update(d);prev=d;})
    .catch(e=>console.warn('fetch error',e));
}

// initial + interval (30s matches cache TTL)
fetchData();
setInterval(fetchData,30000);

})();
</script>
</body>
</html>"""


# --------------- HTTP handler ---------------

AUTH_USER = "111blackjack111"
AUTH_PASS = "qwertrewq123454321"


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def _check_auth(self):
        auth = self.headers.get("Authorization")
        if auth and auth.startswith("Basic "):
            decoded = base64.b64decode(auth[6:]).decode()
            if decoded == f"{AUTH_USER}:{AUTH_PASS}":
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Trading Dashboard"')
        self.end_headers()
        return False

    def do_GET(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/data":
            qs = parse_qs(parsed.query)
            time_range = qs.get("timeRange", ["all"])[0]
            data = build_api_data(time_range=time_range)
            payload = json.dumps(data, ensure_ascii=False, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload.encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = 8080
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[Dashboard v5] Running on http://0.0.0.0:{port}")
    server.serve_forever()
