"""
Night Runner — прогоняет A/B тесты конфигураций за ночь.

Запуск:
  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... venv/bin/python3 agents/night_runner.py

Тесты:
  A: FVG min_size=0.25, OB=true, NY=false  (ослабленный FVG)
  B: FVG min_size=0.35, OB=false, NY=false  (без OB confluence)
  C: FVG min_size=0.35, OB=true, NY=true    (NY сессия)

Каждый тест: baseline + 20 итераций оптимизации.
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
from agents.orchestrator_v2 import (
    request_backtest, wait_for_backtest, save_experiment,
    init_db, send_telegram, ParamBlacklist
)
from agents.optimizer_agent import suggest_change

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")

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


def run_test(test_name, test_config, iterations):
    """Прогоняет один тест: baseline + N итераций оптимизации."""
    print(f"\n{'='*60}")
    print(f"TEST {test_name}: {test_config['description']}")
    print(f"{'='*60}")

    # Загрузим базовые параметры и применим overrides
    base_params = load_params()
    for k, v in test_config["overrides"].items():
        base_params[k] = v

    # Сохраняем params для backtest_agent
    save_params(base_params)

    # Baseline
    print(f"\n[{test_name}] Baseline backtest...")
    req_id = f"night_{test_name}_baseline"
    request_backtest(base_params, req_id)
    baseline_result = wait_for_backtest(req_id)
    baseline_score = baseline_result.get("avg_score", 0)

    results = {
        "test": test_name,
        "description": test_config["description"],
        "overrides": test_config["overrides"],
        "baseline_score": baseline_score,
        "baseline_results": baseline_result.get("results", {}),
        "best_score": baseline_score,
        "best_params": copy.deepcopy(base_params),
        "iterations": [],
        "total_trades_baseline": sum(
            m.get("total_trades", 0)
            for m in baseline_result.get("results", {}).values()
            if m
        ),
    }

    print(f"  Baseline: score={baseline_score:.4f}, trades={results['total_trades_baseline']}")

    # Итерации оптимизации
    best_score = baseline_score
    best_params = copy.deepcopy(base_params)
    blacklist = ParamBlacklist()
    consecutive_reverts = 0

    for i in range(1, iterations + 1):
        print(f"\n[{test_name}] Iteration {i}/{iterations} (best: {best_score:.4f})")

        current_params = copy.deepcopy(best_params)
        suggestion = suggest_change(current_params, blacklist)

        if not suggestion:
            print("  No suggestion available, skipping")
            results["iterations"].append({"iter": i, "action": "skip", "reason": "no_suggestion"})
            continue

        param_name = suggestion["param"]
        new_value = suggestion["new_value"]
        old_value = current_params.get(param_name, suggestion.get("old_value"))

        # Применяем изменение
        new_params = copy.deepcopy(current_params)
        # Поддержка nested params (forex_overrides.be_trigger_rr)
        if "." in param_name:
            parts = param_name.split(".")
            new_params[parts[0]][parts[1]] = new_value
        else:
            new_params[param_name] = new_value

        # Не позволяем менять overrides теста
        for k, v in test_config["overrides"].items():
            new_params[k] = v

        save_params(new_params)

        req_id = f"night_{test_name}_iter{i}"
        request_backtest(new_params, req_id, changed_param=param_name)
        bt_result = wait_for_backtest(req_id)
        new_score = bt_result.get("avg_score", 0)
        total_trades = sum(
            m.get("total_trades", 0)
            for m in bt_result.get("results", {}).values()
            if m
        )

        if new_score > best_score:
            action = "keep"
            best_score = new_score
            best_params = copy.deepcopy(new_params)
            consecutive_reverts = 0
            print(f"  KEEP: {param_name} {old_value}->{new_value}, score {new_score:.4f} (+{new_score - baseline_score:.4f})")
        else:
            action = "revert"
            save_params(best_params)  # откатываем
            consecutive_reverts += 1
            blacklist.record_revert(param_name, i)
            print(f"  REVERT: {param_name} {old_value}->{new_value}, score {new_score:.4f}")

        results["iterations"].append({
            "iter": i,
            "param": param_name,
            "old_value": old_value,
            "new_value": new_value,
            "score": new_score,
            "trades": total_trades,
            "action": action,
        })

    results["best_score"] = best_score
    results["best_params"] = {k: v for k, v in best_params.items()
                               if not isinstance(v, dict)}  # skip nested for readability
    results["improvement"] = best_score - baseline_score
    results["keeps"] = sum(1 for it in results["iterations"] if it.get("action") == "keep")
    results["reverts"] = sum(1 for it in results["iterations"] if it.get("action") == "revert")

    return results


def main():
    init_db()
    os.makedirs(RESULTS_DIR, exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    all_results = {}

    send_telegram(f"🌙 Night Runner started: {len(TESTS)} tests × {ITERATIONS_PER_TEST} iterations")

    # Сохраняем оригинальные параметры для восстановления
    original_params = load_params()

    try:
        for test_name, test_config in TESTS.items():
            start = time.time()
            result = run_test(test_name, test_config, ITERATIONS_PER_TEST)
            elapsed = (time.time() - start) / 60
            result["elapsed_minutes"] = round(elapsed, 1)
            all_results[test_name] = result

            # Промежуточный Telegram
            send_telegram(
                f"✅ Test {test_name} done ({elapsed:.0f}m)\n"
                f"Score: {result['baseline_score']:.4f} → {result['best_score']:.4f}\n"
                f"Trades: {result['total_trades_baseline']}\n"
                f"Keeps: {result['keeps']}, Reverts: {result['reverts']}"
            )

    except Exception as e:
        send_telegram(f"❌ Night Runner error: {e}")
        raise
    finally:
        # Восстанавливаем оригинальные параметры
        save_params(original_params)
        print("\n[Night Runner] Original params restored.")

    # Сохраняем результаты
    results_file = os.path.join(RESULTS_DIR, f"night_tests_{date_str}.json")
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    print(f"\nResults saved: {results_file}")

    # Финальный отчёт в Telegram
    report = "🏁 Night Runner Complete!\n\n"
    for name, r in all_results.items():
        report += (
            f"<b>{name}</b>: {r['description']}\n"
            f"  Score: {r['baseline_score']:.4f} → {r['best_score']:.4f} ({r['improvement']:+.4f})\n"
            f"  Trades: {r['total_trades_baseline']} | Keeps: {r['keeps']}/{ITERATIONS_PER_TEST}\n\n"
        )
    send_telegram(report)


if __name__ == "__main__":
    main()
