#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

YT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = YT_ROOT.parent.parent
RUNS_DIR = YT_ROOT / "pipeline" / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

MEDIA_OUTBOUND = Path(os.getenv("YT_AUTOMATOR_MEDIA_OUTBOUND", str(WORKSPACE / "media" / "outbound")))
MEDIA_OUTBOUND.mkdir(parents=True, exist_ok=True)

INTEL_FILE = Path(os.getenv("YT_AUTOMATOR_INTEL_FILE", str(WORKSPACE / "intel" / "DAILY-INTEL.md")))

BACKEND_URL = (
    os.getenv("YT_AUTOMATOR_BACKEND_URL")
    or os.getenv("YT_BACKEND_URL")
    or "http://69.62.93.146:8020"
).rstrip("/")
JOB_POLL_SECONDS = float(os.getenv("YT_AUTOMATOR_JOB_POLL_SECONDS", "5"))
JOB_TIMEOUT_SECONDS = int(os.getenv("YT_AUTOMATOR_JOB_TIMEOUT_SECONDS", "3600"))


def _log(msg: str):
    print(f"[daily_sprint1] {msg}", flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw.strip())
    except Exception:
        return None


def _run_cmd(cmd: list[str], timeout: int = 120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _extract_trend_label(item) -> str:
    if isinstance(item, dict):
        for k in ("name", "trend", "query", "title", "topic"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return json.dumps(item, ensure_ascii=False)[:120]
    return str(item)[:120]


def _clean_topic(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^[-\d\.)\s]*", "", s)
    s = re.sub(r"^(tema\s*:|theme\s*:)", "", s, flags=re.I).strip()
    s = s.replace("“", '"').replace("”", '"').strip()
    s = s.strip('"\'“”')
    return s[:180]


def collect_trends(limit: int = 12):
    """
    YouTube-first intake.
    Prioridade de extração no intel:
    1) linhas de "Temas priorizados para próxima semana"
    2) headings "### N) Tema: ..."
    3) fallback interno
    """
    labels = []

    if INTEL_FILE.exists():
        try:
            raw = INTEL_FILE.read_text(encoding="utf-8")
            lines = raw.splitlines()

            in_priority = False
            for line in lines:
                if "temas priorizados para próxima semana" in line.lower():
                    in_priority = True
                    continue
                if in_priority and line.strip().startswith("##"):
                    break
                if in_priority and line.strip().startswith("- tema:"):
                    topic = _clean_topic(line.split(":", 1)[1])
                    if topic:
                        labels.append(topic)

            if len(labels) < 3:
                for line in lines:
                    m = re.match(r"^###\s*\d+\)\s*Tema:\s*(.+)$", line.strip(), flags=re.I)
                    if m:
                        topic = _clean_topic(m.group(1))
                        if topic and topic not in labels:
                            labels.append(topic)

            labels = labels[:limit]
            if labels:
                return {
                    "source": "youtube_intel_file",
                    "ok": True,
                    "error": None,
                    "trends": labels,
                }
        except Exception as e:
            return {
                "source": "youtube_fallback",
                "ok": False,
                "error": f"failed reading intel file: {e}",
                "trends": [],
            }

    return {
        "source": "youtube_fallback",
        "ok": True,
        "error": None,
        "trends": [],
    }


def build_briefs(trend_pack: dict, limit: int = 3):
    trends = trend_pack.get("trends") or []

    if not trends:
        trends = [
            "Como chegar nos primeiros R$100 mil sem aumentar salário (estratégia em etapas)",
            "Os 5 erros que atrasam sua liberdade financeira por anos",
            "O plano de 12 meses para sair do caos financeiro e ganhar tração",
        ]

    briefs = []
    for i, t in enumerate(trends[:limit], start=1):
        brief = (
            f"Tema principal: {t}. "
            "Público: brasileiros 18-40 que querem melhorar vida financeira com linguagem simples. "
            "Objetivo do vídeo: gerar alta retenção, didático, com virada psicológica + exemplo numérico simples. "
            "Formato: vídeo faceless longo (8-15 minutos), com 48-90 cenas, ritmo dinâmico e mini-viradas a cada 4-6 cenas. "
            "Tom: Nick estrito em PT-BR, sem promessas milagrosas, CTA suave no final."
        )
        briefs.append({"rank": i, "topic": t, "brief": brief})

    return briefs


def build_brief_for_topic(topic: str) -> dict:
    clean_topic = _clean_topic(topic) or "Finanças pessoais práticas"
    brief = (
        f"Tema principal: {clean_topic}. "
        "Público: brasileiros 18-40 que querem melhorar vida financeira com linguagem simples. "
        "Objetivo do vídeo: gerar alta retenção, didático, com virada psicológica + exemplo numérico simples. "
        "Formato: vídeo faceless longo (8-15 minutos), com 48-90 cenas, ritmo dinâmico e mini-viradas a cada 4-6 cenas. "
        "Tom: Nick estrito em PT-BR, sem promessas milagrosas, CTA suave no final."
    )
    return {"rank": 1, "topic": clean_topic, "brief": brief}


def build_explicit_selection(topic: str | None, brief: str | None) -> tuple[dict, list[dict], dict]:
    if brief:
        clean_topic = _clean_topic(topic or "")
        if not clean_topic:
            m = re.search(r"Tema principal:\s*([^\.]+)", brief, flags=re.I)
            clean_topic = _clean_topic(m.group(1)) if m else "Tema manual"
        chosen = {"rank": 1, "topic": clean_topic, "brief": brief.strip()}
        return chosen, [chosen], {"source": "explicit_brief", "ok": True, "error": None, "trends": [clean_topic]}

    chosen = build_brief_for_topic(topic or "")
    return chosen, [chosen], {"source": "explicit_topic", "ok": True, "error": None, "trends": [chosen["topic"]]}


def _stage_media_for_whatsapp(local_video: str | None) -> str | None:
    if not local_video:
        return None
    src = Path(local_video)
    if not src.exists():
        return None
    dst = MEDIA_OUTBOUND / src.name
    shutil.copy2(src, dst)
    return str(dst)


def _normalize_public_asset_url(asset_url: str | None) -> str | None:
    if not asset_url:
        return asset_url
    asset = urlparse(str(asset_url))
    backend = urlparse(BACKEND_URL)
    if (asset.hostname or "").strip().lower() not in {"localhost", "127.0.0.1", "::1"}:
        return asset_url
    if (backend.hostname or "").strip().lower() in {"", "localhost", "127.0.0.1", "::1"}:
        return asset_url
    return urlunparse(
        (
            backend.scheme or asset.scheme or "http",
            backend.netloc,
            asset.path,
            asset.params,
            asset.query,
            asset.fragment,
        )
    )


def generate_video(brief: str):
    _log("gerando vídeo via /auto-generate (pode levar alguns minutos)...")
    payload = {
        "brief": brief,
        "voice_id": "Antonio",
        "mode": "layers",
    }
    target_duration_sec = _env_float("YT_AUTOMATOR_TARGET_DURATION_SEC")
    validation_mode = (os.getenv("YT_AUTOMATOR_VALIDATION_MODE") or "").strip()
    test_mode = _env_bool("YT_AUTOMATOR_TEST_MODE")
    if target_duration_sec is not None:
        payload["target_duration_sec"] = target_duration_sec
    if validation_mode:
        payload["validation_mode"] = validation_mode
    if test_mode is not None:
        payload["test_mode"] = test_mode
    r = requests.post(f"{BACKEND_URL}/auto-generate", json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    job_id = data.get("job_id")
    if not job_id:
        raise RuntimeError(data.get("message", "auto-generate did not return job_id"))

    _log(f"job enfileirado job_id={job_id}, aguardando conclusão...")
    deadline = time.time() + JOB_TIMEOUT_SECONDS
    while time.time() < deadline:
        status_resp = requests.get(f"{BACKEND_URL}/jobs/{job_id}", timeout=60)
        status_resp.raise_for_status()
        job = status_resp.json()
        queue_status = job.get("queue_status")
        stage = job.get("stage")
        _log(f"job {job_id} status={queue_status} stage={stage}")
        if queue_status == "completed":
            result_data = requests.get(f"{BACKEND_URL}/jobs/{job_id}/result", timeout=60).json()
            data = result_data.get("result") or {}
            break
        if queue_status == "failed":
            raise RuntimeError(job.get("error") or "auto-generate failed")
        time.sleep(JOB_POLL_SECONDS)
    else:
        raise RuntimeError(f"job {job_id} excedeu timeout de {JOB_TIMEOUT_SECONDS}s")

    video_url = _normalize_public_asset_url(data.get("video_url"))
    subtitles_url = _normalize_public_asset_url(data.get("subtitles_url"))
    local_video = None
    if isinstance(video_url, str) and "/static/" in video_url:
        name = video_url.split("/static/", 1)[1]
        local_video = str(YT_ROOT / "backend" / "static" / name)

    outbound_video = _stage_media_for_whatsapp(local_video)

    return {
        "job_id": job_id,
        "title": data.get("title"),
        "description": data.get("description"),
        "video_url": video_url,
        "subtitles_url": subtitles_url,
        "video_path": local_video,
        "outbound_video_path": outbound_video,
        "message": data.get("message"),
    }


def _write_run_file(result: dict) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_file = RUNS_DIR / f"run-{stamp}.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_file


def _result_summary(result: dict, chosen: dict, run_file: Path, dry_run: bool) -> dict:
    video = result.get("video") or {}
    return {
        "ok": result["ok"],
        "dry_run": dry_run,
        "run_file": str(run_file),
        "trend_source": result["trend_source"],
        "trend_error": result["trend_error"],
        "chosen_topic": chosen.get("topic"),
        "chosen_brief": chosen.get("brief"),
        "job_id": video.get("job_id"),
        "video_path": video.get("video_path"),
        "outbound_video_path": video.get("outbound_video_path"),
        "video_url": video.get("video_url"),
        "subtitles_url": video.get("subtitles_url"),
        "error": result["error"],
    }


def main():
    parser = argparse.ArgumentParser(description="Daily yt-automator pipeline")
    parser.add_argument("--topic", help="Força um tópico único e ignora intake de intel")
    parser.add_argument("--brief", help="Força um brief completo e ignora seleção de trends")
    parser.add_argument("--dry-run", action="store_true", help="Valida o fluxo e gera somente artefatos de brief")
    parser.add_argument("--limit-briefs", type=int, default=3, help="Limita número de briefs gerados a partir do intel")
    args = parser.parse_args()

    started_at = _now_iso()
    _log("início da execução")

    if args.topic or args.brief:
        chosen, briefs, trend_pack = build_explicit_selection(args.topic, args.brief)
        _log(f"seleção explícita: {chosen.get('topic')}")
    else:
        _log("coletando tendências...")
        trend_pack = collect_trends(limit=max(1, args.limit_briefs))
        _log(f"tendências source={trend_pack.get('source')} error={trend_pack.get('error')}")
        _log("montando briefs...")
        briefs = build_briefs(trend_pack, limit=max(1, args.limit_briefs))
        chosen = briefs[0]
        _log(f"brief escolhido: {chosen.get('topic')}")

    result = {
        "ok": True,
        "dry_run": args.dry_run,
        "started_at": started_at,
        "finished_at": None,
        "trend_source": trend_pack.get("source"),
        "trend_error": trend_pack.get("error"),
        "briefs": briefs,
        "chosen_brief": chosen,
        "video": None,
        "error": None,
    }

    if args.dry_run:
        _log("dry-run ativo: pulando geração de vídeo")
    else:
        try:
            result["video"] = generate_video(chosen["brief"])
            _log("vídeo gerado com sucesso")
        except Exception as e:
            msg = str(e)
            _log(f"falha na geração de vídeo: {msg}")
            result["ok"] = False
            result["error"] = msg

    result["finished_at"] = _now_iso()
    out_file = _write_run_file(result)
    _log(f"run salvo em: {out_file}")

    print(json.dumps(_result_summary(result, chosen, out_file, args.dry_run), ensure_ascii=False))


if __name__ == "__main__":
    main()
