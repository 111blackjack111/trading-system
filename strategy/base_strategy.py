"""
SMC Strategy — Smart Money Concepts
Логика входа:
1. H1: определить тренд через BOS (Break of Structure)
2. H1: найти FVG (Fair Value Gap) в направлении тренда
3. Не торговать против FVG
4. M3: дождаться входа цены в FVG
5. Проверить временное окно
6. Войти на закрытии M3 свечи при реакции от FVG
"""

import json
import os
from datetime import time as dtime

import numpy as np
import pandas as pd

PARAMS_PATH = os.path.join(os.path.dirname(__file__), "params.json")


def load_params(path=None):
    with open(path or PARAMS_PATH) as f:
        return json.load(f)


def save_params(params, path=None):
    with open(path or PARAMS_PATH, "w") as f:
        json.dump(params, f, indent=2)


# ============================================================
# Структурный анализ (H1)
# ============================================================

def detect_swing_points(df, swing_length):
    """Находит swing high/low для определения структуры.
    FIXED: Только backward-looking — swing point подтверждается через swing_length
    баров ПОСЛЕ пика (ожидаем подтверждение, не заглядываем в будущее).
    Swing high на баре i подтверждается на баре i + swing_length,
    поэтому записывается с задержкой (confirmed_idx = i + swing_length).
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    swing_highs = np.full(n, np.nan)
    swing_lows = np.full(n, np.nan)

    for i in range(swing_length, n):
        # Кандидат на swing point — бар (i - swing_length)
        # Проверяем что он был максимумом/минимумом за окно ТОЛЬКО из прошлых данных
        candidate = i - swing_length
        window = highs[candidate - swing_length:i + 1]  # от (candidate - swing_length) до i включительно
        if len(window) > 0 and highs[candidate] == max(window):
            swing_highs[i] = highs[candidate]  # записываем на баре i (момент подтверждения)

        window_low = lows[candidate - swing_length:i + 1]
        if len(window_low) > 0 and lows[candidate] == min(window_low):
            swing_lows[i] = lows[candidate]  # записываем на баре i (момент подтверждения)

    return swing_highs, swing_lows


def detect_bos(df, swing_length):
    """
    Break of Structure — определяет тренд.
    BOS вверх: цена пробивает предыдущий swing high -> бычий тренд
    BOS вниз: цена пробивает предыдущий swing low -> медвежий тренд
    Возвращает Series с значениями: 1 (bullish), -1 (bearish), 0 (neutral)
    """
    swing_highs, swing_lows = detect_swing_points(df, swing_length)
    n = len(df)
    trend = np.zeros(n)

    last_sh = np.nan
    last_sl = np.nan
    current_trend = 0

    for i in range(n):
        if not np.isnan(swing_highs[i]):
            last_sh = swing_highs[i]
        if not np.isnan(swing_lows[i]):
            last_sl = swing_lows[i]

        if not np.isnan(last_sh) and df["close"].iloc[i] > last_sh:
            current_trend = 1  # bullish BOS
        elif not np.isnan(last_sl) and df["close"].iloc[i] < last_sl:
            current_trend = -1  # bearish BOS

        trend[i] = current_trend

    return pd.Series(trend, index=df.index)


def detect_order_blocks(df, swing_length):
    """
    Order Block — последняя свеча перед импульсным движением (displacement).
    Bullish OB: последняя медвежья свеча перед бычьим импульсом
    Bearish OB: последняя бычья свеча перед медвежьим импульсом
    """
    n = len(df)
    ob_list = []
    atr = calculate_atr(df)

    for i in range(2, n - 1):
        atr_val = atr.iloc[i] if not np.isnan(atr.iloc[i]) else 0
        if atr_val <= 0:
            continue

        # Displacement = движение > 1.5 ATR за 1-2 бара
        move_up = df["close"].iloc[i] - df["close"].iloc[i - 2]
        move_down = df["close"].iloc[i - 2] - df["close"].iloc[i]

        if move_up > atr_val * 1.5:
            # Bullish displacement — ищем последнюю медвежью свечу перед ним
            for j in range(i - 1, max(i - 4, 0), -1):
                if df["close"].iloc[j] < df["open"].iloc[j]:  # медвежья свеча
                    ob_list.append({
                        "index": i,
                        "timestamp": df.index[i],
                        "type": "bullish",
                        "top": df["high"].iloc[j],
                        "bottom": df["low"].iloc[j],
                    })
                    break

        elif move_down > atr_val * 1.5:
            # Bearish displacement — ищем последнюю бычью свечу перед ним
            for j in range(i - 1, max(i - 4, 0), -1):
                if df["close"].iloc[j] > df["open"].iloc[j]:  # бычья свеча
                    ob_list.append({
                        "index": i,
                        "timestamp": df.index[i],
                        "type": "bearish",
                        "top": df["high"].iloc[j],
                        "bottom": df["low"].iloc[j],
                    })
                    break

    return ob_list


def detect_liquidity_sweep(df_h1, current_idx, swing_highs, swing_lows, lookback=20):
    """
    Liquidity Sweep — цена пробила свинг и вернулась.
    Институционалы собирают стопы перед реальным движением.
    Returns: 'bullish_sweep', 'bearish_sweep', or None
    """
    if current_idx < lookback + 2:
        return None

    # Ищем равные хаи/лои в последних lookback барах
    recent_highs = []
    recent_lows = []
    for j in range(current_idx - lookback, current_idx - 1):
        if j >= 0 and not np.isnan(swing_highs[j]):
            recent_highs.append(swing_highs[j])
        if j >= 0 and not np.isnan(swing_lows[j]):
            recent_lows.append(swing_lows[j])

    if not recent_highs and not recent_lows:
        return None

    current_high = df_h1["high"].iloc[current_idx]
    current_low = df_h1["low"].iloc[current_idx]
    current_close = df_h1["close"].iloc[current_idx]

    # Bullish sweep: цена пробила лой вниз но закрылась выше (собрала стопы)
    if recent_lows:
        min_low = min(recent_lows)
        if current_low < min_low and current_close > min_low:
            return "bullish_sweep"

    # Bearish sweep: цена пробила хай вверх но закрылась ниже
    if recent_highs:
        max_high = max(recent_highs)
        if current_high > max_high and current_close < max_high:
            return "bearish_sweep"

    return None


def detect_choch(df, swing_length):
    """
    Change of Character (CHoCH) — ранний сигнал разворота тренда.
    CHoCH = первый пробой структуры ПРОТИВ текущего тренда.
    Returns: Series с 1 (bullish CHoCH), -1 (bearish CHoCH), 0 (нет)
    """
    swing_highs, swing_lows = detect_swing_points(df, swing_length)
    n = len(df)
    choch = np.zeros(n)
    trend = detect_bos(df, swing_length)

    last_sh = np.nan
    last_sl = np.nan

    for i in range(n):
        if not np.isnan(swing_highs[i]):
            last_sh = swing_highs[i]
        if not np.isnan(swing_lows[i]):
            last_sl = swing_lows[i]

        # CHoCH: пробой против текущего тренда
        if trend.iloc[i] == -1 and not np.isnan(last_sh):
            if df["close"].iloc[i] > last_sh:
                choch[i] = 1  # bullish CHoCH в медвежьем тренде
        elif trend.iloc[i] == 1 and not np.isnan(last_sl):
            if df["close"].iloc[i] < last_sl:
                choch[i] = -1  # bearish CHoCH в бычьем тренде

    return pd.Series(choch, index=df.index)


def detect_fvg(df, min_size_multiplier, atr_period=14):
    """
    Fair Value Gap — имбаланс.
    Bullish FVG: low[i] > high[i-2] (gap up)
    Bearish FVG: high[i] < low[i-2] (gap down)
    Фильтруем по размеру: gap >= ATR * min_size_multiplier
    """
    atr = calculate_atr(df, atr_period)
    n = len(df)

    fvg_list = []

    for i in range(2, n):
        gap_up = df["low"].iloc[i] - df["high"].iloc[i - 2]
        gap_down = df["low"].iloc[i - 2] - df["high"].iloc[i]
        min_size = atr.iloc[i] * min_size_multiplier

        if gap_up > 0 and gap_up >= min_size:
            fvg_list.append({
                "index": i,
                "timestamp": df.index[i],
                "type": "bullish",
                "top": df["low"].iloc[i],
                "bottom": df["high"].iloc[i - 2],
                "mid": (df["low"].iloc[i] + df["high"].iloc[i - 2]) / 2,
            })
        elif gap_down > 0 and gap_down >= min_size:
            fvg_list.append({
                "index": i,
                "timestamp": df.index[i],
                "type": "bearish",
                "top": df["low"].iloc[i - 2],
                "bottom": df["high"].iloc[i],
                "mid": (df["low"].iloc[i - 2] + df["high"].iloc[i]) / 2,
            })

    return fvg_list


# ============================================================
# Индикаторы
# ============================================================

def calculate_atr(df, period=14):
    """Average True Range."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ============================================================
# Фильтры
# ============================================================

def session_filter(timestamp, params):
    """Проверяет временное окно (UTC+3, Kyiv)."""
    if not params.get("session_filter", True):
        return True

    t = timestamp.time() if hasattr(timestamp, "time") else timestamp

    # London: 09:00-14:00 UTC+3 = 06:00-11:00 UTC
    # NY:     15:00-17:00 UTC+3 = 12:00-14:00 UTC
    main_windows = [
        (dtime(6, 0), dtime(11, 0)),
        (dtime(12, 0), dtime(14, 0)),
    ]

    if params.get("silver_bullet_only", False):
        # Silver Bullet окна (UTC):
        # 10:00-11:00 UTC+3 = 07:00-08:00 UTC
        # 17:00-18:00 UTC+3 = 14:00-15:00 UTC
        # 21:00-22:00 UTC+3 = 18:00-19:00 UTC
        sb_windows = [
            (dtime(7, 0), dtime(8, 0)),
            (dtime(14, 0), dtime(15, 0)),
            (dtime(18, 0), dtime(19, 0)),
        ]
        return any(start <= t <= end for start, end in sb_windows)

    return any(start <= t <= end for start, end in main_windows)


def volatility_filter(atr_value, atr_series, params):
    """Проверяет минимальную волатильность."""
    if not params.get("volatility_filter", True):
        return True
    percentile = params.get("min_atr_percentile", 40)
    threshold = np.nanpercentile(atr_series.dropna().values, percentile)
    return atr_value >= threshold


def is_monday_opening(timestamp):
    """Пропускать первые 2 часа понедельника."""
    if hasattr(timestamp, "weekday"):
        if timestamp.weekday() == 0:  # Monday
            t = timestamp.time() if hasattr(timestamp, "time") else timestamp
            return t < dtime(8, 0)  # до 08:00 UTC = 11:00 UTC+3
    return False


def is_after_close(timestamp):
    """Закрыть все до 22:00 UTC+3 = 19:00 UTC."""
    t = timestamp.time() if hasattr(timestamp, "time") else timestamp
    return t >= dtime(19, 0)


def is_asian_session(timestamp):
    """Asian session: 00:00-06:00 UTC (03:00-09:00 UTC+3). Worst WR for forex."""
    t = timestamp.time() if hasattr(timestamp, "time") else timestamp
    return t < dtime(6, 0)


# ============================================================
# Генератор сигналов
# ============================================================

def is_crypto_instrument(instrument):
    """Проверяет является ли инструмент криптовалютой."""
    crypto = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
    return instrument in crypto if instrument else False


def generate_signals(df_h1, df_m3, params, instrument=None):
    """
    Генерирует торговые сигналы на основе SMC логики.

    Args:
        df_h1: DataFrame с H1 данными (тренд + FVG)
        df_m3: DataFrame с M3 данными (вход)
        params: словарь параметров стратегии
        instrument: название инструмента (для определения крипта/форекс)

    Returns:
        list of dict: сигналы [{timestamp, direction, entry, sl, tp, fvg}, ...]
    """
    is_crypto = is_crypto_instrument(instrument)

    # Применяем overrides для крипты/форекса
    params = params.copy()
    if is_crypto and "crypto_overrides" in params:
        params.update(params["crypto_overrides"])
    elif not is_crypto and "forex_overrides" in params:
        params.update(params["forex_overrides"])

    fvg_max_age = params.get("fvg_max_age_bars", 20)
    use_ob_filter = params.get("ob_confluence", True)
    use_sweep_filter = params.get("sweep_filter", True)
    use_choch = params.get("choch_filter", False)

    # 1. Определяем тренд на H1
    trend = detect_bos(df_h1, params["bos_swing_length"])

    # 2. Находим FVG на H1
    fvg_list = detect_fvg(df_h1, params["fvg_min_size_multiplier"])

    # 3. ATR для SL и фильтров
    atr_h1 = calculate_atr(df_h1)
    atr_m3 = calculate_atr(df_m3)

    # 4. Order Blocks (для confluence фильтра)
    ob_list = detect_order_blocks(df_h1, params["bos_swing_length"]) if use_ob_filter else []

    # 5. Swing points (для liquidity sweep)
    swing_highs, swing_lows = detect_swing_points(df_h1, params["bos_swing_length"])

    # 6. CHoCH (ранние развороты)
    choch = detect_choch(df_h1, params["bos_swing_length"]) if use_choch else None

    signals = []
    active_fvgs = []  # FVG которые ещё не отработали

    for fvg in fvg_list:
        idx = fvg["index"]
        if idx >= len(trend):
            continue

        # FVG должен совпадать с трендом или CHoCH
        trend_ok = False
        if fvg["type"] == "bullish" and trend.iloc[idx] == 1:
            trend_ok = True
        elif fvg["type"] == "bearish" and trend.iloc[idx] == -1:
            trend_ok = True
        # CHoCH: разрешаем вход на ранних разворотах
        if not trend_ok and use_choch and choch is not None:
            if fvg["type"] == "bullish" and choch.iloc[idx] == 1:
                trend_ok = True
            elif fvg["type"] == "bearish" and choch.iloc[idx] == -1:
                trend_ok = True

        if not trend_ok:
            continue

        # OB Confluence: FVG должен быть рядом с Order Block
        if use_ob_filter and ob_list:
            has_ob = False
            for ob in ob_list:
                if ob["type"] != fvg["type"]:
                    continue
                # OB в пределах 3 баров от FVG и зоны перекрываются
                if abs(ob["index"] - fvg["index"]) <= 5:
                    # Проверяем перекрытие зон
                    overlap = min(ob["top"], fvg["top"]) - max(ob["bottom"], fvg["bottom"])
                    if overlap > 0:
                        has_ob = True
                        break
            if not has_ob:
                continue  # Нет OB confluence — пропускаем FVG

        active_fvgs.append(fvg)

    # Построим маппинг H1 timestamps -> bar index для проверки возраста FVG
    h1_time_to_idx = {ts: i for i, ts in enumerate(df_h1.index)}

    # 4. Ищем входы на M3
    for i in range(1, len(df_m3)):
        ts = df_m3.index[i]

        # Фильтры времени — для крипты отключены
        if not is_crypto:
            if is_monday_opening(ts):
                continue
            # Asian session filter (WR 20-24% на форексе — убыточно)
            if params.get("asian_filter_forex", False) and is_asian_session(ts):
                continue
            if is_after_close(ts):
                continue
            if not session_filter(ts, params):
                continue

        # Фильтр волатильности (работает для всех)
        if i < 14:
            continue
        if not volatility_filter(atr_m3.iloc[i], atr_m3.iloc[:i], params):
            continue

        # Текущий H1 бар (округляем M3 timestamp до часа)
        current_h1_time = ts.floor("h")
        current_h1_idx = h1_time_to_idx.get(current_h1_time)

        # Проверяем каждый активный FVG
        remaining_fvgs = []
        for fvg in active_fvgs:
            # FVG должен быть в прошлом
            if fvg["timestamp"] > ts:
                remaining_fvgs.append(fvg)
                continue

            # Проверяем возраст FVG
            if current_h1_idx is not None:
                fvg_age = current_h1_idx - fvg["index"]
                if fvg_age > fvg_max_age:
                    continue  # FVG слишком старый — удаляем

            # Liquidity Sweep: проверяем был ли sweep перед входом
            if use_sweep_filter and current_h1_idx is not None:
                sweep = detect_liquidity_sweep(df_h1, current_h1_idx, swing_highs, swing_lows)
                if sweep is not None:
                    # Sweep есть — входим только если совпадает с направлением
                    if fvg["type"] == "bullish" and sweep != "bullish_sweep":
                        remaining_fvgs.append(fvg)
                        continue
                    if fvg["type"] == "bearish" and sweep != "bearish_sweep":
                        remaining_fvgs.append(fvg)
                        continue
                    # sweep совпал — бонус к confidence (проходим дальше)

            close = df_m3["close"].iloc[i]
            low = df_m3["low"].iloc[i]
            high = df_m3["high"].iloc[i]
            entry_depth = params["fvg_entry_depth"]

            # Confirmation candle filter
            confirm_pct = params.get("confirmation_candle_pct", 0.0)
            candle_range = high - low if high > low else 0.0001

            if fvg["type"] == "bullish":
                # Цена вошла в bullish FVG (зона между bottom и top)
                entry_level = fvg["top"] - (fvg["top"] - fvg["bottom"]) * entry_depth
                if low <= entry_level and close > fvg["bottom"]:
                    # Confirmation: ПРЕДЫДУЩИЙ бар (i-1) должен закрыться в верхней части
                    # FIXED: проверяем бар i-1 (уже закрытый), не текущий
                    if confirm_pct > 0 and i > 0:
                        prev_close = df_m3["close"].iloc[i - 1]
                        prev_low = df_m3["low"].iloc[i - 1]
                        prev_high = df_m3["high"].iloc[i - 1]
                        prev_range = prev_high - prev_low if prev_high > prev_low else 0.0001
                        if (prev_close - prev_low) / prev_range < confirm_pct:
                            remaining_fvgs.append(fvg)
                            continue  # Слабая реакция на предыдущем баре — пропускаем

                    # FIXED: Entry на open СЛЕДУЮЩЕГО бара (i+1), не close текущего
                    if i + 1 >= len(df_m3):
                        remaining_fvgs.append(fvg)
                        continue
                    entry_price = df_m3["open"].iloc[i + 1]
                    entry_ts = df_m3.index[i + 1]

                    atr_val = atr_m3.iloc[i] if i < len(atr_m3) and not np.isnan(atr_m3.iloc[i]) else (atr_h1.iloc[-1] if len(atr_h1) > 0 and not np.isnan(atr_h1.iloc[-1]) else None)
                    if atr_val is None or np.isnan(atr_val) or atr_val <= 0:
                        remaining_fvgs.append(fvg)
                        continue
                    sl = fvg["bottom"] - atr_val * params["sl_atr_multiplier"]
                    risk = entry_price - sl
                    if risk <= 0:
                        continue
                    tp = entry_price + risk * params["tp_rr_ratio"]
                    be_level = entry_price + risk * params["be_trigger_rr"]

                    signals.append({
                        "timestamp": entry_ts,
                        "direction": "long",
                        "entry": entry_price,
                        "sl": sl,
                        "tp": tp,
                        "be_level": be_level,
                        "risk": risk,
                        "fvg": fvg,
                    })
                    continue  # FVG отработал

            elif fvg["type"] == "bearish":
                entry_level = fvg["bottom"] + (fvg["top"] - fvg["bottom"]) * entry_depth
                if high >= entry_level and close < fvg["top"]:
                    # Confirmation: ПРЕДЫДУЩИЙ бар (i-1) должен закрыться в нижней части
                    # FIXED: проверяем бар i-1, не текущий
                    if confirm_pct > 0 and i > 0:
                        prev_close = df_m3["close"].iloc[i - 1]
                        prev_low = df_m3["low"].iloc[i - 1]
                        prev_high = df_m3["high"].iloc[i - 1]
                        prev_range = prev_high - prev_low if prev_high > prev_low else 0.0001
                        if (prev_high - prev_close) / prev_range < confirm_pct:
                            remaining_fvgs.append(fvg)
                            continue

                    # FIXED: Entry на open СЛЕДУЮЩЕГО бара (i+1)
                    if i + 1 >= len(df_m3):
                        remaining_fvgs.append(fvg)
                        continue
                    entry_price = df_m3["open"].iloc[i + 1]
                    entry_ts = df_m3.index[i + 1]

                    atr_val = atr_m3.iloc[i] if i < len(atr_m3) and not np.isnan(atr_m3.iloc[i]) else (atr_h1.iloc[-1] if len(atr_h1) > 0 and not np.isnan(atr_h1.iloc[-1]) else None)
                    if atr_val is None or np.isnan(atr_val) or atr_val <= 0:
                        remaining_fvgs.append(fvg)
                        continue
                    sl = fvg["top"] + atr_val * params["sl_atr_multiplier"]
                    risk = sl - entry_price
                    if risk <= 0:
                        continue
                    tp = entry_price - risk * params["tp_rr_ratio"]
                    be_level = entry_price - risk * params["be_trigger_rr"]

                    signals.append({
                        "timestamp": entry_ts,
                        "direction": "short",
                        "entry": entry_price,
                        "sl": sl,
                        "tp": tp,
                        "be_level": be_level,
                        "risk": risk,
                        "fvg": fvg,
                    })
                    continue

            remaining_fvgs.append(fvg)

        active_fvgs = remaining_fvgs

    return signals


# ============================================================
# Симуляция сделок
# ============================================================

def simulate_trades(signals, df_m3, params, instrument=None):
    """
    Симулирует сделки по сигналам.
    Для каждого сигнала проверяет: достиг TP, SL, или BE.

    Returns:
        list of dict: сделки с результатами
    """
    is_crypto = is_crypto_instrument(instrument)
    trades = []

    for signal in signals:
        entry_time = signal["timestamp"]
        direction = signal["direction"]
        entry = signal["entry"]
        sl = signal["sl"]
        tp = signal["tp"]
        be_level = signal["be_level"]
        be_triggered = False

        # Ищем выход после входа
        mask = df_m3.index > entry_time
        future_bars = df_m3[mask]

        result = None
        exit_time = None
        exit_price = None
        risk = signal["risk"]
        mfe_r = 0.0  # Maximum Favorable Excursion (в R)
        mae_r = 0.0  # Maximum Adverse Excursion (в R)

        for j in range(len(future_bars)):
            bar = future_bars.iloc[j]
            bar_time = future_bars.index[j]

            # Track MFE/MAE
            if direction == "long":
                bar_mfe = (bar["high"] - entry) / risk
                bar_mae = (entry - bar["low"]) / risk
            else:
                bar_mfe = (entry - bar["low"]) / risk
                bar_mae = (bar["high"] - entry) / risk
            mfe_r = max(mfe_r, bar_mfe)
            mae_r = max(mae_r, bar_mae)

            # Закрытие до 22:00 UTC+3 — только для форекс
            if not is_crypto and is_after_close(bar_time):
                exit_price = bar["close"]
                exit_time = bar_time
                result = "time_exit"
                break

            if direction == "long":
                # Проверяем BE
                if not be_triggered and bar["high"] >= be_level:
                    be_triggered = True
                    sl = entry  # SL на entry (безубыток)

                # SL
                if bar["low"] <= sl:
                    exit_price = sl
                    exit_time = bar_time
                    result = "sl" if not be_triggered else "be"
                    break

                # TP
                if bar["high"] >= tp:
                    exit_price = tp
                    exit_time = bar_time
                    result = "tp"
                    break

            elif direction == "short":
                if not be_triggered and bar["low"] <= be_level:
                    be_triggered = True
                    sl = entry

                if bar["high"] >= sl:
                    exit_price = sl
                    exit_time = bar_time
                    result = "sl" if not be_triggered else "be"
                    break

                if bar["low"] <= tp:
                    exit_price = tp
                    exit_time = bar_time
                    result = "tp"
                    break

        if result is None:
            # Сделка не закрылась в данных
            continue

        pnl_r = (exit_price - entry) / risk if direction == "long" else (entry - exit_price) / risk

        trades.append({
            "entry_time": entry_time,
            "exit_time": exit_time,
            "direction": direction,
            "entry": entry,
            "exit": exit_price,
            "sl": signal["sl"],
            "tp": tp,
            "result": result,
            "pnl_r": round(pnl_r, 4),
            "mfe_r": round(mfe_r, 4),
            "mae_r": round(mae_r, 4),
            "bars_held": j + 1,
        })

    return trades
