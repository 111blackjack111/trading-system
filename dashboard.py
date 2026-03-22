"""
Real-time Terminal Dashboard v2.
http://server:8080
Auto-refreshes every 10 seconds.
Shows: agent status, errors, current activity, experiments, trade_log.
"""

import os
import json
import sqlite3
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "db", "experiments.db")
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
RESULTS_TSV = os.path.join(BASE_DIR, "results", "results.tsv")
PARAMS_PATH = os.path.join(BASE_DIR, "strategy", "params.json")
ORCH_LOG = os.path.join(BASE_DIR, "results", "orchestrator.log")
MONITOR_LOG = os.path.join(BASE_DIR, "results", "monitor.log")


def get_tmux_sessions():
    try:
        out = subprocess.check_output(["tmux", "list-sessions"], stderr=subprocess.DEVNULL, text=True)
        sessions = {}
        for line in out.strip().split("\n"):
            name = line.split(":")[0]
            sessions[name] = True
        return sessions
    except Exception:
        return {}


def get_agent_activity(name, lines=5):
    """Get last N lines of agent's tmux pane."""
    try:
        out = subprocess.check_output(
            ["tmux", "capture-pane", "-t", name, "-p", "-S", f"-{lines}"],
            stderr=subprocess.DEVNULL, text=True
        )
        result = [l for l in out.strip().split("\n") if l.strip()]
        return result if result else ["idle"]
    except Exception:
        return ["not running"]


def get_process_info():
    """Check running python processes related to trading system."""
    try:
        out = subprocess.check_output(
            ["ps", "aux"], stderr=subprocess.DEVNULL, text=True
        )
        procs = {}
        for line in out.split("\n"):
            if "python3" not in line or "grep" in line:
                continue
            if "orchestrator" in line:
                parts = line.split()
                cpu = parts[2]
                mem = parts[3]
                procs["orchestrator"] = {"cpu": cpu, "mem": mem}
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


def get_experiments():
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT iteration, param_changed, old_value, new_value, "
            "round(avg_score,4) as avg_score, round(avg_winrate,4) as avg_winrate, "
            "total_trades, best_instrument, action, timestamp FROM experiments ORDER BY id DESC LIMIT 15"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_experiment_stats():
    """Get summary stats from experiments."""
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
        return {"total": row[0], "kept": row[1], "reverted": row[2],
                "errors": row[3], "baselines": row[4]}
    except Exception:
        return {}


def get_trade_log():
    path = os.path.join(RUNTIME_DIR, "trade_log.json")
    if not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
        age = datetime.now().timestamp() - mtime
        with open(path) as f:
            data = json.load(f)
        data["_age_seconds"] = int(age)
        return data
    except Exception:
        return None


def get_suggestion():
    path = os.path.join(RUNTIME_DIR, "suggestion.json")
    if not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
        age = datetime.now().timestamp() - mtime
        with open(path) as f:
            data = json.load(f)
        data["_age_seconds"] = int(age)
        return data
    except Exception:
        return None


def get_params():
    if not os.path.exists(PARAMS_PATH):
        return {}
    with open(PARAMS_PATH) as f:
        return json.load(f)


def get_last_log_lines(path, n=5):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 2000))
            lines = f.read().decode("utf-8", errors="replace").split("\n")
        return [l for l in lines if l.strip()][-n:]
    except Exception:
        return []


def get_runtime_files():
    """Check what's in runtime/."""
    files = {}
    if os.path.exists(RUNTIME_DIR):
        for f in os.listdir(RUNTIME_DIR):
            path = os.path.join(RUNTIME_DIR, f)
            mtime = os.path.getmtime(path)
            age = int(datetime.now().timestamp() - mtime)
            size = os.path.getsize(path)
            files[f] = {"age": age, "size": size}
    return files


def format_age(seconds):
    if seconds < 60:
        return f"{seconds}s ago"
    elif seconds < 3600:
        return f"{seconds // 60}m ago"
    elif seconds < 86400:
        return f"{seconds // 3600}h ago"
    else:
        return f"{seconds // 86400}d ago"


def render_bar(value, max_val=50, width=20):
    filled = int(value / max_val * width) if max_val > 0 else 0
    filled = min(filled, width)
    return "█" * filled + "░" * (width - filled)


def build_page():
    sessions = get_tmux_sessions()
    experiments = get_experiments()
    exp_stats = get_experiment_stats()
    trade_log = get_trade_log()
    suggestion = get_suggestion()
    params = get_params()
    procs = get_process_info()
    runtime_files = get_runtime_files()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Agent statuses with detailed info
    agents_config = [
        ("orchestrator", "Orchestrator", "Координирует итерации"),
        ("backtest", "BacktestAgent", "Параллельный бэктест"),
        ("monitor", "MonitorAgent", "Telegram + надзор"),
        ("impulse", "ImpulseAgent", "Сканер импульсов"),
        ("dashboard", "Dashboard", "Этот дашборд"),
    ]

    agent_lines = ""
    for name, label, desc in agents_config:
        alive = name in sessions or name == "dashboard"
        proc = procs.get(name, {})
        status = "🟢" if alive else "🔴"

        if alive and name != "dashboard":
            activity_lines = get_agent_activity(name, 3)
            last_line = activity_lines[-1] if activity_lines else "idle"
            if len(last_line) > 55:
                last_line = last_line[:52] + "..."
            cpu = proc.get("cpu", "?")
            mem = proc.get("mem", "?")
            agent_lines += f'  {status} <span class="agent-name">{label:15s}</span> CPU:{cpu:>5s}% MEM:{mem:>5s}%  <span class="dim">{last_line}</span>\n'
        elif name == "dashboard":
            agent_lines += f'  {status} <span class="agent-name">{label:15s}</span> <span class="dim">http://0.0.0.0:8080</span>\n'
        else:
            agent_lines += f'  {status} <span class="agent-name">{label:15s}</span> <span class="red">NOT RUNNING</span>\n'

    # Workers info
    workers = procs.get("workers", {})
    if workers.get("count", 0) > 0:
        agent_lines += f'  ⚙️  <span class="agent-name">{"Backtest Workers":15s}</span> {workers["count"]} processes, CPU: {workers["total_cpu"]:.0f}%  <span class="yellow">calculating...</span>\n'

    # Runtime files status
    req_file = runtime_files.get("backtest_request.json")
    done_file = runtime_files.get("backtest_done.json")
    if req_file:
        agent_lines += f'  📤 <span class="yellow">backtest_request.json</span> waiting ({format_age(req_file["age"])})\n'
    if done_file:
        agent_lines += f'  📥 <span class="green">backtest_done.json</span> ready ({format_age(done_file["age"])})\n'

    # Score / iteration info
    total = exp_stats.get("total", 0)
    kept = exp_stats.get("kept", 0)
    reverted = exp_stats.get("reverted", 0)
    errors = exp_stats.get("errors", 0)
    current_iter = experiments[0]["iteration"] if experiments else 0

    if len(experiments) > 1:
        baselines = [e for e in experiments if e["action"] == "baseline"]
        keeps = [e for e in experiments if e["action"] == "keep"]
        first_score = baselines[-1]["avg_score"] if baselines else (experiments[-1]["avg_score"] or 0)
        last_score = keeps[0]["avg_score"] if keeps else (experiments[0]["avg_score"] or 0)
        best_score = max((e["avg_score"] or -999) for e in experiments)
        score_dir = "▲" if last_score > first_score else "▼" if last_score < first_score else "="
    else:
        first_score = last_score = best_score = experiments[0]["avg_score"] if experiments else 0
        score_dir = "="

    # Error section
    error_lines = ""
    if errors and errors > 0:
        error_exps = [e for e in experiments if e["action"] == "error"]
        if error_exps:
            error_lines = f'\n  <span class="red">⚠️  Last error: {error_exps[0].get("param_changed", "unknown")[:60]}</span>\n'

    # Trade log section
    tl_section = ""
    tl_age = ""
    if trade_log:
        tl_age = format_age(trade_log.get("_age_seconds", 0))
        wr = trade_log.get("overall_winrate", 0)
        total_trades = trade_log.get("total_trades", 0)
        tl_section += f'  WR: <span class="highlight">{wr:.1%}</span>  |  Trades: {total_trades}  |  Bars to SL: {trade_log.get("avg_bars_to_stop", "N/A")}  <span class="dim">({tl_age})</span>\n\n'

        exits = trade_log.get("exit_reason_breakdown", {})
        for reason in ["tp", "be", "sl", "time_exit"]:
            if reason not in exits:
                continue
            data = exits[reason]
            pct = data["count"] / total_trades * 100 if total_trades > 0 else 0
            color = "green" if reason == "tp" else "red" if reason == "sl" else "yellow"
            tl_section += f'  <span class="{color}">{reason:10s}</span> {render_bar(pct, 60)} {pct:5.1f}% ({data["count"]})\n'

        tl_section += "\n"

        instruments = trade_log.get("win_by_instrument", {})
        for inst, data in sorted(instruments.items(), key=lambda x: -x[1]["winrate"]):
            wr_i = data["winrate"]
            r = data["total_r"]
            color = "green" if r > 0 else "red"
            tl_section += f'  <span class="{color}">{inst:12s}</span> {render_bar(wr_i * 100, 50)} WR {wr_i:5.1%}  <span class="{color}">{r:+6.0f}R</span>  ({data["total_trades"]} trades)\n'

        tl_section += "\n"
        sessions_data = trade_log.get("win_by_session", {})
        for sess, data in sorted(sessions_data.items(), key=lambda x: -x[1]["winrate"]):
            tl_section += f'  {sess:12s} {render_bar(data["winrate"] * 100, 50)} WR {data["winrate"]:5.1%}  ({data["total_trades"]} trades)\n'

    # Suggestion
    sugg_section = ""
    if suggestion:
        sugg_age = format_age(suggestion.get("_age_seconds", 0))
        param = suggestion.get("param", "?")
        reasoning = suggestion.get("reasoning", "")[:100]
        if suggestion.get("type") == "code_change":
            sugg_section = f'  <span class="yellow">CODE CHANGE:</span> {suggestion.get("change_description", "N/A")}\n'
        else:
            old = suggestion.get("old_value", "?")
            new = suggestion.get("new_value", "?")
            sugg_section = f'  <span class="highlight">{param}</span>: {old} → {new}\n'
        sugg_section += f'  <span class="dim">{reasoning}</span>\n'
        sugg_section += f'  <span class="dim">({sugg_age})</span>\n'

    # Experiments table
    exp_lines = ""
    for e in experiments[:10]:
        action = e["action"] or "?"
        action_color = "green" if action == "keep" else "red" if action == "revert" else "yellow"
        action_icon = "✅" if action == "keep" else "❌" if action == "revert" else "⚠️" if action == "error" else "📊"
        param = (e["param_changed"] or "?")[:25]
        score = e["avg_score"] or 0
        wr = e["avg_winrate"] or 0
        exp_lines += f'  {action_icon} <span class="{action_color}">#{e["iteration"]:3d}</span>  {param:25s}  score {score:8.2f}  WR {wr:5.1%}  {e["total_trades"]:4d} trades  <span class="{action_color}">{action:7s}</span>\n'

    # Orchestrator log (last 5 lines)
    orch_log_lines = get_last_log_lines(ORCH_LOG, 5)
    orch_log_section = ""
    for line in orch_log_lines:
        if len(line) > 80:
            line = line[:77] + "..."
        if "error" in line.lower():
            orch_log_section += f'  <span class="red">{line}</span>\n'
        elif "KEEP" in line:
            orch_log_section += f'  <span class="green">{line}</span>\n'
        elif "REVERT" in line:
            orch_log_section += f'  <span class="red">{line}</span>\n'
        else:
            orch_log_section += f'  <span class="dim">{line}</span>\n'

    # Params summary
    params_section = ""
    if params:
        crypto = params.get("crypto_overrides", {})
        forex = params.get("forex_overrides", {})
        params_section = f"""  <span class="highlight">Global:</span> be={params.get("be_trigger_rr")} sl={params.get("sl_atr_multiplier")} tp={params.get("tp_rr_ratio")} fvg_age={params.get("fvg_max_age_bars")}
  <span class="yellow">Crypto:</span> be={crypto.get("be_trigger_rr")} sl={crypto.get("sl_atr_multiplier")} tp={crypto.get("tp_rr_ratio")} atr={crypto.get("min_atr_percentile")}
  <span class="agent-name">Forex:</span>  be={forex.get("be_trigger_rr")} sl={forex.get("sl_atr_multiplier")} tp={forex.get("tp_rr_ratio")} atr={forex.get("min_atr_percentile")}"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Trading System Dashboard</title>
<style>
  body {{
    background: #0a0a0a;
    color: #00ff41;
    font-family: 'Courier New', monospace;
    font-size: 13px;
    padding: 15px;
    margin: 0;
  }}
  .container {{ max-width: 1000px; margin: 0 auto; }}
  .header {{
    border: 1px solid #00ff41;
    padding: 10px 15px;
    margin-bottom: 10px;
  }}
  .section {{
    border: 1px solid #333;
    padding: 8px 12px;
    margin-bottom: 8px;
  }}
  .section-title {{
    color: #00ff41;
    font-weight: bold;
    margin-bottom: 6px;
    border-bottom: 1px solid #333;
    padding-bottom: 3px;
  }}
  pre {{ margin: 0; white-space: pre-wrap; line-height: 1.4; }}
  .highlight {{ color: #ffff00; }}
  .dim {{ color: #666; }}
  .green {{ color: #00ff41; }}
  .red {{ color: #ff4444; }}
  .yellow {{ color: #ffaa00; }}
  .agent-name {{ color: #00aaff; }}
  .blink {{ animation: blink 1s infinite; }}
  @keyframes blink {{ 50% {{ opacity: 0.5; }} }}
  .two-col {{ display: flex; gap: 8px; }}
  .two-col > div {{ flex: 1; }}
</style>
</head>
<body>
<div class="container">

<div class="header">
<pre>
<span class="highlight">╔══════════════════════════════════════════════════════════════╗
║  🤖 TRADING SYSTEM — LIVE DASHBOARD v2                     ║
╚══════════════════════════════════════════════════════════════╝</span>
  {now}  |  Total: <span class="highlight">{total}</span> experiments  |  ✅ {kept}  ❌ {reverted}  ⚠️ {errors}
  Score: {first_score:.2f} → <span class="highlight">{last_score:.2f}</span> {score_dir}  |  Best: <span class="green">{best_score:.2f}</span>{error_lines}</pre>
</div>

<div class="section">
<div class="section-title">$ agents status</div>
<pre>
{agent_lines}</pre>
</div>

<div class="two-col">
<div class="section">
<div class="section-title">$ current suggestion <span class="blink">_</span></div>
<pre>
{sugg_section if sugg_section else '  <span class="dim">waiting for optimizer...</span>'}
</pre>
</div>
<div class="section">
<div class="section-title">$ params</div>
<pre>
{params_section if params_section else '  <span class="dim">no params</span>'}
</pre>
</div>
</div>

<div class="section">
<div class="section-title">$ trade_log</div>
<pre>
{tl_section if tl_section else '  <span class="dim">no data yet</span>'}
</pre>
</div>

<div class="section">
<div class="section-title">$ experiments --last 10</div>
<pre>
{exp_lines if exp_lines else '  <span class="dim">no experiments yet</span>'}
</pre>
</div>

<div class="section">
<div class="section-title">$ orchestrator.log --tail 5</div>
<pre>
{orch_log_section if orch_log_section else '  <span class="dim">empty</span>'}
</pre>
</div>

</div>
</body>
</html>"""
    return html


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(build_page().encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = 8080
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[Dashboard] Running on http://0.0.0.0:{port}")
    server.serve_forever()
