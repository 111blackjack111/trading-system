# AI Trading Optimization System

SMC стратегия + autoresearch оптимизация. Владелец: Виталик.
Детали: `docs/architecture.md`, `docs/hypotheses.md`

## Ключевые правила
- Optimizer меняет ОДИН параметр за итерацию
- Каждая итерация → db/experiments.db (keep/revert)
- Минимум 30 сделок для валидной метрики
- Агенты в TMUX, общение через runtime/ файлы
- Claude CLI: `claude -p --output-format text` (Max подписка, $0)
- ПРОТИВ ИМБОВ НЕ ТОРГОВАТЬ
- **Хостинг**: VPS `204.168.165.150` (ssh root@204.168.165.150), код в `/root/trading-system/`

## Стратегия (SMC)
1. H1: тренд через BOS → найти FVG в направлении тренда
2. M3: вход при реакции от FVG + confirmation candle
3. SL: за FVG (ATR × multiplier), TP: фиксированный RR, БУ: be_trigger_rr
4. Фильтры: session, volatility, OB confluence, news
5. Сессии (UTC+3): 09-14, 15-17. Silver Bullet: 10-11, 17-18, 21-22

## Score
`score = sharpe * 0.35 + (winrate * profit_factor) * 0.35 - max_drawdown * 0.2 + 0.1`

## Текущий статус (25.03.2026)
- **Запущено на VPS**: autoresearch 100 итераций, ~30 мин/итерация (3 forex пары × 600K M3 свечей)
- **Best score**: 0.2133 (итерация 11, forex BE trigger 1.0). Параметрическая оптимизация упёрлась — 49 revert из 55 экспериментов
- **ACTIVE**: USD_JPY, EUR_GBP, GBP_USD (core). BTCUSDT перемещён в night-only (слишком тяжёлый — 700K свечей)
- Исключены: BNBUSDT, ETHUSDT, SOLUSDT, XAU_USD, GER40 (статистика)
- **WR**: стабильно 34-36%, ниже порога 40%. RR 2.0-2.2 → на грани breakeven
- **Безопасность VPS**: fail2ban + UFW (22, 8080) + SSH key-only + dashboard basic auth
- **Watchdog v2**: cron */30, автопочинка tmux/backtest/orchestrator + Telegram алерты
- **Следующий шаг**: дождаться 100 итераций → holdout тест → решение: продолжать SMC или переключиться на MM/funding arb

## Инфраструктура на VPS
- **Watchdog**: `agents/watchdog.py` — cron каждые 30 мин, чинит зависания, шлёт Telegram
- **restart.sh**: безопасный перезапуск orchestrator + backtest (не убивает SSH)
- **Telegram**: token в env при запуске monitor/orchestrator (не в файлах)
- **Dashboard**: http://204.168.165.150:8080 (auth: 111blackjack111 / qwertrewq123454321)
