#!/bin/bash
# Safe restart of all agents
# Used by watchdog and manual restarts
cd /root/trading-system

rm -f runtime/backtest_request.json runtime/backtest_done.json
tmux kill-session -t orchestrator 2>/dev/null
tmux kill-session -t backtest 2>/dev/null
tmux kill-session -t monitor 2>/dev/null
tmux kill-session -t impulse 2>/dev/null
sleep 2

# Kill zombie workers
for pid in $(pgrep -f "multiprocessing.spawn"); do kill -9 $pid 2>/dev/null; done
sleep 1

# Start backtest
tmux new-session -d -s backtest "cd /root/trading-system && source venv/bin/activate && python3 -u agents/backtest_agent.py --mode watch"
sleep 3

# Start orchestrator with Telegram
tmux new-session -d -s orchestrator "cd /root/trading-system && source venv/bin/activate && export TELEGRAM_BOT_TOKEN=8588577391:AAE5poxdFXYDFVlf8fkCe3kZXOGCHRqVFfI && export TELEGRAM_CHAT_ID=438218324 && python3 -u agents/orchestrator_v2.py --iterations 100 --skip-data 2>&1 | tee results/orchestrator.log"

# Start monitor with Telegram
tmux new-session -d -s monitor "cd /root/trading-system && source venv/bin/activate && export TELEGRAM_BOT_TOKEN=8588577391:AAE5poxdFXYDFVlf8fkCe3kZXOGCHRqVFfI && export TELEGRAM_CHAT_ID=438218324 && python3 -u agents/monitor_agent.py 2>&1 | tee results/monitor.log"

# Start impulse agent (runs scan every hour)
tmux new-session -d -s impulse "cd /root/trading-system && source venv/bin/activate && while true; do python3 agents/impulse_agent.py --mode scan --days 7 2>&1 | tee -a results/impulse.log; sleep 3600; done"

echo "[$(date)] Restart complete" >> /root/trading-system/results/restart.log
