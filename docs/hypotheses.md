# Evolution Hypotheses

Гипотезы для StrategyEvolutionAgent. Читать только когда нужно работать с гипотезами.

1. **Dead hours filter крипта (01-06 UTC)** — Asian WR 25.5%, -20% сделок, +5% WR
2. **OB + FVG confluence** — уже реализовано (ob_confluence param)
3. **Минимальный размер BOS** — фильтр ложных BOS < 1.5 ATR
4. **Разная BE логика крипта/форекс** — реализовано через overrides
5. **Volume spike confirmation** — вход при volume > avg (только крипта)
6. **Confirmation candle** — реализовано (confirmation_candle_pct param)
7. **Trailing stop** — увеличение avg win при уменьшении WR
8. **Запрет первых 2ч понедельника** — -5% сделок
9. **Ресёрч SMC связок** — H1+M3, H4+M15, H1+M15
10. **Regime detection** — тренд vs боковик по кварталам
11. **Динамический TP/SL по уровням ликвидности** — сложная задача
12. **Новостной фильтр ForexFactory** — СЛЕДУЮЩИЙ ШАГ перед paper trading

Статус: параметрическая оптимизация завершена (score 4.28).
Следующий шаг: новостной фильтр → paper trading → live.
