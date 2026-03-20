"""
OrchestratorAgent v2 — координирует независимые агенты через файлы.

Агенты работают в отдельных TMUX сессиях:
- BacktestAgent: слушает runtime/backtest_request.json
- OptimizerAgent: вызывается напрямую (быстрый API запрос)
- ImpulseAgent: работает полностью независимо
- DataAgent: вызывается один раз в начале

Orchestrator пишет запросы и читает результаты.
"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.base_strategy import load_params, save_params
from agents.optimizer_agent import suggest_change
from agents.data_agent import run as run_data_agent

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")
DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")
DB_PATH = os.path.join(DB_DIR, "experiments.db")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")
RESULTS_TSV = os.path.join(RESULTS_DIR, "results.tsv")

REQUEST_FILE = os.path.join(RUNTIME_DIR, "backtest_request.json")
DONE_FILE = os.path.join(RUNTIME_DIR, "backtest_done.json")

TIMEOUT_BACKTEST = 600  # 10 минут макс на бэктест


def init_db():
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


def request_backtest(params, request_id):
    """Отправляет запрос BacktestAgent через файл."""
    os.makedirs(RUNTIME_DIR, exist_ok=True)

    # Очищаем предыдущий результат
    if os.path.exists(DONE_FILE):
        os.remove(DONE_FILE)

    request = {
        "id": request_id,
        "params": params,
        "timestamp": time.time(),
    }
    with open(REQUEST_FILE, "w") as f:
        json.dump(request, f, indent=2)

    print(f"  [Orchestrator] Backtest request #{request_id} sent")


def wait_for_backtest(request_id):
    """Ждёт результат от BacktestAgent."""
    start = time.time()
    while time.time() - start < TIMEOUT_BACKTEST:
        if os.path.exists(DONE_FILE):
            with open(DONE_FILE) as f:
                result = json.load(f)

            if result.get("id") == request_id or "error" in result:
                os.remove(DONE_FILE)
                return result

        time.sleep(2)

    print("  [Orchestrator] WARNING: Backtest timeout!")
    return {"error": "timeout", "avg_score": 0, "results": {}}


def save_experiment(iteration, suggestion, backtest_result, action, params):
    """Сохраняет итерацию в БД и TSV."""
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()

    metrics_all = backtest_result.get("results", {})
    scores, winrates, pfs = [], [], []
    best_score, best_inst, total_trades = 0, "", 0

    for inst, m in metrics_all.items():
        if m and m.get("score") is not None:
            scores.append(m["score"])
            total_trades += m.get("total_trades", 0)
            winrates.append(m.get("winrate", 0))
            pfs.append(m.get("profit_factor", 0))
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
        iteration, now,
        suggestion.get("param", "baseline"),
        suggestion.get("old_value", 0),
        suggestion.get("new_value", 0),
        round(avg_score, 4),
        round(best_score, 4),
        best_inst, total_trades,
        round(sum(winrates) / len(winrates), 4) if winrates else 0,
        round(sum(pfs) / len(pfs), 4) if pfs else 0,
        action,
        suggestion.get("reasoning", ""),
        json.dumps(params),
    ))
    conn.commit()
    conn.close()

    # TSV
    os.makedirs(RESULTS_DIR, exist_ok=True)
    header_needed = not os.path.exists(RESULTS_TSV)
    with open(RESULTS_TSV, "a") as f:
        if header_needed:
            f.write("iteration\ttimestamp\tparam\told_val\tnew_val\tavg_score\tbest_score\tbest_inst\ttrades\twinrate\tpf\taction\n")
        f.write(f"{iteration}\t{now}\t"
                f"{suggestion.get('param', 'baseline')}\t{suggestion.get('old_value', 0)}\t"
                f"{suggestion.get('new_value', 0)}\t{round(avg_score, 4)}\t{round(best_score, 4)}\t"
                f"{best_inst}\t{total_trades}\t"
                f"{round(sum(winrates) / len(winrates), 4) if winrates else 0}\t"
                f"{round(sum(pfs) / len(pfs), 4) if pfs else 0}\t{action}\n")


def run(max_iterations=100, skip_data_download=False):
    """Главный цикл — координирует агентов."""
    init_db()

    print("=" * 60)
    print(f"ORCHESTRATOR v2: Starting autoresearch ({max_iterations} iterations)")
    print("  Using independent BacktestAgent (parallel)")
    print("=" * 60)

    # Шаг 0: Данные
    if not skip_data_download:
        print("\n[Step 0] Downloading data...")
        run_data_agent(months=12)

    # Шаг 1: Baseline
    print("\n[Iteration 0] Baseline backtest...")
    params = load_params()
    request_backtest(params, "baseline")
    baseline_result = wait_for_backtest("baseline")
    baseline_score = baseline_result.get("avg_score", 0)

    save_experiment(0, {"param": "baseline", "reasoning": "Initial baseline"}, baseline_result, "baseline", params)
    print(f"\n  Baseline avg_score: {baseline_score:.4f}")

    best_score = baseline_score
    no_improvement_count = 0

    # Шаг 2: Итерации
    for i in range(1, max_iterations + 1):
        print(f"\n{'=' * 60}")
        print(f"[Iteration {i}/{max_iterations}]")
        print(f"{'=' * 60}")

        params_backup = load_params()

        # Optimizer (Claude API)
        print("\n  [Optimizer] Getting suggestion...")
        try:
            suggestion = suggest_change(params_backup)
        except Exception as e:
            print(f"  Optimizer error: {e}")
            continue

        # Применяем
        new_params = params_backup.copy()
        new_params[suggestion["param"]] = suggestion["new_value"]
        save_params(new_params)

        # Backtest (через агента — параллельно)
        print("\n  [Backtest] Requesting parallel backtest...")
        request_id = f"iter_{i}"
        request_backtest(new_params, request_id)
        bt_result = wait_for_backtest(request_id)

        new_score = bt_result.get("avg_score", 0)

        # Keep / Revert
        if new_score > best_score:
            action = "keep"
            improvement = new_score - best_score
            best_score = new_score
            no_improvement_count = 0
            print(f"\n  KEEP: score {new_score:.4f} (+{improvement:.4f})")
        else:
            action = "revert"
            save_params(params_backup)
            no_improvement_count += 1
            print(f"\n  REVERT: score {new_score:.4f} (best: {best_score:.4f})")

        save_experiment(i, suggestion, bt_result, action, new_params)

        if no_improvement_count >= 20:
            print(f"\n  WARNING: {no_improvement_count} iterations without improvement!")

    # Финал
    print(f"\n{'=' * 60}")
    print(f"ORCHESTRATOR v2: Complete. Best score: {best_score:.4f}")
    print(f"{'=' * 60}")

    generate_report(best_score)


def generate_report(best_score):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    params = load_params()
    now = datetime.now(timezone.utc).isoformat()
    report = f"""# Autoresearch Report
Generated: {now}

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
    print("  Report saved to results/REPORT.md")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--skip-data", action="store_true")
    args = parser.parse_args()

    run(max_iterations=args.iterations, skip_data_download=args.skip_data)
