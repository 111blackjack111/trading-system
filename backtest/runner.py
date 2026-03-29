"""
Backtest Runner — движок бэктеста.
Запускает SMC стратегию на исторических данных,
считает метрики и возвращает результат.
"""

import os
import sys
import json
import math
import hashlib
import pickle

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.base_strategy import (
    load_params,
    generate_signals,
    compute_trade_levels,
    simulate_trades,
)

CSV_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "csv")
RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")
SIGNAL_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime", "signal_cache")

# Entry-only params — если меняется один из них, кеш инвалидируется
ENTRY_PARAMS = {
    "bos_swing_length", "fvg_min_size_multiplier", "fvg_entry_depth",
    "fvg_max_age_bars", "ob_lookback", "ob_confluence", "sweep_filter",
    "choch_filter", "session_filter", "silver_bullet_only", "ny_session",
    "ny_instruments", "volatility_filter", "min_atr_percentile",
    "confirmation_candle_pct", "asian_filter_forex", "monday_filter",
    "crypto_hours_filter", "news_filter", "news_minutes_before", "news_minutes_after",
}


def _entry_params_hash(params, instrument):
    """Хеш entry-параметров для ключа кеша."""
    from strategy.base_strategy import is_crypto_instrument
    is_crypto = is_crypto_instrument(instrument)

    # Apply overrides same way as generate_signals does
    p = params.copy()
    if is_crypto and "crypto_overrides" in p:
        p.update(p["crypto_overrides"])
    elif not is_crypto and "forex_overrides" in p:
        p.update(p["forex_overrides"])

    # Extract only entry params
    entry_vals = {k: p.get(k) for k in sorted(ENTRY_PARAMS) if k in p}
    key_str = json.dumps(entry_vals, sort_keys=True, default=str)
    return hashlib.md5(key_str.encode()).hexdigest()[:12]


def _cache_path(instrument, params_hash):
    return os.path.join(SIGNAL_CACHE_DIR, f"{instrument}_{params_hash}.pkl")


def _load_cached_signals(instrument, params_hash):
    """Загрузить кешированные сырые сигналы."""
    path = _cache_path(instrument, params_hash)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None
    return None


def _save_cached_signals(instrument, params_hash, raw_signals):
    """Сохранить сырые сигналы в кеш."""
    os.makedirs(SIGNAL_CACHE_DIR, exist_ok=True)
    path = _cache_path(instrument, params_hash)
    with open(path, "wb") as f:
        pickle.dump(raw_signals, f)


def load_data(instrument, timeframe):
    """Загружает CSV файл с данными, ограничивая последними 4 годами."""
    filepath = os.path.join(CSV_DIR, f"{instrument}_{timeframe}.csv")
    if not os.path.exists(filepath):
        return None
    df = pd.read_csv(filepath, index_col="timestamp", parse_dates=True)
    # Limit to last 2 years to keep backtests fast (~30 trades/year on GBP_USD)
    # 2 years = ~60-100 trades, enough for statistical significance
    cutoff = pd.Timestamp.now() - pd.DateOffset(years=2)
    df = df[df.index >= cutoff]
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
    # Capped to [-3, 3] — with <200 trades, extreme values are noise
    if len(pnls) > 1:
        mean_pnl = np.mean(pnls)
        std_pnl = np.std(pnls, ddof=1)
        sharpe = (mean_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0
        sharpe = max(-3.0, min(3.0, sharpe))
    else:
        sharpe = 0

    # Max drawdown (в R-множителях, абсолютный — не нормализованный к пику)
    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    drawdown = (peak - cumulative)
    max_drawdown_abs = float(np.max(drawdown)) if len(drawdown) > 0 else 0
    # Нормализуем к количеству сделок (dd per 100 trades) — стабильная метрика
    max_dd_pct = max_drawdown_abs / max(total, 1) * 100

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
    Composite score v4 — stable & quality-focused.
    max_drawdown here is dd_per_100_trades (absolute R normalized by trade count).
    sharpe is capped to [-3, 3].
    No double penalty. Single clear formula.
    """
    if total_trades < 5:
        return -999

    quality = winrate * profit_factor  # WR × PF: core metric

    # Single drawdown penalty: dd_per_100 above 5R is penalized
    dd_penalty = max(0, max_drawdown - 5.0) * 0.1

    score = (sharpe * 0.25
             + quality * 0.45
             + 0.1
             - dd_penalty)

    return score


def run_backtest(instrument, params=None, use_cache=True):
    """
    Запускает бэктест для одного инструмента.
    С кешированием сырых сигналов: если entry-параметры не изменились,
    пропускает дорогой generate_signals() и только пересчитывает exit-уровни.

    Args:
        instrument: название (e.g. "GBP_USD", "BTCUSDT")
        params: dict параметров или None (загрузит из params.json)
        use_cache: использовать кеш сигналов (default True)

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

    # Проверяем кеш сырых сигналов
    params_hash = _entry_params_hash(params, instrument)
    raw_signals = None

    if use_cache:
        raw_signals = _load_cached_signals(instrument, params_hash)

    if raw_signals is not None:
        print(f"  Running backtest: {instrument} (CACHED, {len(raw_signals)} raw signals)")
    else:
        print(f"  Running backtest: {instrument} (H1: {len(df_h1)}, M3: {len(df_m3)} candles)")
        # Генерируем сырые сигналы (дорогой шаг)
        raw_signals = generate_signals(df_h1, df_m3, params, instrument=instrument)
        print(f"  Raw signals found: {len(raw_signals)}")
        # Сохраняем в кеш
        if use_cache:
            _save_cached_signals(instrument, params_hash, raw_signals)

    # Вычисляем exit-уровни (дешёвый шаг)
    signals = compute_trade_levels(raw_signals, params, instrument=instrument)
    print(f"  Signals with valid levels: {len(signals)}")

    # Симулируем сделки
    trades = simulate_trades(signals, df_m3, params, instrument=instrument)
    print(f"  Trades executed: {len(trades)}")

    # Считаем метрики
    metrics = calculate_metrics(trades)

    return {
        "instrument": instrument,
        "metrics": metrics,
        "trades": trades,
    }


def clear_signal_cache():
    """Очищает весь кеш сигналов."""
    if os.path.exists(SIGNAL_CACHE_DIR):
        import shutil
        shutil.rmtree(SIGNAL_CACHE_DIR)
        print("  [Cache] Signal cache cleared")


# Active instruments — only these are used for optimization
CORE_INSTRUMENTS = {"GBP_USD", "EUR_GBP", "USD_JPY", "GBP_JPY", "EUR_USD"}


def run_all(params=None, instruments_override=None):
    """Запускает бэктест по активным инструментам."""
    if params is None:
        params = load_params()

    # Use override or CORE_INSTRUMENTS (not all CSV files!)
    if instruments_override:
        instruments = set(instruments_override)
    else:
        instruments = set()
        if os.path.exists(CSV_DIR):
            for f in os.listdir(CSV_DIR):
                if f.endswith("_H1.csv"):
                    inst = f.replace("_H1.csv", "")
                    if inst in CORE_INSTRUMENTS:
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
