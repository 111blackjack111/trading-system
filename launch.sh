#!/bin/bash
# Запуск всех агентов в одной TMUX сессии с 4 сплитами
# Usage: ./launch.sh [iterations]
#
# Layout (2x2):
#   ┌──────────────┬──────────────┐
#   │ orchestrator  │   backtest   │
#   ├──────────────┼──────────────┤
#   │   impulse    │   monitor    │
#   └──────────────┴──────────────┘
#
# Навигация: Ctrl+B, стрелки

ITERATIONS=${1:-100}
DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv/bin/activate"
SESSION="trading"

# Claude CLI использует подписку Max, НЕ API ключ.
unset ANTHROPIC_API_KEY
export PATH="/Users/a1/.npm-global/bin:$PATH"

echo "=== Trading System Launch ==="
echo "Directory: $DIR"
echo "Iterations: $ITERATIONS"

# Убиваем старую сессию
tmux kill-session -t $SESSION 2>/dev/null

# Создаём сессию с первым pane — orchestrator
tmux new-session -d -s $SESSION -n agents \
  "cd $DIR && source $VENV && python3 agents/orchestrator_v2.py --iterations $ITERATIONS --skip-data 2>&1 | tee results/orchestrator.log; echo '[Orchestrator finished. Press Enter]'; read"

# Split right — backtest
tmux split-window -h -t $SESSION:agents \
  "cd $DIR && source $VENV && python3 agents/backtest_agent.py --mode watch; echo '[Backtest stopped. Press Enter]'; read"

# Split bottom-left — impulse
tmux select-pane -t $SESSION:agents.0
tmux split-window -v -t $SESSION:agents \
  "cd $DIR && source $VENV && while true; do echo \"[ImpulseAgent] Scan started at \$(date)\"; python3 agents/impulse_agent.py --mode scan --days 7 2>&1 | tee -a results/impulse.log; echo \"[ImpulseAgent] Sleeping 1 hour...\"; sleep 3600; done"

# Split bottom-right — monitor
tmux select-pane -t $SESSION:agents.2
tmux split-window -v -t $SESSION:agents \
  "cd $DIR && source $VENV && export TELEGRAM_BOT_TOKEN='${TELEGRAM_BOT_TOKEN}' && export TELEGRAM_CHAT_ID='${TELEGRAM_CHAT_ID}' && python3 agents/monitor_agent.py 2>&1 | tee results/monitor.log; echo '[Monitor stopped. Press Enter]'; read"

# Выравниваем panes
tmux select-layout -t $SESSION:agents tiled

echo ""
echo "=== All agents started in tmux session '$SESSION' ==="
echo ""
echo "  tmux attach -t $SESSION     # view all agents"
echo "  Ctrl+B, arrow keys          # switch between panes"
echo "  Ctrl+B, D                   # detach"
echo ""
