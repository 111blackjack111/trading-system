"""
Fetcher для исторических форекс данных через histdata.com.
Скачивает M1 свечи и агрегирует в M3 и H1.

Бесплатно, без API ключей, до 15 лет истории.
"""

import os
import io
import zipfile
import pandas as pd
import urllib.request
from datetime import datetime

CSV_DIR = os.path.join(os.path.dirname(__file__), "csv")

# Маппинг наших инструментов → histdata тикеры
INSTRUMENT_MAP = {
    "GBP_USD": "GBPUSD",
    "EUR_GBP": "EURGBP",
    "USD_JPY": "USDJPY",
    "EUR_USD": "EURUSD",
    "GBP_JPY": "GBPJPY",
    "XAU_USD": "XAUUSD",
}

# GER40 (DAX) нет на histdata — оставим Yahoo для него


def download_histdata_month(pair, year, month):
    """
    Скачивает M1 данные за один месяц с histdata.com.
    Returns: DataFrame или None
    """
    # histdata URL format
    url = (
        f"https://www.histdata.com/download-free-forex-data/"
        f"?/ascii/1-minute-bar-quotes/{pair.lower()}/{year}/{month}"
    )

    # histdata требует POST запрос для скачивания
    # Используем альтернативный источник: github.com/philipperemy/FX-1-Minute-Data
    # Этот репозиторий содержит уже скачанные данные histdata в CSV формате
    github_url = (
        f"https://raw.githubusercontent.com/philipperemy/FX-1-Minute-Data/"
        f"master/data/{pair}/{year}/{month}.csv"
    )

    try:
        req = urllib.request.Request(github_url)
        req.add_header("User-Agent", "Mozilla/5.0")
        response = urllib.request.urlopen(req, timeout=30)
        data = response.read().decode("utf-8")

        # Parse CSV — format: timestamp,open,high,low,close,volume
        df = pd.read_csv(
            io.StringIO(data),
            names=["timestamp", "open", "high", "low", "close", "volume"],
            parse_dates=["timestamp"],
        )
        df.set_index("timestamp", inplace=True)
        return df
    except Exception as e:
        # Try alternative format (some files have different structure)
        try:
            df = pd.read_csv(
                io.StringIO(data),
                sep=";",
                names=["date", "time", "open", "high", "low", "close", "volume"],
            )
            df["timestamp"] = pd.to_datetime(df["date"] + " " + df["time"])
            df.set_index("timestamp", inplace=True)
            df.drop(["date", "time"], axis=1, inplace=True)
            return df
        except Exception:
            print(f"    Failed to download {pair} {year}/{month}: {e}")
            return None


def download_pair(instrument, months=12):
    """
    Скачивает M1 данные для инструмента и агрегирует в M3 и H1.
    """
    pair = INSTRUMENT_MAP.get(instrument)
    if not pair:
        print(f"  {instrument}: not in histdata map, skipping")
        return False

    print(f"  Downloading {instrument} ({pair}) — {months} months M1 data...")

    now = datetime.now()
    all_data = []

    for i in range(months):
        # Go back i months
        month_offset = now.month - i - 1
        year = now.year + (month_offset // 12)
        month = (month_offset % 12) + 1

        print(f"    Fetching {year}/{month:02d}...")
        df = download_histdata_month(pair, year, month)
        if df is not None and len(df) > 0:
            all_data.append(df)

    if not all_data:
        print(f"  {instrument}: no data downloaded, trying yfinance fallback")
        return False

    # Combine all months
    df_m1 = pd.concat(all_data)
    df_m1.sort_index(inplace=True)
    df_m1 = df_m1[~df_m1.index.duplicated(keep="first")]

    print(f"    Total M1 candles: {len(df_m1)}")

    # Aggregate M1 → M3
    df_m3 = df_m1.resample("3min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()

    # Aggregate M1 → H1
    df_h1 = df_m1.resample("1h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()

    # Save
    os.makedirs(CSV_DIR, exist_ok=True)

    m3_path = os.path.join(CSV_DIR, f"{instrument}_M3.csv")
    h1_path = os.path.join(CSV_DIR, f"{instrument}_H1.csv")

    df_m3.to_csv(m3_path)
    df_h1.to_csv(h1_path)

    print(f"    Saved: M3={len(df_m3)} candles, H1={len(df_h1)} candles")
    return True


def run(instruments=None, months=12):
    """Скачивает данные для всех форекс инструментов."""
    if instruments is None:
        instruments = list(INSTRUMENT_MAP.keys())

    print(f"\n=== HistData Fetcher: {len(instruments)} instruments, {months} months ===")

    success = 0
    for inst in instruments:
        if download_pair(inst, months):
            success += 1

    print(f"\n=== Done: {success}/{len(instruments)} instruments ===")
    return success


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruments", nargs="+", default=None)
    parser.add_argument("--months", type=int, default=12)
    args = parser.parse_args()

    run(args.instruments, args.months)
