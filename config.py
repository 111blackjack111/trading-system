import os

# OANDA (Forex, XAU/USD, GER40)
OANDA_API_KEY = os.environ.get("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")
OANDA_ENV = "practice"  # practice = demo, live = real

# Anthropic (OptimizerAgent)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Binance (Crypto) - public данные работают без ключей
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET = os.environ.get("BINANCE_SECRET")

# Telegram (алерты)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
