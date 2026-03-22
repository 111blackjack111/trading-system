"""
OrchestratorAgent v3 — автономный координатор с самозащитой.

Три механизма автономности:
1. Blacklist: параметр с 2+ revert → не трогать 20 итераций
2. Stuck detector: 7 ревертов подряд → алерт + расширить диапазоны
3. Conflict detector: WR↑ но score↓ в 10x → аномалия, пропустить
"""

import os
import sys
import json
import time
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.base_strategy import load_params, save_params
from agents.optimizer_agent import suggest_change, PARAM_RANGES
from agents.analyst_agent import run_analysis, apply_recommendations
from agents.data_agent import run as run_data_agent

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")
DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")
DB_PATH = os.path.join(DB_DIR, "experiments.db")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")
RESULTS_TSV = os.path.join(RESULTS_DIR, "results.tsv")

REQUEST_FILE = os.path.join(RUNTIME_DIR, "backtest_request.json")
DONE_FILE = os.path.join(RUNTIME_DIR, "backtest_done.json")

TIMEOUT_BACKTEST = 1200  # 20 минут макс на бэктест

# ============================================================
# Telegram helper
# ============================================================

def send_telegram(message):
    """Отправляет сообщение в Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


# ============================================================
# Blacklist — параметры с 2+ revert не трогать 20 итераций
# ============================================================

class ParamBlacklist:
    def __init__(self):
        self.revert_counts = {}  # param -> count
        self.cooldown = {}       # param -> iteration when unblocked

    def record_revert(self, param, current_iter):
        self.revert_counts[param] = self.revert_counts.get(param, 0) + 1
        if self.revert_counts[param] >= 2:
            self.cooldown[param] = current_iter + 20
            print(f"  [Blacklist] {param} blocked for 20 iterations (reverts: {self.revert_counts[param]})")

    def record_keep(self, param):
        # Reset on success
        self.revert_counts[param] = 0
        if param in self.cooldown:
            del self.cooldown[param]

    def is_blocked(self, param, current_iter):
        if param in self.cooldown and current_iter < self.cooldown[param]:
            return True
        elif param in self.cooldown and current_iter >= self.cooldown[param]:
            # Cooldown expired — reset
            del self.cooldown[param]
            self.revert_counts[param] = 0
        return False


# ============================================================
# Stuck detector — 7 ревертов подряд → алерт + расширить диапазоны
# ============================================================

def expand_param_ranges(factor=1.2):
    """Расширяет диапазоны параметров на factor."""
    for param in PARAM_RANGES:
        low, high = PARAM_RANGES[param]
        center = (low + high) / 2
        half_range = (high - low) / 2 * factor
        PARAM_RANGES[param] = (round(center - half_range, 2), round(center + half_range, 2))
    print(f"  [StuckDetector] Param ranges expanded by {factor}x")


# ============================================================
# Conflict detector — WR↑ но score↓ в 10x → аномалия
# ============================================================

def is_anomaly(new_score, best_score, new_wr, baseline_wr):
    """WR выше но score упал в 10+ раз — аномалия."""
    if best_score <= 0:
        return False
    if new_wr > baseline_wr and new_score < 0:
        score_drop = abs(new_score) / max(abs(best_score), 0.01)
        if score_drop > 10:
            return True
    return False


# ============================================================
# DB & File operations
# ============================================================

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
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    if os.path.exists(DONE_FILE):
        os.remove(DONE_FILE)
    request = {"id": request_id, "params": params, "timestamp": time.time()}
    with open(REQUEST_FILE, "w") as f:
        json.dump(request, f, indent=2)
    print(f"  [Orchestrator] Backtest request #{request_id} sent")


def wait_for_backtest(request_id):
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
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()

    metrics_all = backtest_result.get("results", {})
    scores, winrates, pfs = [], [], []
    best_score, best_inst, total_trades = -float("inf"), "", 0

    for inst, m in metrics_all.items():
        if m and m.get("score") is not None:
            scores.append(m["score"])
            total_trades += m.get("total_trades", 0)
            winrates.append(m.get("winrate", 0))
            pfs.append(m.get("profit_factor", 0))
            if m["score"] > best_score:
                best_score = m["score"]
                best_inst = inst

    if scores:
        avg_score = sum(scores) / len(scores)
    else:
        avg_score = backtest_result.get("avg_score", 0)
    if best_score == -float("inf"):
        best_score = 0

    avg_wr = round(sum(winrates) / len(winrates), 4) if winrates else 0
    avg_pf = round(sum(pfs) / len(pfs), 4) if pfs else 0

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
        round(avg_score, 4), round(best_score, 4),
        best_inst, total_trades, avg_wr, avg_pf,
        action, suggestion.get("reasoning", ""),
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
                f"{best_inst}\t{total_trades}\t{avg_wr}\t{avg_pf}\t{action}\n")


# ============================================================
# Main loop
# ============================================================

def run(max_iterations=100, skip_data_download=False):
    init_db()
    blacklist = ParamBlacklist()

    print("=" * 60)
    print(f"ORCHESTRATOR v3: Autoresearch ({max_iterations} iterations)")
    print("  Blacklist: ON | Stuck detector: ON | Conflict detector: ON")
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

    # Get baseline WR for conflict detection
    baseline_wr = 0
    for m in baseline_result.get("results", {}).values():
        if m and m.get("winrate"):
            baseline_wr = max(baseline_wr, m.get("winrate", 0))

    save_experiment(0, {"param": "baseline", "reasoning": "Initial baseline"}, baseline_result, "baseline", params)
    print(f"\n  Baseline avg_score: {baseline_score:.4f}")

    best_score = baseline_score
    no_improvement_count = 0
    consecutive_reverts = 0
    ranges_expanded = False

    # Шаг 2: Итерации
    for i in range(1, max_iterations + 1):
        print(f"\n{'=' * 60}")
        print(f"[Iteration {i}/{max_iterations}] (reverts: {consecutive_reverts}, best: {best_score:.4f})")
        print(f"{'=' * 60}")

        params_backup = load_params()

        # Optimizer
        print("\n  [Optimizer] Getting suggestion...")
        try:
            suggestion = suggest_change(params_backup)
        except Exception as e:
            print(f"  Optimizer error: {e}")
            save_experiment(i, {"param": f"error: {e}", "old_value": 0, "new_value": 0, "reasoning": str(e)},
                           {"avg_score": 0, "results": {}}, "error", params_backup)
            continue

        param_name = suggestion.get("param", "")

        # === BLACKLIST CHECK ===
        if blacklist.is_blocked(param_name, i):
            print(f"  [Blacklist] {param_name} is blocked — skipping")
            save_experiment(i, suggestion, {"avg_score": 0, "results": {}}, "blacklisted", params_backup)
            continue

        # Применяем
        change_type = suggestion.get("type", "param_change")
        strategy_backup = None

        if change_type == "code_change":
            strategy_path = os.path.join(os.path.dirname(__file__), "..", "strategy", "base_strategy.py")
            with open(strategy_path) as f:
                strategy_backup = f.read()
            old_code = suggestion.get("old_code", "")
            new_code = suggestion.get("new_code", "")
            if old_code and new_code and old_code in strategy_backup:
                new_strategy = strategy_backup.replace(old_code, new_code, 1)
                with open(strategy_path, "w") as f:
                    f.write(new_strategy)
                print(f"  [Orchestrator] Applied code change: {suggestion.get('change_description', 'N/A')}")
                new_params = params_backup.copy()
            else:
                print(f"  [Orchestrator] Code change failed — old_code not found")
                save_experiment(i, suggestion, {"avg_score": 0, "results": {}}, "skip_code_fail", params_backup)
                continue
        else:
            new_params = params_backup.copy()
            if "." in param_name:
                group, key = param_name.split(".", 1)
                if group not in new_params:
                    new_params[group] = {}
                new_params[group][key] = suggestion["new_value"]
            else:
                new_params[param_name] = suggestion["new_value"]
            save_params(new_params)

        # Backtest
        print("\n  [Backtest] Requesting parallel backtest...")
        request_id = f"iter_{i}"
        request_backtest(new_params, request_id)
        bt_result = wait_for_backtest(request_id)

        new_score = bt_result.get("avg_score", 0)
        total_trades_new = sum(
            m.get("total_trades", 0) for m in bt_result.get("results", {}).values() if m
        )
        new_wr = max(
            (m.get("winrate", 0) for m in bt_result.get("results", {}).values() if m),
            default=0,
        )

        # === CONFLICT DETECTOR ===
        if is_anomaly(new_score, best_score, new_wr, baseline_wr):
            print(f"\n  ANOMALY: WR↑ but score dropped {abs(new_score/max(abs(best_score),0.01)):.0f}x — skipping")
            save_params(params_backup)
            save_experiment(i, suggestion, bt_result, "anomaly", new_params)
            blacklist.record_revert(param_name, i)
            continue

        # Keep / Revert
        if new_score > best_score and total_trades_new >= 30 and new_score != 0:
            action = "keep"
            improvement = new_score - best_score
            best_score = new_score
            no_improvement_count = 0
            consecutive_reverts = 0
            blacklist.record_keep(param_name)
            print(f"\n  KEEP: score {new_score:.4f} (+{improvement:.4f})")
        else:
            action = "revert"
            if change_type == "code_change" and strategy_backup:
                strategy_path = os.path.join(os.path.dirname(__file__), "..", "strategy", "base_strategy.py")
                with open(strategy_path, "w") as f:
                    f.write(strategy_backup)
                print(f"\n  REVERT CODE: score {new_score:.4f} (best: {best_score:.4f})")
            else:
                save_params(params_backup)
                print(f"\n  REVERT: score {new_score:.4f} (best: {best_score:.4f})")
            no_improvement_count += 1
            consecutive_reverts += 1
            blacklist.record_revert(param_name, i)

        save_experiment(i, suggestion, bt_result, action, new_params)

        # === STUCK DETECTOR ===
        if consecutive_reverts >= 7 and not ranges_expanded:
            print(f"\n  [StuckDetector] {consecutive_reverts} reverts in a row!")
            expand_param_ranges(1.2)
            ranges_expanded = True
            send_telegram(
                f"⚠️ <b>Stuck detected!</b>\n"
                f"{consecutive_reverts} reverts подряд (iter {i})\n"
                f"Диапазоны расширены на 20%\n"
                f"Best score: {best_score:.4f}"
            )

        if consecutive_reverts >= 15:
            send_telegram(
                f"🔴 <b>Система застряла!</b>\n"
                f"{consecutive_reverts} reverts подряд\n"
                f"Score: {best_score:.4f}\n"
                f"Нужно вмешательство CEO"
            )

        if no_improvement_count >= 20:
            print(f"\n  WARNING: {no_improvement_count} iterations without improvement!")

        # === ANALYST AGENT (каждые 10 итераций) ===
        if i % 10 == 0:
            print(f"\n  [Analyst] Running meta-analysis (every 10 iterations)...")
            try:
                bl_info = ", ".join(f"{p}(until iter {v})" for p, v in blacklist.cooldown.items()) or "none"
                report = run_analysis(consecutive_reverts, bl_info)
                if report:
                    applied = apply_recommendations(report, PARAM_RANGES)
                    if applied:
                        print(f"  [Analyst] Applied: {applied}")
                        # Reset stuck detector if analyst made changes
                        if ranges_expanded and any("Range" in a for a in applied):
                            ranges_expanded = False
            except Exception as e:
                print(f"  [Analyst] Error: {e}")

    # Финал
    print(f"\n{'=' * 60}")
    print(f"ORCHESTRATOR v3: Complete. Best score: {best_score:.4f}")
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
