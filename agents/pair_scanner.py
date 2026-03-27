"""
Pair Scanner — тестирует новые валютные пары за ночь.

Скачивает данные для всех пар, прогоняет бэктест с текущими параметрами,
ранжирует по score. Пары с score > 0.3 — кандидаты в CORE.

Запуск:
  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... venv/bin/python3 -u agents/pair_scanner.py
"""

import os
import sys
import json
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.base_strategy import load_params
from data.fetcher_yahoo import INSTRUMENTS as YAHOO_INSTRUMENTS, fetch_instrument as _fetch_raw
from agents.orchestrator_v2 import send_telegram

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")
CSV_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "csv")

# Пропускаем крипту и уже известные плохие
SKIP = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "GER40"}


def scan_all_pairs():
    """Скачивает данные и тестирует все пары."""
    params = load_params()
    results = {}

    # Все форекс пары из Yahoo fetcher
    pairs = [p for p in YAHOO_INSTRUMENTS.keys() if p not in SKIP]

    send_telegram(f"Pair Scanner started: {len(pairs)} pairs to test")
    print(f"Pair Scanner: testing {len(pairs)} pairs")
    print(f"Pairs: {', '.join(sorted(pairs))}")

    for pair in sorted(pairs):
        print(f"\n{'='*50}")
        print(f"Testing {pair}...")

        try:
            # Step 1: Download data (H1 + M3)
            print(f"  Downloading data...")
            h1_path = os.path.join(CSV_DIR, f"{pair}_H1.csv")
            m3_path = os.path.join(CSV_DIR, f"{pair}_M3.csv")

            # Download H1 (max 729 days) and M3 (max 59 days)
            yahoo_ticker = YAHOO_INSTRUMENTS.get(pair)
            if not yahoo_ticker:
                print(f"  SKIP: no Yahoo ticker for {pair}")
                results[pair] = {"error": "no_ticker"}
                continue
            _fetch_raw(pair, yahoo_ticker, "H1", months=24)
            _fetch_raw(pair, yahoo_ticker, "M3", months=2)

            if not os.path.exists(h1_path) or not os.path.exists(m3_path):
                print(f"  SKIP: data not available")
                results[pair] = {"error": "no_data"}
                continue

            # Step 2: Run backtest
            print(f"  Running backtest...")
            from backtest.runner import run_backtest
            result = run_backtest(pair, params)

            metrics = result.get("metrics", {})
            if not metrics:
                print(f"  SKIP: no metrics")
                results[pair] = {"error": "no_metrics"}
                continue

            score = metrics.get("score", 0)
            trades = metrics.get("total_trades", 0)
            wr = metrics.get("winrate", 0)
            pf = metrics.get("profit_factor", 0)
            total_r = metrics.get("total_r", 0)

            results[pair] = {
                "score": round(score, 4),
                "trades": trades,
                "winrate": round(wr, 4),
                "profit_factor": round(pf, 4),
                "total_r": round(total_r, 2),
            }

            emoji = "+" if score > 0 else "-"
            print(f"  {pair}: score={score:+.4f}, trades={trades}, WR={wr:.0%}, PF={pf:.2f}, R={total_r:+.1f}")

        except Exception as e:
            print(f"  ERROR: {e}")
            results[pair] = {"error": str(e)}

    # Save results
    os.makedirs(RESULTS_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    results_file = os.path.join(RESULTS_DIR, f"pair_scan_{date_str}.json")
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)

    # Rank by score
    scored = {p: r for p, r in results.items() if isinstance(r, dict) and "score" in r}
    ranked = sorted(scored.items(), key=lambda x: x[1]["score"], reverse=True)

    # Telegram report
    report = "Pair Scanner Complete!\n\n"
    report += "Ranking by score:\n"
    for pair, r in ranked:
        emoji = "+" if r["score"] > 0.3 else (" " if r["score"] > 0 else "-")
        report += f"  {emoji} {pair}: {r['score']:+.2f} ({r['trades']}tr, {r['winrate']:.0%}WR, {r['total_r']:+.1f}R)\n"

    candidates = [p for p, r in ranked if r["score"] > 0.3 and r["trades"] >= 10]
    if candidates:
        report += f"\nCandidates for CORE: {', '.join(candidates)}"
    else:
        report += "\nNo new candidates found"

    send_telegram(report)
    print(f"\nResults saved: {results_file}")
    print(f"\nRanking:")
    for pair, r in ranked:
        marker = " ***" if r["score"] > 0.3 and r["trades"] >= 10 else ""
        print(f"  {pair}: score={r['score']:+.4f}, trades={r['trades']}, WR={r['winrate']:.0%}, R={r['total_r']:+.1f}{marker}")


if __name__ == "__main__":
    scan_all_pairs()
