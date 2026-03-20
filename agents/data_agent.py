"""
DataAgent — оркестрирует загрузку данных из OANDA и Binance.
Запускается как самостоятельный скрипт или вызывается из Orchestrator.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.fetcher_oanda import fetch_all as fetch_oanda
from data.fetcher_crypto import fetch_all as fetch_crypto


def run(months=12):
    """Запускает загрузку всех данных."""
    print("=" * 50)
    print("DataAgent: Starting data download")
    print("=" * 50)

    print("\n--- OANDA (Forex + Gold + GER40) ---")
    try:
        oanda_results = fetch_oanda(months)
    except Exception as e:
        print(f"OANDA fetch failed: {e}")
        oanda_results = {}

    print("\n--- Crypto (Binance) ---")
    try:
        crypto_results = fetch_crypto(months)
    except Exception as e:
        print(f"Crypto fetch failed: {e}")
        crypto_results = {}

    total = sum(oanda_results.values()) + sum(crypto_results.values())
    print(f"\n{'=' * 50}")
    print(f"DataAgent: Done. Total candles downloaded: {total}")
    print(f"{'=' * 50}")

    return {**oanda_results, **crypto_results}


if __name__ == "__main__":
    run()
