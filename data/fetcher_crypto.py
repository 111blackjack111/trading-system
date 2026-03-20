"""
DataAgent: Crypto fetcher (ccxt / Binance)
Скачивает OHLCV данные для BTC, ETH, SOL, BNB
Таймфреймы: 3m, 15m, 1h
История: 12 месяцев
Public API — ключи не нужны.
"""

import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import ccxt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Крипто инструменты
INSTRUMENTS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
]

# Маппинг таймфреймов: наш формат -> ccxt формат
TIMEFRAMES = {
    "M3": "3m",
    "M15": "15m",
    "H1": "1h",
}

# Binance отдаёт максимум 1000 свечей за запрос
MAX_CANDLES = 1000

CSV_DIR = os.path.join(os.path.dirname(__file__), "csv")


def get_exchange():
    return ccxt.binance({"enableRateLimit": True})


def fetch_candles(exchange, symbol, timeframe_ccxt, from_ts, to_ts):
    """Загружает свечи порциями по MAX_CANDLES."""
    all_candles = []
    current_from = from_ts

    while current_from < to_ts:
        try:
            candles = exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe_ccxt,
                since=current_from,
                limit=MAX_CANDLES,
            )
        except Exception as e:
            print(f"  Error fetching {symbol} {timeframe_ccxt}: {e}")
            break

        if not candles:
            break

        all_candles.extend(candles)

        # Двигаем from к последней свече + 1ms
        current_from = candles[-1][0] + 1

        time.sleep(0.5)

    return all_candles


def candles_to_dataframe(candles):
    """Конвертирует ccxt OHLCV в DataFrame."""
    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.set_index("timestamp")
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def symbol_to_filename(symbol):
    """BTC/USDT -> BTCUSDT"""
    return symbol.replace("/", "")


def fetch_instrument(exchange, symbol, timeframe, months=12):
    """Скачивает данные для одного инструмента и таймфрейма."""
    timeframe_ccxt = TIMEFRAMES[timeframe]
    to_time = datetime.utcnow()
    from_time = to_time - timedelta(days=months * 30)

    from_ts = int(from_time.timestamp() * 1000)
    to_ts = int(to_time.timestamp() * 1000)

    print(f"  Fetching {symbol} {timeframe} from {from_time.date()} to {to_time.date()}...")

    candles = fetch_candles(exchange, symbol, timeframe_ccxt, from_ts, to_ts)
    df = candles_to_dataframe(candles)

    if df.empty:
        print(f"  WARNING: No data for {symbol} {timeframe}")
        return df

    # Сохраняем в CSV
    filename = f"{symbol_to_filename(symbol)}_{timeframe}.csv"
    filepath = os.path.join(CSV_DIR, filename)
    df.to_csv(filepath)
    print(f"  Saved {len(df)} candles to {filename}")

    return df


def fetch_all(months=12):
    """Скачивает все крипто инструменты и таймфреймы."""
    os.makedirs(CSV_DIR, exist_ok=True)
    exchange = get_exchange()

    results = {}
    for symbol in INSTRUMENTS:
        for timeframe in TIMEFRAMES:
            key = f"{symbol_to_filename(symbol)}_{timeframe}"
            df = fetch_instrument(exchange, symbol, timeframe, months)
            results[key] = len(df)
            time.sleep(1)

    print("\n=== Crypto Download Summary ===")
    for key, count in results.items():
        print(f"  {key}: {count} candles")

    return results


if __name__ == "__main__":
    fetch_all()
