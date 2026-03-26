"""One-time status report to Telegram."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["TELEGRAM_BOT_TOKEN"] = "8588577391:AAE5poxdFXYDFVlf8fkCe3kZXOGCHRqVFfI"
os.environ["TELEGRAM_CHAT_ID"] = "438218324"

import config
config.TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
config.TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

from agents.monitor_agent import read_results_tsv, read_trade_log, send_telegram, check_tmux_session

rows = read_results_tsv()
trade_log = read_trade_log()

current_iter = max(int(r.get("iteration", 0)) for r in rows) if rows else 0

# Scores
scores = [float(r.get("avg_score", 0)) for r in rows if r.get("avg_score", "0") != "0"]
first_score = scores[0] if scores else 0
last_score = scores[-1] if scores else 0
best_score = max(scores) if scores else 0

# Actions
kept = sum(1 for r in rows if r.get("action") == "keep")
reverted = sum(1 for r in rows if r.get("action") == "revert")
code_changes = sum(1 for r in rows if r.get("param", "") == "code_change")

# Agents
backtest_alive = check_tmux_session("backtest")
orch_alive = check_tmux_session("orchestrator")
monitor_alive = check_tmux_session("monitor")

# Trade log data
wr = trade_log.get("overall_winrate", 0) if trade_log else 0
total_trades = trade_log.get("total_trades", 0) if trade_log else 0
bars_to_sl = trade_log.get("avg_bars_to_stop", 0) if trade_log else 0

# Instruments
inst_lines = []
if trade_log and "win_by_instrument" in trade_log:
    for inst, d in trade_log["win_by_instrument"].items():
        emoji = "\u2705" if d["total_r"] > 0 else "\u274c"
        inst_lines.append(
            f"  {emoji} {inst}: WR {d['winrate']:.1%}, {d['total_r']:+.0f}R ({d['total_trades']})"
        )
inst_str = "\n".join(inst_lines)

# Sessions
sess_lines = []
if trade_log and "win_by_session" in trade_log:
    for s, d in trade_log["win_by_session"].items():
        sess_lines.append(f"  {s}: {d['winrate']:.1%} ({d['total_trades']})")
sess_str = "\n".join(sess_lines)

# Exit reasons
exit_lines = []
if trade_log and "exit_reason_breakdown" in trade_log:
    for reason, d in trade_log["exit_reason_breakdown"].items():
        pct = d["count"] / total_trades * 100 if total_trades else 0
        exit_lines.append(f"  {reason}: {d['count']} ({pct:.0f}%) avg {d['avg_pnl']:+.1f}R")
exit_str = "\n".join(exit_lines)

score_dir = "\U0001f4c8" if last_score > first_score else "\U0001f4c9"

bt = "\U0001f7e2" if backtest_alive else "\U0001f534"
oc = "\U0001f7e2" if orch_alive else "\U0001f534"
mo = "\U0001f7e2" if monitor_alive else "\U0001f534"

msg = f"""<b>\U0001f916 Trading System — Status Report</b>
<b>Cycle 3 | Iteration {current_iter}</b>

{score_dir} <b>Score:</b> {first_score:.4f} \u2192 {last_score:.4f} (best: {best_score:.4f})
\U0001f4ca <b>WR:</b> {wr:.1%} ({total_trades} trades)
\u23f1 <b>Avg bars to SL:</b> {bars_to_sl}
\u2705 Kept: {kept} | \u274c Reverted: {reverted} | \U0001f527 Code changes: {code_changes}

<b>Instruments:</b>
{inst_str}

<b>Sessions:</b>
{sess_str}

<b>Exit reasons:</b>
{exit_str}

<b>Agents:</b>
  backtest: {bt}
  orchestrator: {oc}
  monitor: {mo}

\u23f3 Optimization continues..."""

send_telegram(msg)
