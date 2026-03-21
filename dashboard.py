"""
Real-time Terminal Dashboard.
Opens http://server:8080 in browser.
Auto-refreshes every 10 seconds.
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


def get_tmux_sessions():
    """Check which tmux sessions are running."""
    try:
        out = subprocess.check_output(["tmux", "list-sessions"], stderr=subprocess.DEVNULL, text=True)
        sessions = {}
        for line in out.strip().split("\n"):
            name = line.split(":")[0]
            sessions[name] = True
        return sessions
    except Exception:
        return {}


def get_agent_activity(name):
    """Get last line of agent's tmux pane."""
    try:
        out = subprocess.check_output(
            ["tmux", "capture-pane", "-t", name, "-p", "-S", "-5"],
            stderr=subprocess.DEVNULL, text=True
        )
        lines = [l for l in out.strip().split("\n") if l.strip()]
        return lines[-1] if lines else "idle"
    except Exception:
        return "not running"


def get_experiments():
    """Read experiments from DB."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT iteration, param_changed, old_value, new_value, "
            "round(avg_score,4) as avg_score, round(avg_winrate,4) as avg_winrate, "
            "total_trades, best_instrument, action FROM experiments ORDER BY id DESC LIMIT 15"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_trade_log():
    path = os.path.join(RUNTIME_DIR, "trade_log.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def get_suggestion():
    path = os.path.join(RUNTIME_DIR, "suggestion.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def get_params():
    if not os.path.exists(PARAMS_PATH):
        return {}
    with open(PARAMS_PATH) as f:
        return json.load(f)


def render_bar(value, max_val=50, width=20):
    filled = int(value / max_val * width) if max_val > 0 else 0
    filled = min(filled, width)
    return "█" * filled + "░" * (width - filled)


def build_page():
    sessions = get_tmux_sessions()
    experiments = get_experiments()
    trade_log = get_trade_log()
    suggestion = get_suggestion()
    params = get_params()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Agent statuses
    agents = [
        ("orchestrator", "Orchestrator", "Координирует итерации"),
        ("backtest", "BacktestAgent", "Параллельный бэктест"),
        ("monitor", "MonitorAgent", "Telegram + надзор"),
        ("impulse", "ImpulseAgent", "Сканер импульсов"),
    ]

    agent_lines = ""
    for name, label, desc in agents:
        alive = name in sessions
        status = "🟢" if alive else "🔴"
        activity = get_agent_activity(name) if alive else "stopped"
        # Truncate activity
        if len(activity) > 60:
            activity = activity[:57] + "..."
        agent_lines += f'  {status} <span class="agent-name">{label:15s}</span> <span class="dim">{activity}</span>\n'

    # Current iteration
    current_iter = experiments[0]["iteration"] if experiments else 0
    total_kept = sum(1 for e in experiments if e["action"] == "keep")
    total_reverted = sum(1 for e in experiments if e["action"] == "revert")

    # Score progress
    if len(experiments) > 1:
        first_score = experiments[-1]["avg_score"] or 0
        last_score = experiments[0]["avg_score"] or 0
        score_dir = "▲" if last_score > first_score else "▼" if last_score < first_score else "="
    else:
        first_score = last_score = experiments[0]["avg_score"] if experiments else 0
        score_dir = "="

    # Trade log section
    tl_section = ""
    if trade_log:
        wr = trade_log.get("overall_winrate", 0)
        total = trade_log.get("total_trades", 0)
        tl_section += f'  WR: <span class="highlight">{wr:.1%}</span>  |  Trades: {total}  |  Bars to SL: {trade_log.get("avg_bars_to_stop", "N/A")}\n\n'

        # Exits
        exits = trade_log.get("exit_reason_breakdown", {})
        for reason, data in exits.items():
            pct = data["count"] / total * 100 if total > 0 else 0
            color = "green" if reason == "tp" else "red" if reason == "sl" else "yellow"
            tl_section += f'  <span class="{color}">{reason:10s}</span> {render_bar(pct, 60)} {pct:5.1f}% ({data["count"]})\n'

        tl_section += "\n"

        # Instruments
        instruments = trade_log.get("win_by_instrument", {})
        for inst, data in sorted(instruments.items(), key=lambda x: -x[1]["winrate"]):
            wr_i = data["winrate"]
            r = data["total_r"]
            color = "green" if r > 0 else "red"
            tl_section += f'  <span class="{color}">{inst:12s}</span> {render_bar(wr_i * 100, 50)} WR {wr_i:5.1%}  <span class="{color}">{r:+6.0f}R</span>  ({data["total_trades"]} trades)\n'

        tl_section += "\n"

        # Sessions
        sessions_data = trade_log.get("win_by_session", {})
        for sess, data in sorted(sessions_data.items(), key=lambda x: -x[1]["winrate"]):
            tl_section += f'  {sess:12s} {render_bar(data["winrate"] * 100, 50)} WR {data["winrate"]:5.1%}  ({data["total_trades"]} trades)\n'

    # Suggestion
    sugg_section = ""
    if suggestion:
        param = suggestion.get("param", "?")
        reasoning = suggestion.get("reasoning", "")[:80]
        if suggestion.get("type") == "code_change":
            sugg_section = f'  <span class="yellow">CODE CHANGE:</span> {suggestion.get("change_description", "N/A")}\n'
        else:
            old = suggestion.get("old_value", "?")
            new = suggestion.get("new_value", "?")
            sugg_section = f'  <span class="highlight">{param}</span>: {old} → {new}\n'
        sugg_section += f'  <span class="dim">{reasoning}</span>\n'

    # Experiments table
    exp_lines = ""
    for e in experiments[:10]:
        action_color = "green" if e["action"] == "keep" else "red" if e["action"] == "revert" else "yellow"
        action_icon = "✅" if e["action"] == "keep" else "❌" if e["action"] == "revert" else "📊"
        param = (e["param_changed"] or "?")[:20]
        score = e["avg_score"] or 0
        wr = e["avg_winrate"] or 0
        exp_lines += f'  {action_icon} <span class="{action_color}">#{e["iteration"]:3d}</span>  {param:20s}  score {score:8.4f}  WR {wr:5.1%}  {e["total_trades"]:4d} trades  <span class="{action_color}">{e["action"]:7s}</span>\n'

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
    font-size: 14px;
    padding: 20px;
    margin: 0;
  }}
  .container {{ max-width: 1000px; margin: 0 auto; }}
  .header {{
    border: 1px solid #00ff41;
    padding: 10px 15px;
    margin-bottom: 15px;
  }}
  .section {{
    border: 1px solid #333;
    padding: 10px 15px;
    margin-bottom: 10px;
  }}
  .section-title {{
    color: #00ff41;
    font-weight: bold;
    margin-bottom: 8px;
    border-bottom: 1px solid #333;
    padding-bottom: 4px;
  }}
  pre {{ margin: 0; white-space: pre-wrap; line-height: 1.5; }}
  .highlight {{ color: #ffff00; }}
  .dim {{ color: #666; }}
  .green {{ color: #00ff41; }}
  .red {{ color: #ff4444; }}
  .yellow {{ color: #ffaa00; }}
  .agent-name {{ color: #00aaff; }}
  .blink {{ animation: blink 1s infinite; }}
  @keyframes blink {{ 50% {{ opacity: 0.5; }} }}
</style>
</head>
<body>
<div class="container">

<div class="header">
<pre>
<span class="highlight">╔══════════════════════════════════════════════════════════════╗
║  🤖 TRADING SYSTEM — LIVE DASHBOARD                        ║
║  Autoresearch Optimization Engine                           ║
╚══════════════════════════════════════════════════════════════╝</span>
  {now}  |  Iteration: <span class="highlight">{current_iter}</span>/100  |  Kept: <span class="green">{total_kept}</span>  Reverted: <span class="red">{total_reverted}</span>
  Score: {first_score:.4f} → <span class="highlight">{last_score:.4f}</span> {score_dir}
</pre>
</div>

<div class="section">
<div class="section-title">$ agents status</div>
<pre>
{agent_lines}</pre>
</div>

<div class="section">
<div class="section-title">$ current suggestion <span class="blink">_</span></div>
<pre>
{sugg_section if sugg_section else '  <span class="dim">waiting for optimizer...</span>'}
</pre>
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
        pass  # Suppress access logs


if __name__ == "__main__":
    port = 8080
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[Dashboard] Running on http://0.0.0.0:{port}")
    server.serve_forever()
