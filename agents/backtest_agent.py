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

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")
CSV_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "csv")
REQUEST_FILE = os.path.join(RUNTIME_DIR, "backtest_request.json")
DONE_FILE = os.path.join(RUNTIME_DIR, "backtest_done.json")


# Проверенные пары (всегда активны)
CORE_INSTRUMENTS = {
    "USD_JPY", "BTCUSDT", "EUR_GBP", "GBP_USD",
}

# Тестовые пары для ночного режима — все доступные, дадим второй шанс
NIGHT_INSTRUMENTS = {
    "EUR_USD", "XAU_USD", "NZD_JPY", "ETHUSDT",
    "BNBUSDT", "SOLUSDT", "GBP_JPY", "GER40",
}

# Обратная совместимость
ACTIVE_INSTRUMENTS = CORE_INSTRUMENTS


def is_night_mode():
    """Ночной режим: 00:00-08:00 Kyiv (UTC+3) — расширенный набор инструментов + Opus."""
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
    mode = "NIGHT (all 12)" if is_night_mode() else "DAY (core 4)"
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


def save_metrics(results):
    """Сохраняет метрики в runtime/."""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    for inst, res in results.items():
        metrics = res.get("metrics")
        if metrics:
            path = os.path.join(RUNTIME_DIR, f"metrics_{inst}.json")
            with open(path, "w") as f:
                json.dump(metrics, f, indent=2)


def classify_session(timestamp):
    """Определяет торговую сессию по UTC времени."""
    if not hasattr(timestamp, "hour"):
        return "unknown"
    h = timestamp.hour
    if 6 <= h < 11:     # London 09:00-14:00 UTC+3
        return "london"
    elif 12 <= h < 17:   # New York overlap + US 15:00-20:00 UTC+3
        return "new_york"
    elif 0 <= h < 6:     # Asian 03:00-09:00 UTC+3
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
        entry_time = t.get("entry_time")
        exit_time = t.get("exit_time")
        # Считаем bars_held (приблизительно: разница в минутах / 3)
        bars_held = 0
        if hasattr(entry_time, "timestamp") and hasattr(exit_time, "timestamp"):
            bars_held = int((exit_time.timestamp() - entry_time.timestamp()) / 180)
        losing_trades.append({
            "instrument": t.get("instrument"),
            "entry_time": str(entry_time),
            "direction": t.get("direction"),
            "entry_price": t.get("entry"),
            "sl_price": t.get("sl"),
            "exit_price": t.get("exit"),
            "bars_held": bars_held,
            "exit_reason": t.get("result"),
            "pnl_r": t.get("pnl_r"),
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

    # 4. Avg bars to stop
    sl_trades = [t for t in all_trades if t.get("result") == "sl"]
    bars_to_stop = []
    for t in sl_trades:
        entry_time = t.get("entry_time")
        exit_time = t.get("exit_time")
        if hasattr(entry_time, "timestamp") and hasattr(exit_time, "timestamp"):
            bars = int((exit_time.timestamp() - entry_time.timestamp()) / 180)
            bars_to_stop.append(bars)

    avg_bars_to_stop = round(sum(bars_to_stop) / len(bars_to_stop), 1) if bars_to_stop else 0

    # 5. FVG age distribution (approximate from trade timing)
    # We don't have fvg_age in trades directly, so we report by exit_reason breakdown
    exit_reasons = {}
    for t in all_trades:
        reason = t.get("result", "unknown")
        if reason not in exit_reasons:
            exit_reasons[reason] = {"count": 0, "avg_pnl": 0, "total_pnl": 0}
        exit_reasons[reason]["count"] += 1
        exit_reasons[reason]["total_pnl"] += t.get("pnl_r", 0)
    for reason in exit_reasons:
        c = exit_reasons[reason]["count"]
        exit_reasons[reason]["avg_pnl"] = round(exit_reasons[reason]["total_pnl"] / c, 4) if c > 0 else 0

    trade_log = {
        "total_trades": len(all_trades),
        "overall_winrate": round(len([t for t in all_trades if t.get("pnl_r", 0) > 0]) / len(all_trades), 4),
        "losing_trades": losing_trades,
        "win_by_session": win_by_session,
        "win_by_instrument": win_by_instrument,
        "avg_bars_to_stop": avg_bars_to_stop,
        "exit_reason_breakdown": exit_reasons,
    }

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(os.path.join(RUNTIME_DIR, "trade_log.json"), "w") as f:
        json.dump(trade_log, f, indent=2)

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

                save_metrics(results)
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
