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

## Score (v4 — stable)
```
quality = winrate * profit_factor
dd_penalty = max(0, dd_per_100_trades - 5.0) * 0.1
score = sharpe * 0.25 + quality * 0.45 + 0.1 - dd_penalty
```
- Sharpe capped [-3, 3], dd = абсолютный R на 100 сделок (не ratio)
- Нормальный диапазон: -2 to +2. GBP_USD baseline: +1.17

## Текущий статус (25.03.2026)
- **РЕФАКТОРИНГ ЗАВЕРШЁН**: score v4, anomaly detector v2, чистая БД, фикс optimizer
- **Baseline после фиксов**: GBP_USD +1.17 (48% WR), EUR_GBP -0.04 (40% WR), USD_JPY -0.19 (40% WR)
- **ACTIVE**: USD_JPY, EUR_GBP, GBP_USD (core). BTCUSDT в night-only
- Исключены: BNBUSDT, ETHUSDT, SOLUSDT, XAU_USD, GER40 (статистика)
- **Безопасность VPS**: fail2ban + UFW (22, 8080) + SSH key-only + dashboard basic auth
- **Watchdog v2**: cron */30, автопочинка tmux/backtest/orchestrator + Telegram алерты
- **Следующий шаг**: autoresearch 100 итераций с новой формулой → holdout → paper trading

## Инфраструктура на VPS
- **Watchdog**: `agents/watchdog.py` — cron каждые 30 мин, чинит зависания, шлёт Telegram
- **restart.sh**: безопасный перезапуск orchestrator + backtest (не убивает SSH)
- **Telegram**: token в env при запуске monitor/orchestrator (не в файлах)
- **Dashboard**: http://204.168.165.150:8080 (auth: 111blackjack111 / qwertrewq123454321)
