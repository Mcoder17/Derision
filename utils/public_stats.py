import sqlite3
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "public_stats.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SLOT_SECONDS = 8 * 60 * 60

# Serialize writes inside this module to reduce SQLite lock contention.
_WRITE_LOCK = threading.RLock()

app = FastAPI(title="Derision Public Stats API")

app.add_middleware(
    GZipMiddleware,
    minimum_size=512,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=60,
        isolation_level=None,  # autocommit mode
    )
    conn.row_factory = sqlite3.Row

    # Best-practice pragmas for a small app with mixed reads/writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-20000")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")

    return conn


def _write(op: Callable[[sqlite3.Connection], Any]) -> Any:
    """
    Run a write operation under a module-local lock.
    This keeps public_stats writes from colliding with each other.
    """
    with _WRITE_LOCK, _conn() as conn:
        return op(conn)


def init_db() -> None:
    def _init(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                interactions_total INTEGER NOT NULL DEFAULT 0,
                messages_analyzed_total INTEGER NOT NULL DEFAULT 0,
                current_ping_ms REAL NOT NULL DEFAULT 0,
                public_state TEXT NOT NULL DEFAULT 'healthy',
                public_note TEXT NOT NULL DEFAULT '',
                last_started_at INTEGER NOT NULL DEFAULT 0,
                restart_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                interactions INTEGER NOT NULL DEFAULT 0,
                messages_analyzed INTEGER NOT NULL DEFAULT 0,
                avg_ping REAL NOT NULL DEFAULT 0,
                ping_samples INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                resolved_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS availability_samples (
                slot_start INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
            """
        )

        conn.execute("INSERT OR IGNORE INTO stats (id) VALUES (1)")
        conn.commit()

    _write(_init)


@app.on_event("startup")
def _startup_init() -> None:
    init_db()


def mark_startup() -> None:
    now = int(time.time())

    def _mark(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE stats
            SET last_started_at = ?, restart_count = restart_count + 1
            WHERE id = 1
            """,
            (now,),
        )
        conn.commit()

    _write(_mark)


def _today() -> str:
    return date.today().isoformat()


def _ensure_daily_row(conn: sqlite3.Connection, day: str) -> None:
    conn.execute("INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (day,))


def increment_total(field: str, amount: int = 1) -> None:
    if field not in {"interactions_total", "messages_analyzed_total"}:
        raise ValueError("Unsupported total field")

    def _inc(conn: sqlite3.Connection) -> None:
        conn.execute(f"UPDATE stats SET {field} = {field} + ? WHERE id = 1", (amount,))
        conn.commit()

    _write(_inc)


def add_daily(metric: str, amount: int = 1) -> None:
    if metric not in {"interactions", "messages_analyzed"}:
        raise ValueError("Unsupported daily metric")

    day = _today()

    def _add(conn: sqlite3.Connection) -> None:
        _ensure_daily_row(conn, day)
        conn.execute(
            f"UPDATE daily_stats SET {metric} = {metric} + ? WHERE date = ?",
            (amount, day),
        )
        conn.commit()

    _write(_add)


def record_interaction(amount: int = 1) -> None:
    """
    Counts user-facing interactions.
    """
    increment_total("interactions_total", amount)
    add_daily("interactions", amount)


def record_messages_analyzed(amount: int = 1) -> None:
    """
    Counts unique messages ingested into the stylometry database.
    Call this only for rows actually inserted, not ignored duplicates.
    """
    increment_total("messages_analyzed_total", amount)
    add_daily("messages_analyzed", amount)


def record_ping(ping_ms: float) -> None:
    day = _today()

    def _ping(conn: sqlite3.Connection) -> None:
        _ensure_daily_row(conn, day)

        row = conn.execute(
            "SELECT avg_ping, ping_samples FROM daily_stats WHERE date = ?",
            (day,),
        ).fetchone()

        samples = int(row["ping_samples"])
        avg = float(row["avg_ping"])
        new_samples = samples + 1
        new_avg = ((avg * samples) + float(ping_ms)) / new_samples

        conn.execute(
            """
            UPDATE daily_stats
            SET avg_ping = ?, ping_samples = ?
            WHERE date = ?
            """,
            (new_avg, new_samples, day),
        )
        conn.execute(
            "UPDATE stats SET current_ping_ms = ? WHERE id = 1",
            (float(ping_ms),),
        )
        conn.commit()

    _write(_ping)


def set_public_state(state: str, note: str = "") -> None:
    state = state.strip().lower()
    if state not in {"healthy", "degraded", "maintenance", "offline"}:
        raise ValueError("Unsupported state")

    def _state(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE stats
            SET public_state = ?, public_note = ?
            WHERE id = 1
            """,
            (state, note[:500]),
        )
        conn.commit()

    _write(_state)


def get_public_state() -> dict[str, Any]:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT public_state, public_note, current_ping_ms, last_started_at, restart_count
            FROM stats
            WHERE id = 1
            """
        ).fetchone()

        if row is None:
            return {
                "public_state": "healthy",
                "public_note": "",
                "current_ping_ms": 0,
                "last_started_at": 0,
                "restart_count": 0,
            }

        return dict(row)


def sample_availability() -> None:
    now = int(time.time())
    slot_start = (now // SLOT_SECONDS) * SLOT_SECONDS
    current = get_public_state()

    def _sample(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO availability_samples (slot_start, state, note, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(slot_start) DO UPDATE SET
                state = excluded.state,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (slot_start, current["public_state"], current["public_note"], now),
        )
        conn.commit()

    _write(_sample)


def status_label_to_state(status: str) -> str:
    value = status.strip().lower()
    if value in {"scheduled", "maintenance", "in progress"}:
        return "maintenance"
    if value in {"investigating", "identified", "degraded"}:
        return "degraded"
    if value in {"offline", "down"}:
        return "offline"
    return "healthy"


def create_incident(title: str, body: str, severity: str, status: str) -> int:
    now = int(time.time())

    def _create(conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            """
            INSERT INTO incidents (title, body, severity, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title[:200], body[:4000], severity[:80], status[:80], now, now),
        )
        conn.commit()
        return int(cursor.lastrowid)

    return _write(_create)


def update_incident(
    incident_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    severity: str | None = None,
    status: str | None = None,
) -> None:
    fields = []
    params: list[Any] = []

    if title is not None:
        fields.append("title = ?")
        params.append(title[:200])
    if body is not None:
        fields.append("body = ?")
        params.append(body[:4000])
    if severity is not None:
        fields.append("severity = ?")
        params.append(severity[:80])
    if status is not None:
        fields.append("status = ?")
        params.append(status[:80])
        if status.strip().lower() in {"resolved", "completed"}:
            fields.append("resolved_at = ?")
            params.append(int(time.time()))
        else:
            fields.append("resolved_at = ?")
            params.append(None)

    fields.append("updated_at = ?")
    params.append(int(time.time()))
    params.append(int(incident_id))

    def _update(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"UPDATE incidents SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        conn.commit()

    _write(_update)


def list_incidents(limit: int = 10) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, title, body, severity, status, created_at, updated_at, resolved_at
            FROM incidents
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def _range_dates(days: int) -> list[date]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    return [start + timedelta(days=i) for i in range(days)]


def _bucket_start(d: date, granularity: str) -> date:
    if granularity == "day":
        return d
    if granularity == "week":
        return d - timedelta(days=d.weekday())
    if granularity == "month":
        return d.replace(day=1)
    if granularity == "quarter":
        month = ((d.month - 1) // 3) * 3 + 1
        return d.replace(month=month, day=1)
    raise ValueError("Unsupported granularity")


def _granularity_for_span(span_days: int) -> str:
    if span_days <= 30:
        return "day"
    if span_days <= 180:
        return "week"
    if span_days <= 730:
        return "month"
    return "quarter"


def _normalize_granularity(value: str | None) -> str:
    if value is None:
        return "auto"

    value = value.strip().lower()
    if value in {"", "auto"}:
        return "auto"
    if value in {"day", "daily"}:
        return "day"
    if value in {"week", "weekly"}:
        return "week"
    if value in {"month", "monthly"}:
        return "month"
    if value in {"quarter", "quarterly"}:
        return "quarter"

    raise HTTPException(status_code=400, detail="Invalid granularity")


def _advance_bucket(d: date, granularity: str) -> date:
    if granularity == "day":
        return d + timedelta(days=1)
    if granularity == "week":
        return d + timedelta(days=7)
    if granularity == "month":
        month = d.month + 1
        year = d.year
        if month > 12:
            month = 1
            year += 1
        return date(year, month, 1)
    if granularity == "quarter":
        month = d.month + 3
        year = d.year
        while month > 12:
            month -= 12
            year += 1
        return date(year, month, 1)
    raise ValueError("Unsupported granularity")


def _daily_rows() -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            """
            SELECT date, interactions, messages_analyzed, avg_ping, ping_samples
            FROM daily_stats
            ORDER BY date ASC
            """
        ).fetchall()


def history_points(metric: str, period: str, granularity: str = "auto") -> dict[str, Any]:
    if metric not in {"interactions", "messages_analyzed", "ping"}:
        raise HTTPException(status_code=400, detail="Invalid metric")
    if period not in {"30d", "lifetime"}:
        raise HTTPException(status_code=400, detail="Invalid period")

    requested_granularity = _normalize_granularity(granularity)

    rows = _daily_rows()
    if not rows:
        effective = "day" if requested_granularity == "auto" else requested_granularity
        return {"metric": metric, "period": period, "granularity": effective, "points": []}

    if period == "30d":
        range_start = date.today() - timedelta(days=29)
        range_end = date.today()
        relevant_rows = [
            row for row in rows
            if range_start.isoformat() <= row["date"] <= range_end.isoformat()
        ]
    else:
        range_start = date.fromisoformat(rows[0]["date"])
        range_end = date.fromisoformat(rows[-1]["date"])
        relevant_rows = rows

    if requested_granularity == "auto":
        effective_granularity = (
            "day"
            if period == "30d"
            else _granularity_for_span((range_end - range_start).days + 1)
        )
    else:
        effective_granularity = requested_granularity

    if effective_granularity == "day":
        row_map = {row["date"]: row for row in relevant_rows}
        points = []

        cursor = range_start
        while cursor <= range_end:
            key = cursor.isoformat()
            row = row_map.get(key)

            if row is None:
                value = 0
            elif metric == "ping":
                value = float(row["avg_ping"])
            else:
                value = int(row[metric])

            points.append({"label": key, "value": value})
            cursor += timedelta(days=1)

        return {
            "metric": metric,
            "period": period,
            "granularity": effective_granularity,
            "points": points,
        }

    start_bucket = _bucket_start(range_start, effective_granularity)
    end_bucket = _bucket_start(range_end, effective_granularity)

    buckets: dict[date, dict[str, float]] = defaultdict(lambda: {"value": 0.0, "samples": 0.0})

    for row in relevant_rows:
        d = date.fromisoformat(row["date"])
        bucket = _bucket_start(d, effective_granularity)

        if metric == "ping":
            buckets[bucket]["value"] += float(row["avg_ping"]) * int(row["ping_samples"])
            buckets[bucket]["samples"] += int(row["ping_samples"])
        else:
            buckets[bucket]["value"] += float(row[metric])

    points = []
    bucket = start_bucket
    while bucket <= end_bucket:
        if metric == "ping":
            samples = buckets[bucket]["samples"]
            value = round(buckets[bucket]["value"] / samples, 2) if samples else 0
        else:
            value = int(buckets[bucket]["value"])

        points.append({"label": bucket.isoformat(), "value": value})
        bucket = _advance_bucket(bucket, effective_granularity)

    return {
        "metric": metric,
        "period": period,
        "granularity": effective_granularity,
        "points": points,
    }


def availability_grid(days: int = 30) -> dict[str, Any]:
    if days != 30:
        raise HTTPException(status_code=400, detail="Only 30-day availability is supported")

    current = get_public_state()
    started_at = int(current.get("last_started_at") or 0)

    now = int(time.time())
    end_slot = (now // SLOT_SECONDS) * SLOT_SECONDS
    start_slot = end_slot - ((days * 24 // 8) - 1) * SLOT_SECONDS

    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT slot_start, state, note, updated_at
            FROM availability_samples
            WHERE slot_start BETWEEN ? AND ?
            ORDER BY slot_start ASC
            """,
            (start_slot, end_slot),
        ).fetchall()

    by_slot = {int(row["slot_start"]): dict(row) for row in rows}

    slots = []
    healthy = degraded = maintenance = offline = 0

    slot = start_slot
    while slot <= end_slot:
        row = by_slot.get(slot)

        if slot < started_at:
            # Assume healthy before monitoring existed.
            state = "healthy"
            note = "Monitoring not yet enabled"
            present = False
            healthy += 1
        else:
            if row:
                state = row["state"]
                note = row["note"]
                present = True
            else:
                state = "offline"
                note = ""
                present = False

            if state == "healthy":
                healthy += 1
            elif state == "degraded":
                degraded += 1
            elif state == "maintenance":
                maintenance += 1
            else:
                offline += 1

        slots.append(
            {
                "slot_start": slot,
                "label": datetime.utcfromtimestamp(slot).strftime("%b %d %H:%M"),
                "state": state,
                "note": note,
                "present": present,
            }
        )

        slot += SLOT_SECONDS

    monitored_total = healthy + degraded + maintenance + offline
    availability_pct = (
        round(((healthy + degraded + maintenance) / monitored_total) * 100, 2)
        if monitored_total
        else 100.0
    )

    return {
        "days": 30,
        "slot_hours": 8,
        "availability_pct": availability_pct,
        "counts": {
            "healthy": healthy,
            "degraded": degraded,
            "maintenance": maintenance,
            "offline": offline,
        },
        "slots": slots,
    }


def snapshot_stats() -> dict[str, Any]:
    today = date.today()
    since_30 = (today - timedelta(days=29)).isoformat()

    with _conn() as conn:
        stats = conn.execute(
            """
            SELECT interactions_total, messages_analyzed_total, current_ping_ms,
                   public_state, public_note, last_started_at, restart_count
            FROM stats
            WHERE id = 1
            """
        ).fetchone()

        if stats is None:
            stats = {
                "interactions_total": 0,
                "messages_analyzed_total": 0,
                "current_ping_ms": 0,
                "public_state": "healthy",
                "public_note": "",
                "last_started_at": 0,
                "restart_count": 0,
            }
        else:
            stats = dict(stats)

        daily = conn.execute(
            """
            SELECT
                COALESCE(SUM(interactions), 0) AS interactions_30d,
                COALESCE(SUM(messages_analyzed), 0) AS messages_30d,
                CASE
                    WHEN COALESCE(SUM(ping_samples), 0) = 0 THEN 0
                    ELSE ROUND(SUM(avg_ping * ping_samples) / SUM(ping_samples), 2)
                END AS ping_avg_30d
            FROM daily_stats
            WHERE date >= ?
            """,
            (since_30,),
        ).fetchone()

        daily = dict(daily)

    interactions_30d = int(daily["interactions_30d"])
    messages_30d = int(daily["messages_30d"])

    return {
        "status": stats["public_state"],
        "status_note": stats["public_note"],
        "current_ping_ms": round(float(stats["current_ping_ms"]), 2),
        "last_started_at": int(stats["last_started_at"]),
        "interactions": {
            "lifetime": int(stats["interactions_total"]),
            "days_30": interactions_30d,
            "avg_per_day_30": round(interactions_30d / 30, 2),
        },
        "messages_analyzed": {
            "lifetime": int(stats["messages_analyzed_total"]),
            "days_30": messages_30d,
            "avg_per_day_30": round(messages_30d / 30, 2),
        },
        "ping": {
            "current": round(float(stats["current_ping_ms"]), 2),
            "avg_30d": float(daily["ping_avg_30d"]),
        },
        "restart_count": int(stats["restart_count"]),
    }


@app.head("/")
def root_head():
    return Response(status_code=200)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "Derision Public Stats API",
        "status": "online",
    }


@app.get("/api/stats")
def api_stats() -> dict[str, Any]:
    payload = snapshot_stats()
    payload["availability"] = availability_grid()
    payload["incidents"] = list_incidents(limit=5)
    return payload


@app.get("/api/public")
def api_public() -> dict[str, Any]:
    return api_stats()


@app.get("/api/history")
def api_history(metric: str = "interactions", period: str = "30d", granularity: str = "auto") -> dict[str, Any]:
    return history_points(metric, period, granularity)


@app.get("/api/availability")
def api_availability() -> dict[str, Any]:
    return availability_grid()


@app.get("/api/incidents")
def api_incidents(limit: int = 10) -> dict[str, Any]:
    return {"incidents": list_incidents(limit=limit)}