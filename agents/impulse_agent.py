"""
ImpulseAgent — ищет монеты с импульсным ростом и анализирует паттерны перед импульсом.

Логика:
1. Скачивает топ-200 монет по объёму за 6 месяцев
2. Находит монеты с импульсом +50%+ за 1-7 дней
3. Анализирует что было за 3-10 дней до импульса (9 признаков)
4. Сохраняет паттерны в db/impulse_patterns.db
5. Мониторит текущий рынок на совпадение паттернов
"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import ccxt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")
DB_PATH = os.path.join(DB_DIR, "impulse_patterns.db")

# Порог импульса
IMPULSE_THRESHOLD = 0.50  # +50%
IMPULSE_WINDOW_DAYS = 7
PRE_IMPULSE_LOOKBACK = 10  # дней до импульса для анализа
MATCH_THRESHOLD = 7  # из 9 признаков для алерта


def get_exchange():
    return ccxt.binance({"enableRateLimit": True})


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS impulse_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            impulse_date TEXT,
            impulse_pct REAL,
            impulse_days INTEGER,
            pre_volume_spike REAL,
            pre_price_compression REAL,
            pre_rsi REAL,
            pre_volume_trend TEXT,
            pre_price_near_support INTEGER,
            pre_low_volatility INTEGER,
            pre_accumulation_pattern INTEGER,
            pre_breakout_from_range INTEGER,
            pre_increasing_lows INTEGER,
            pattern_score INTEGER,
            raw_data TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            match_date TEXT,
            match_score INTEGER,
            matching_features TEXT,
            current_price REAL,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


# ============================================================
# Загрузка данных
# ============================================================

def fetch_top_symbols(exchange, limit=200):
    """Получает топ монет по объёму за 24ч."""
    tickers = exchange.fetch_tickers()
    usdt_pairs = {
        k: v for k, v in tickers.items()
        if k.endswith("/USDT")
        and v.get("quoteVolume") is not None
        and v["quoteVolume"] > 0
    }

    sorted_pairs = sorted(usdt_pairs.items(), key=lambda x: x[1]["quoteVolume"], reverse=True)
    symbols = [pair[0] for pair in sorted_pairs[:limit]]

    # Исключаем стейблкоины
    stables = {"USDC/USDT", "BUSD/USDT", "DAI/USDT", "TUSD/USDT", "FDUSD/USDT"}
    symbols = [s for s in symbols if s not in stables]

    return symbols


def fetch_daily_data(exchange, symbol, days=180):
    """Загружает дневные свечи."""
    since = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe="1d", since=since, limit=days)
    except Exception as e:
        print(f"  Error fetching {symbol}: {e}")
        return None

    if not candles or len(candles) < 30:
        return None

    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.set_index("timestamp")
    return df


# ============================================================
# Обнаружение импульсов
# ============================================================

def find_impulses(df, symbol):
    """Находит импульсные движения +50%+ за 1-7 дней."""
    impulses = []
    closes = df["close"].values
    n = len(closes)

    for i in range(IMPULSE_WINDOW_DAYS, n):
        for window in range(1, IMPULSE_WINDOW_DAYS + 1):
            if i - window < 0:
                continue
            start_price = closes[i - window]
            end_price = closes[i]
            if start_price <= 0:
                continue
            pct_change = (end_price - start_price) / start_price

            if pct_change >= IMPULSE_THRESHOLD:
                impulses.append({
                    "symbol": symbol,
                    "date": df.index[i],
                    "start_date": df.index[i - window],
                    "pct": round(pct_change * 100, 2),
                    "days": window,
                    "idx": i,
                })
                break  # Берём первый подходящий window

    # Дедупликация — оставляем самый сильный импульс в каждом 7-дневном окне
    if not impulses:
        return []

    filtered = [impulses[0]]
    for imp in impulses[1:]:
        if (imp["date"] - filtered[-1]["date"]).days >= IMPULSE_WINDOW_DAYS:
            filtered.append(imp)
        elif imp["pct"] > filtered[-1]["pct"]:
            filtered[-1] = imp

    return filtered


# ============================================================
# Анализ паттернов перед импульсом (9 признаков)
# ============================================================

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))


def analyze_pre_impulse(df, impulse_idx):
    """
    Анализирует 9 признаков за 3-10 дней до импульса.

    Признаки:
    1. volume_spike: объём вырос >2x vs среднее за 20 дней
    2. price_compression: диапазон цены сжался (ATR снизился)
    3. rsi_oversold: RSI был <35 (перепроданность)
    4. volume_trend: объём рос последние 5 дней
    5. near_support: цена была у минимума за 30 дней
    6. low_volatility: волатильность ниже медианы
    7. accumulation: несколько дней закрытия выше открытия при растущем объёме
    8. breakout_from_range: цена была в боковике (диапазон <10%)
    9. increasing_lows: минимумы росли последние 5 дней
    """
    start = max(0, impulse_idx - PRE_IMPULSE_LOOKBACK)
    end = impulse_idx
    pre = df.iloc[start:end]

    if len(pre) < 5:
        return None

    features = {}

    # 1. Volume spike
    if impulse_idx >= 20:
        avg_vol = df["volume"].iloc[impulse_idx - 20:impulse_idx].mean()
        recent_vol = pre["volume"].iloc[-3:].max()
        features["volume_spike"] = round(recent_vol / (avg_vol + 1e-10), 2)
        features["pre_volume_spike"] = int(features["volume_spike"] > 2.0)
    else:
        features["volume_spike"] = 0
        features["pre_volume_spike"] = 0

    # 2. Price compression
    atr_recent = (pre["high"] - pre["low"]).mean()
    if impulse_idx >= 30:
        atr_long = (df["high"].iloc[impulse_idx - 30:impulse_idx] - df["low"].iloc[impulse_idx - 30:impulse_idx]).mean()
        features["price_compression"] = round(atr_recent / (atr_long + 1e-10), 2)
        features["pre_price_compression"] = int(features["price_compression"] < 0.7)
    else:
        features["price_compression"] = 1.0
        features["pre_price_compression"] = 0

    # 3. RSI
    rsi = calculate_rsi(df["close"], 14)
    pre_rsi = rsi.iloc[start:end].min()
    features["pre_rsi"] = round(float(pre_rsi) if not np.isnan(pre_rsi) else 50, 2)
    features["rsi_oversold"] = int(features["pre_rsi"] < 35)

    # 4. Volume trend
    vol_series = pre["volume"].values
    if len(vol_series) >= 5:
        vol_trend = np.polyfit(range(len(vol_series[-5:])), vol_series[-5:], 1)[0]
        features["pre_volume_trend"] = "rising" if vol_trend > 0 else "falling"
    else:
        features["pre_volume_trend"] = "unknown"

    # 5. Near support (цена у 30-дневного минимума)
    if impulse_idx >= 30:
        low_30 = df["low"].iloc[impulse_idx - 30:impulse_idx].min()
        current_low = pre["low"].min()
        distance = (current_low - low_30) / (low_30 + 1e-10)
        features["pre_price_near_support"] = int(distance < 0.05)
    else:
        features["pre_price_near_support"] = 0

    # 6. Low volatility
    if impulse_idx >= 60:
        vol_all = (df["high"].iloc[:impulse_idx] - df["low"].iloc[:impulse_idx]) / df["close"].iloc[:impulse_idx]
        median_vol = vol_all.median()
        recent_vol = ((pre["high"] - pre["low"]) / pre["close"]).mean()
        features["pre_low_volatility"] = int(recent_vol < median_vol)
    else:
        features["pre_low_volatility"] = 0

    # 7. Accumulation pattern
    bullish_days = ((pre["close"] > pre["open"]) & (pre["volume"] > pre["volume"].shift(1))).sum()
    features["pre_accumulation_pattern"] = int(bullish_days >= 3)

    # 8. Breakout from range
    if len(pre) >= 5:
        price_range = (pre["high"].max() - pre["low"].min()) / (pre["low"].min() + 1e-10)
        features["pre_breakout_from_range"] = int(price_range < 0.10)
    else:
        features["pre_breakout_from_range"] = 0

    # 9. Increasing lows
    if len(pre) >= 5:
        lows = pre["low"].values[-5:]
        features["pre_increasing_lows"] = int(all(lows[j] >= lows[j - 1] for j in range(1, len(lows))))
    else:
        features["pre_increasing_lows"] = 0

    # Итоговый score
    binary_features = [
        features["pre_volume_spike"],
        features["pre_price_compression"],
        features["rsi_oversold"],
        1 if features["pre_volume_trend"] == "rising" else 0,
        features["pre_price_near_support"],
        features["pre_low_volatility"],
        features["pre_accumulation_pattern"],
        features["pre_breakout_from_range"],
        features["pre_increasing_lows"],
    ]
    features["pattern_score"] = sum(binary_features)

    return features


# ============================================================
# Сканирование и сохранение
# ============================================================

def scan_historical_impulses(symbols=None, days=180):
    """Сканирует монеты на исторические импульсы и сохраняет паттерны."""
    init_db()
    exchange = get_exchange()

    if symbols is None:
        print("Fetching top symbols by volume...")
        symbols = fetch_top_symbols(exchange, limit=200)
        print(f"  Found {len(symbols)} symbols")

    conn = sqlite3.connect(DB_PATH)
    total_impulses = 0

    for idx, symbol in enumerate(symbols):
        if (idx + 1) % 20 == 0:
            print(f"  Progress: {idx + 1}/{len(symbols)}")

        df = fetch_daily_data(exchange, symbol, days)
        if df is None:
            continue

        impulses = find_impulses(df, symbol)

        for imp in impulses:
            features = analyze_pre_impulse(df, imp["idx"])
            if features is None:
                continue

            conn.execute("""
                INSERT INTO impulse_events
                (symbol, impulse_date, impulse_pct, impulse_days,
                 pre_volume_spike, pre_price_compression, pre_rsi,
                 pre_volume_trend, pre_price_near_support, pre_low_volatility,
                 pre_accumulation_pattern, pre_breakout_from_range,
                 pre_increasing_lows, pattern_score, raw_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol,
                imp["date"].isoformat(),
                imp["pct"],
                imp["days"],
                features.get("volume_spike", 0),
                features.get("price_compression", 1.0),
                features["pre_rsi"],
                features["pre_volume_trend"],
                features["pre_price_near_support"],
                features["pre_low_volatility"],
                features["pre_accumulation_pattern"],
                features["pre_breakout_from_range"],
                features["pre_increasing_lows"],
                features["pattern_score"],
                json.dumps(features),
                datetime.utcnow().isoformat(),
            ))
            total_impulses += 1

        conn.commit()
        time.sleep(0.3)

    conn.close()
    print(f"\nTotal impulses found and saved: {total_impulses}")
    return total_impulses


def scan_live_matches(symbols=None):
    """
    Мониторит текущий рынок — проверяет каждую монету
    на совпадение паттернов (7+ из 9 признаков).
    """
    init_db()
    exchange = get_exchange()

    if symbols is None:
        print("Fetching top symbols...")
        symbols = fetch_top_symbols(exchange, limit=200)

    matches = []

    for idx, symbol in enumerate(symbols):
        if (idx + 1) % 50 == 0:
            print(f"  Scanning: {idx + 1}/{len(symbols)}")

        df = fetch_daily_data(exchange, symbol, days=60)
        if df is None or len(df) < 20:
            continue

        # Анализируем текущее состояние как "перед потенциальным импульсом"
        features = analyze_pre_impulse(df, len(df))
        if features is None:
            continue

        if features["pattern_score"] >= MATCH_THRESHOLD:
            match = {
                "symbol": symbol,
                "score": features["pattern_score"],
                "features": features,
                "price": float(df["close"].iloc[-1]),
            }
            matches.append(match)

            # Сохраняем в БД
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                INSERT INTO live_matches
                (symbol, match_date, match_score, matching_features, current_price, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                symbol,
                datetime.utcnow().isoformat(),
                features["pattern_score"],
                json.dumps(features),
                float(df["close"].iloc[-1]),
                datetime.utcnow().isoformat(),
            ))
            conn.commit()
            conn.close()

            print(f"  MATCH: {symbol} score={features['pattern_score']}/9 price={df['close'].iloc[-1]:.4f}")

        time.sleep(0.3)

    print(f"\nTotal matches (>={MATCH_THRESHOLD}/9): {len(matches)}")
    for m in matches:
        print(f"  {m['symbol']}: {m['score']}/9 @ {m['price']:.4f}")

    return matches


def get_pattern_stats():
    """Статистика по найденным паттернам."""
    if not os.path.exists(DB_PATH):
        print("No database found. Run scan_historical_impulses first.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Общая статистика
    total = conn.execute("SELECT COUNT(*) as cnt FROM impulse_events").fetchone()["cnt"]
    print(f"\nTotal impulse events: {total}")

    if total == 0:
        conn.close()
        return

    # Распределение по pattern_score
    print("\nPattern score distribution:")
    for row in conn.execute("SELECT pattern_score, COUNT(*) as cnt FROM impulse_events GROUP BY pattern_score ORDER BY pattern_score"):
        print(f"  Score {row['pattern_score']}/9: {row['cnt']} events")

    # Средний импульс по score
    print("\nAvg impulse % by pattern score:")
    for row in conn.execute("SELECT pattern_score, ROUND(AVG(impulse_pct), 1) as avg_pct, COUNT(*) as cnt FROM impulse_events GROUP BY pattern_score ORDER BY pattern_score"):
        print(f"  Score {row['pattern_score']}/9: avg +{row['avg_pct']}% ({row['cnt']} events)")

    # Топ монеты по количеству импульсов
    print("\nTop symbols by impulse count:")
    for row in conn.execute("SELECT symbol, COUNT(*) as cnt, ROUND(AVG(impulse_pct), 1) as avg_pct FROM impulse_events GROUP BY symbol ORDER BY cnt DESC LIMIT 10"):
        print(f"  {row['symbol']}: {row['cnt']} impulses, avg +{row['avg_pct']}%")

    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["scan", "live", "stats"], default="scan")
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args()

    if args.mode == "scan":
        scan_historical_impulses(days=args.days)
    elif args.mode == "live":
        scan_live_matches()
    elif args.mode == "stats":
        get_pattern_stats()
