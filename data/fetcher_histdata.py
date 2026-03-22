"""
Fetcher для исторических форекс данных через histdata.com (pip install histdata).
Скачивает M1 свечи за полные годы и агрегирует в M3 и H1.

Бесплатно, без API ключей, до 15 лет истории.
"""

import os
import io
import zipfile
import tempfile
import pandas as pd
from datetime import datetime

CSV_DIR = os.path.join(os.path.dirname(__file__), "csv")

# Маппинг наших инструментов → histdata тикеры
INSTRUMENT_MAP = {
    "GBP_USD": "gbpusd",
    "EUR_GBP": "eurgbp",
    "USD_JPY": "usdjpy",
    "EUR_USD": "eurusd",
    "GBP_JPY": "gbpjpy",
    "XAU_USD": "xauusd",
}


def download_year(pair_histdata, year):
    """Скачивает M1 данные за год через histdata пакет."""
    from histdata import download_hist_data
    from histdata.api import Platform, TimeFrame

    with tempfile.TemporaryDirectory() as tmp:
        try:
            result = download_hist_data(
                year=str(year),
                pair=pair_histdata,
                platform=Platform.META_TRADER,
                time_frame=TimeFrame.ONE_MINUTE,
                output_directory=tmp,
            )
        except Exception as e:
            print(f"    Download error for {pair_histdata} {year}: {e}")
            return None

        if not result or not os.path.exists(result):
            return None

        # Unzip and parse CSV
        with zipfile.ZipFile(result) as z:
            csv_files = [n for n in z.namelist() if n.endswith(".csv")]
            if not csv_files:
                return None

            with z.open(csv_files[0]) as f:
                content = f.read().decode("utf-8")

        # Parse: format is "2024.01.01,17:00,1.271840,1.271840,1.271840,1.271840,0"
        df = pd.read_csv(
            io.StringIO(content),
            header=None,
            names=["date", "time", "open", "high", "low", "close", "volume"],
        )
        df["timestamp"] = pd.to_datetime(df["date"] + " " + df["time"], format="%Y.%m.%d %H:%M")
        df.set_index("timestamp", inplace=True)
        df.drop(["date", "time"], axis=1, inplace=True)
        df = df.astype(float)

        return df


def download_current_year_months(pair_histdata):
    """Скачивает текущий год помесячно."""
    from histdata import download_hist_data
    from histdata.api import Platform, TimeFrame

    now = datetime.now()
    all_data = []

    for month in range(1, now.month + 1):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result = download_hist_data(
                    year=str(now.year),
                    month=str(month),
                    pair=pair_histdata,
                    platform=Platform.META_TRADER,
                    time_frame=TimeFrame.ONE_MINUTE,
                    output_directory=tmp,
                )
            except Exception as e:
                print(f"    Month {month}: {e}")
                continue

            if not result or not os.path.exists(result):
                continue

            with zipfile.ZipFile(result) as z:
                csv_files = [n for n in z.namelist() if n.endswith(".csv")]
                if not csv_files:
                    continue
                with z.open(csv_files[0]) as f:
                    content = f.read().decode("utf-8")

            df = pd.read_csv(
                io.StringIO(content),
                header=None,
                names=["date", "time", "open", "high", "low", "close", "volume"],
            )
            df["timestamp"] = pd.to_datetime(df["date"] + " " + df["time"], format="%Y.%m.%d %H:%M")
            df.set_index("timestamp", inplace=True)
            df.drop(["date", "time"], axis=1, inplace=True)
            df = df.astype(float)
            all_data.append(df)
            print(f"    Month {month}: {len(df)} candles")

    return pd.concat(all_data) if all_data else None


def download_pair(instrument, months=12):
    """Скачивает M1 данные и агрегирует в M3 и H1."""
    pair = INSTRUMENT_MAP.get(instrument)
    if not pair:
        print(f"  {instrument}: not in histdata map, skipping")
        return False

    print(f"  Downloading {instrument} ({pair}) — {months} months M1 data...")

    now = datetime.now()
    all_data = []

    # Previous year(s)
    years_needed = set()
    for i in range(months):
        dt = datetime(now.year, now.month, 1)
        month_back = now.month - i - 1
        y = now.year + (month_back // 12)
        years_needed.add(y)

    for year in sorted(years_needed):
        if year < now.year:
            print(f"    Downloading {year} (full year)...")
            df = download_year(pair, year)
            if df is not None:
                all_data.append(df)
                print(f"    {year}: {len(df)} M1 candles")
        else:
            print(f"    Downloading {year} (current year, by month)...")
            df = download_current_year_months(pair)
            if df is not None:
                all_data.append(df)

    if not all_data:
        print(f"  {instrument}: no data downloaded")
        return False

    # Combine
    df_m1 = pd.concat(all_data)
    df_m1.sort_index(inplace=True)
    df_m1 = df_m1[~df_m1.index.duplicated(keep="first")]

    # Keep only last N months
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
    df_m1 = df_m1[df_m1.index >= cutoff]

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
    df_m3.to_csv(os.path.join(CSV_DIR, f"{instrument}_M3.csv"))
    df_h1.to_csv(os.path.join(CSV_DIR, f"{instrument}_H1.csv"))

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
