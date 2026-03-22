"""
ImpulseAgent v2 — полный цикл поиска и предсказания импульсов.

1. Скачивает 2 года данных топ-200 монет (daily candles, Binance)
2. Находит все импульсы +50% за 1-7 дней
3. Для каждого собирает 12 признаков за 3-10 дней ДО импульса
4. Анализирует паттерны — что общего у монет перед импульсом
5. Мониторит текущий рынок на совпадение паттернов → Telegram
"""

import os
import sys
import json
import time
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import ccxt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")
DB_PATH = os.path.join(DB_DIR, "impulse_patterns.db")
RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")

# Параметры
IMPULSE_THRESHOLD = 0.50  # +50%
IMPULSE_WINDOW_DAYS = 7
PRE_IMPULSE_LOOKBACK = 10  # дней до импульса для анализа
MATCH_THRESHOLD = 7  # из 12 признаков для алерта
SCAN_DAYS = 730  # 2 года


def get_exchange():
    return ccxt.binance({"enableRateLimit": True})


def send_telegram(message):
    """Отправляет алерт в Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


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
            -- 12 features
            f_volume_spike REAL,
            f_price_compression REAL,
            f_rsi REAL,
            f_volume_trend REAL,
            f_near_support INTEGER,
            f_low_volatility INTEGER,
            f_accumulation INTEGER,
            f_range_breakout INTEGER,
            f_increasing_lows INTEGER,
            f_btc_correlation REAL,
            f_volume_profile TEXT,
            f_candle_pattern TEXT,
            pattern_score INTEGER,
            raw_features TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pattern_weights (
            feature TEXT PRIMARY KEY,
            weight REAL,
            avg_when_impulse REAL,
            avg_when_no_impulse REAL,
            predictive_power REAL,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            alert_date TEXT,
            match_score REAL,
            features TEXT,
            price_at_alert REAL,
            price_after_7d REAL,
            result TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


# ============================================================
# Загрузка данных
# ============================================================

def fetch_top_symbols(exchange, limit=200):
    """Получает топ монет по объёму."""
    tickers = exchange.fetch_tickers()
    usdt_pairs = {
        k: v for k, v in tickers.items()
        if k.endswith("/USDT")
        and v.get("quoteVolume") is not None
        and v["quoteVolume"] > 0
    }
    sorted_pairs = sorted(usdt_pairs.items(), key=lambda x: x[1]["quoteVolume"], reverse=True)
    symbols = [pair[0] for pair in sorted_pairs[:limit]]

    # Исключаем стейблкоины и leveraged
    exclude = {"USDC/USDT", "BUSD/USDT", "DAI/USDT", "TUSD/USDT", "FDUSD/USDT",
               "USD1/USDT", "WBTC/USDT", "STETH/USDT"}
    symbols = [s for s in symbols if s not in exclude and "UP/" not in s and "DOWN/" not in s]
    return symbols


def fetch_daily_data(exchange, symbol, days=730):
    """Загружает дневные свечи за N дней (чанками по 1000)."""
    all_candles = []
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    while True:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe="1d", since=since, limit=1000)
        except Exception:
            break

        if not candles:
            break

        all_candles.extend(candles)
        since = candles[-1][0] + 86400000  # +1 day

        if len(candles) < 1000:
            break
        time.sleep(0.1)

    if not all_candles or len(all_candles) < 30:
        return None

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.set_index("timestamp").drop_duplicates()
    return df


def fetch_btc_data(exchange, days=730):
    """Загружает BTC данные для корреляции."""
    return fetch_daily_data(exchange, "BTC/USDT", days)


# ============================================================
# Обнаружение импульсов
# ============================================================

def find_impulses(df, symbol):
    """Находит все импульсные движения +50%+ за 1-7 дней."""
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
                break

    # Дедупликация
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
# 12 признаков перед импульсом
# ============================================================

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))


def analyze_pre_impulse(df, impulse_idx, btc_df=None):
    """
    12 признаков за 3-10 дней до импульса.

    1. volume_spike: объём > 2x среднего за 20 дней
    2. price_compression: ATR сжался < 0.7 от 30-дневного
    3. rsi_oversold: RSI < 35
    4. volume_trend: наклон объёма за 5 дней (нормализован)
    5. near_support: цена у 30-дневного минимума (<5%)
    6. low_volatility: vol ниже медианы
    7. accumulation: 3+ бычьих свечи с растущим объёмом
    8. range_breakout: 10-дневный диапазон < 10%
    9. increasing_lows: минимумы росли 5 дней подряд
    10. btc_correlation: корреляция с BTC за 10 дней
    11. volume_profile: "accumulation" / "distribution" / "neutral"
    12. candle_pattern: "doji_cluster" / "hammer" / "engulfing" / "none"
    """
    start = max(0, impulse_idx - PRE_IMPULSE_LOOKBACK)
    end = impulse_idx
    pre = df.iloc[start:end]

    if len(pre) < 5:
        return None

    f = {}

    # 1. Volume spike
    if impulse_idx >= 20:
        avg_vol = df["volume"].iloc[impulse_idx - 20:impulse_idx].mean()
        recent_vol = pre["volume"].iloc[-3:].max()
        f["volume_spike"] = round(recent_vol / (avg_vol + 1e-10), 2)
    else:
        f["volume_spike"] = 0

    # 2. Price compression
    atr_recent = (pre["high"] - pre["low"]).mean()
    if impulse_idx >= 30:
        atr_long = (df["high"].iloc[impulse_idx - 30:impulse_idx] - df["low"].iloc[impulse_idx - 30:impulse_idx]).mean()
        f["price_compression"] = round(atr_recent / (atr_long + 1e-10), 2)
    else:
        f["price_compression"] = 1.0

    # 3. RSI
    rsi = calculate_rsi(df["close"], 14)
    pre_rsi = rsi.iloc[start:end].min()
    f["rsi"] = round(float(pre_rsi) if not np.isnan(pre_rsi) else 50, 2)

    # 4. Volume trend (slope normalized)
    vol_series = pre["volume"].values
    if len(vol_series) >= 5:
        x = np.arange(len(vol_series[-5:]))
        slope = np.polyfit(x, vol_series[-5:], 1)[0]
        avg = np.mean(vol_series[-5:]) + 1e-10
        f["volume_trend"] = round(slope / avg, 4)
    else:
        f["volume_trend"] = 0

    # 5. Near support
    if impulse_idx >= 30:
        low_30 = df["low"].iloc[impulse_idx - 30:impulse_idx].min()
        current_low = pre["low"].min()
        f["near_support"] = int((current_low - low_30) / (low_30 + 1e-10) < 0.05)
    else:
        f["near_support"] = 0

    # 6. Low volatility
    if impulse_idx >= 60:
        vol_all = (df["high"].iloc[:impulse_idx] - df["low"].iloc[:impulse_idx]) / (df["close"].iloc[:impulse_idx] + 1e-10)
        median_vol = vol_all.median()
        recent_vol = ((pre["high"] - pre["low"]) / (pre["close"] + 1e-10)).mean()
        f["low_volatility"] = int(recent_vol < median_vol)
    else:
        f["low_volatility"] = 0

    # 7. Accumulation
    bullish_vol = ((pre["close"] > pre["open"]) & (pre["volume"] > pre["volume"].shift(1))).sum()
    f["accumulation"] = int(bullish_vol >= 3)

    # 8. Range breakout
    if len(pre) >= 5:
        price_range = (pre["high"].max() - pre["low"].min()) / (pre["low"].min() + 1e-10)
        f["range_breakout"] = int(price_range < 0.10)
    else:
        f["range_breakout"] = 0

    # 9. Increasing lows
    if len(pre) >= 5:
        lows = pre["low"].values[-5:]
        f["increasing_lows"] = int(all(lows[j] >= lows[j - 1] * 0.99 for j in range(1, len(lows))))
    else:
        f["increasing_lows"] = 0

    # 10. BTC correlation
    if btc_df is not None and len(pre) >= 5:
        try:
            btc_slice = btc_df.loc[pre.index[0]:pre.index[-1]]["close"]
            if len(btc_slice) >= 5:
                coin_returns = pre["close"].pct_change().dropna()
                btc_returns = btc_slice.pct_change().dropna()
                common = coin_returns.index.intersection(btc_returns.index)
                if len(common) >= 3:
                    corr = coin_returns.loc[common].corr(btc_returns.loc[common])
                    f["btc_correlation"] = round(float(corr) if not np.isnan(corr) else 0, 3)
                else:
                    f["btc_correlation"] = 0
            else:
                f["btc_correlation"] = 0
        except Exception:
            f["btc_correlation"] = 0
    else:
        f["btc_correlation"] = 0

    # 11. Volume profile
    if len(pre) >= 5:
        up_vol = pre.loc[pre["close"] > pre["open"], "volume"].sum()
        down_vol = pre.loc[pre["close"] <= pre["open"], "volume"].sum()
        total = up_vol + down_vol + 1e-10
        if up_vol / total > 0.65:
            f["volume_profile"] = "accumulation"
        elif down_vol / total > 0.65:
            f["volume_profile"] = "distribution"
        else:
            f["volume_profile"] = "neutral"
    else:
        f["volume_profile"] = "neutral"

    # 12. Candle patterns
    candle_pattern = "none"
    if len(pre) >= 3:
        last3 = pre.iloc[-3:]
        bodies = abs(last3["close"] - last3["open"])
        ranges = last3["high"] - last3["low"] + 1e-10

        # Doji cluster: 2+ small body candles
        dojis = (bodies / ranges < 0.3).sum()
        if dojis >= 2:
            candle_pattern = "doji_cluster"

        # Hammer: small body at top, long lower shadow
        last = pre.iloc[-1]
        body = abs(last["close"] - last["open"])
        lower_shadow = min(last["close"], last["open"]) - last["low"]
        upper_shadow = last["high"] - max(last["close"], last["open"])
        total_range = last["high"] - last["low"] + 1e-10
        if lower_shadow / total_range > 0.6 and body / total_range < 0.3:
            candle_pattern = "hammer"

        # Bullish engulfing
        if len(pre) >= 2:
            prev = pre.iloc[-2]
            curr = pre.iloc[-1]
            if (prev["close"] < prev["open"] and curr["close"] > curr["open"]
                    and curr["close"] > prev["open"] and curr["open"] < prev["close"]):
                candle_pattern = "engulfing"

    f["candle_pattern"] = candle_pattern

    # Score (binary features)
    binary = [
        int(f["volume_spike"] > 2.0),
        int(f["price_compression"] < 0.7),
        int(f["rsi"] < 35),
        int(f["volume_trend"] > 0),
        f["near_support"],
        f["low_volatility"],
        f["accumulation"],
        f["range_breakout"],
        f["increasing_lows"],
        int(abs(f["btc_correlation"]) < 0.3),  # Независимое от BTC движение
        int(f["volume_profile"] == "accumulation"),
        int(f["candle_pattern"] in ("hammer", "engulfing", "doji_cluster")),
    ]
    f["pattern_score"] = sum(binary)
    f["binary_features"] = binary

    return f


# ============================================================
# Паттерн-анализ: что общего у импульсов
# ============================================================

def analyze_patterns():
    """
    Анализирует все найденные импульсы и определяет
    какие признаки наиболее предсказательны.
    Сохраняет веса в pattern_weights.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    events = conn.execute("SELECT * FROM impulse_events").fetchall()
    if not events:
        print("  No impulse events to analyze")
        conn.close()
        return None

    print(f"\n  Analyzing {len(events)} impulse events...")

    # Собираем features
    feature_names = [
        "volume_spike", "price_compression", "rsi", "volume_trend",
        "near_support", "low_volatility", "accumulation", "range_breakout",
        "increasing_lows", "btc_correlation"
    ]

    # Считаем статистику по каждому признаку
    stats = {}
    for fn in feature_names:
        values = []
        for e in events:
            raw = json.loads(e["raw_features"]) if e["raw_features"] else {}
            if fn in raw:
                v = raw[fn]
                if isinstance(v, (int, float)):
                    values.append(v)

        if not values:
            continue

        avg = np.mean(values)
        std = np.std(values)

        # Корреляция с силой импульса
        impulse_pcts = []
        feat_vals = []
        for e in events:
            raw = json.loads(e["raw_features"]) if e["raw_features"] else {}
            if fn in raw and isinstance(raw[fn], (int, float)):
                feat_vals.append(raw[fn])
                impulse_pcts.append(e["impulse_pct"])

        if len(feat_vals) >= 5:
            corr = np.corrcoef(feat_vals, impulse_pcts)[0, 1]
            corr = 0 if np.isnan(corr) else corr
        else:
            corr = 0

        stats[fn] = {
            "avg": round(avg, 4),
            "std": round(std, 4),
            "corr_with_impulse": round(corr, 4),
            "predictive_power": round(abs(corr), 4),
        }

    # Volume profile и candle pattern — категориальные
    for cat_fn in ["volume_profile", "candle_pattern"]:
        counts = {}
        for e in events:
            raw = json.loads(e["raw_features"]) if e["raw_features"] else {}
            v = raw.get(cat_fn, "unknown")
            counts[v] = counts.get(v, 0) + 1
        stats[cat_fn] = {"distribution": counts, "predictive_power": 0}

    # Сохраняем веса
    now = datetime.now(timezone.utc).isoformat()
    for fn, s in stats.items():
        pp = s.get("predictive_power", 0)
        conn.execute("""
            INSERT OR REPLACE INTO pattern_weights
            (feature, weight, avg_when_impulse, avg_when_no_impulse, predictive_power, updated_at)
            VALUES (?, ?, ?, 0, ?, ?)
        """, (fn, pp, s.get("avg", 0), pp, now))

    conn.commit()

    # Печатаем результаты
    print("\n  Feature importance (by correlation with impulse strength):")
    sorted_stats = sorted(stats.items(), key=lambda x: x[1].get("predictive_power", 0), reverse=True)
    for fn, s in sorted_stats:
        if "distribution" in s:
            print(f"    {fn}: {s['distribution']}")
        else:
            print(f"    {fn}: avg={s['avg']:.3f} corr={s['corr_with_impulse']:+.3f} power={s['predictive_power']:.3f}")

    # Pattern score distribution
    scores = [json.loads(e["raw_features"]).get("pattern_score", 0) for e in events if e["raw_features"]]
    print(f"\n  Pattern score: avg={np.mean(scores):.1f}, median={np.median(scores):.1f}")
    for s in range(max(scores) + 1):
        cnt = scores.count(s)
        if cnt > 0:
            print(f"    Score {s}/12: {cnt} events ({cnt/len(scores)*100:.0f}%)")

    # Сохраняем отчёт
    report = {
        "total_events": len(events),
        "features": stats,
        "score_distribution": {str(s): scores.count(s) for s in range(13)},
        "analyzed_at": now,
    }
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(os.path.join(RUNTIME_DIR, "impulse_analysis.json"), "w") as f:
        json.dump(report, f, indent=2)

    conn.close()
    return report


# ============================================================
# Сканирование исторических импульсов (2 года)
# ============================================================

def scan_historical(symbols=None, days=SCAN_DAYS):
    """Полный скан: 2 года, 200 монет, 12 признаков."""
    init_db()
    exchange = get_exchange()

    if symbols is None:
        print("Fetching top symbols by volume...")
        symbols = fetch_top_symbols(exchange, limit=200)
        print(f"  Found {len(symbols)} symbols")

    print(f"  Scanning {days} days of history...")
    print(f"  Downloading BTC data for correlation...")
    btc_df = fetch_btc_data(exchange, days)

    conn = sqlite3.connect(DB_PATH)
    # Clear old data
    conn.execute("DELETE FROM impulse_events")
    conn.commit()

    total_impulses = 0
    now = datetime.now(timezone.utc).isoformat()

    for idx, symbol in enumerate(symbols):
        if (idx + 1) % 10 == 0:
            print(f"  Progress: {idx + 1}/{len(symbols)} ({total_impulses} impulses found)")

        df = fetch_daily_data(exchange, symbol, days)
        if df is None:
            continue

        impulses = find_impulses(df, symbol)

        for imp in impulses:
            features = analyze_pre_impulse(df, imp["idx"], btc_df)
            if features is None:
                continue

            conn.execute("""
                INSERT INTO impulse_events
                (symbol, impulse_date, impulse_pct, impulse_days,
                 f_volume_spike, f_price_compression, f_rsi, f_volume_trend,
                 f_near_support, f_low_volatility, f_accumulation,
                 f_range_breakout, f_increasing_lows, f_btc_correlation,
                 f_volume_profile, f_candle_pattern, pattern_score,
                 raw_features, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, imp["date"].isoformat(), imp["pct"], imp["days"],
                features["volume_spike"], features["price_compression"],
                features["rsi"], features["volume_trend"],
                features["near_support"], features["low_volatility"],
                features["accumulation"], features["range_breakout"],
                features["increasing_lows"], features["btc_correlation"],
                features["volume_profile"], features["candle_pattern"],
                features["pattern_score"],
                json.dumps(features), now,
            ))
            total_impulses += 1

        conn.commit()
        time.sleep(0.2)  # Rate limit

    conn.close()
    print(f"\n  Total impulses found: {total_impulses}")

    # Анализируем паттерны
    print("\n  Running pattern analysis...")
    analyze_patterns()

    return total_impulses


# ============================================================
# Живой мониторинг с weighted scoring
# ============================================================

def get_pattern_weights():
    """Читает веса из БД."""
    if not os.path.exists(DB_PATH):
        return {}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM pattern_weights").fetchall()
    conn.close()
    return {r["feature"]: dict(r) for r in rows}


def weighted_score(features, weights):
    """Считает взвешенный score на основе исторических данных."""
    if not weights:
        return features.get("pattern_score", 0), 12  # fallback to binary

    score = 0
    max_score = 0

    feature_checks = {
        "volume_spike": features.get("volume_spike", 0) > 2.0,
        "price_compression": features.get("price_compression", 1.0) < 0.7,
        "rsi": features.get("rsi", 50) < 35,
        "volume_trend": features.get("volume_trend", 0) > 0,
        "near_support": features.get("near_support", 0) == 1,
        "low_volatility": features.get("low_volatility", 0) == 1,
        "accumulation": features.get("accumulation", 0) == 1,
        "range_breakout": features.get("range_breakout", 0) == 1,
        "increasing_lows": features.get("increasing_lows", 0) == 1,
        "btc_correlation": abs(features.get("btc_correlation", 0)) < 0.3,
    }

    for fn, is_true in feature_checks.items():
        w = weights.get(fn, {}).get("predictive_power", 0.1)
        w = max(w, 0.05)  # minimum weight
        max_score += w
        if is_true:
            score += w

    # Categorical bonuses
    if features.get("volume_profile") == "accumulation":
        score += 0.1
        max_score += 0.1
    if features.get("candle_pattern") in ("hammer", "engulfing", "doji_cluster"):
        score += 0.1
        max_score += 0.1

    return round(score, 3), round(max_score, 3)


def monitor_live(symbols=None):
    """
    Мониторит текущий рынок — ищет монеты с паттернами
    похожими на пре-импульсные. Шлёт алерты в Telegram.
    """
    init_db()
    exchange = get_exchange()
    weights = get_pattern_weights()

    if symbols is None:
        print("Fetching top symbols...")
        symbols = fetch_top_symbols(exchange, limit=200)

    print(f"  Monitoring {len(symbols)} symbols with weighted scoring...")

    btc_df = fetch_daily_data(exchange, "BTC/USDT", days=60)
    matches = []

    for idx, symbol in enumerate(symbols):
        if (idx + 1) % 50 == 0:
            print(f"  Scanning: {idx + 1}/{len(symbols)}")

        df = fetch_daily_data(exchange, symbol, days=60)
        if df is None or len(df) < 20:
            continue

        features = analyze_pre_impulse(df, len(df), btc_df)
        if features is None:
            continue

        score, max_score = weighted_score(features, weights)
        pct = score / max_score * 100 if max_score > 0 else 0

        if pct >= 60:  # 60%+ match
            match = {
                "symbol": symbol,
                "score": score,
                "max_score": max_score,
                "pct": round(pct, 1),
                "pattern_score": features["pattern_score"],
                "price": float(df["close"].iloc[-1]),
                "features": features,
            }
            matches.append(match)

            # Save to DB
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                INSERT INTO live_alerts
                (symbol, alert_date, match_score, features, price_at_alert, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                symbol,
                datetime.now(timezone.utc).isoformat(),
                round(pct, 1),
                json.dumps(features),
                float(df["close"].iloc[-1]),
                datetime.now(timezone.utc).isoformat(),
            ))
            conn.commit()
            conn.close()

            print(f"  MATCH: {symbol} {pct:.0f}% (score {score}/{max_score}) price={df['close'].iloc[-1]:.4f}")

        time.sleep(0.3)

    # Telegram alert
    if matches:
        top = sorted(matches, key=lambda x: -x["pct"])[:5]
        msg = "<b>🚀 ImpulseAgent: Совпадения найдены!</b>\n\n"
        for m in top:
            msg += f"<b>{m['symbol']}</b>: {m['pct']:.0f}% match @ ${m['price']:.4f}\n"
            msg += f"  Score: {m['pattern_score']}/12 | Vol spike: {m['features']['volume_spike']:.1f}x\n"
            msg += f"  RSI: {m['features']['rsi']:.0f} | Vol profile: {m['features']['volume_profile']}\n\n"
        send_telegram(msg)

    print(f"\n  Total matches (>=60%): {len(matches)}")
    return matches


def get_stats():
    """Статистика по найденным паттернам."""
    if not os.path.exists(DB_PATH):
        print("No database found.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) as cnt FROM impulse_events").fetchone()["cnt"]
    print(f"\nTotal impulse events: {total}")

    if total == 0:
        conn.close()
        return

    # Score distribution
    print("\nPattern score distribution:")
    for row in conn.execute("SELECT pattern_score, COUNT(*) as cnt, ROUND(AVG(impulse_pct),1) as avg_pct FROM impulse_events GROUP BY pattern_score ORDER BY pattern_score"):
        bar = "█" * row["cnt"]
        print(f"  {row['pattern_score']:2d}/12: {row['cnt']:3d} events, avg +{row['avg_pct']}% {bar}")

    # Top symbols
    print("\nTop symbols by impulse count:")
    for row in conn.execute("SELECT symbol, COUNT(*) as cnt, ROUND(AVG(impulse_pct),1) as avg_pct, ROUND(MAX(impulse_pct),1) as max_pct FROM impulse_events GROUP BY symbol ORDER BY cnt DESC LIMIT 15"):
        print(f"  {row['symbol']}: {row['cnt']} impulses, avg +{row['avg_pct']}%, max +{row['max_pct']}%")

    # Pattern weights
    print("\nFeature importance:")
    for row in conn.execute("SELECT * FROM pattern_weights ORDER BY predictive_power DESC"):
        print(f"  {row['feature']}: power={row['predictive_power']:.3f} avg={row['avg_when_impulse']:.3f}")

    # Recent live alerts
    alerts = conn.execute("SELECT * FROM live_alerts ORDER BY id DESC LIMIT 5").fetchall()
    if alerts:
        print("\nRecent live alerts:")
        for a in alerts:
            print(f"  {a['symbol']}: {a['match_score']}% match @ ${a['price_at_alert']:.4f} ({a['alert_date'][:10]})")

    conn.close()


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["scan", "analyze", "monitor", "stats", "full"], default="full")
    parser.add_argument("--days", type=int, default=SCAN_DAYS)
    args = parser.parse_args()

    if args.mode == "scan":
        scan_historical(days=args.days)
    elif args.mode == "analyze":
        analyze_patterns()
    elif args.mode == "monitor":
        monitor_live()
    elif args.mode == "stats":
        get_stats()
    elif args.mode == "full":
        # Полный цикл: скан → анализ → мониторинг
        print("=" * 60)
        print("ImpulseAgent v2: Full cycle (2 years, 200 coins)")
        print("=" * 60)
        scan_historical(days=args.days)
        print("\n" + "=" * 60)
        print("Live monitoring...")
        print("=" * 60)
        monitor_live()
