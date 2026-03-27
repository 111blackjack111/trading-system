"""
OrchestratorAgent v3 — автономный координатор с самозащитой.

Три механизма автономности:
1. Blacklist: параметр с 2+ revert → не трогать 20 итераций
2. Stuck detector: 7 ревертов подряд → алерт + расширить диапазоны
3. Conflict detector: WR↑ но score↓ в 10x → аномалия, пропустить
"""

import os
import sys
import json
import time
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.base_strategy import load_params, save_params
from agents.optimizer_agent import suggest_change, PARAM_RANGES
from agents.analyst_agent import run_analysis, apply_recommendations
from agents.trade_analyst import run_analysis as run_trade_analysis
from agents.data_agent import run as run_data_agent

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "runtime")
DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")
DB_PATH = os.path.join(DB_DIR, "experiments.db")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")
REQUEST_FILE = os.path.join(RUNTIME_DIR, "backtest_request.json")
DONE_FILE = os.path.join(RUNTIME_DIR, "backtest_done.json")

TIMEOUT_BACKTEST_DAY = 1800   # 30 min for 1-2 core instruments (GBP_USD only during day)
TIMEOUT_BACKTEST_NIGHT = 3600  # 60 min for 7 instruments during night


def is_night_mode():
    """Ночной режим: 00:00-08:00 Kyiv (UTC+2). Opus + все инструменты + автономия."""
    kyiv_hour = datetime.now(timezone.utc).hour + 2  # UTC+2 (EET)
    if kyiv_hour >= 24:
        kyiv_hour -= 24
    return 0 <= kyiv_hour < 8

# ============================================================
# Telegram helper
# ============================================================

def send_telegram(message):
    """Отправляет сообщение в Telegram."""
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


# ============================================================
# Blacklist — параметры с 2+ revert не трогать 20 итераций
# ============================================================

class ParamBlacklist:
    """Blacklist с persistence в SQLite — выживает рестарты."""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.revert_counts = {}  # param -> count
        self.cooldown = {}       # param -> iteration when unblocked
        self._init_table()
        self._load()

    def _init_table(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS param_blacklist (
                param TEXT PRIMARY KEY,
                revert_count INTEGER DEFAULT 0,
                cooldown_until INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

    def _load(self):
        conn = sqlite3.connect(self.db_path)
        for row in conn.execute("SELECT param, revert_count, cooldown_until FROM param_blacklist"):
            self.revert_counts[row[0]] = row[1]
            if row[2] > 0:
                self.cooldown[row[0]] = row[2]
        conn.close()
        if self.cooldown:
            print(f"  [Blacklist] Loaded {len(self.cooldown)} blocked params from DB")

    def _save(self, param):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO param_blacklist (param, revert_count, cooldown_until)
            VALUES (?, ?, ?)
        """, (param, self.revert_counts.get(param, 0), self.cooldown.get(param, 0)))
        conn.commit()
        conn.close()

    def record_revert(self, param, current_iter):
        self.revert_counts[param] = self.revert_counts.get(param, 0) + 1
        if self.revert_counts[param] >= 2:
            self.cooldown[param] = current_iter + 20
            print(f"  [Blacklist] {param} blocked for 20 iterations (reverts: {self.revert_counts[param]})")
        self._save(param)

    def record_keep(self, param):
        self.revert_counts[param] = 0
        if param in self.cooldown:
            del self.cooldown[param]
        self._save(param)

    def is_blocked(self, param, current_iter):
        if param in self.cooldown and current_iter < self.cooldown[param]:
            return True
        elif param in self.cooldown and current_iter >= self.cooldown[param]:
            del self.cooldown[param]
            self.revert_counts[param] = 0
            self._save(param)
        return False


# ============================================================
# Stuck detector — 7 ревертов подряд → алерт + расширить диапазоны
# ============================================================

def expand_param_ranges(factor=1.2):
    """Расширяет диапазоны параметров на factor."""
    for param in PARAM_RANGES:
        low, high = PARAM_RANGES[param]
        center = (low + high) / 2
        half_range = (high - low) / 2 * factor
        PARAM_RANGES[param] = (round(center - half_range, 2), round(center + half_range, 2))
    print(f"  [StuckDetector] Param ranges expanded by {factor}x")


# ============================================================
# Conflict detector — WR↑ но score↓ в 10x → аномалия
# ============================================================

def is_anomaly(new_score, best_score, total_trades):
    """Определяет аномальные результаты бэктеста.
    - score < -100: явно сломано (< 10 trades = -999)
    - score < -10 и < 30 trades: недостаточно данных для надёжного результата
    """
    if new_score < -100:
        return True
    if new_score < -10 and total_trades < 30:
        return True
    return False


# ============================================================
# Snapshot — сохранение/восстановление params перед рискованными изменениями
# ============================================================

SNAPSHOT_PATH = os.path.join(RUNTIME_DIR, "params_snapshot.json")
SNAPSHOT_SCORE_PATH = os.path.join(RUNTIME_DIR, "snapshot_score.json")


def save_snapshot(params, score):
    """Сохраняет snapshot params перед рекомендациями AnalystAgent."""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(params, f, indent=2)
    with open(SNAPSHOT_SCORE_PATH, "w") as f:
        json.dump({"score": score, "timestamp": time.time()}, f)
    print(f"  [Snapshot] Saved (score: {score:.4f})")


def restore_snapshot():
    """Восстанавливает params из snapshot."""
    if not os.path.exists(SNAPSHOT_PATH):
        return None, None
    with open(SNAPSHOT_PATH) as f:
        params = json.load(f)
    score = 0
    if os.path.exists(SNAPSHOT_SCORE_PATH):
        with open(SNAPSHOT_SCORE_PATH) as f:
            score = json.load(f).get("score", 0)
    save_params(params)
    print(f"  [Snapshot] Restored (score: {score:.4f})")
    return params, score


def check_degradation(current_score, iterations_since_analyst):
    """Проверяет деградацию после рекомендаций AnalystAgent (через 3 итерации).
    Ночью строже: 15% порог, днём: 20%."""
    if iterations_since_analyst != 3:
        return False
    if not os.path.exists(SNAPSHOT_SCORE_PATH):
        return False
    with open(SNAPSHOT_SCORE_PATH) as f:
        snapshot_score = json.load(f).get("score", 0)
    if snapshot_score <= 0:
        return False
    threshold = 0.85 if is_night_mode() else 0.80  # ночью строже
    if current_score < snapshot_score * threshold:
        return True
    return False


# ============================================================
# DB & File operations
# ============================================================

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
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
        )
    """)
    conn.commit()
    conn.close()


def is_duplicate_experiment(param, new_value, tolerance=0.01):
    """Проверяет, был ли уже такой (param, value) в экспериментах."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT COUNT(*) FROM experiments WHERE param_changed = ? AND ABS(new_value - ?) < ?",
        (param, new_value, tolerance)
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0


def request_backtest(params, request_id, changed_param=""):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    if os.path.exists(DONE_FILE):
        os.remove(DONE_FILE)
    request = {"id": request_id, "params": params, "timestamp": time.time(), "changed_param": changed_param}
    # Atomic write: temp file + rename to prevent partial reads
    tmp_req = REQUEST_FILE + ".tmp"
    with open(tmp_req, "w") as f:
        json.dump(request, f, indent=2)
    os.rename(tmp_req, REQUEST_FILE)
    group = "crypto" if changed_param.startswith("crypto_overrides.") else ("forex" if changed_param.startswith("forex_overrides.") else "all")
    print(f"  [Orchestrator] Backtest request #{request_id} sent (group: {group})")


def wait_for_backtest(request_id):
    timeout = TIMEOUT_BACKTEST_NIGHT if is_night_mode() else TIMEOUT_BACKTEST_DAY
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(DONE_FILE):
            with open(DONE_FILE) as f:
                result = json.load(f)
            if result.get("id") == request_id or "error" in result:
                os.remove(DONE_FILE)
                return result
        time.sleep(2)
    print("  [Orchestrator] WARNING: Backtest timeout!")
    return {"error": "timeout", "avg_score": 0, "results": {}}


def save_experiment(iteration, suggestion, backtest_result, action, params):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()

    metrics_all = backtest_result.get("results", {})
    scores, winrates, pfs = [], [], []
    best_score, best_inst, total_trades = -float("inf"), "", 0

    for inst, m in metrics_all.items():
        if m and m.get("score") is not None:
            scores.append(m["score"])
            total_trades += m.get("total_trades", 0)
            winrates.append(m.get("winrate", 0))
            pfs.append(m.get("profit_factor", 0))
            if m["score"] > best_score:
                best_score = m["score"]
                best_inst = inst

    if scores:
        avg_score = sum(scores) / len(scores)
    else:
        avg_score = backtest_result.get("avg_score", 0)
    if best_score == -float("inf"):
        best_score = 0

    avg_wr = round(sum(winrates) / len(winrates), 4) if winrates else 0
    avg_pf = round(sum(pfs) / len(pfs), 4) if pfs else 0

    conn.execute("""
        INSERT INTO experiments
        (iteration, timestamp, param_changed, old_value, new_value,
         avg_score, best_score, best_instrument, total_trades,
         avg_winrate, avg_pf, action, notes, params_snapshot)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        iteration, now,
        suggestion.get("param", "baseline"),
        str(suggestion.get("old_value", 0)),
        str(suggestion.get("new_value", 0)),
        round(avg_score, 4), round(best_score, 4),
        best_inst, total_trades, avg_wr, avg_pf,
        action, suggestion.get("reasoning", ""),
        json.dumps(params),
    ))
    conn.commit()
    conn.close()

    # TSV removed — all data is in experiments.db


# ============================================================
# Night report — отправляется утром при переходе ночь→день
# ============================================================

CORE_INSTRUMENTS = {"GBP_USD"}


def send_night_report(night_results):
    """Утренний отчёт: как ночные пары показали себя."""
    if not night_results:
        return

    lines = ["🌙 <b>Ночной отчёт (Opus autonomous)</b>\n"]

    core_lines = []
    test_lines = []
    for inst in sorted(night_results.keys()):
        r = night_results[inst]
        emoji = "✅" if r.get("total_r", 0) > 0 else "❌"
        line = (f"  {emoji} {inst}: score={r.get('score', 0):.2f}, "
                f"WR={r.get('winrate', 0):.0%}, "
                f"{r.get('total_r', 0):+.0f}R, "
                f"PF={r.get('profit_factor', 0):.1f} "
                f"({r.get('trades', 0)} trades)")
        if inst in CORE_INSTRUMENTS:
            core_lines.append(line)
        else:
            test_lines.append(line)

    lines.append("📊 <b>Core пары:</b>")
    lines.extend(core_lines if core_lines else ["  Нет данных"])
    lines.append("\n🧪 <b>Тестовые пары:</b>")
    lines.extend(test_lines if test_lines else ["  Нет данных"])

    # Рекомендации по тестовым
    good_tests = [inst for inst, r in night_results.items()
                  if inst not in CORE_INSTRUMENTS
                  and r.get("total_r", 0) > 0
                  and r.get("trades", 0) >= 10]
    if good_tests:
        lines.append(f"\n💡 <b>Кандидаты в CORE:</b> {', '.join(good_tests)}")
        lines.append("Решение за CEO")

    send_telegram("\n".join(lines))
    print(f"  [Night Report] Sent to Telegram ({len(night_results)} instruments)")


# ============================================================
# Main loop
# ============================================================

def run(max_iterations=100, skip_data_download=False):
    init_db()
    blacklist = ParamBlacklist()

    print("=" * 60)
    print(f"ORCHESTRATOR v3: Autoresearch ({max_iterations} iterations)")
    print("  Blacklist: ON | Stuck detector: ON | Conflict detector: ON")
    print("=" * 60)

    # Шаг 0: Данные
    if not skip_data_download:
        print("\n[Step 0] Downloading data...")
        run_data_agent(months=12)

    # Шаг 1: Baseline
    print("\n[Iteration 0] Baseline backtest...")
    params = load_params()
    request_backtest(params, "baseline")
    baseline_result = wait_for_backtest("baseline")
    baseline_score = baseline_result.get("avg_score", 0)

    save_experiment(0, {"param": "baseline", "reasoning": "Initial baseline"}, baseline_result, "baseline", params)
    print(f"\n  Baseline avg_score: {baseline_score:.4f}")

    best_score = baseline_score
    no_improvement_count = 0
    consecutive_reverts = 0
    ranges_expanded = False
    run._analyst_iter = -100  # sentinel
    was_night = is_night_mode()
    night_results = {}  # {instrument: {score, trades, winrate, total_r}} — для ночного отчёта

    # Шаг 2: Итерации
    for i in range(1, max_iterations + 1):
        night = is_night_mode()
        mode_str = "🌙 NIGHT (Opus+all)" if night else "☀️ DAY (Sonnet+core)"
        print(f"\n{'=' * 60}")
        print(f"[Iteration {i}/{max_iterations}] {mode_str} (reverts: {consecutive_reverts}, best: {best_score:.4f})")
        print(f"{'=' * 60}")

        # Переход ночь→день: отправить ночной отчёт
        if was_night and not night:
            send_night_report(night_results)
            night_results = {}
        was_night = night

        params_backup = load_params()

        # Ночью: snapshot каждую итерацию (усиленная защита)
        if night:
            save_snapshot(params_backup, best_score)

        # Optimizer
        print("\n  [Optimizer] Getting suggestion...")
        try:
            blocked = {p for p, until_iter in blacklist.cooldown.items() if i < until_iter}
            suggestion = suggest_change(params_backup, blacklisted_params=blocked)
        except Exception as e:
            print(f"  Optimizer error: {e}")
            save_experiment(i, {"param": f"error: {e}", "old_value": 0, "new_value": 0, "reasoning": str(e)},
                           {"avg_score": 0, "results": {}}, "error", params_backup)
            continue

        param_name = suggestion.get("param", "")

        # === BLACKLIST CHECK ===
        if blacklist.is_blocked(param_name, i):
            print(f"  [Blacklist] {param_name} is blocked — skipping")
            save_experiment(i, suggestion, {"avg_score": 0, "results": {}}, "blacklisted", params_backup)
            continue

        # === DEDUP CHECK ===
        if suggestion.get("type", "param_change") == "param_change":
            if is_duplicate_experiment(param_name, suggestion.get("new_value", 0)):
                print(f"  [Dedup] {param_name}={suggestion['new_value']} already tried — skipping")
                save_experiment(i, suggestion, {"avg_score": 0, "results": {}}, "duplicate", params_backup)
                continue

        # Применяем
        change_type = suggestion.get("type", "param_change")
        strategy_backup = None

        if change_type == "code_change":
            strategy_path = os.path.join(os.path.dirname(__file__), "..", "strategy", "base_strategy.py")
            with open(strategy_path) as f:
                strategy_backup = f.read()
            old_code = suggestion.get("old_code", "")
            new_code = suggestion.get("new_code", "")
            if old_code and new_code and old_code in strategy_backup:
                new_strategy = strategy_backup.replace(old_code, new_code, 1)
                with open(strategy_path, "w") as f:
                    f.write(new_strategy)
                print(f"  [Orchestrator] Applied code change: {suggestion.get('change_description', 'N/A')}")
                new_params = params_backup.copy()
            else:
                print(f"  [Orchestrator] Code change failed — old_code not found")
                save_experiment(i, suggestion, {"avg_score": 0, "results": {}}, "skip_code_fail", params_backup)
                continue
        else:
            new_params = params_backup.copy()
            change_type = suggestion.get("type", "param_change")

            if change_type == "multi_param_change":
                # Применяем несколько параметров
                for ch in suggestion.get("changes", []):
                    p = ch["param"]
                    if "." in p:
                        group, key = p.split(".", 1)
                        if group not in new_params:
                            new_params[group] = {}
                        new_params[group][key] = ch["new_value"]
                    else:
                        new_params[p] = ch["new_value"]
            elif "." in param_name:
                group, key = param_name.split(".", 1)
                if group not in new_params:
                    new_params[group] = {}
                new_params[group][key] = suggestion["new_value"]
            else:
                new_params[param_name] = suggestion["new_value"]
            save_params(new_params)

        # Backtest
        print("\n  [Backtest] Requesting parallel backtest...")
        request_id = f"iter_{i}"
        request_backtest(new_params, request_id, changed_param=param_name)
        bt_result = wait_for_backtest(request_id)

        new_score = bt_result.get("avg_score", 0)
        total_trades_new = sum(
            m.get("total_trades", 0) for m in bt_result.get("results", {}).values() if m
        )
        # Трекинг ночных результатов по инструментам
        if is_night_mode():
            for inst, res in bt_result.get("results", {}).items():
                if res and res.get("metrics"):
                    m = res["metrics"]
                    night_results[inst] = {
                        "score": m.get("score", 0),
                        "trades": m.get("total_trades", 0),
                        "winrate": m.get("winrate", 0),
                        "total_r": m.get("total_r", 0),
                        "profit_factor": m.get("profit_factor", 0),
                    }

        # === ANOMALY DETECTOR ===
        if is_anomaly(new_score, best_score, total_trades_new):
            print(f"\n  ANOMALY: score={new_score:.4f}, trades={total_trades_new} — skipping")
            save_params(params_backup)
            save_experiment(i, suggestion, bt_result, "anomaly", new_params)
            blacklist.record_revert(param_name, i)
            continue

        # === EXPLORATION MODE ===
        # Первые 30 итераций: принимаем результаты в пределах 95% от лучшего
        # После 30: строгий monotonic improvement
        explore_tolerance = 0.95 if i <= 30 else 1.0
        score_threshold = best_score * explore_tolerance if best_score > 0 else best_score

        # Keep / Revert
        if new_score >= score_threshold and total_trades_new >= 30 and new_score != 0:
            action = "keep"
            improvement = new_score - best_score
            if new_score > best_score:
                best_score = new_score
            no_improvement_count = 0
            consecutive_reverts = 0
            blacklist.record_keep(param_name)
            if improvement > 0:
                print(f"\n  KEEP: score {new_score:.4f} (+{improvement:.4f})")
            else:
                print(f"\n  KEEP (explore): score {new_score:.4f} (best: {best_score:.4f}, within {explore_tolerance:.0%})")
        else:
            action = "revert"
            if change_type == "code_change" and strategy_backup:
                strategy_path = os.path.join(os.path.dirname(__file__), "..", "strategy", "base_strategy.py")
                with open(strategy_path, "w") as f:
                    f.write(strategy_backup)
                print(f"\n  REVERT CODE: score {new_score:.4f} (best: {best_score:.4f})")
            else:
                save_params(params_backup)
                print(f"\n  REVERT: score {new_score:.4f} (threshold: {score_threshold:.4f})")
            no_improvement_count += 1
            consecutive_reverts += 1
            blacklist.record_revert(param_name, i)

        save_experiment(i, suggestion, bt_result, action, new_params)

        # === OVERFITTING CHECK (on keep) ===
        if action == "keep":
            warnings = []
            # Check 1: trades dropped more than 20%
            baseline_trades = baseline_result.get("results", {})
            baseline_total = sum(m.get("total_trades", 0) for m in baseline_trades.values() if m)
            if baseline_total > 0 and total_trades_new < baseline_total * 0.8:
                warnings.append(f"Trades dropped {baseline_total} -> {total_trades_new} ({total_trades_new/baseline_total:.0%})")

            # Check 2: single instrument dominance (>60% of total score)
            inst_results = bt_result.get("results", {})
            inst_scores = {}
            for inst, m in inst_results.items():
                if m:
                    s = m.get("score", m.get("metrics", {}).get("score", 0))
                    inst_scores[inst] = s
            if inst_scores:
                total_score_abs = sum(abs(s) for s in inst_scores.values())
                if total_score_abs > 0:
                    for inst, s in inst_scores.items():
                        if abs(s) / total_score_abs > 0.6:
                            warnings.append(f"{inst} dominates score ({s:.2f} = {abs(s)/total_score_abs:.0%})")

            if warnings:
                warn_msg = " | ".join(warnings)
                print(f"\n  [Overfitting] WARNING: {warn_msg}")
                send_telegram(f"Overfitting warning (iter {i}):\n{warn_msg}")

        # === STUCK DETECTOR ===
        if consecutive_reverts >= 7 and not ranges_expanded:
            print(f"\n  [StuckDetector] {consecutive_reverts} reverts in a row!")
            expand_param_ranges(1.2)
            ranges_expanded = True
            send_telegram(
                f"⚠️ <b>Stuck detected!</b>\n"
                f"{consecutive_reverts} reverts подряд (iter {i})\n"
                f"Диапазоны расширены на 20%\n"
                f"Best score: {best_score:.4f}"
            )

        if consecutive_reverts >= 15:
            send_telegram(
                f"🔴 <b>Система застряла!</b>\n"
                f"{consecutive_reverts} reverts подряд\n"
                f"Score: {best_score:.4f}\n"
                f"Нужно вмешательство CEO"
            )

        if no_improvement_count >= 20:
            print(f"\n  WARNING: {no_improvement_count} iterations without improvement!")

        # === DEGRADATION CHECK (3 итерации после AnalystAgent) ===
        if hasattr(run, '_analyst_iter') and i - run._analyst_iter == 3:
            if check_degradation(best_score, 3):
                old_score = best_score
                params_restored, snap_score = restore_snapshot()
                if params_restored:
                    best_score = snap_score
                    send_telegram(
                        f"🔄 <b>Автооткат!</b>\n"
                        f"Рекомендации AnalystAgent ухудшили систему\n"
                        f"Score: {snap_score:.2f} → {old_score:.2f} (деградация > 20%)\n"
                        f"Восстановлены params из snapshot"
                    )
                    print(f"  [Degradation] Auto-rollback! Score {old_score:.4f} → restored {snap_score:.4f}")
                    consecutive_reverts = 0

        # === TRADE ANALYST (каждые 15 итераций — глубокий анализ сделок) ===
        if i % 15 == 0:
            try:
                analysis = run_trade_analysis()
                if analysis:
                    summary = analysis.get("summary_ru", "")
                    recs = analysis.get("recommendations", [])
                    if summary:
                        send_telegram(f"Trade Analysis:\n{summary}")
                    # Высокоуверенные рекомендации → в Telegram для CEO
                    for r in recs:
                        if r.get("confidence", 0) >= 0.7:
                            send_telegram(
                                f"Trade Analyst rec ({r.get('confidence', 0):.0%}):\n"
                                f"{r.get('description', '')}\n"
                                f"Impact: {r.get('expected_impact', '')}"
                            )
            except Exception as e:
                print(f"  [TradeAnalyst] Error: {e}")

        # === ANALYST AGENT (каждые 10 итераций) ===
        if i % 10 == 0:
            print(f"\n  [Analyst] Running meta-analysis (every 10 iterations)...")
            try:
                # SNAPSHOT перед любыми изменениями
                save_snapshot(load_params(), best_score)
                run._analyst_iter = i

                bl_info = ", ".join(f"{p}(until iter {v})" for p, v in blacklist.cooldown.items()) or "none"
                report = run_analysis(consecutive_reverts, bl_info)
                if report:
                    # Применяем ТОЛЬКО ОДНУ рекомендацию за раз (не все сразу)
                    # Ночью: порог 0.5 (Opus автономен), днём: 0.8 (ждём CEO)
                    conf_threshold = 0.5 if is_night_mode() else 0.8
                    recs = report.get("recommendations", [])
                    auto_recs = [r for r in recs if r.get("confidence", 0) >= conf_threshold]
                    ceo_recs = [r for r in recs if r.get("confidence", 0) < conf_threshold]
                    if is_night_mode():
                        print(f"  [Night Mode] Confidence threshold: {conf_threshold} (auto: {len(auto_recs)}, CEO: {len(ceo_recs)})")

                    if auto_recs:
                        # Применяем только первую авто-рекомендацию
                        applied = apply_recommendations({"recommendations": auto_recs[:1]}, PARAM_RANGES)
                        if applied:
                            print(f"  [Analyst] Applied ONE rec: {applied}")
                            if ranges_expanded and any("Range" in a for a in applied):
                                ranges_expanded = False

                    if ceo_recs:
                        # Остальные — в Telegram для CEO
                        for r in ceo_recs:
                            send_telegram(
                                f"🔍 Analyst рекомендует (confidence: {r.get('confidence', 0):.0%})\n"
                                f"{r.get('type', 'unknown')}: {r.get('description', 'N/A')}\n"
                                f"{r.get('reasoning', '')}\n"
                                f"Применить? Ответь в чате с Claude Code"
                            )
            except Exception as e:
                print(f"  [Analyst] Error: {e}")

    # Отправить ночной отчёт если закончили ночью
    if night_results:
        send_night_report(night_results)

    # Финал
    print(f"\n{'=' * 60}")
    print(f"ORCHESTRATOR v3: Complete. Best score: {best_score:.4f}")
    print(f"{'=' * 60}")
    generate_report(best_score)


def generate_report(best_score):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    params = load_params()
    now = datetime.now(timezone.utc).isoformat()
    report = f"""# Autoresearch Report
Generated: {now}

## Best Score: {best_score:.4f}

## Optimized Parameters
```json
{json.dumps(params, indent=2)}
```

## Results
See `results.tsv` for full history.
See `db/experiments.db` for detailed metrics.
"""
    with open(os.path.join(RESULTS_DIR, "REPORT.md"), "w") as f:
        f.write(report)
    print("  Report saved to results/REPORT.md")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--skip-data", action="store_true")
    args = parser.parse_args()

    run(max_iterations=args.iterations, skip_data_download=args.skip_data)
