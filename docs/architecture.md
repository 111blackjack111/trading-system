# Architecture & Setup Details

## Структура проекта
```
trading-system-new/
├── CLAUDE.md              # краткие инструкции
├── docs/                  # детальная документация
├── strategy/
│   ├── base_strategy.py   # SMC логика
│   └── params.json        # текущие параметры
├── backtest/runner.py     # движок бэктеста
├── agents/
│   ├── orchestrator_v2.py # главный цикл (v3 с blacklist, stuck detector)
│   ├── backtest_agent.py  # параллельный бэктест
│   ├── optimizer_agent.py # Claude CLI suggestions
│   ├── analyst_agent.py   # мета-анализ каждые 10 итераций
│   ├── monitor_agent.py   # watchdog + Telegram alerts
│   └── impulse_agent.py   # крипто-импульсы
├── data/csv/              # OHLCV данные
├── db/experiments.db      # история экспериментов
├── runtime/               # межагентная коммуникация
├── dashboard.py           # веб-дашборд :8080
└── launch.sh              # запуск всех агентов
```

## Сервер
- Hetzner CX23: 204.168.165.150, 2 vCPU, 4GB RAM
- Dashboard: http://204.168.165.150:8080
- Telegram: @trading_vit_algo_bot, chat_id: 438218324

## Config (env vars)
OANDA_API_KEY, OANDA_ACCOUNT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Claude CLI через Max подписку (unset ANTHROPIC_API_KEY в tmux)

## Агенты
- **DataAgent**: OHLCV через Yahoo/histdata (форекс) + ccxt (крипта). M3/M15/H1, 12 мес.
- **BacktestAgent**: ProcessPoolExecutor spawn context, smart cache по группам (crypto/forex)
- **OptimizerAgent**: `claude -p` через stdin, stateless, ~2K токенов/вызов
- **OrchestratorAgent**: keep/revert loop, blacklist (2+ reverts), stuck detector (7 reverts)
- **AnalystAgent**: каждые 10 итераций, confidence > 0.8 auto-apply, snapshot/rollback protection
- **MonitorAgent**: 1800s timeout, Telegram alerts
- **ImpulseAgent v3**: 200 монет, 12 фич, self-learning predictions

## Текущее состояние
- ACTIVE_INSTRUMENTS: {"USD_JPY", "BTCUSDT", "EUR_GBP", "GBP_USD"}
- Best score: ~4.28 (holdout confirmed: full year +21R, WR 57%)
- Агенты ОСТАНОВЛЕНЫ. Ждём news filter → paper trading
