#!/bin/bash
# Запуск всех агентов в отдельных TMUX сессиях
# Usage: ./launch.sh [iterations]

ITERATIONS=${1:-100}
DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv/bin/activate"

export PATH="/usr/local/bin:/root/.npm-global/bin:/Users/a1/.npm-global/bin:$PATH"

echo "=== Trading System Launch ==="
echo "Directory: $DIR"
echo "Iterations: $ITERATIONS"

# Убиваем старые сессии
tmux kill-session -t backtest 2>/dev/null
tmux kill-session -t orchestrator 2>/dev/null
tmux kill-session -t impulse 2>/dev/null
tmux kill-session -t monitor 2>/dev/null
tmux kill-session -t trading 2>/dev/null

# 1. BacktestAgent
echo "Starting BacktestAgent..."
tmux new-session -d -s backtest "
cd $DIR && source $VENV
python3 agents/backtest_agent.py --mode watch
"

sleep 2

# 2. Orchestrator v2
echo "Starting Orchestrator v2..."
tmux new-session -d -s orchestrator "
cd $DIR && source $VENV
python3 agents/orchestrator_v2.py --iterations $ITERATIONS --skip-data 2>&1 | tee results/orchestrator.log
"

# 3. ImpulseAgent
echo "Starting ImpulseAgent..."
tmux new-session -d -s impulse "
cd $DIR && source $VENV
while true; do
  echo \"[ImpulseAgent] Scan started at \$(date)\"
  python3 agents/impulse_agent.py --mode scan --days 7 2>&1 | tee -a results/impulse.log
  echo \"[ImpulseAgent] Sleeping 1 hour...\"
  sleep 3600
done
"

# 4. MonitorAgent
echo "Starting MonitorAgent..."
tmux new-session -d -s monitor "
cd $DIR && source $VENV
python3 agents/monitor_agent.py 2>&1 | tee results/monitor.log
"

echo ""
echo "=== All agents started ==="
echo ""
echo "TMUX sessions:"
tmux list-sessions
echo ""
echo "Commands:"
echo "  tmux attach -t orchestrator  # watch optimization"
echo "  tmux attach -t backtest      # watch backtests"
echo "  tmux attach -t impulse       # watch impulse scanner"
echo "  tmux attach -t monitor       # watch monitor/telegram"
echo "  Ctrl+B, D                    # detach from session"
