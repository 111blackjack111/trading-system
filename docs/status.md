# Статус проекта (обновлено 2026-03-27)

## Что сделано ✅
- SMC стратегия (BOS + FVG + OB confluence + confirmation candle + session/volatility filters)
- Autoresearch loop: orchestrator → backtest → optimizer → keep/revert
- Score v4 (stable): sharpe*0.25 + quality*0.45 + 0.1 - dd_penalty
- **Partial TP**: 50% фиксация на 1.0R, остаток до полного TP (WR 56-74%)
- **Per-instrument NY session**: USD_JPY торгует London + NY (score 2.14)
- **Multi-param optimization**: optimizer может менять 2-3 параметра за раз
- **Trade Analyst**: Claude Opus анализирует каждую сделку каждые 15 итераций
- **Overfitting detection**: алерт при падении trades >20% или доминации одной пары
- Enriched trade_log: час входа, winning/losing trades, WR по часам
- Crypto/forex overrides (разные параметры для разных классов)
- AnalystAgent (мета-анализ каждые 10 итераций, snapshot/rollback protection)
- MonitorAgent (watchdog + Telegram alerts)
- Night Runner (A/B тестирование конфигураций за ночь)
- Dashboard v5 (modern dark theme, real-time, http://204.168.165.150:8080)
- Telegram bot: отчёты на русском, алерты при stuck/ошибках
- Claude CLI вместо API ($0 расход через Max подписку)
- Безопасность VPS: fail2ban + UFW + SSH key-only + dashboard basic auth

## Текущие результаты (27.03.2026)
- **Baseline score: 1.05** (avg по 4 парам с partial TP + NY)
- GBP_USD: score 0.01, 41 trades, WR 56%, +10.3R
- EUR_GBP: score 0.72, 35 trades, WR 63%, +10.6R
- USD_JPY: score 2.14, 19 trades, WR 74%, +10.6R (с NY сессией)
- GBP_JPY: score 1.33, 25 trades, WR 72%, +9.8R
- **Итого: 120 trades за 4 года, +41.3R**

## ACTIVE_INSTRUMENTS
- Core (всегда): GBP_USD, EUR_GBP, USD_JPY, GBP_JPY
- Night only: EUR_USD, XAU_USD (мониторинг, не в core)
- Исключены: BNBUSDT, ETHUSDT, SOLUSDT, GER40 (статистика)

## Ночные тесты (27.03.2026) — результаты
- **FVG 0.25**: score 0.23 — хуже, больше мусорных сделок. ОТБРОШЕНО
- **Без OB confluence**: score 0.00 — 734 trades но WR 30%. ОТБРОШЕНО
- **NY сессия**: score 0.66 — USD_JPY +1.89. ПРИНЯТО (реализовано per-instrument)

## Что дальше (по порядку)
1. **Autoresearch 50 итераций** — с partial TP, multi-param, trade analyst (ЗАПУЩЕНО)
2. **Per-instrument params** — если autoresearch снова на плато
3. **Trailing stop** — параметры есть, логика не реализована
4. **Holdout тест** — проверка на out-of-sample данных
5. **Paper trading** — 4 недели, минимум 12 сделок
6. **Live** — начать с $500-1000

## Известные проблемы / уроки
- fvg_min_size_multiplier: 0.35 оптимально, ослабление до 0.25 ухудшает качество
- OB confluence обязателен — без него 734 trades но score 0
- EUR_USD стабильно минус на всех конфигурациях — не включать в core
- Optimizer иногда галлюцинирует параметры (sweep_filter) — try/except обязателен
- Параметры на плато после ~60 итераций — нужны структурные изменения (partial TP помог)
- fork() кеширует модули → spawn context обязателен для code changes
- Длинные чаты = огромный расход токенов. Короткие сессии по одной задаче!

## Ключевые решения Виталика
- GBP/USD НЕ исключать (есть реальный опыт торговли)
- Отчёты в Telegram на русском
- Paper trading минимум 4 недели до live
- Начальный депозит $500-10k
- Kyiv = UTC+2 (EET), не менять на UTC+3
