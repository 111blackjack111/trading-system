"""
DataAgent: OANDA fetcher
Скачивает OHLCV данные для форекс пар, XAU/USD, GER40
Таймфреймы: M3, M15, H1
История: 12 месяцев
"""

import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import oandapyV20
import oandapyV20.endpoints.instruments as instruments

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

# OANDA инструменты
INSTRUMENTS = [
    "GBP_USD",
    "EUR_USD",
    "GBP_JPY",
    "USD_JPY",
    "EUR_GBP",
    "XAU_USD",
    "DE30_EUR",  # GER40/DAX в OANDA
]

# Маппинг таймфреймов OANDA
TIMEFRAMES = {
    "M3": "M3",
    "M15": "M15",
    "H1": "H1",
}

# OANDA отдаёт максимум 5000 свечей за запрос
MAX_CANDLES = 5000

CSV_DIR = os.path.join(os.path.dirname(__file__), "csv")


def get_client():
    if not config.OANDA_API_KEY:
        raise ValueError("OANDA_API_KEY not set in environment")
    environment = "practice" if config.OANDA_ENV == "practice" else "live"
    return oandapyV20.API(access_token=config.OANDA_API_KEY, environment=environment)


def fetch_candles(client, instrument, granularity, from_time, to_time):
    """Загружает свечи порциями по MAX_CANDLES."""
    all_candles = []
    current_from = from_time

    while current_from < to_time:
        params = {
            "granularity": granularity,
            "from": current_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": to_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count": MAX_CANDLES,
            "price": "M",  # mid prices
        }

        req = instruments.InstrumentsCandles(instrument=instrument, params=params)
        try:
            response = client.request(req)
        except Exception as e:
            print(f"  Error fetching {instrument} {granularity}: {e}")
            break

        candles = response.get("candles", [])
        if not candles:
            break

        all_candles.extend(candles)

        # Двигаем from к последней свече + 1 секунда
        last_time = candles[-1]["time"]
        current_from = datetime.strptime(last_time[:19], "%Y-%m-%dT%H:%M:%S") + timedelta(seconds=1)

        # Пауза чтобы не превысить rate limit
        time.sleep(0.5)

    return all_candles


def candles_to_dataframe(candles):
    """Конвертирует OANDA candles в DataFrame."""
    rows = []
    for c in candles:
        if not c.get("complete", False):
            continue
        mid = c["mid"]
        rows.append({
            "timestamp": c["time"][:19].replace("T", " "),
            "open": float(mid["o"]),
            "high": float(mid["h"]),
            "low": float(mid["l"]),
            "close": float(mid["c"]),
            "volume": int(c["volume"]),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
    return df


def fetch_instrument(client, instrument, timeframe, months=12):
    """Скачивает данные для одного инструмента и таймфрейма."""
    granularity = TIMEFRAMES[timeframe]
    to_time = datetime.utcnow()
    from_time = to_time - timedelta(days=months * 30)

    print(f"  Fetching {instrument} {timeframe} from {from_time.date()} to {to_time.date()}...")

    candles = fetch_candles(client, instrument, granularity, from_time, to_time)
    df = candles_to_dataframe(candles)

    if df.empty:
        print(f"  WARNING: No data for {instrument} {timeframe}")
        return df

    # Сохраняем в CSV
    filename = f"{instrument}_{timeframe}.csv"
    filepath = os.path.join(CSV_DIR, filename)
    df.to_csv(filepath)
    print(f"  Saved {len(df)} candles to {filename}")

    return df


def fetch_all(months=12):
    """Скачивает все инструменты и таймфреймы."""
    os.makedirs(CSV_DIR, exist_ok=True)
    client = get_client()

    results = {}
    for instrument in INSTRUMENTS:
        for timeframe in TIMEFRAMES:
            key = f"{instrument}_{timeframe}"
            df = fetch_instrument(client, instrument, timeframe, months)
            results[key] = len(df)
            time.sleep(1)  # пауза между инструментами

    print("\n=== OANDA Download Summary ===")
    for key, count in results.items():
        print(f"  {key}: {count} candles")

    return results


if __name__ == "__main__":
    fetch_all()
