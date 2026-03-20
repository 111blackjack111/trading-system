# AI Trading Optimization System

## Контекст проекта

Система автономной оптимизации торговой стратегии на основе SMC (Smart Money Concepts).
Инвестор и стратег: Виталик. Исполнитель: Claude Code + агенты.

Цель: найти лучшие параметры SMC стратегии и лучшие торговые инструменты через
автономный optimization loop (как в эксперименте Nunchi на X).

---

## Режим работы

Используй **Agent Teams** через TMUX.
Каждый агент запускается в отдельной TMUX сессии.
Агенты общаются через файлы в `/runtime/` и SQLite базу `db/experiments.db`.

Активация Agent Teams: settings.json уже настроен.

Команды TMUX:
- `tmux new-session -d -s <name>` - новая сессия
- `tmux send-keys -t <name> "<command>" Enter` - отправить команду
- `Ctrl+B, стрелки` - переключение между окнами

---

## Архитектура агентов

### DataAgent (`agents/data_agent.py`)
- Качает OHLCV данные для всех инструментов
- OANDA API: форекс пары + XAU/USD + GER40
- ccxt/Binance: BTC, ETH, SOL, BNB
- Сохраняет в `data/csv/<instrument>_<timeframe>.csv`
- Таймфреймы: M3, M15, H1
- История: 12 месяцев

### BacktestAgent (`agents/backtest_agent.py`)
- Принимает `strategy/params.json` и название инструмента
- Запускает `backtest/runner.py`
- Возвращает JSON метрики в `runtime/metrics_<instrument>.json`
- Запускает по всем инструментам параллельно

### OptimizerAgent (`agents/optimizer_agent.py`)
- Читает текущие `strategy/params.json`
- Читает историю экспериментов из `db/experiments.db`
- Вызывает Claude API (claude-sonnet-4-5)
- Предлагает ОДНО изменение одного параметра
- Пишет предложение в `runtime/suggestion.json`

### ImpulseAgent (`agents/impulse_agent.py`)
- Скачивает топ-200 монет по объёму за 6 месяцев
- Находит монеты с импульсом +50%+ за 1-7 дней
- Анализирует что было за 3-10 дней до импульса
- Сохраняет паттерны в `db/impulse_patterns.db`
- Мониторит текущий рынок на совпадение
- Алерт в Telegram при совпадении 7+ из 9 признаков

### OrchestratorAgent (`agents/orchestrator.py`)
- Главный цикл: запускает DataAgent → BacktestAgent → OptimizerAgent
- Принимает/откатывает изменения (keep/revert)
- Сохраняет каждую итерацию в `db/experiments.db`
- Накапливает `results/results.tsv` для мета-обучения
- По умолчанию: 100 итераций

---

## Торговые инструменты

### Форекс (OANDA)
- GBP/USD - основная, есть реальный торговый опыт
- EUR/USD - самая ликвидная
- GBP/JPY - высокая волатильность, хорошие SMC паттерны
- USD/JPY - чистые структуры
- EUR/GBP - медленнее, чистые FVG

### Металлы и Индексы (OANDA)
- XAU/USD - золото, отличные SMC зоны
- GER40 - DAX, европейская сессия

### Крипта (ccxt / Binance)
- BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT

---

## Стратегия (SMC)

Основана на реальной торговой системе трейдера.

### Логика входа
1. H1: определить тренд через BOS (Break of Structure)
2. H1: найти FVG (Fair Value Gap) в направлении тренда
3. Не торговать против FVG (ПРОТИВ ИМБОВ НЕ ТОРГОВАТЬ)
4. M3: дождаться входа цены в FVG
5. Проверить временное окно
6. Войти на закрытии M3 свечи при реакции от FVG

### Временные окна (UTC+3, Kyiv)
- Основные: 09:00-14:00, 15:00-17:00
- Silver Bullet: 10:00-11:00, 17:00-18:00, 21:00-22:00
- Закрыть все позиции до 22:00
- Пропускать понедельник (первые 2 часа)
- Осторожно в пятницу

### Управление позицией
- SL: за FVG (ATR x multiplier)
- TP: фиксированный RR
- БУ: при достижении 50% движения (be_trigger_rr = 0.5)

### Фильтры
- Не торговать в новости
- Волатильность: min ATR percentile
- Сессионный фильтр

---

## params.json - параметры оптимизации

```json
{
  "fvg_min_size_multiplier": 0.3,
  "fvg_entry_depth": 0.5,
  "ob_lookback": 15,
  "bos_swing_length": 10,
  "sl_atr_multiplier": 1.5,
  "be_trigger_rr": 0.5,
  "tp_rr_ratio": 2.0,
  "session_filter": true,
  "silver_bullet_only": false,
  "volatility_filter": true,
  "min_atr_percentile": 40,
  "news_filter": true,
  "crypto_hours_filter": true
}
```

Диапазоны для оптимизации:
- fvg_min_size_multiplier: 0.1 - 1.0
- fvg_entry_depth: 0.3 - 0.7
- ob_lookback: 5 - 30
- bos_swing_length: 5 - 25
- sl_atr_multiplier: 1.0 - 3.0
- be_trigger_rr: 0.3 - 0.7
- tp_rr_ratio: 1.5 - 3.0
- min_atr_percentile: 20 - 60

---

## Метрика оптимизации

```python
score = sharpe * 0.4 + profit_factor * 0.3 - max_drawdown * 0.2 + winrate * 0.1

# Штрафы (score = 0):
# - меньше 30 сделок
# - max_drawdown > 0.10
# - winrate < 0.40
```

---

## Структура проекта

```
trading-system/
├── CLAUDE.md                    # этот файл
├── requirements.txt
├── config.py                    # API ключи из env переменных
│
├── data/
│   ├── fetcher_oanda.py         # OANDA API
│   ├── fetcher_crypto.py        # ccxt / Binance
│   └── csv/                     # локальные данные
│
├── strategy/
│   ├── base_strategy.py         # SMC логика
│   └── params.json              # текущие параметры
│
├── backtest/
│   └── runner.py                # движок бэктеста
│
├── agents/
│   ├── orchestrator.py
│   ├── data_agent.py
│   ├── backtest_agent.py
│   ├── optimizer_agent.py
│   └── impulse_agent.py
│
├── db/
│   ├── experiments.db           # SQLite: все итерации
│   └── impulse_patterns.db      # паттерны перед импульсами
│
├── runtime/                     # временные файлы между агентами
│   ├── suggestion.json
│   └── metrics_<instrument>.json
│
└── results/
    ├── results.tsv              # мета-обучение
    └── REPORT.md                # лучшая стратегия текущего цикла
```

---

## config.py - переменные окружения

```python
import os

OANDA_API_KEY = os.environ.get("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")
OANDA_ENV = "practice"  # practice = demo, live = real

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")  # опционально для крипты
BINANCE_SECRET = os.environ.get("BINANCE_SECRET")    # public данные без ключей тоже работают

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
```

Установить переменные на сервере:
```bash
export OANDA_API_KEY="your_key"
export OANDA_ACCOUNT_ID="your_account"
export ANTHROPIC_API_KEY="your_key"
```

---

## Зависимости (requirements.txt)

```
smartmoneyconcepts
backtesting
oandapyv20
ccxt
anthropic
pandas
numpy
python-telegram-bot
sqlite3  # встроен в Python
```

---

## Порядок реализации

### Фаза 0 - Окружение
1. Создать структуру папок
2. Написать requirements.txt
3. Написать config.py
4. Установить зависимости: `pip install -r requirements.txt`

### Фаза 1 - DataAgent
1. `data/fetcher_oanda.py` - форекс + gold + GER40
2. `data/fetcher_crypto.py` - BTC/ETH/SOL/BNB
3. Скачать 12 месяцев истории, сохранить в CSV
4. Проверить данные

### Фаза 2 - Стратегия
1. `strategy/base_strategy.py` - FVG + BOS + сессионный фильтр
2. `strategy/params.json` - базовые параметры
3. Ручная проверка на 10-15 сделках

### Фаза 3 - Бэктест
1. `backtest/runner.py` - движок
2. Запуск по всем 11 инструментам
3. Baseline: рейтинг инструментов

### Фаза 4 - Агенты
1. `agents/optimizer_agent.py` - Claude API
2. `agents/orchestrator.py` - главный цикл
3. Запуск 20-30 итераций
4. Анализ результатов

### Фаза 5 - ImpulseAgent
1. `agents/impulse_agent.py`
2. Анализ 200 монет за 6 месяцев
3. Паттерны → мониторинг → Telegram

### Фаза 6 - Алерты
1. Telegram бот для сигналов стратегии
2. Алерты ImpulseAgent
3. Мониторинг в реальном времени

---

## Autoresearch — принцип работы системы

Термин popularised Андреем Карпати (бывший директор AI Tesla, сооснователь OpenAI).

Суть: AI делает весь исследовательский цикл сам:

```
гипотеза → изменил код → протестировал → результат лучше?
    ↓ да: keep                ↓ нет: revert
    └──────── следующая гипотеза ←──────┘
```

100 итераций без перерыва. AI не устаёт, не имеет эго, честно откатывает неудачные эксперименты.

### Отличие от ML
- ML меняет числа (веса). Архитектура фиксирована.
- Autoresearch меняет сам код и логику — может добавить индикатор, убрать старый, переписать логику входа.

### Итерация самого процесса (важно!)
Улучшать нужно не только стратегию, но и сам процесс поиска:

- **Cycle 1** — базовый запуск. Смотрим что нашёл. Анализируем какие изменения давали прирост.
- **Cycle 2** — улучшаем CLAUDE.md: добавляем новые параметры, расширяем диапазоны, меняем метрику если нужно.
- **Cycle 2.1** — новые данные (следующий месяц), старт с лучшей стратегии предыдущего цикла.
- **Cycle N** — мета-обучение: Claude анализирует results.tsv всех циклов, ищет паттерны ("RSI работает в тренде, mean-reversion — в боковике").

### Ограничения (держать в голове)
- Overfitting — главный враг. Holdout тест (последние 2 месяца) обязателен перед live.
- Локальный оптимум — если 20 итераций подряд нет улучшения, перезапустить с другой точки.
- Качество = качество CLAUDE.md. Плохие инструкции = плохой поиск.
- AI не придумает принципиально новую концепцию — только комбинирует известное.

### Расписание циклов
- Каждую неделю: перекачать свежие данные, запустить новый цикл 100 итераций.
- Каждый месяц: обновить CLAUDE.md на основе накопленных результатов.
- Автоматизировать через cron на Cloud сервере.

---

## Важные правила для агентов

- Optimizer меняет ОДИН параметр за итерацию - не несколько
- Каждая итерация пишется в db/experiments.db независимо от результата
- Если score улучшился - keep, если нет - revert params.json
- Минимум 30 сделок для валидной метрики
- Крипта торгуется 24/7 но фильтровать по London/NY часам
- Форекс: строго по временным окнам стратегии
- Не торговать XAU/USD и GER40 ночью
