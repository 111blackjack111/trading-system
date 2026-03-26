"""
OrchestratorAgent — главный цикл autoresearch.
DataAgent → BacktestAgent → OptimizerAgent → Apply/Revert → Repeat

По умолчанию: 100 итераций.
"""

import os
import sys
import json
import sqlite3
import shutil
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.base_strategy import load_params, save_params
from backtest.runner import run_all as run_backtest_all
from agents.optimizer_agent import suggest_change
from agents.data_agent import run as run_data_agent

DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")
DB_PATH = os.path.join(DB_DIR, "experiments.db")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")
PARAMS_PATH = os.path.join(os.path.dirname(__file__), "..", "strategy", "params.json")


def init_db():
    """Создаёт таблицу экспериментов если не существует."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration INTEGER,
            timestamp TEXT,
            param_changed TEXT,
            old_value REAL,
            new_value REAL,
            avg_score REAL,
            best_score REAL,
            best_instrument TEXT,
            total_trades INTEGER,
            avg_winrate REAL,
            avg_pf REAL,
            action TEXT,
            notes TEXT,
            params_snapshot TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_experiment(iteration, suggestion, metrics_all, action, params):
    """Сохраняет итерацию в БД и TSV."""
    conn = sqlite3.connect(DB_PATH)

    scores = []
    best_score = 0
    best_inst = ""
    total_trades = 0
    winrates = []
    pfs = []

    for inst, res in metrics_all.items():
        m = res.get("metrics")
        if m and m.get("score") is not None:
            scores.append(m["score"])
            total_trades += m["total_trades"]
            winrates.append(m["winrate"])
            pfs.append(m["profit_factor"])
            if m["score"] > best_score:
                best_score = m["score"]
                best_inst = inst

    avg_score = sum(scores) / len(scores) if scores else 0

    conn.execute("""
        INSERT INTO experiments
        (iteration, timestamp, param_changed, old_value, new_value,
         avg_score, best_score, best_instrument, total_trades,
         avg_winrate, avg_pf, action, notes, params_snapshot)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        iteration,
        datetime.utcnow().isoformat(),
        suggestion.get("param", "baseline"),
        suggestion.get("old_value", 0),
        suggestion.get("new_value", 0),
        round(avg_score, 4),
        round(best_score, 4),
        best_inst,
        total_trades,
        round(sum(winrates) / len(winrates), 4) if winrates else 0,
        round(sum(pfs) / len(pfs), 4) if pfs else 0,
        action,
        suggestion.get("reasoning", ""),
        json.dumps(params),
    ))
    conn.commit()
    conn.close()

    # TSV removed — all data is in experiments.db


def get_avg_score(metrics_all):
    """Считает средний score по всем инструментам (игнорирует -999 penalty)."""
    scores = []
    for inst, res in metrics_all.items():
        m = res.get("metrics")
        if m and m.get("score") is not None and m["score"] > -900:
            scores.append(m["score"])
    return sum(scores) / len(scores) if scores else -999


def run(max_iterations=100, skip_data_download=False):
    """Главный цикл autoresearch."""
    init_db()

    print("=" * 60)
    print(f"ORCHESTRATOR: Starting autoresearch ({max_iterations} iterations)")
    print("=" * 60)

    # Шаг 0: Скачиваем данные только если устарели (>24ч) или отсутствуют
    if not skip_data_download:
        import time as _time
        csv_dir = os.path.join(os.path.dirname(__file__), "..", "data", "csv")
        need_download = False
        check_file = os.path.join(csv_dir, "GBP_USD_H1.csv")
        if not os.path.exists(check_file):
            need_download = True
            print("\n[Step 0] No data found, downloading...")
        elif _time.time() - os.path.getmtime(check_file) > 86400:
            need_download = True
            print("\n[Step 0] Data older than 24h, updating...")
        else:
            age_h = (_time.time() - os.path.getmtime(check_file)) / 3600
            print(f"\n[Step 0] Data fresh ({age_h:.1f}h old), skipping download")
        if need_download:
            run_data_agent(months=48)

    # Шаг 1: Baseline — бэктест с текущими параметрами
    print("\n[Iteration 0] Baseline backtest...")
    params = load_params()
    baseline_results = run_backtest_all(params)
    baseline_score = get_avg_score(baseline_results)
    save_experiment(0, {"param": "baseline", "reasoning": "Initial baseline"}, baseline_results, "baseline", params)

    print(f"\nBaseline avg_score: {baseline_score:.4f}")
    best_score = baseline_score

    # Шаг 2: Итерации оптимизации
    no_improvement_count = 0

    for i in range(1, max_iterations + 1):
        print(f"\n{'=' * 60}")
        print(f"[Iteration {i}/{max_iterations}]")
        print(f"{'=' * 60}")

        # Сохраняем текущие параметры (для отката)
        params_backup = load_params()

        # Получаем предложение от Claude
        print("\n  [Optimizer] Getting suggestion...")
        try:
            suggestion = suggest_change(params_backup)
        except Exception as e:
            print(f"  Optimizer error: {e}")
            continue

        # Применяем изменение
        new_params = params_backup.copy()
        new_params[suggestion["param"]] = suggestion["new_value"]
        save_params(new_params)

        # Бэктест с новыми параметрами
        print("\n  [Backtest] Running...")
        new_results = run_backtest_all(new_params)
        new_score = get_avg_score(new_results)

        # Решение: keep или revert
        if new_score > best_score:
            action = "keep"
            improvement = new_score - best_score
            best_score = new_score
            no_improvement_count = 0
            print(f"\n  KEEP: score {new_score:.4f} (+{improvement:.4f})")
        else:
            action = "revert"
            save_params(params_backup)  # Откатываем
            no_improvement_count += 1
            print(f"\n  REVERT: score {new_score:.4f} (best: {best_score:.4f})")

        # Сохраняем результат
        save_experiment(i, suggestion, new_results, action, new_params)

        # Если 20 итераций без улучшения — предупреждение
        if no_improvement_count >= 20:
            print(f"\n  WARNING: {no_improvement_count} iterations without improvement!")
            print("  Consider restarting with different base parameters.")

    # Финальный отчёт
    print(f"\n{'=' * 60}")
    print("ORCHESTRATOR: Autoresearch complete")
    print(f"  Best score: {best_score:.4f}")
    print(f"  Final params: {json.dumps(load_params(), indent=2)}")
    print(f"{'=' * 60}")

    generate_report(best_score)


def generate_report(best_score):
    """Генерирует REPORT.md с лучшими результатами."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    params = load_params()

    report = f"""# Autoresearch Report
Generated: {datetime.utcnow().isoformat()}

## Best Score: {best_score:.4f}

## Optimized Parameters
```json
{json.dumps(params, indent=2)}
```

## Results
See `results.tsv` for full history.
See `db/experiments.db` for detailed metrics.
"""

    with open(os.path.join(RESULTS_DIR, "REPORT.md"), "w") as f:
        f.write(report)
    print(f"  Report saved to results/REPORT.md")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--skip-data", action="store_true")
    args = parser.parse_args()

    run(max_iterations=args.iterations, skip_data_download=args.skip_data)
