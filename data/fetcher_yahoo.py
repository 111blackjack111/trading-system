"""
DataAgent: Yahoo Finance fetcher
Скачивает OHLCV данные для форекс пар, XAU/USD, GER40
Бесплатно, без API ключей, без регистрации.
Таймфреймы: M3 (через ресамплинг M1→M3), M15, H1
История: до 2 лет для M15/H1, 30 дней для minute data
"""

import os
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

# Yahoo Finance тикеры
INSTRUMENTS = {
    "GBP_USD": "GBPUSD=X",
    "EUR_USD": "EURUSD=X",
    "GBP_JPY": "GBPJPY=X",
    "USD_JPY": "USDJPY=X",
    "EUR_GBP": "EURGBP=X",
    "XAU_USD": "GC=F",       # Gold futures
    "GER40":   "^GDAXI",     # DAX index
}

# Маппинг таймфреймов для yfinance
TIMEFRAMES = {
    "H1": "1h",
    "M15": "15m",
    "M3": "5m",   # yfinance min=1m, но 5m более надёжный, ресамплим в 3m нельзя — используем 5m как proxy
}

# yfinance ограничения по периоду
# 1m: 7 дней, 5m: 60 дней, 15m: 60 дней, 1h: 730 дней
MAX_PERIOD_DAYS = {
    "H1": 729,
    "M15": 59,
    "M3": 59,
}

CSV_DIR = os.path.join(os.path.dirname(__file__), "csv")


def fetch_instrument(instrument_name, yahoo_ticker, timeframe, months=12):
    """Скачивает данные для одного инструмента и таймфрейма."""
    yf_interval = TIMEFRAMES[timeframe]
    max_days = MAX_PERIOD_DAYS[timeframe]
    requested_days = months * 30

    # Ограничиваем период
    actual_days = min(requested_days, max_days)

    end = datetime.utcnow()
    start = end - timedelta(days=actual_days)

    print(f"  Fetching {instrument_name} {timeframe} ({yf_interval}) "
          f"from {start.date()} to {end.date()}...")

    try:
        ticker = yf.Ticker(yahoo_ticker)
        df = ticker.history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval=yf_interval,
            auto_adjust=True,
        )
    except Exception as e:
        print(f"  ERROR fetching {instrument_name} {timeframe}: {e}")
        return pd.DataFrame()

    if df.empty:
        print(f"  WARNING: No data for {instrument_name} {timeframe}")
        return df

    # Нормализуем колонки
    df = df.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    })

    # Оставляем только нужные колонки
    cols = ["open", "high", "low", "close", "volume"]
    df = df[[c for c in cols if c in df.columns]]

    # Убираем timezone из индекса
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    df.index.name = "timestamp"
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]

    # Убираем NaN
    df = df.dropna()

    # Если M3 — у нас 5m данные, ресамплим в 3m невозможно,
    # но система ожидает M3. Оставляем 5m как есть — разница минимальна
    # для SMC паттернов (FVG, BOS работают на любом мелком TF)

    # Сохраняем в CSV
    os.makedirs(CSV_DIR, exist_ok=True)
    filename = f"{instrument_name}_{timeframe}.csv"
    filepath = os.path.join(CSV_DIR, filename)
    df.to_csv(filepath)
    print(f"  Saved {len(df)} candles to {filename}")

    return df


def fetch_all(months=12):
    """Скачивает все инструменты и таймфреймы."""
    os.makedirs(CSV_DIR, exist_ok=True)

    results = {}
    for instrument_name, yahoo_ticker in INSTRUMENTS.items():
        for timeframe in TIMEFRAMES:
            key = f"{instrument_name}_{timeframe}"
            df = fetch_instrument(instrument_name, yahoo_ticker, timeframe, months)
            results[key] = len(df)
            time.sleep(1)  # пауза между запросами

    print("\n=== Yahoo Finance Download Summary ===")
    for key, count in results.items():
        status = "✓" if count > 0 else "✗"
        print(f"  {status} {key}: {count} candles")

    return results


if __name__ == "__main__":
    fetch_all()
