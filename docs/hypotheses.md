# Evolution Hypotheses

Гипотезы для развития стратегии. Статус: ✅ реализовано, ❌ отброшено, ⏳ в работе, 💡 идея.

## Реализованные ✅
1. **OB + FVG confluence** — ob_confluence param, обязателен (без него score 0)
2. **Разная BE логика крипта/форекс** — через overrides
3. **Confirmation candle** — confirmation_candle_pct param
4. **Новостной фильтр** — ±30мин от High Impact новостей
5. **Partial TP** — 50% фиксация на 1.0R, остаток до полного TP. WR +15-20pp
6. **Per-instrument NY session** — USD_JPY торгует London+NY, score 2.14

## Отброшенные ❌
7. **Ослабление FVG фильтра** (0.25 вместо 0.35) — больше сделок но score 0.23 vs 1.05
8. **Убрать OB confluence** — 734 trades, WR 30%, score 0. Мусор
9. **Directional news trading** — слабый edge (+0.098R/trade vs +0.40R у SMC)
10. **Улучшенный news filter** — почти без разницы, текущий ±30мин уже хорош

## В работе ⏳
11. **Multi-param optimization** — optimizer меняет 2-3 параметра за раз
12. **Trade Analyst agent** — Claude анализирует каждую сделку, ищет паттерны
13. **Autoresearch с partial TP** — 50 итераций, ищем новый оптимум

## Идеи 💡
14. **Per-instrument params** — отдельные be_trigger/tp_rr для каждой пары
15. **Trailing stop** — параметры есть (activation 1.2R, distance 0.2R), логика не реализована
16. **Минимальный размер BOS** — фильтр ложных BOS < 1.5 ATR
17. **Volume spike confirmation** — вход при volume > avg (только крипта)
18. **Regime detection** — тренд vs боковик по кварталам
19. **Динамический TP/SL по уровням ликвидности** — сложная задача
20. **Запрет первых 2ч понедельника** — уже есть monday_filter param
21. **Ресёрч SMC связок** — H4+M15, H1+M15 таймфреймы
22. **Логи в БД** — все логи агентов в SQLite для анализа
