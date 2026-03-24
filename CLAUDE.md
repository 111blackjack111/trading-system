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

## Стратегия (SMC)
1. H1: тренд через BOS → найти FVG в направлении тренда
2. M3: вход при реакции от FVG + confirmation candle
3. SL: за FVG (ATR × multiplier), TP: фиксированный RR, БУ: be_trigger_rr
4. Фильтры: session, volatility, OB confluence, news
5. Сессии (UTC+3): 09-14, 15-17. Silver Bullet: 10-11, 17-18, 21-22

## Score
`score = sharpe * 0.35 + (winrate * profit_factor) * 0.35 - max_drawdown * 0.2 + 0.1`

## Текущий статус
- Best score: ~4.28, holdout: +21R/year, WR 57% (train)
- ACTIVE: USD_JPY, BTCUSDT, EUR_GBP, GBP_USD
- Исключены: BNBUSDT, ETHUSDT, SOLUSDT, XAU_USD, GER40 (статистика)
- **Следующий шаг**: news filter → paper trading → live
