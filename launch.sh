#!/bin/bash
# Запуск всех агентов в отдельных TMUX сессиях
# Usage: ./launch.sh [iterations]

ITERATIONS=${1:-100}
DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv/bin/activate"

export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}"

echo "=== Trading System Launch ==="
echo "Directory: $DIR"
echo "Iterations: $ITERATIONS"

# Убиваем старые сессии
tmux kill-session -t backtest 2>/dev/null
tmux kill-session -t orchestrator 2>/dev/null
tmux kill-session -t impulse 2>/dev/null

# 1. BacktestAgent — параллельный бэктест, слушает запросы
echo "Starting BacktestAgent..."
tmux new-session -d -s backtest "
cd $DIR && source $VENV
export ANTHROPIC_API_KEY='$ANTHROPIC_API_KEY'
python3 agents/backtest_agent.py --mode watch
"

sleep 2

# 2. OrchestratorAgent — главный цикл, общается с BacktestAgent через файлы
echo "Starting Orchestrator v2..."
tmux new-session -d -s orchestrator "
cd $DIR && source $VENV
export ANTHROPIC_API_KEY='$ANTHROPIC_API_KEY'
python3 agents/orchestrator_v2.py --iterations $ITERATIONS --skip-data 2>&1 | tee results/orchestrator.log
"

# 3. ImpulseAgent — независимый, сканирует крипту на импульсы
echo "Starting ImpulseAgent..."
tmux new-session -d -s impulse "
cd $DIR && source $VENV
echo 'ImpulseAgent: Scanning historical impulses...'
python3 agents/impulse_agent.py --mode scan --days 180 2>&1 | tee results/impulse.log
echo '---'
echo 'ImpulseAgent: Switching to live monitoring...'
python3 agents/impulse_agent.py --mode live 2>&1 | tee -a results/impulse.log
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
echo "  Ctrl+B, D                    # detach from session"
