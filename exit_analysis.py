#!/usr/bin/env python3
"""
Exit Analysis — детальная аналитика выходов для SMC trading system.
Запуск: python3 exit_analysis.py [--results /path/to/results.tsv] [--data /path/to/data/]

Что делает:
1. BE exits: считает сколько сделок дошли бы до TP без BE (max favorable excursion)
2. SL exits: через сколько баров срабатывает стоп (ранний = плохой вход)
3. Time exits: в какой R-зоне были на момент истечения
4. Общая картина: distribution of exits, avg R по каждому типу
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────
DEFAULT_RESULTS = "/root/trading-system/results/results.tsv"
DEFAULT_PARAMS = "/root/trading-system/strategy/params.json"
DEFAULT_DATA_DIR = "/root/trading-system/data/"


def load_results(path):
    """Load results.tsv — адаптируется под разные форматы колонок."""
    trades = []
    with open(path, "r") as f:
        reader = csv.DictReader(f, delimiter="\t")
        columns = reader.fieldnames
        print(f"[INFO] Колонки в results.tsv: {columns}")
        for row in reader:
            trades.append(row)
    print(f"[INFO] Загружено {len(trades)} сделок")
    return trades, columns


def load_params(path):
    """Load params.json."""
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def safe_float(val, default=0.0):
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def classify_exit(trade):
    """Определяет тип выхода. Адаптируется под разные названия колонок."""
    # Пробуем разные варианты названий
    exit_type = (
        trade.get("exit_type", "")
        or trade.get("exit_reason", "")
        or trade.get("close_reason", "")
        or trade.get("reason", "")
    ).lower().strip()

    if any(x in exit_type for x in ["sl", "stop_loss", "stoploss", "stop"]):
        return "SL"
    elif any(x in exit_type for x in ["be", "breakeven", "break_even"]):
        return "BE"
    elif any(x in exit_type for x in ["time", "expire", "timeout", "max_bars"]):
        return "TIME"
    elif any(x in exit_type for x in ["tp", "take_profit", "takeprofit", "target"]):
        return "TP"
    else:
        return f"OTHER({exit_type})"


def get_r_multiple(trade):
    """Извлекает R-multiple из сделки."""
    for key in ["r_multiple", "r_mult", "R", "pnl_r", "result_r", "profit_r"]:
        val = trade.get(key)
        if val is not None:
            return safe_float(val)
    # Попробовать посчитать из PnL/risk
    pnl = safe_float(trade.get("pnl", trade.get("profit", 0)))
    risk = safe_float(trade.get("risk", trade.get("sl_distance", 0)))
    if risk > 0:
        return pnl / risk
    return 0.0


def get_bars_held(trade):
    """Сколько баров держалась сделка."""
    for key in ["bars_held", "duration_bars", "bars", "num_bars", "holding_bars"]:
        val = trade.get(key)
        if val is not None:
            return safe_int(val)
    return None


def get_max_favorable(trade):
    """Max favorable excursion в R."""
    for key in ["mfe", "max_favorable", "mfe_r", "max_r", "max_profit_r"]:
        val = trade.get(key)
        if val is not None:
            return safe_float(val)
    return None


def get_max_adverse(trade):
    """Max adverse excursion в R."""
    for key in ["mae", "max_adverse", "mae_r", "max_loss_r", "max_drawdown_r"]:
        val = trade.get(key)
        if val is not None:
            return safe_float(val)
    return None


def analyze_exits(trades):
    """Основной анализ."""
    exit_groups = defaultdict(list)

    for t in trades:
        exit_type = classify_exit(t)
        r = get_r_multiple(t)
        bars = get_bars_held(t)
        mfe = get_max_favorable(t)
        mae = get_max_adverse(t)

        exit_groups[exit_type].append({
            "r": r,
            "bars": bars,
            "mfe": mfe,
            "mae": mae,
            "raw": t,
        })

    return exit_groups


def print_separator(char="─", length=60):
    print(char * length)


def print_header(title):
    print()
    print_separator("═")
    print(f"  {title}")
    print_separator("═")


def report_overview(exit_groups, total):
    """Общая картина."""
    print_header("OVERVIEW — Распределение выходов")

    for exit_type in ["TP", "BE", "SL", "TIME"]:
        group = exit_groups.get(exit_type, [])
        count = len(group)
        pct = count / total * 100 if total > 0 else 0
        avg_r = sum(t["r"] for t in group) / count if count > 0 else 0
        total_r = sum(t["r"] for t in group)
        print(f"  {exit_type:6s}: {count:4d} сделок ({pct:5.1f}%)  |  avg R: {avg_r:+.3f}  |  total R: {total_r:+.2f}")

    # Неизвестные типы
    for exit_type, group in exit_groups.items():
        if exit_type not in ["TP", "BE", "SL", "TIME"]:
            count = len(group)
            pct = count / total * 100 if total > 0 else 0
            avg_r = sum(t["r"] for t in group) / count if count > 0 else 0
            print(f"  {exit_type:6s}: {count:4d} сделок ({pct:5.1f}%)  |  avg R: {avg_r:+.3f}")

    total_r = sum(t["r"] for group in exit_groups.values() for t in group)
    wr = sum(1 for group in exit_groups.values() for t in group if t["r"] > 0) / total * 100 if total > 0 else 0
    print_separator()
    print(f"  TOTAL:  {total} сделок  |  WR: {wr:.1f}%  |  Total R: {total_r:+.2f}")


def report_be_analysis(be_trades):
    """BE exits: сколько дошли бы до TP."""
    print_header("BE EXITS — Упущенная прибыль")

    if not be_trades:
        print("  Нет BE exits для анализа")
        return

    has_mfe = any(t["mfe"] is not None for t in be_trades)

    if has_mfe:
        # У нас есть MFE данные
        would_tp_1_5 = sum(1 for t in be_trades if t["mfe"] and t["mfe"] >= 1.5)
        would_tp_2_0 = sum(1 for t in be_trades if t["mfe"] and t["mfe"] >= 2.0)
        would_tp_2_5 = sum(1 for t in be_trades if t["mfe"] and t["mfe"] >= 2.5)
        would_tp_3_0 = sum(1 for t in be_trades if t["mfe"] and t["mfe"] >= 3.0)

        mfe_values = [t["mfe"] for t in be_trades if t["mfe"] is not None]
        avg_mfe = sum(mfe_values) / len(mfe_values) if mfe_values else 0

        total = len(be_trades)
        print(f"  Всего BE exits: {total}")
        print(f"  Avg MFE (max favorable excursion): {avg_mfe:.2f}R")
        print()
        print(f"  Дошли бы до TP если бы BE не сработал:")
        print(f"    TP 1.5R: {would_tp_1_5:3d} из {total} ({would_tp_1_5/total*100:.0f}%)")
        print(f"    TP 2.0R: {would_tp_2_0:3d} из {total} ({would_tp_2_0/total*100:.0f}%)")
        print(f"    TP 2.5R: {would_tp_2_5:3d} из {total} ({would_tp_2_5/total*100:.0f}%)")
        print(f"    TP 3.0R: {would_tp_3_0:3d} из {total} ({would_tp_3_0/total*100:.0f}%)")

        # Посчитать упущенную прибыль
        lost_r_at_2 = sum(min(t["mfe"], 2.0) for t in be_trades if t["mfe"] and t["mfe"] >= 2.0)
        print(f"\n  Упущенная прибыль (при TP=2.0R): {lost_r_at_2:+.1f}R")
    else:
        print("  ⚠ MFE данные отсутствуют в results.tsv")
        print("  Нужно добавить колонку mfe (max favorable excursion) в бэктестер.")
        print("  См. промпт для Claude Code ниже.")

    # Распределение R у BE exits
    r_values = [t["r"] for t in be_trades]
    if r_values:
        print(f"\n  R-distribution у BE exits:")
        print(f"    min: {min(r_values):.3f}  max: {max(r_values):.3f}  avg: {sum(r_values)/len(r_values):.3f}")


def report_sl_analysis(sl_trades):
    """SL exits: скорость стопа = качество входа."""
    print_header("SL EXITS — Качество входов")

    if not sl_trades:
        print("  Нет SL exits для анализа")
        return

    has_bars = any(t["bars"] is not None for t in sl_trades)

    if has_bars:
        bars_data = [(t["bars"], t["r"]) for t in sl_trades if t["bars"] is not None]

        early = [(b, r) for b, r in bars_data if b <= 3]
        medium = [(b, r) for b, r in bars_data if 4 <= b <= 10]
        late = [(b, r) for b, r in bars_data if b > 10]

        total = len(bars_data)
        print(f"  Всего SL exits с данными bars: {total}")
        print()
        print(f"  По скорости стопа:")
        print(f"    Ранний (1-3 бара):   {len(early):3d} ({len(early)/total*100:.0f}%) — ПЛОХОЙ ВХОД, цена сразу против")
        print(f"    Средний (4-10 баров): {len(medium):3d} ({len(medium)/total*100:.0f}%) — нормальный стоп")
        print(f"    Поздний (>10 баров):  {len(late):3d} ({len(late)/total*100:.0f}%) — разворот после движения")

        if early:
            avg_r_early = sum(r for _, r in early) / len(early)
            print(f"\n  Avg R у ранних стопов: {avg_r_early:.3f}")
            print(f"  Потери от ранних стопов: {sum(r for _, r in early):.1f}R")

        if has_mfe_data := any(t["mfe"] is not None for t in sl_trades):
            # Сколько SL сделок вообще заходили в плюс перед стопом
            went_positive = sum(1 for t in sl_trades if t["mfe"] and t["mfe"] > 0.3)
            print(f"\n  SL exits что заходили в плюс (MFE > 0.3R): {went_positive} из {len(sl_trades)}")
            print(f"  → Это потенциальные BE/partial TP если trailing stop")
    else:
        print("  ⚠ bars_held данные отсутствуют в results.tsv")
        print("  Нужно добавить колонку bars_held в бэктестер.")

    # MAE анализ
    has_mae = any(t["mae"] is not None for t in sl_trades)
    if has_mae:
        mae_values = [t["mae"] for t in sl_trades if t["mae"] is not None]
        print(f"\n  MAE (max adverse excursion):")
        print(f"    avg: {sum(mae_values)/len(mae_values):.3f}R")
        print(f"    max: {min(mae_values):.3f}R")  # MAE обычно отрицательный


def report_time_analysis(time_trades):
    """Time exits: что с ними делать."""
    print_header("TIME EXITS — Зависшие сделки")

    if not time_trades:
        print("  Нет Time exits для анализа")
        return

    total = len(time_trades)
    r_values = [t["r"] for t in time_trades]
    positive = [r for r in r_values if r > 0]
    negative = [r for r in r_values if r < 0]
    near_zero = [r for r in r_values if -0.2 <= r <= 0.2]

    print(f"  Всего Time exits: {total}")
    print(f"  Avg R: {sum(r_values)/total:.3f}")
    print(f"  Total R: {sum(r_values):.1f}")
    print()
    print(f"  В плюсе на момент истечения: {len(positive):3d} ({len(positive)/total*100:.0f}%)")
    print(f"  В минусе на момент истечения: {len(negative):3d} ({len(negative)/total*100:.0f}%)")
    print(f"  Около нуля (±0.2R):          {len(near_zero):3d} ({len(near_zero)/total*100:.0f}%)")

    if positive:
        avg_pos = sum(positive) / len(positive)
        print(f"\n  Avg R у положительных: {avg_pos:.3f}")
        print(f"  Потенциал partial TP: {sum(positive):.1f}R (если закрывать в плюсе раньше)")

    has_mfe = any(t["mfe"] is not None for t in time_trades)
    if has_mfe:
        mfe_values = [t["mfe"] for t in time_trades if t["mfe"] is not None]
        avg_mfe = sum(mfe_values) / len(mfe_values) if mfe_values else 0
        would_tp = sum(1 for t in time_trades if t["mfe"] and t["mfe"] >= 1.5)
        print(f"\n  Avg MFE у Time exits: {avg_mfe:.2f}R")
        print(f"  Дошли бы до TP 1.5R: {would_tp} из {total}")


def report_recommendations(exit_groups, total):
    """Конкретные рекомендации на основе данных."""
    print_header("РЕКОМЕНДАЦИИ")

    sl_count = len(exit_groups.get("SL", []))
    be_count = len(exit_groups.get("BE", []))
    time_count = len(exit_groups.get("TIME", []))
    tp_count = len(exit_groups.get("TP", []))

    sl_pct = sl_count / total * 100 if total > 0 else 0
    be_pct = be_count / total * 100 if total > 0 else 0
    time_pct = time_count / total * 100 if total > 0 else 0

    print(f"  Приоритет оптимизации (по impact):")
    print()

    priorities = []
    if sl_pct > 30:
        priorities.append(("HIGH", f"SL exits {sl_pct:.0f}% — улучшить фильтр входов или SL placement"))
    if be_pct > 10:
        priorities.append(("HIGH", f"BE exits {be_pct:.0f}% — протестировать BE trigger 1.0-1.2R или partial TP"))
    if time_pct > 10:
        priorities.append(("MED", f"Time exits {time_pct:.0f}% — добавить partial TP при +0.5R перед timeout"))

    # Проверка: MFE/MAE данные есть?
    has_mfe = any(
        t["mfe"] is not None
        for group in exit_groups.values()
        for t in group
    )
    if not has_mfe:
        priorities.insert(0, ("CRITICAL", "Добавить MFE/MAE в бэктестер — без них оптимизация слепая"))

    has_bars = any(
        t["bars"] is not None
        for group in exit_groups.values()
        for t in group
    )
    if not has_bars:
        priorities.insert(0, ("CRITICAL", "Добавить bars_held в бэктестер — нужно для анализа качества входов"))

    for priority, text in priorities:
        print(f"  [{priority}] {text}")

    if not priorities:
        print("  Данных недостаточно для конкретных рекомендаций.")
        print("  Добавь MFE, MAE, bars_held в results.tsv и перезапусти.")


def main():
    parser = argparse.ArgumentParser(description="Exit Analysis для SMC Trading System")
    parser.add_argument("--results", default=DEFAULT_RESULTS, help="Путь к results.tsv")
    parser.add_argument("--params", default=DEFAULT_PARAMS, help="Путь к params.json")
    args = parser.parse_args()

    print("=" * 60)
    print("  EXIT ANALYSIS — SMC Trading System")
    print("=" * 60)

    # Load data
    if not os.path.exists(args.results):
        print(f"\n  ❌ Файл не найден: {args.results}")
        print(f"  Укажи правильный путь: python3 exit_analysis.py --results /path/to/results.tsv")
        sys.exit(1)

    trades, columns = load_results(args.results)
    params = load_params(args.params)

    if params:
        print(f"\n  Текущие параметры:")
        for k, v in params.items():
            if not k.startswith("_"):
                print(f"    {k}: {v}")

    # Analyze
    exit_groups = analyze_exits(trades)
    total = len(trades)

    # Reports
    report_overview(exit_groups, total)
    report_be_analysis(exit_groups.get("BE", []))
    report_sl_analysis(exit_groups.get("SL", []))
    report_time_analysis(exit_groups.get("TIME", []))
    report_recommendations(exit_groups, total)

    print()
    print_separator("═")
    print("  Готово. Скопируй вывод и скинь мне для разбора.")
    print_separator("═")


if __name__ == "__main__":
    main()
