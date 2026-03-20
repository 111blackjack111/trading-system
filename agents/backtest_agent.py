"""
BacktestAgent — независимый процесс.
Слушает runtime/backtest_request.json
Запускает бэктест по всем инструментам ПАРАЛЛЕЛЬНО.
Пишет результат в runtime/backtest_done.json
"""

import os
import sys
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.runner import run_backtest, calculate_metrics

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")
CSV_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "csv")
REQUEST_FILE = os.path.join(RUNTIME_DIR, "backtest_request.json")
DONE_FILE = os.path.join(RUNTIME_DIR, "backtest_done.json")


def get_instruments():
    """Определяет инструменты по наличию CSV файлов."""
    instruments = set()
    if os.path.exists(CSV_DIR):
        for f in os.listdir(CSV_DIR):
            if f.endswith("_H1.csv"):
                instruments.add(f.replace("_H1.csv", ""))
    return sorted(instruments)


def run_single_backtest(args):
    """Запускает бэктест для одного инструмента (для ProcessPoolExecutor)."""
    instrument, params = args
    result = run_backtest(instrument, params)
    return instrument, result


def run_parallel_backtest(params):
    """Запускает бэктест по всем инструментам параллельно."""
    instruments = get_instruments()
    if not instruments:
        print("  [BacktestAgent] No instruments found")
        return {}

    print(f"  [BacktestAgent] Running parallel backtest on {len(instruments)} instruments...")

    results = {}
    args_list = [(inst, params) for inst in instruments]

    with ProcessPoolExecutor(max_workers=min(4, len(instruments))) as executor:
        futures = {executor.submit(run_single_backtest, args): args[0] for args in args_list}
        for future in as_completed(futures):
            instrument = futures[future]
            try:
                inst, result = future.result()
                results[inst] = result
                m = result.get("metrics", {})
                print(f"  [BacktestAgent] {inst}: score={m.get('score', 0)}, "
                      f"trades={m.get('total_trades', 0)}, WR={m.get('winrate', 0)}")
            except Exception as e:
                print(f"  [BacktestAgent] {instrument} failed: {e}")
                results[instrument] = {"instrument": instrument, "error": str(e), "metrics": None}

    return results


def save_metrics(results):
    """Сохраняет метрики в runtime/."""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    for inst, res in results.items():
        metrics = res.get("metrics")
        if metrics:
            path = os.path.join(RUNTIME_DIR, f"metrics_{inst}.json")
            with open(path, "w") as f:
                json.dump(metrics, f, indent=2)


def watch():
    """Режим наблюдателя — ждёт запросы на бэктест."""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    print("[BacktestAgent] Watching for requests...")

    while True:
        if os.path.exists(REQUEST_FILE):
            try:
                with open(REQUEST_FILE) as f:
                    request = json.load(f)

                params = request.get("params", {})
                request_id = request.get("id", "unknown")
                print(f"\n[BacktestAgent] Request #{request_id} received")

                # Удаляем запрос
                os.remove(REQUEST_FILE)

                # Запускаем параллельный бэктест
                results = run_parallel_backtest(params)
                save_metrics(results)

                # Считаем средний score
                scores = []
                for inst, res in results.items():
                    m = res.get("metrics")
                    if m and m.get("score") is not None:
                        scores.append(m["score"])
                avg_score = sum(scores) / len(scores) if scores else 0

                # Пишем результат
                done = {
                    "id": request_id,
                    "avg_score": round(avg_score, 4),
                    "results": {k: v.get("metrics") for k, v in results.items()},
                    "timestamp": time.time(),
                }
                with open(DONE_FILE, "w") as f:
                    json.dump(done, f, indent=2)

                print(f"[BacktestAgent] Request #{request_id} done. avg_score={avg_score:.4f}")

            except Exception as e:
                print(f"[BacktestAgent] Error: {e}")
                # Пишем ошибку
                with open(DONE_FILE, "w") as f:
                    json.dump({"error": str(e)}, f)

        time.sleep(2)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["watch", "once"], default="watch")
    args = parser.parse_args()

    if args.mode == "once":
        from strategy.base_strategy import load_params
        params = load_params()
        results = run_parallel_backtest(params)
        save_metrics(results)
    else:
        watch()
