import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "jobs.sqlite3")
WORKER_STALE_AFTER_SECONDS = int(os.getenv("YT_AUTOMATOR_WORKER_STALE_AFTER_SECONDS", "90"))


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db_path() -> str:
    return os.getenv("YT_AUTOMATOR_JOBS_DB", DEFAULT_DB_PATH)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _json_dumps(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _row_to_job(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    data["request_payload"] = _json_loads(data.pop("request_json", None)) or {}
    data["result_payload"] = _json_loads(data.pop("result_json", None))
    data["progress"] = _json_loads(data.pop("progress_json", None)) or {}
    return data


def init_db() -> None:
    os.makedirs(os.path.dirname(get_db_path()), exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                route_name TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 1,
                request_json TEXT NOT NULL,
                result_json TEXT,
                progress_json TEXT,
                error_text TEXT,
                worker_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at
            ON jobs(status, created_at);

            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                current_job_id TEXT,
                last_heartbeat TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def enqueue_job(job_type: str, route_name: str, payload: dict, max_attempts: int = 1) -> dict:
    init_db()
    job_id = uuid.uuid4().hex[:12]
    now = utcnow_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, job_type, route_name, status, stage, attempts, max_attempts,
                request_json, result_json, progress_json, error_text, worker_id,
                created_at, updated_at, started_at, finished_at
            ) VALUES (?, ?, ?, 'queued', 'queued', 0, ?, ?, NULL, ?, NULL, NULL, ?, ?, NULL, NULL)
            """,
            (
                job_id,
                job_type,
                route_name,
                max_attempts,
                _json_dumps(payload) or "{}",
                _json_dumps({"llm_ok": False, "script_ok": False, "audio_ok": False, "render_ok": False}),
                now,
                now,
            ),
        )
    return get_job(job_id)


def get_job(job_id: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row)


def list_jobs(limit: int = 20) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [_row_to_job(row) for row in rows]


def record_worker_heartbeat(worker_id: str, status: str, current_job_id: str | None = None) -> None:
    init_db()
    now = utcnow_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO workers(worker_id, status, current_job_id, last_heartbeat, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                status = excluded.status,
                current_job_id = excluded.current_job_id,
                last_heartbeat = excluded.last_heartbeat,
                updated_at = excluded.updated_at
            """,
            (worker_id, status, current_job_id, now, now),
        )


def list_workers() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM workers ORDER BY updated_at DESC").fetchall()
    return [dict(row) for row in rows]


def queue_stats() -> dict:
    init_db()
    with _connect() as conn:
        counts = {
            row["status"]: row["total"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS total FROM jobs GROUP BY status"
            ).fetchall()
        }
    workers = list_workers()
    now = datetime.now(timezone.utc)
    active_workers = 0
    for worker in workers:
        try:
            heartbeat = datetime.fromisoformat(worker["last_heartbeat"])
            age = (now - heartbeat).total_seconds()
            if age <= WORKER_STALE_AFTER_SECONDS:
                active_workers += 1
        except Exception:
            continue
    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "workers": len(workers),
        "active_workers": active_workers,
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def active_worker_ids(stale_after_seconds: int | None = None) -> set[str]:
    stale_after = int(stale_after_seconds or WORKER_STALE_AFTER_SECONDS)
    now = datetime.now(timezone.utc)
    active: set[str] = set()
    for worker in list_workers():
        heartbeat = _parse_iso(worker.get("last_heartbeat"))
        if not heartbeat:
            continue
        if (now - heartbeat).total_seconds() <= stale_after:
            worker_id = str(worker.get("worker_id") or "").strip()
            if worker_id:
                active.add(worker_id)
    return active


def fail_orphaned_jobs(
    *,
    stale_after_seconds: int | None = None,
    running_grace_seconds: int = 180,
) -> list[str]:
    init_db()
    stale_after = int(stale_after_seconds or WORKER_STALE_AFTER_SECONDS)
    active_ids = active_worker_ids(stale_after)
    now = datetime.now(timezone.utc)
    failed_ids: list[str] = []

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, worker_id, updated_at, started_at
            FROM jobs
            WHERE status = 'running'
            ORDER BY created_at ASC
            """
        ).fetchall()

    for row in rows:
        job_id = str(row["id"])
        worker_id = str(row["worker_id"] or "").strip()
        reference_time = _parse_iso(row["updated_at"]) or _parse_iso(row["started_at"])
        if not reference_time:
            continue
        age_seconds = (now - reference_time).total_seconds()
        if age_seconds < max(stale_after, running_grace_seconds):
            continue
        if worker_id and worker_id in active_ids:
            continue
        fail_job(
            job_id,
            f"orphaned_job: worker={worker_id or 'missing'} stale_after={max(stale_after, running_grace_seconds)}s",
            {
                "job_id": job_id,
                "status": "error",
                "message": "Job órfão: worker sem heartbeat ativo.",
                "worker_id": worker_id or None,
            },
        )
        failed_ids.append(job_id)

    return failed_ids


def claim_next_job(worker_id: str, job_type: str | None = None) -> dict | None:
    init_db()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if job_type:
            row = conn.execute(
                """
                SELECT id FROM jobs
                WHERE status = 'queued' AND job_type = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (job_type,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id FROM jobs
                WHERE status = 'queued'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None

        now = utcnow_iso()
        conn.execute(
            """
            UPDATE jobs
            SET status = 'running',
                stage = 'starting',
                attempts = attempts + 1,
                worker_id = ?,
                started_at = COALESCE(started_at, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (worker_id, now, now, row["id"]),
        )
        conn.execute("COMMIT")
        return get_job(row["id"])
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def update_job_progress(
    job_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    progress_updates: dict | None = None,
    error_text: str | None = None,
) -> dict | None:
    init_db()
    job = get_job(job_id)
    if not job:
        return None

    progress = dict(job.get("progress") or {})
    if progress_updates:
        progress.update(progress_updates)

    updates = []
    params = []
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if stage is not None:
        updates.append("stage = ?")
        params.append(stage)
    updates.append("progress_json = ?")
    params.append(_json_dumps(progress))
    if error_text is not None:
        updates.append("error_text = ?")
        params.append(error_text)
    updates.append("updated_at = ?")
    params.append(utcnow_iso())
    params.append(job_id)

    with _connect() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", params)
    return get_job(job_id)


def complete_job(job_id: str, result_payload: dict) -> dict | None:
    init_db()
    now = utcnow_iso()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'completed',
                stage = 'completed',
                result_json = ?,
                error_text = NULL,
                updated_at = ?,
                finished_at = ?
            WHERE id = ?
            """,
            (_json_dumps(result_payload), now, now, job_id),
        )
    return get_job(job_id)


def fail_job(job_id: str, error_text: str, result_payload: dict | None = None) -> dict | None:
    init_db()
    now = utcnow_iso()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'failed',
                stage = 'failed',
                result_json = ?,
                error_text = ?,
                updated_at = ?,
                finished_at = ?
            WHERE id = ?
            """,
            (_json_dumps(result_payload), error_text[:4000], now, now, job_id),
        )
    return get_job(job_id)
