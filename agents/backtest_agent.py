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
import importlib
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.runner import run_backtest, calculate_metrics
from db.db_manager import save_instrument_metrics as db_save_metrics, save_trade_log as db_save_trade_log

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")
CSV_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "csv")
REQUEST_FILE = os.path.join(RUNTIME_DIR, "backtest_request.json")
DONE_FILE = os.path.join(RUNTIME_DIR, "backtest_done.json")


# Проверенные пары (всегда активны) — только GBP_USD показывает стабильный положительный score
CORE_INSTRUMENTS = {
    "GBP_USD", "EUR_GBP", "USD_JPY", "GBP_JPY",
}

# Тестовые пары для ночного режима — только пары с достаточной статистикой
# Ночной режим: пары с отрицательным score для мониторинга
NIGHT_INSTRUMENTS = {
    "EUR_USD", "XAU_USD",
}
# Обратная совместимость
ACTIVE_INSTRUMENTS = CORE_INSTRUMENTS


def is_night_mode():
    """Ночной режим: 00:00-08:00 Kyiv (UTC+2) — расширенный набор инструментов + Opus."""
    from datetime import timezone, timedelta
    kyiv_hour = datetime.now(timezone.utc).hour + 2  # UTC+2 (EET)
    if kyiv_hour >= 24:
        kyiv_hour -= 24
    return 0 <= kyiv_hour < 8


def get_instruments():
    """Определяет инструменты по наличию CSV + режим дня/ночи."""
    active = CORE_INSTRUMENTS | NIGHT_INSTRUMENTS if is_night_mode() else CORE_INSTRUMENTS
    instruments = set()
    if os.path.exists(CSV_DIR):
        for f in os.listdir(CSV_DIR):
            if f.endswith("_H1.csv"):
                inst = f.replace("_H1.csv", "")
                if inst in active:
                    instruments.add(inst)
    mode = "NIGHT (all 12)" if is_night_mode() else "DAY (core 5)"
    print(f"  [Instruments] {mode}: {sorted(instruments)}")
    return sorted(instruments)


def run_single_backtest(args):
    """Запускает бэктест для одного инструмента (для ProcessPoolExecutor)."""
    instrument, params = args
    result = run_backtest(instrument, params)
    return instrument, result


def reload_strategy():
    """Перезагружает модули стратегии для подхвата code changes."""
    import strategy.base_strategy
    import backtest.runner
    importlib.reload(strategy.base_strategy)
    importlib.reload(backtest.runner)
    # Re-import after reload
    global run_backtest, calculate_metrics
    from backtest.runner import run_backtest, calculate_metrics
    print("  [BacktestAgent] Strategy modules reloaded")


def run_parallel_backtest(params, force_reload=False, instruments_override=None):
    """Запускает бэктест по инструментам параллельно."""
    if force_reload:
        reload_strategy()

    instruments = instruments_override if instruments_override else get_instruments()
    if not instruments:
        print("  [BacktestAgent] No instruments found")
        return {}

    print(f"  [BacktestAgent] Running parallel backtest on {len(instruments)} instruments...")

    results = {}
    args_list = [(inst, params) for inst in instruments]

    # Use 'spawn' context to get fresh imports in workers (important for code changes)
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=min(4, len(instruments)), mp_context=ctx) as executor:
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


def save_metrics(results, iteration=None):
    """Сохраняет метрики в БД."""
    for inst, res in results.items():
        metrics = res.get("metrics")
        if metrics:
            db_save_metrics(iteration, inst, metrics)


def classify_session(timestamp):
    """Определяет торговую сессию по UTC времени."""
    if not hasattr(timestamp, "hour"):
        return "unknown"
    h = timestamp.hour
    if 7 <= h < 12:     # London 09:00-14:00 UTC+2 = 07:00-12:00 UTC
        return "london"
    elif 13 <= h < 17:   # New York overlap + US 15:00-19:00 UTC+2 = 13:00-17:00 UTC
        return "new_york"
    elif 1 <= h < 7:     # Asian 03:00-09:00 UTC+2 = 01:00-07:00 UTC
        return "asian"
    else:
        return "off_hours"


def generate_trade_log(results):
    """
    Генерирует trade_log.json с аналитикой для оптимизатора.
    - losing_trades: последние 20 проигрышных сделок
    - win_by_session: WR по сессиям
    - win_by_instrument: WR по инструментам
    - avg_bars_to_stop: среднее баров до стопа
    - fvg_age_distribution: WR по возрасту FVG
    """
    all_trades = []
    for inst, res in results.items():
        trades = res.get("trades", [])
        for t in trades:
            t["instrument"] = inst
            all_trades.append(t)

    if not all_trades:
        return {}

    # 1. Losing trades (последние 20)
    losers = [t for t in all_trades if t.get("pnl_r", 0) < 0]
    losing_trades = []
    for t in losers[-20:]:
        bars_held = t.get("bars_held", 0)
        if not bars_held:
            entry_time = t.get("entry_time")
            exit_time = t.get("exit_time")
            if hasattr(entry_time, "timestamp") and hasattr(exit_time, "timestamp"):
                bars_held = int((exit_time.timestamp() - entry_time.timestamp()) / 180)
        losing_trades.append({
            "instrument": t.get("instrument"),
            "entry_time": str(t.get("entry_time")),
            "direction": t.get("direction"),
            "entry_price": t.get("entry"),
            "sl_price": t.get("sl"),
            "exit_price": t.get("exit"),
            "bars_held": bars_held,
            "exit_reason": t.get("result"),
            "pnl_r": t.get("pnl_r"),
            "mfe_r": t.get("mfe_r", 0),
            "mae_r": t.get("mae_r", 0),
        })

    # 2. Win by session
    session_stats = {}
    for t in all_trades:
        entry_time = t.get("entry_time")
        session = classify_session(entry_time)
        if session not in session_stats:
            session_stats[session] = {"wins": 0, "total": 0}
        session_stats[session]["total"] += 1
        if t.get("pnl_r", 0) > 0:
            session_stats[session]["wins"] += 1

    win_by_session = {}
    for s, stats in session_stats.items():
        win_by_session[s] = {
            "winrate": round(stats["wins"] / stats["total"], 4) if stats["total"] > 0 else 0,
            "total_trades": stats["total"],
            "wins": stats["wins"],
        }

    # 3. Win by instrument
    inst_stats = {}
    for t in all_trades:
        inst = t.get("instrument", "unknown")
        if inst not in inst_stats:
            inst_stats[inst] = {"wins": 0, "total": 0, "pnl_sum": 0}
        inst_stats[inst]["total"] += 1
        inst_stats[inst]["pnl_sum"] += t.get("pnl_r", 0)
        if t.get("pnl_r", 0) > 0:
            inst_stats[inst]["wins"] += 1

    win_by_instrument = {}
    for inst, stats in inst_stats.items():
        win_by_instrument[inst] = {
            "winrate": round(stats["wins"] / stats["total"], 4) if stats["total"] > 0 else 0,
            "total_trades": stats["total"],
            "total_r": round(stats["pnl_sum"], 2),
        }

    # 4. Avg bars to stop (use bars_held from trade if available)
    sl_trades = [t for t in all_trades if t.get("result") == "sl"]
    bars_to_stop = [t.get("bars_held", 0) for t in sl_trades if t.get("bars_held", 0) > 0]
    if not bars_to_stop:
        for t in sl_trades:
            entry_time = t.get("entry_time")
            exit_time = t.get("exit_time")
            if hasattr(entry_time, "timestamp") and hasattr(exit_time, "timestamp"):
                bars_to_stop.append(int((exit_time.timestamp() - entry_time.timestamp()) / 180))

    avg_bars_to_stop = round(sum(bars_to_stop) / len(bars_to_stop), 1) if bars_to_stop else 0

    # 5. Exit reason breakdown with MFE/MAE
    exit_reasons = {}
    for t in all_trades:
        reason = t.get("result", "unknown")
        if reason not in exit_reasons:
            exit_reasons[reason] = {"count": 0, "total_pnl": 0, "mfe_sum": 0, "mae_sum": 0}
        exit_reasons[reason]["count"] += 1
        exit_reasons[reason]["total_pnl"] += t.get("pnl_r", 0)
        exit_reasons[reason]["mfe_sum"] += t.get("mfe_r", 0)
        exit_reasons[reason]["mae_sum"] += t.get("mae_r", 0)
    for reason in exit_reasons:
        c = exit_reasons[reason]["count"]
        exit_reasons[reason]["avg_pnl"] = round(exit_reasons[reason]["total_pnl"] / c, 4) if c > 0 else 0
        exit_reasons[reason]["avg_mfe"] = round(exit_reasons[reason]["mfe_sum"] / c, 4) if c > 0 else 0
        exit_reasons[reason]["avg_mae"] = round(exit_reasons[reason]["mae_sum"] / c, 4) if c > 0 else 0
        del exit_reasons[reason]["mfe_sum"]
        del exit_reasons[reason]["mae_sum"]

    # 6. MFE/MAE summary
    all_mfe = [t.get("mfe_r", 0) for t in all_trades]
    all_mae = [t.get("mae_r", 0) for t in all_trades]
    be_trades = [t for t in all_trades if t.get("result") == "be"]
    be_mfe = [t.get("mfe_r", 0) for t in be_trades]

    trade_log = {
        "total_trades": len(all_trades),
        "overall_winrate": round(len([t for t in all_trades if t.get("pnl_r", 0) > 0]) / len(all_trades), 4),
        "losing_trades": losing_trades,
        "win_by_session": win_by_session,
        "win_by_instrument": win_by_instrument,
        "avg_bars_to_stop": avg_bars_to_stop,
        "exit_reason_breakdown": exit_reasons,
        "mfe_mae_summary": {
            "avg_mfe": round(sum(all_mfe) / len(all_mfe), 4) if all_mfe else 0,
            "avg_mae": round(sum(all_mae) / len(all_mae), 4) if all_mae else 0,
            "be_avg_mfe": round(sum(be_mfe) / len(be_mfe), 4) if be_mfe else 0,
            "be_count": len(be_trades),
        },
    }

    db_save_trade_log(None, trade_log)

    return trade_log


CRYPTO_INSTRUMENTS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"}
FOREX_INSTRUMENTS = {"USD_JPY", "EUR_GBP", "GBP_USD", "GER40", "XAU_USD", "EUR_USD", "GBP_JPY"}

# Кеш последних результатов для частичного пересчёта
_result_cache = {}


def detect_changed_group(request):
    """Определяет какую группу пересчитывать по changed_param."""
    param = request.get("changed_param", "")
    if param.startswith("crypto_overrides."):
        return "crypto"
    elif param.startswith("forex_overrides."):
        return "forex"
    return "all"  # общий параметр — пересчитать всё


def watch():
    """Режим наблюдателя — ждёт запросы на бэктест."""
    global _result_cache
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    print("[BacktestAgent] Watching for requests (with smart cache)...")

    while True:
        if os.path.exists(REQUEST_FILE):
            try:
                with open(REQUEST_FILE) as f:
                    request = json.load(f)

                params = request.get("params", {})
                request_id = request.get("id", "unknown")
                changed_group = detect_changed_group(request)
                print(f"\n[BacktestAgent] Request #{request_id} received (group: {changed_group})")

                # Удаляем запрос
                os.remove(REQUEST_FILE)

                # Определяем какие инструменты пересчитывать
                if changed_group == "all" or not _result_cache:
                    # Пересчитать всё
                    results = run_parallel_backtest(params, force_reload=True)
                    _result_cache = dict(results)
                else:
                    # Частичный пересчёт — только затронутая группа
                    active = get_instruments()
                    if changed_group == "crypto":
                        recalc = [i for i in active if i in CRYPTO_INSTRUMENTS]
                        keep = [i for i in active if i not in CRYPTO_INSTRUMENTS]
                    else:
                        recalc = [i for i in active if i in FOREX_INSTRUMENTS]
                        keep = [i for i in active if i not in FOREX_INSTRUMENTS]

                    print(f"  [Cache] Recalc: {recalc}, Cached: {keep}")
                    # Пересчитываем только нужные
                    partial = run_parallel_backtest(params, force_reload=True, instruments_override=recalc)
                    # Мержим с кешем
                    results = {i: _result_cache[i] for i in keep if i in _result_cache}
                    results.update(partial)
                    _result_cache = dict(results)

                iteration = request.get("iteration")
                save_metrics(results, iteration=iteration)
                generate_trade_log(results)

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
                # Atomic write: write to temp file then rename to prevent partial reads
                tmp_done = DONE_FILE + ".tmp"
                with open(tmp_done, "w") as f:
                    json.dump(done, f, indent=2)
                os.rename(tmp_done, DONE_FILE)

                print(f"[BacktestAgent] Request #{request_id} done. avg_score={avg_score:.4f}")

            except Exception as e:
                print(f"[BacktestAgent] Error: {e}")
                # Пишем ошибку
                tmp_done = DONE_FILE + ".tmp"
                with open(tmp_done, "w") as f:
                    json.dump({"error": str(e)}, f)
                os.rename(tmp_done, DONE_FILE)

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
