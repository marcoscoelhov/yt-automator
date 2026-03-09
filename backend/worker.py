import asyncio
import os
import signal
import socket
import sys
import time

from job_queue import (
    claim_next_job,
    complete_job,
    fail_job,
    init_db,
    record_worker_heartbeat,
    update_job_progress,
)
from main import AutoGenerateRequest, _run_auto_generate


POLL_INTERVAL_SECONDS = float(os.getenv("YT_AUTOMATOR_WORKER_POLL_SECONDS", "2.0"))
WORKER_ID = os.getenv(
    "YT_AUTOMATOR_WORKER_ID",
    f"{socket.gethostname()}-{os.getpid()}",
)
STOP_REQUESTED = False


def _handle_stop(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def _stage_callback(job_id: str):
    def callback(stage: str, progress_updates: dict):
        update_job_progress(
            job_id,
            status="running",
            stage=stage,
            progress_updates=progress_updates,
            error_text=(progress_updates or {}).get("error"),
        )
    return callback


async def process_job(job: dict) -> None:
    job_id = job["id"]
    payload = AutoGenerateRequest(**(job.get("request_payload") or {}))
    response = await _run_auto_generate(
        payload,
        request=None,
        route_name=job.get("route_name") or "/auto-generate",
        stage_callback=_stage_callback(job_id),
    )
    result_payload = response.model_dump()
    result_payload["job_id"] = job_id
    if response.status == "completed":
        complete_job(job_id, result_payload)
    else:
        fail_job(job_id, result_payload.get("message") or "job failed", result_payload)


async def worker_loop() -> int:
    init_db()
    print(f"[Worker] iniciado worker_id={WORKER_ID}")
    while not STOP_REQUESTED:
        record_worker_heartbeat(WORKER_ID, "idle", None)
        job = claim_next_job(WORKER_ID, "auto_generate")
        if not job:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        job_id = job["id"]
        print(f"[Worker] job claim job_id={job_id} route={job.get('route_name')}")
        record_worker_heartbeat(WORKER_ID, "running", job_id)
        update_job_progress(job_id, status="running", stage="starting", progress_updates={"worker_id": WORKER_ID})
        try:
            await process_job(job)
            print(f"[Worker] job concluído job_id={job_id}")
        except Exception as exc:
            print(f"[Worker] job falhou job_id={job_id}: {exc}", file=sys.stderr)
            fail_job(job_id, str(exc), {"job_id": job_id, "status": "error", "message": str(exc)})
        finally:
            record_worker_heartbeat(WORKER_ID, "idle", None)

    record_worker_heartbeat(WORKER_ID, "stopped", None)
    print(f"[Worker] encerrado worker_id={WORKER_ID}")
    return 0


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    return asyncio.run(worker_loop())


if __name__ == "__main__":
    raise SystemExit(main())
