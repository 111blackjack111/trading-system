"""
Centralized DB manager for the trading system.
All agents and dashboard use this module to read/write data.
"""

import os
import json
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "experiments.db")


def _conn():
    """Get a connection with WAL mode for concurrent reads."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration INTEGER,
            timestamp TEXT,
            param_changed TEXT,
            old_value REAL,
            new_value REAL,
            avg_score REAL,
            best_score REAL,
            best_instrument TEXT,
            total_trades INTEGER,
            avg_winrate REAL,
            avg_pf REAL,
            action TEXT,
            notes TEXT,
            params_snapshot TEXT
        );

        CREATE TABLE IF NOT EXISTS instrument_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration INTEGER,
            instrument TEXT,
            total_trades INTEGER,
            winrate REAL,
            profit_factor REAL,
            sharpe REAL,
            max_drawdown REAL,
            avg_rr REAL,
            total_r REAL,
            score REAL,
            timestamp TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration INTEGER,
            data TEXT,
            timestamp TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration INTEGER,
            type TEXT,
            param TEXT,
            old_value REAL,
            new_value REAL,
            reasoning TEXT,
            timestamp TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS holdout_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            timestamp TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS analyst_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration INTEGER,
            diagnosis TEXT,
            trend TEXT,
            recommendations TEXT,
            summary TEXT,
            timestamp TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.close()


# ── Instrument Metrics ──

def save_instrument_metrics(iteration, instrument, metrics):
    """Save metrics for one instrument after a backtest run."""
    conn = _conn()
    conn.execute(
        "INSERT INTO instrument_metrics "
        "(iteration, instrument, total_trades, winrate, profit_factor, "
        "sharpe, max_drawdown, avg_rr, total_r, score) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            iteration, instrument,
            metrics.get("total_trades", 0),
            metrics.get("winrate", 0),
            metrics.get("profit_factor", 0),
            metrics.get("sharpe", 0),
            metrics.get("max_drawdown", 0),
            metrics.get("avg_rr", 0),
            metrics.get("total_r", 0),
            metrics.get("score", 0),
        ),
    )
    conn.commit()
    conn.close()


def get_latest_instrument_metrics():
    """Get metrics from the most recent iteration for each instrument."""
    conn = _conn()
    rows = conn.execute("""
        SELECT instrument, total_trades, winrate, profit_factor, sharpe,
               max_drawdown, avg_rr, total_r, score, iteration, timestamp
        FROM instrument_metrics
        WHERE iteration = (SELECT MAX(iteration) FROM instrument_metrics)
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_instrument_metrics_history(instrument=None, cutoff=None):
    """Get metrics history, optionally filtered by instrument and time."""
    conn = _conn()
    q = "SELECT * FROM instrument_metrics WHERE 1=1"
    params = []
    if instrument:
        q += " AND instrument = ?"
        params.append(instrument)
    if cutoff:
        q += " AND timestamp >= ?"
        params.append(cutoff)
    q += " ORDER BY id"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Trade Log ──

def save_trade_log(iteration, data):
    """Save trade log data (JSON blob) for an iteration."""
    conn = _conn()
    conn.execute(
        "INSERT INTO trade_log (iteration, data) VALUES (?, ?)",
        (iteration, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def get_latest_trade_log():
    """Get the most recent trade log entry."""
    conn = _conn()
    row = conn.execute(
        "SELECT data, iteration, timestamp FROM trade_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row:
        return {"data": json.loads(row["data"]), "iteration": row["iteration"], "timestamp": row["timestamp"]}
    return None


# ── Suggestions ──

def _serialize_value(v):
    """Serialize dicts/lists to JSON string for SQLite storage."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def save_suggestion(iteration, suggestion):
    """Save optimizer suggestion."""
    conn = _conn()
    conn.execute(
        "INSERT INTO suggestions (iteration, type, param, old_value, new_value, reasoning) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            iteration,
            suggestion.get("type", "param_change"),
            suggestion.get("param", ""),
            _serialize_value(suggestion.get("old_value")),
            _serialize_value(suggestion.get("new_value")),
            suggestion.get("reasoning", ""),
        ),
    )
    conn.commit()
    conn.close()


def get_latest_suggestion():
    """Get the most recent suggestion."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM suggestions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Holdout Results ──

def save_holdout(data):
    """Save holdout test results."""
    conn = _conn()
    conn.execute(
        "INSERT INTO holdout_results (data) VALUES (?)",
        (json.dumps(data, ensure_ascii=False),),
    )
    conn.commit()
    conn.close()


def get_latest_holdout():
    """Get the most recent holdout results."""
    conn = _conn()
    row = conn.execute(
        "SELECT data, timestamp FROM holdout_results ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row:
        return {"data": json.loads(row["data"]), "timestamp": row["timestamp"]}
    return None


# ── Analyst Reports ──

def save_analyst_report(iteration, report):
    """Save analyst meta-analysis report."""
    conn = _conn()
    conn.execute(
        "INSERT INTO analyst_reports (iteration, diagnosis, trend, recommendations, summary) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            iteration,
            report.get("diagnosis", ""),
            report.get("trend", ""),
            json.dumps(report.get("recommendations", []), ensure_ascii=False),
            report.get("summary", ""),
        ),
    )
    conn.commit()
    conn.close()


def get_latest_analyst_report():
    """Get the most recent analyst report."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM analyst_reports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row:
        r = dict(row)
        r["recommendations"] = json.loads(r["recommendations"]) if r["recommendations"] else []
        return r
    return None


# Auto-init on import
init_db()
