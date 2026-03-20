"""
Backtest Runner — движок бэктеста.
Запускает SMC стратегию на исторических данных,
считает метрики и возвращает результат.
"""

import os
import sys
import json
import math

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.base_strategy import (
    load_params,
    generate_signals,
    simulate_trades,
)

CSV_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "csv")
RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")


def load_data(instrument, timeframe):
    """Загружает CSV файл с данными."""
    filepath = os.path.join(CSV_DIR, f"{instrument}_{timeframe}.csv")
    if not os.path.exists(filepath):
        return None
    df = pd.read_csv(filepath, index_col="timestamp", parse_dates=True)
    return df


def calculate_metrics(trades):
    """
    Считает метрики по списку сделок.

    Returns:
        dict: {total_trades, winrate, profit_factor, sharpe, max_drawdown, avg_rr, score}
    """
    if not trades:
        return {
            "total_trades": 0,
            "winrate": 0,
            "profit_factor": 0,
            "sharpe": 0,
            "max_drawdown": 0,
            "avg_rr": 0,
            "score": 0,
        }

    pnls = [t["pnl_r"] for t in trades]
    total = len(pnls)

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    winrate = len(wins) / total if total > 0 else 0

    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0.001
    profit_factor = gross_profit / gross_loss

    # Sharpe ratio (annualized, assuming ~252 trading days)
    if len(pnls) > 1:
        mean_pnl = np.mean(pnls)
        std_pnl = np.std(pnls)
        sharpe = (mean_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0
    else:
        sharpe = 0

    # Max drawdown (в R-множителях)
    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    drawdown = (peak - cumulative)
    max_drawdown = np.max(drawdown) if len(drawdown) > 0 else 0
    # Нормализуем к доле от пика
    max_dd_pct = max_drawdown / (abs(peak.max()) + 0.001)

    avg_rr = np.mean(pnls)

    # Composite score
    score = calculate_score(sharpe, profit_factor, max_dd_pct, winrate, total)

    return {
        "total_trades": total,
        "winrate": round(winrate, 4),
        "profit_factor": round(profit_factor, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max_dd_pct, 4),
        "avg_rr": round(avg_rr, 4),
        "total_r": round(sum(pnls), 4),
        "score": round(score, 4),
    }


def calculate_score(sharpe, profit_factor, max_drawdown, winrate, total_trades):
    """
    Composite score для оптимизации.
    score = sharpe * 0.4 + profit_factor * 0.3 - max_drawdown * 0.2 + winrate * 0.1

    Штрафы (score = 0):
    - меньше 30 сделок
    - max_drawdown > 0.10
    - winrate < 0.40
    """
    if total_trades < 30:
        return 0
    if max_drawdown > 0.10:
        return 0
    if winrate < 0.40:
        return 0

    return sharpe * 0.4 + profit_factor * 0.3 - max_drawdown * 0.2 + winrate * 0.1


def run_backtest(instrument, params=None):
    """
    Запускает бэктест для одного инструмента.

    Args:
        instrument: название (e.g. "GBP_USD", "BTCUSDT")
        params: dict параметров или None (загрузит из params.json)

    Returns:
        dict: метрики + список сделок
    """
    if params is None:
        params = load_params()

    # Загружаем данные
    df_h1 = load_data(instrument, "H1")
    df_m3 = load_data(instrument, "M3")

    if df_h1 is None or df_m3 is None:
        print(f"  No data for {instrument}")
        return {"instrument": instrument, "error": "no_data", "metrics": None}

    print(f"  Running backtest: {instrument} (H1: {len(df_h1)}, M3: {len(df_m3)} candles)")

    # Генерируем сигналы
    signals = generate_signals(df_h1, df_m3, params)
    print(f"  Signals found: {len(signals)}")

    # Симулируем сделки
    trades = simulate_trades(signals, df_m3, params)
    print(f"  Trades executed: {len(trades)}")

    # Считаем метрики
    metrics = calculate_metrics(trades)

    return {
        "instrument": instrument,
        "metrics": metrics,
        "trades": trades,
    }


def run_all(params=None):
    """Запускает бэктест по всем инструментам."""
    if params is None:
        params = load_params()

    # Определяем инструменты по наличию файлов
    instruments = set()
    if os.path.exists(CSV_DIR):
        for f in os.listdir(CSV_DIR):
            if f.endswith("_H1.csv"):
                inst = f.replace("_H1.csv", "")
                instruments.add(inst)

    results = {}
    for instrument in sorted(instruments):
        result = run_backtest(instrument, params)
        results[instrument] = result

        # Сохраняем метрики в runtime
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        metrics_path = os.path.join(RUNTIME_DIR, f"metrics_{instrument}.json")
        with open(metrics_path, "w") as f:
            json.dump(result["metrics"], f, indent=2)

    # Сводка
    print("\n=== Backtest Summary ===")
    for inst, res in results.items():
        m = res.get("metrics")
        if m:
            print(f"  {inst}: score={m['score']}, trades={m['total_trades']}, "
                  f"WR={m['winrate']}, PF={m['profit_factor']}, Sharpe={m['sharpe']}")
        else:
            print(f"  {inst}: {res.get('error', 'unknown error')}")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", type=str, default=None)
    args = parser.parse_args()

    if args.instrument:
        result = run_backtest(args.instrument)
        print(json.dumps(result["metrics"], indent=2))
    else:
        run_all()
