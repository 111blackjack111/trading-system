# Статус проекта (обновлено 2026-03-23)

## Что сделано ✅
- SMC стратегия (BOS + FVG + OB confluence + confirmation candle + session/volatility filters)
- Autoresearch loop: orchestrator → backtest → optimizer → keep/revert
- ~50 итераций оптимизации, best score 4.28
- Holdout тест пройден: +21R за год, WR 57% (train), все 4 квартала прибыльные
- Look-ahead bias check пройден
- Crypto/forex overrides (разные параметры для разных классов)
- AnalystAgent (мета-анализ каждые 10 итераций, snapshot/rollback protection)
- MonitorAgent (watchdog + Telegram alerts)
- ImpulseAgent v3 (200 монет, 12 фич, self-learning)
- Dashboard v5 (modern dark theme, real-time, http://204.168.165.150:8080)
- Telegram bot: отчёты на русском, алерты при stuck/ошибках
- Claude CLI вместо API ($0 расход через Max подписку)

## Текущее состояние
- Все агенты ОСТАНОВЛЕНЫ на сервере
- Params зафиксированы на лучшей точке (score 4.28)
- ACTIVE_INSTRUMENTS: USD_JPY, BTCUSDT, EUR_GBP, GBP_USD
- Исключены навсегда: BNBUSDT (-42R), ETHUSDT (-16R), SOLUSDT (-20R), XAU_USD (-5R), GER40 (мало сделок)

## Что дальше (по порядку)
1. **News filter** — ForexFactory/MQL5, ±30мин от High Impact новостей. Гипотеза #12
2. **Holdout с news filter** — проверить что не ухудшил
3. **Paper trading** — 4 недели, минимум 12 сделок, target WR 40%+
4. **Live** — начать с $500-1000

## Известные проблемы / уроки
- fvg_min_size_multiplier BLACKLISTED — даёт аномальные scores (-200/-400)
- score=0 + baseline<0 = ложный keep. Fix: require total_trades>=30 AND score!=0
- fork() кеширует модули → spawn context обязателен для code changes
- AnalystAgent может сломать пару (USD/JPY WR 61%→15.8%). Snapshot/rollback обязателен
- Длинные чаты = огромный расход токенов. Короткие сессии по одной задаче!

## Ключевые решения Виталика
- GBP/USD НЕ исключать (есть реальный опыт торговли)
- Отчёты в Telegram на русском
- Сначала news filter, потом paper trading
- Paper trading минимум 4 недели до live
- Начальный депозит $500-10k
