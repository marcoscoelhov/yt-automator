#!/usr/bin/env python3
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import requests

WORKSPACE = Path('/root/.openclaw/workspace')
YT_ROOT = WORKSPACE / 'yt-automator'
RUNS_DIR = YT_ROOT / 'pipeline' / 'runs'
RUNS_DIR.mkdir(parents=True, exist_ok=True)

MEDIA_OUTBOUND = Path('/root/.openclaw/media/outbound')
MEDIA_OUTBOUND.mkdir(parents=True, exist_ok=True)

INTEL_FILE = WORKSPACE / 'intel' / 'DAILY-INTEL.md'

BACKEND_URL = 'http://127.0.0.1:8000'


def _log(msg: str):
    print(f"[daily_sprint1] {msg}", flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_cmd(cmd: list[str], timeout: int = 120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _extract_trend_label(item) -> str:
    if isinstance(item, dict):
        for k in ('name', 'trend', 'query', 'title', 'topic'):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return json.dumps(item, ensure_ascii=False)[:120]
    return str(item)[:120]


def _clean_topic(s: str) -> str:
    s = s.strip()
    s = re.sub(r'^[-\d\.)\s]*', '', s)
    s = re.sub(r'^(tema\s*:|theme\s*:)', '', s, flags=re.I).strip()
    s = s.replace('“', '"').replace('”', '"').strip()
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
            raw = INTEL_FILE.read_text(encoding='utf-8')
            lines = raw.splitlines()

            # 1) seção de temas priorizados (mais limpa para prompt)
            in_priority = False
            for line in lines:
                if 'temas priorizados para próxima semana' in line.lower():
                    in_priority = True
                    continue
                if in_priority and line.strip().startswith('##'):
                    break
                if in_priority and line.strip().startswith('- tema:'):
                    topic = _clean_topic(line.split(':', 1)[1])
                    if topic:
                        labels.append(topic)

            # 2) headings tipo "### 1) Tema: ..."
            if len(labels) < 3:
                for line in lines:
                    m = re.match(r'^###\s*\d+\)\s*Tema:\s*(.+)$', line.strip(), flags=re.I)
                    if m:
                        topic = _clean_topic(m.group(1))
                        if topic and topic not in labels:
                            labels.append(topic)

            labels = labels[:3]
            if labels:
                return {
                    'source': 'youtube_intel_file',
                    'ok': True,
                    'error': None,
                    'trends': labels,
                }
        except Exception as e:
            return {
                'source': 'youtube_fallback',
                'ok': False,
                'error': f'failed reading intel file: {e}',
                'trends': [],
            }

    return {
        'source': 'youtube_fallback',
        'ok': True,
        'error': None,
        'trends': [],
    }


def build_briefs(trend_pack: dict):
    trends = trend_pack.get('trends') or []

    if not trends:
        trends = [
            'Como chegar nos primeiros R$100 mil sem aumentar salário (estratégia em etapas)',
            'Os 5 erros que atrasam sua liberdade financeira por anos',
            'O plano de 12 meses para sair do caos financeiro e ganhar tração',
        ]

    briefs = []
    for i, t in enumerate(trends[:3], start=1):
        brief = (
            f"Tema principal: {t}. "
            "Público: brasileiros 18-40 que querem melhorar vida financeira com linguagem simples. "
            "Objetivo do vídeo: gerar alta retenção, didático, com virada psicológica + exemplo numérico simples. "
            "Formato: vídeo faceless longo (8-15 minutos), com 48-90 cenas, ritmo dinâmico e mini-viradas a cada 4-6 cenas. "
            "Tom: Nick estrito em PT-BR, sem promessas milagrosas, CTA suave no final."
        )
        briefs.append({'rank': i, 'topic': t, 'brief': brief})

    return briefs


def _stage_media_for_whatsapp(local_video: str | None) -> str | None:
    if not local_video:
        return None
    src = Path(local_video)
    if not src.exists():
        return None
    dst = MEDIA_OUTBOUND / src.name
    shutil.copy2(src, dst)
    return str(dst)


def generate_video(brief: str):
    _log('gerando vídeo via /auto-generate (pode levar alguns minutos)...')
    payload = {
        'brief': brief,
        'voice_id': 'Antonio',
        'mode': 'layers',
    }
    r = requests.post(f'{BACKEND_URL}/auto-generate', json=payload, timeout=3600)
    r.raise_for_status()
    data = r.json()
    if data.get('status') != 'completed':
        raise RuntimeError(data.get('message', 'auto-generate failed'))

    video_url = data.get('video_url')
    local_video = None
    if isinstance(video_url, str) and '/static/' in video_url:
        name = video_url.split('/static/', 1)[1]
        local_video = str(YT_ROOT / 'backend' / 'static' / name)

    outbound_video = _stage_media_for_whatsapp(local_video)

    return {
        'title': data.get('title'),
        'description': data.get('description'),
        'video_url': video_url,
        'video_path': local_video,
        'outbound_video_path': outbound_video,
        'message': data.get('message'),
    }


def main():
    started_at = _now_iso()

    _log('início da execução')

    # 1) trend intake
    _log('coletando tendências...')
    trend_pack = collect_trends()
    _log(f"tendências source={trend_pack.get('source')} error={trend_pack.get('error')}")

    # 2) generate 3 briefs
    _log('montando briefs...')
    briefs = build_briefs(trend_pack)
    chosen = briefs[0]
    _log(f"brief escolhido: {chosen.get('topic')}")

    # 3) render 1 video from top brief
    result = {
        'ok': True,
        'started_at': started_at,
        'finished_at': None,
        'trend_source': trend_pack.get('source'),
        'trend_error': trend_pack.get('error'),
        'briefs': briefs,
        'chosen_brief': chosen,
        'video': None,
        'error': None,
    }

    try:
        result['video'] = generate_video(chosen['brief'])
        _log('vídeo gerado com sucesso')
    except Exception as e:
        msg = str(e)
        _log(f'falha na 1a tentativa: {msg}')
        # Retry 80/20: brief simplificado quando backend reporta JSON inválido
        if 'valid JSON' in msg or 'JSON' in msg:
            retry_brief = (
                f"Tema principal: {_clean_topic(chosen.get('topic','Finanças pessoais práticas'))}. "
                "Público: brasileiros 18-40. "
                "Formato obrigatório: vídeo faceless longo de 8-15 minutos. "
                "Use estrutura clara em blocos, com exemplos numéricos simples e CTA leve."
            )
            try:
                result['video'] = generate_video(retry_brief)
                _log('vídeo gerado com sucesso na 2a tentativa')
            except Exception as e2:
                _log(f'falha na 2a tentativa: {e2}')
                fallback_brief = (
                    "Tema principal: regra dos R$20.000 para sair do modo sobrevivência. "
                    "Público: brasileiros 18-40 que querem melhorar vida financeira com linguagem simples. "
                    "Objetivo: gerar alta retenção com exemplo numérico simples e passos práticos. "
                    "Formato obrigatório: vídeo faceless longo (8-15 minutos), com ritmo dinâmico e mini-viradas."
                )
                try:
                    result['video'] = generate_video(fallback_brief)
                    _log('vídeo gerado com sucesso na 3a tentativa (fallback)')
                except Exception as e3:
                    result['ok'] = False
                    result['error'] = f"{msg} | retry2: {e2} | fallback: {e3}"
        else:
            result['ok'] = False
            result['error'] = msg

    result['finished_at'] = _now_iso()

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    out_file = RUNS_DIR / f'run-{stamp}.json'
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    _log(f"run salvo em: {out_file}")

    summary = {
        'ok': result['ok'],
        'run_file': str(out_file),
        'trend_source': result['trend_source'],
        'trend_error': result['trend_error'],
        'chosen_topic': chosen.get('topic'),
        'video_path': (result.get('video') or {}).get('video_path'),
        'outbound_video_path': (result.get('video') or {}).get('outbound_video_path'),
        'video_url': (result.get('video') or {}).get('video_url'),
        'error': result['error'],
    }

    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
