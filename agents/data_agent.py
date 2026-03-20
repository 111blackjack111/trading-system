"""
DataAgent — оркестрирует загрузку данных из Yahoo Finance и Binance.
Yahoo: форекс, золото, индексы (бесплатно, без ключей)
Binance: крипто (бесплатно, публичные данные)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.fetcher_yahoo import fetch_all as fetch_yahoo
from data.fetcher_crypto import fetch_all as fetch_crypto


def run(months=12):
    """Запускает загрузку всех данных."""
    print("=" * 50)
    print("DataAgent: Starting data download")
    print("=" * 50)

    print("\n--- Yahoo Finance (Forex + Gold + GER40) ---")
    try:
        yahoo_results = fetch_yahoo(months)
    except Exception as e:
        print(f"Yahoo fetch failed: {e}")
        yahoo_results = {}

    print("\n--- Crypto (Binance) ---")
    try:
        crypto_results = fetch_crypto(months)
    except Exception as e:
        print(f"Crypto fetch failed: {e}")
        crypto_results = {}

    total = sum(yahoo_results.values()) + sum(crypto_results.values())
    print(f"\n{'=' * 50}")
    print(f"DataAgent: Done. Total candles downloaded: {total}")
    print(f"{'=' * 50}")

    return {**yahoo_results, **crypto_results}


if __name__ == "__main__":
    run()
