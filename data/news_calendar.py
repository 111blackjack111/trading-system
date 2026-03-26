"""
Economic news calendar for backtesting.
Generates known high-impact recurring events (NFP, CPI, FOMC, etc.)
and fetches historical dates.
"""

import os
import json
import pandas as pd
from datetime import datetime, timedelta
import requests

CALENDAR_FILE = os.path.join(os.path.dirname(__file__), "csv", "news_calendar.csv")

# High-impact recurring events and their typical schedule
# These are the events that cause "helicopters" (wild price swings)
HIGH_IMPACT_EVENTS = {
    "USD": [
        "Non-Farm Payrolls",         # First Friday of month
        "CPI",                        # ~13th of month
        "FOMC Statement",             # 8x per year
        "Fed Interest Rate Decision", # 8x per year
        "GDP",                        # Quarterly
        "Core PCE Price Index",       # ~last Friday of month
        "ISM Manufacturing PMI",      # First business day of month
        "Retail Sales",               # ~15th of month
    ],
    "GBP": [
        "BOE Interest Rate Decision",
        "CPI",
        "GDP",
        "Employment Change",
        "Retail Sales",
        "PMI Manufacturing",
    ],
    "EUR": [
        "ECB Interest Rate Decision",
        "CPI",
        "GDP",
        "PMI Manufacturing",
    ],
    "JPY": [
        "BOJ Interest Rate Decision",
        "CPI",
        "GDP",
        "Tankan Manufacturing Index",
    ],
}

# Currencies affected by each country's news
CURRENCY_MAP = {
    "USD": ["GBP_USD", "EUR_USD", "USD_JPY", "NZD_JPY", "GBP_JPY"],
    "GBP": ["GBP_USD", "EUR_GBP", "GBP_JPY"],
    "EUR": ["EUR_GBP", "EUR_USD"],
    "JPY": ["USD_JPY", "GBP_JPY", "NZD_JPY"],
}


def fetch_forexfactory_calendar(year, month):
    """Scrape ForexFactory calendar for a month."""
    url = f"https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    # This free API gives current week only. For historical, we generate from known patterns.
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


def generate_nfp_dates(start_year, end_year):
    """Generate NFP dates (first Friday of each month)."""
    dates = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            # Find first Friday
            d = datetime(year, month, 1)
            while d.weekday() != 4:  # Friday
                d += timedelta(days=1)
            dates.append({
                "date": d.strftime("%Y-%m-%d"),
                "time": "13:30",  # UTC
                "currency": "USD",
                "event": "Non-Farm Payrolls",
                "impact": "high",
            })
    return dates


def generate_fomc_dates(start_year, end_year):
    """FOMC meetings - 8 per year, known schedule."""
    # Approximate FOMC dates (Wednesday announcements)
    fomc_months = {
        2022: [(1,26),(3,16),(5,4),(6,15),(7,27),(9,21),(11,2),(12,14)],
        2023: [(2,1),(3,22),(5,3),(6,14),(7,26),(9,20),(11,1),(12,13)],
        2024: [(1,31),(3,20),(5,1),(6,12),(7,31),(9,18),(11,7),(12,18)],
        2025: [(1,29),(3,19),(5,7),(6,18),(7,30),(9,17),(11,5),(12,17)],
        2026: [(1,28),(3,18),(5,6),(6,17),(7,29),(9,16),(11,4),(12,16)],
    }
    dates = []
    for year in range(start_year, end_year + 1):
        for month, day in fomc_months.get(year, []):
            dates.append({
                "date": f"{year}-{month:02d}-{day:02d}",
                "time": "19:00",  # UTC
                "currency": "USD",
                "event": "FOMC Statement",
                "impact": "high",
            })
    return dates


def generate_cpi_dates(start_year, end_year, currency="USD"):
    """CPI dates - typically around 13th of month."""
    times = {"USD": "13:30", "GBP": "07:00", "EUR": "10:00", "JPY": "00:30"}
    dates = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            # CPI is usually around the 13th (varies by country)
            day = 13 if currency == "USD" else 15
            # Ensure it's a weekday
            d = datetime(year, month, day)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            dates.append({
                "date": d.strftime("%Y-%m-%d"),
                "time": times.get(currency, "10:00"),
                "currency": currency,
                "event": f"{currency} CPI",
                "impact": "high",
            })
    return dates


def generate_rate_decision_dates(start_year, end_year):
    """Central bank rate decisions."""
    dates = []
    # BOE - 8 per year (roughly every 6 weeks)
    boe_months = {
        2022: [(2,3),(3,17),(5,5),(6,16),(8,4),(9,22),(11,3),(12,15)],
        2023: [(2,2),(3,23),(5,11),(6,22),(8,3),(9,21),(11,2),(12,14)],
        2024: [(2,1),(3,21),(5,9),(6,20),(8,1),(9,19),(11,7),(12,19)],
        2025: [(2,6),(3,20),(5,8),(6,19),(8,7),(9,18),(11,6),(12,18)],
        2026: [(2,5),(3,19),(5,7),(6,18),(8,6),(9,17),(11,5),(12,17)],
    }
    for year in range(start_year, end_year + 1):
        for month, day in boe_months.get(year, []):
            dates.append({
                "date": f"{year}-{month:02d}-{day:02d}",
                "time": "12:00",
                "currency": "GBP",
                "event": "BOE Interest Rate Decision",
                "impact": "high",
            })

    # ECB - roughly monthly
    ecb_months = {
        2022: [(2,3),(3,10),(4,14),(6,9),(7,21),(9,8),(10,27),(12,15)],
        2023: [(2,2),(3,16),(5,4),(6,15),(7,27),(9,14),(10,26),(12,14)],
        2024: [(1,25),(3,7),(4,11),(6,6),(7,18),(9,12),(10,17),(12,12)],
        2025: [(1,30),(3,6),(4,17),(6,5),(7,17),(9,11),(10,30),(12,18)],
        2026: [(1,22),(3,5),(4,16),(6,4),(7,16),(9,10),(10,29),(12,17)],
    }
    for year in range(start_year, end_year + 1):
        for month, day in ecb_months.get(year, []):
            dates.append({
                "date": f"{year}-{month:02d}-{day:02d}",
                "time": "13:15",
                "currency": "EUR",
                "event": "ECB Interest Rate Decision",
                "impact": "high",
            })

    # BOJ - 8 per year
    boj_months = {
        2022: [(1,18),(3,18),(4,28),(6,17),(7,21),(9,22),(10,28),(12,20)],
        2023: [(1,18),(3,10),(4,28),(6,16),(7,28),(9,22),(10,31),(12,19)],
        2024: [(1,23),(3,19),(4,26),(6,14),(7,31),(9,20),(10,31),(12,19)],
        2025: [(1,24),(3,14),(5,1),(6,17),(7,31),(9,19),(10,31),(12,19)],
        2026: [(1,23),(3,13),(4,30),(6,16),(7,30),(9,18),(10,30),(12,18)],
    }
    for year in range(start_year, end_year + 1):
        for month, day in boj_months.get(year, []):
            dates.append({
                "date": f"{year}-{month:02d}-{day:02d}",
                "time": "03:00",
                "currency": "JPY",
                "event": "BOJ Interest Rate Decision",
                "impact": "high",
            })

    return dates




def generate_gdp_dates(start_year, end_year):
    """GDP releases - quarterly, ~last week of month after quarter ends."""
    times = {"USD": "13:30", "GBP": "07:00", "EUR": "10:00", "JPY": "00:50"}
    dates = []
    # GDP released ~1 month after quarter: Jan(Q4), Apr(Q1), Jul(Q2), Oct(Q3)
    gdp_months = [1, 4, 7, 10]
    for year in range(start_year, end_year + 1):
        for currency, time in times.items():
            for month in gdp_months:
                from datetime import datetime, timedelta
                # Advance estimate ~last Thursday of month
                d = datetime(year, month, 25)
                while d.weekday() != 3:  # Thursday
                    d += timedelta(days=1)
                if d.month != month:
                    d = datetime(year, month, 25)
                dates.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "time": time,
                    "currency": currency,
                    "event": f"{currency} GDP",
                    "impact": "high",
                })
    return dates


def generate_pmi_dates(start_year, end_year):
    """ISM/PMI Manufacturing - first business day of month."""
    dates = []
    pmi_config = {
        "USD": ("13:30", "ISM Manufacturing PMI"),
        "GBP": ("09:30", "PMI Manufacturing"),
        "EUR": ("09:00", "PMI Manufacturing"),
    }
    for year in range(start_year, end_year + 1):
        for currency, (time, name) in pmi_config.items():
            for month in range(1, 13):
                from datetime import datetime, timedelta
                d = datetime(year, month, 1)
                while d.weekday() >= 5:
                    d += timedelta(days=1)
                dates.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "time": time,
                    "currency": currency,
                    "event": f"{currency} {name}",
                    "impact": "high",
                })
    return dates


def generate_retail_sales_dates(start_year, end_year):
    """Retail Sales - typically ~15th of month."""
    times = {"USD": "13:30", "GBP": "07:00"}
    dates = []
    for year in range(start_year, end_year + 1):
        for currency, time in times.items():
            for month in range(1, 13):
                from datetime import datetime, timedelta
                d = datetime(year, month, 15)
                while d.weekday() >= 5:
                    d += timedelta(days=1)
                dates.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "time": time,
                    "currency": currency,
                    "event": f"{currency} Retail Sales",
                    "impact": "high",
                })
    return dates


def generate_pce_dates(start_year, end_year):
    """Core PCE Price Index (USD) - last Friday of month."""
    dates = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            from datetime import datetime, timedelta
            # Last day of month
            if month == 12:
                last_day = datetime(year + 1, 1, 1) - timedelta(days=1)
            else:
                last_day = datetime(year, month + 1, 1) - timedelta(days=1)
            # Find last Friday
            d = last_day
            while d.weekday() != 4:
                d -= timedelta(days=1)
            dates.append({
                "date": d.strftime("%Y-%m-%d"),
                "time": "13:30",
                "currency": "USD",
                "event": "Core PCE Price Index",
                "impact": "high",
            })
    return dates


def generate_employment_dates(start_year, end_year):
    """UK Employment Change - typically ~15th of month."""
    dates = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            from datetime import datetime, timedelta
            d = datetime(year, month, 14)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            dates.append({
                "date": d.strftime("%Y-%m-%d"),
                "time": "07:00",
                "currency": "GBP",
                "event": "GBP Employment Change",
                "impact": "high",
            })
    return dates


def build_calendar(start_year=2022, end_year=2026):
    """Build full news calendar."""
    all_events = []

    # NFP
    all_events.extend(generate_nfp_dates(start_year, end_year))

    # FOMC
    all_events.extend(generate_fomc_dates(start_year, end_year))

    # CPI for all currencies
    for curr in ["USD", "GBP", "EUR", "JPY"]:
        all_events.extend(generate_cpi_dates(start_year, end_year, curr))

    # Rate decisions
    all_events.extend(generate_rate_decision_dates(start_year, end_year))

    # GDP (all currencies)
    all_events.extend(generate_gdp_dates(start_year, end_year))

    # PMI Manufacturing (USD, GBP, EUR)
    all_events.extend(generate_pmi_dates(start_year, end_year))

    # Retail Sales (USD, GBP)
    all_events.extend(generate_retail_sales_dates(start_year, end_year))

    # Core PCE (USD)
    all_events.extend(generate_pce_dates(start_year, end_year))

    # UK Employment Change
    all_events.extend(generate_employment_dates(start_year, end_year))

    df = pd.DataFrame(all_events)
    df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"])
    df = df.sort_values("datetime").reset_index(drop=True)

    os.makedirs(os.path.dirname(CALENDAR_FILE), exist_ok=True)
    df.to_csv(CALENDAR_FILE, index=False)
    print(f"Calendar saved: {len(df)} high-impact events ({start_year}-{end_year})")
    return df


def load_calendar():
    """Load pre-built calendar."""
    if not os.path.exists(CALENDAR_FILE):
        return build_calendar()
    return pd.read_csv(CALENDAR_FILE, parse_dates=["datetime"])


def is_near_news(timestamp, instrument, minutes_before=30, minutes_after=30):
    """Check if timestamp is near a high-impact news event for the instrument."""
    cal = load_calendar()

    # Find which currencies affect this instrument
    affected_currencies = set()
    for curr, instruments in CURRENCY_MAP.items():
        if instrument in instruments:
            affected_currencies.add(curr)

    if not affected_currencies:
        return False

    # Filter relevant events
    relevant = cal[cal["currency"].isin(affected_currencies)]

    ts = pd.Timestamp(timestamp)
    before = ts - pd.Timedelta(minutes=minutes_before)
    after = ts + pd.Timedelta(minutes=minutes_after)

    nearby = relevant[(relevant["datetime"] >= before) & (relevant["datetime"] <= after)]
    return len(nearby) > 0


if __name__ == "__main__":
    df = build_calendar()
    print(f"\nSample events:")
    print(df.head(20).to_string())
    print(f"\nEvents by currency:")
    print(df["currency"].value_counts())
