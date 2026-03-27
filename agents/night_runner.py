"""
Night Runner — прогоняет A/B тесты конфигураций за ночь.

Запускает бэктесты НАПРЯМУЮ (без backtest_agent) для полного контроля
над инструментами и параметрами.

Запуск:
  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... venv/bin/python3 -u agents/night_runner.py

Тесты:
  A: FVG min_size=0.25, OB=true, NY=false  (ослабленный FVG)
  B: FVG min_size=0.35, OB=false, NY=false  (без OB confluence)
  C: FVG min_size=0.35, OB=true, NY=true    (NY сессия)

Каждый тест: baseline + 20 итераций оптимизации на ВСЕХ инструментах.
Результаты: results/night_tests_{date}.json + Telegram.
"""

import os
import sys
import json
import time
import copy
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.base_strategy import load_params, save_params
from agents.orchestrator_v2 import init_db, send_telegram, ParamBlacklist
from agents.optimizer_agent import suggest_change

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")

# Все инструменты для тестирования
ALL_INSTRUMENTS = ["GBP_USD", "EUR_GBP", "USD_JPY", "GBP_JPY", "EUR_USD"]

# ============================================================
# Конфигурации тестов
# ============================================================

TESTS = {
    "A_fvg025": {
        "description": "FVG min_size=0.25, OB=true, NY=false",
        "overrides": {
            "fvg_min_size_multiplier": 0.25,
            "ob_confluence": True,
            "ny_session": False,
        }
    },
    "B_no_ob": {
        "description": "FVG min_size=0.35, OB=false, NY=false",
        "overrides": {
            "fvg_min_size_multiplier": 0.35,
            "ob_confluence": False,
            "ny_session": False,
        }
    },
    "C_ny_session": {
        "description": "FVG min_size=0.35, OB=true, NY=true",
        "overrides": {
            "fvg_min_size_multiplier": 0.35,
            "ob_confluence": True,
            "ny_session": True,
        }
    },
}

ITERATIONS_PER_TEST = 20


def _run_single(args):
    """Worker function for parallel backtest."""
    inst, params = args
    from backtest.runner import run_backtest
    return inst, run_backtest(inst, params)


def run_backtest_direct(params):
    """Запускает бэктест напрямую на всех инструментах (параллельно)."""
    save_params(params)

    from multiprocessing import get_context
    ctx = get_context("spawn")
    args_list = [(inst, params) for inst in ALL_INSTRUMENTS]

    results = {}
    scores = []

    with ctx.Pool(processes=2) as pool:
        for inst, data in pool.map(_run_single, args_list):
            metrics = data.get("metrics") if isinstance(data, dict) else None
            if metrics and metrics.get("score") is not None:
                results[inst] = metrics
                scores.append(metrics["score"])

    avg_score = sum(scores) / len(scores) if scores else 0

    return {
        "avg_score": avg_score,
        "results": results,
    }


def run_test(test_name, test_config, iterations):
    """Прогоняет один тест: baseline + N итераций оптимизации."""
    print(f"\n{'='*60}")
    print(f"TEST {test_name}: {test_config['description']}")
    print(f"{'='*60}")

    # Загрузим базовые параметры и применим overrides
    base_params = load_params()
    for k, v in test_config["overrides"].items():
        base_params[k] = v

    # Baseline
    print(f"\n[{test_name}] Baseline backtest...")
    baseline_result = run_backtest_direct(base_params)
    baseline_score = baseline_result.get("avg_score", 0)

    # Per-instrument baseline details
    baseline_details = {}
    total_trades = 0
    for inst, m in baseline_result.get("results", {}).items():
        trades = m.get("total_trades", 0)
        total_trades += trades
        baseline_details[inst] = {
            "score": round(m.get("score", 0), 4),
            "trades": trades,
            "winrate": round(m.get("winrate", 0), 4),
            "profit_factor": round(m.get("profit_factor", 0), 4),
            "total_r": round(m.get("total_r", 0), 2),
        }

    results = {
        "test": test_name,
        "description": test_config["description"],
        "overrides": test_config["overrides"],
        "baseline_score": baseline_score,
        "baseline_per_instrument": baseline_details,
        "best_score": baseline_score,
        "best_params": copy.deepcopy(base_params),
        "iterations": [],
        "total_trades_baseline": total_trades,
    }

    print(f"  Baseline: score={baseline_score:.4f}, trades={total_trades}")
    for inst, d in sorted(baseline_details.items()):
        print(f"    {inst}: score={d['score']}, trades={d['trades']}, WR={d['winrate']}")

    # Итерации оптимизации
    best_score = baseline_score
    best_params = copy.deepcopy(base_params)
    blacklist = ParamBlacklist()
    # Сбрасываем cooldown — night_runner тесты независимы от orchestrator
    blacklist.cooldown.clear()
    blacklist.revert_counts.clear()

    for i in range(1, iterations + 1):
        print(f"\n[{test_name}] Iteration {i}/{iterations} (best: {best_score:.4f})")

        current_params = copy.deepcopy(best_params)
        blocked = {p for p, until_iter in blacklist.cooldown.items() if i < until_iter}
        try:
            suggestion = suggest_change(current_params, blacklisted_params=blocked)
        except Exception as e:
            print(f"  Optimizer error: {e}, skipping")
            results["iterations"].append({"iter": i, "action": "skip", "reason": f"error: {e}"})
            continue

        if not suggestion:
            print("  No suggestion available, skipping")
            results["iterations"].append({"iter": i, "action": "skip", "reason": "no_suggestion"})
            continue

        param_name = suggestion["param"]
        new_value = suggestion["new_value"]
        old_value = current_params.get(param_name, suggestion.get("old_value"))

        # Применяем изменение
        new_params = copy.deepcopy(current_params)
        if "." in param_name:
            parts = param_name.split(".")
            new_params[parts[0]][parts[1]] = new_value
        else:
            new_params[param_name] = new_value

        # Не позволяем менять overrides теста
        for k, v in test_config["overrides"].items():
            new_params[k] = v

        try:
            bt_result = run_backtest_direct(new_params)
        except Exception as e:
            print(f"  Backtest error: {e}, skipping")
            save_params(best_params)
            results["iterations"].append({"iter": i, "action": "skip", "reason": f"bt_error: {e}"})
            continue

        new_score = bt_result.get("avg_score", 0)
        iter_trades = sum(
            m.get("total_trades", 0)
            for m in bt_result.get("results", {}).values()
        )

        if new_score > best_score:
            action = "keep"
            best_score = new_score
            best_params = copy.deepcopy(new_params)
            print(f"  KEEP: {param_name} {old_value}->{new_value}, score {new_score:.4f} (+{new_score - baseline_score:.4f}), trades={iter_trades}")
        else:
            action = "revert"
            save_params(best_params)
            blacklist.record_revert(param_name, i)
            print(f"  REVERT: {param_name} {old_value}->{new_value}, score {new_score:.4f}, trades={iter_trades}")

        results["iterations"].append({
            "iter": i,
            "param": param_name,
            "old_value": old_value,
            "new_value": new_value if not isinstance(new_value, float) else round(new_value, 6),
            "score": round(new_score, 4),
            "trades": iter_trades,
            "action": action,
        })

    # Финальный бэктест с лучшими параметрами — детальные результаты
    print(f"\n[{test_name}] Final backtest with best params...")
    final_result = run_backtest_direct(best_params)
    final_details = {}
    final_trades = 0
    for inst, m in final_result.get("results", {}).items():
        trades = m.get("total_trades", 0)
        final_trades += trades
        final_details[inst] = {
            "score": round(m.get("score", 0), 4),
            "trades": trades,
            "winrate": round(m.get("winrate", 0), 4),
            "profit_factor": round(m.get("profit_factor", 0), 4),
            "total_r": round(m.get("total_r", 0), 2),
        }

    results["best_score"] = best_score
    results["best_per_instrument"] = final_details
    results["total_trades_best"] = final_trades
    results["best_params"] = {k: v for k, v in best_params.items()
                               if not isinstance(v, dict)}
    results["best_params_overrides"] = {
        k: best_params.get(k) for k in ["crypto_overrides", "forex_overrides"]
        if k in best_params
    }
    results["improvement"] = best_score - baseline_score
    results["keeps"] = sum(1 for it in results["iterations"] if it.get("action") == "keep")
    results["reverts"] = sum(1 for it in results["iterations"] if it.get("action") == "revert")

    print(f"\n  Final: score={best_score:.4f}, trades={final_trades}")
    for inst, d in sorted(final_details.items()):
        print(f"    {inst}: score={d['score']}, trades={d['trades']}, WR={d['winrate']}")

    return results


def main():
    init_db()
    os.makedirs(RESULTS_DIR, exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    all_results = {}

    send_telegram(f"Night Runner started: {len(TESTS)} tests x {ITERATIONS_PER_TEST} iterations\nInstruments: {', '.join(ALL_INSTRUMENTS)}")

    # Сохраняем оригинальные параметры для восстановления
    original_params = load_params()

    try:
        for test_name, test_config in TESTS.items():
            start = time.time()
            result = run_test(test_name, test_config, ITERATIONS_PER_TEST)
            elapsed = (time.time() - start) / 60
            result["elapsed_minutes"] = round(elapsed, 1)
            all_results[test_name] = result

            # Per-instrument summary for telegram
            inst_summary = ""
            for inst, d in sorted(result.get("best_per_instrument", {}).items()):
                emoji = "+" if d["score"] > 0 else "-"
                inst_summary += f"  {inst}: {d['score']:+.2f} ({d['trades']}tr, {d['winrate']:.0%}WR)\n"

            send_telegram(
                f"Test {test_name} done ({elapsed:.0f}m)\n"
                f"{result['description']}\n"
                f"Score: {result['baseline_score']:.4f} -> {result['best_score']:.4f}\n"
                f"Trades: {result['total_trades_baseline']} -> {result.get('total_trades_best', '?')}\n"
                f"Keeps: {result['keeps']}, Reverts: {result['reverts']}\n\n"
                f"{inst_summary}"
            )

            # Сохраняем промежуточный результат
            interim_file = os.path.join(RESULTS_DIR, f"night_tests_{date_str}.json")
            with open(interim_file, "w") as f:
                json.dump(all_results, f, indent=2, default=str)

    except Exception as e:
        import traceback
        send_telegram(f"Night Runner error: {e}\n{traceback.format_exc()[-500:]}")
        raise
    finally:
        # Восстанавливаем оригинальные параметры
        save_params(original_params)
        print("\n[Night Runner] Original params restored.")

    # Сохраняем финальные результаты
    results_file = os.path.join(RESULTS_DIR, f"night_tests_{date_str}.json")
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    print(f"\nResults saved: {results_file}")

    # Финальный отчёт в Telegram
    report = "Night Runner Complete!\n\n"
    for name, r in all_results.items():
        report += (
            f"<b>{name}</b>: {r['description']}\n"
            f"  Score: {r['baseline_score']:.4f} -> {r['best_score']:.4f} ({r['improvement']:+.4f})\n"
            f"  Trades: {r['total_trades_baseline']} -> {r.get('total_trades_best', '?')}\n"
            f"  Keeps: {r['keeps']}/{ITERATIONS_PER_TEST}\n\n"
        )
    send_telegram(report)


if __name__ == "__main__":
    main()
