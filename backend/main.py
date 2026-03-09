from datetime import datetime
from fastapi import FastAPI, HTTPException, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Any, Callable, List, Optional
import os
import sys

# Configurar encoding UTF-8 para suportar emojis no terminal Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import time
import asyncio
import json
import base64
import subprocess
import requests
import wave
import io
from google import genai
from google.genai import types
from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip, TextClip, ImageClip
from moviepy.video.fx import resize, loop
import textwrap
import random
import edge_tts
from dotenv import load_dotenv
import PIL.Image
from PIL import Image, ImageDraw, ImageFont
import hashlib  # Para cache de imagens
import httpx  # Para chamadas HTTP assíncronas (Kie.ai API)
import uuid
import re
import unicodedata
from job_queue import (
    enqueue_job,
    get_job,
    init_db as init_job_db,
    list_jobs,
    queue_stats,
)

# Monkey patch para compatibilidade Pillow 10+ com MoviePy antigo
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

# --- Carregar Configuração ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)  # Raiz do projeto (yt-automator/)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# Carregar variáveis de ambiente (.env na raiz do projeto)
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(ENV_PATH)
print(f"[Env] Carregado: {ENV_PATH}")

def load_config():
    """Carrega configurações do arquivo config.json"""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

CONFIG = load_config()

def get_config(path: str, default=None):
    """Acessa configuração por caminho (ex: 'services.image_generation.provider')"""
    keys = path.split(".")
    value = CONFIG
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return default
    return value


def get_static_base_url() -> str:
    base_url = os.getenv("OUTPUT_BASE_URL", "").strip()
    if not base_url:
        base_url = str(get_config("output.base_url", "") or "").strip()
    if base_url and not re.search(r"/static/?$", base_url):
        base_url = f"{base_url.rstrip('/')}/static"
    if not base_url.endswith("/"):
        base_url = f"{base_url}/"
    return base_url


def get_public_static_base_url(request: Request | None = None) -> str:
    base_url = get_static_base_url()
    if base_url and "localhost" not in base_url and "127.0.0.1" not in base_url:
        return base_url

    if request is not None:
        forwarded_proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
        forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        if forwarded_host:
            inferred = f"{forwarded_proto}://{forwarded_host}/static/"
            return inferred

    if base_url:
        return base_url

    port = int(os.getenv("SERVER_PORT") or get_config("server.port", 8000))
    return f"http://127.0.0.1:{port}/static/"


print(f"[Config] Carregado: {CONFIG_PATH}")
print(f"  📷 Imagens: {get_config('services.image_generation.provider', 'seedream')}")
print(f"  🎙️ TTS: {get_config('services.text_to_speech.provider', 'edge_tts')}")
print(f"  🎬 Render: {get_config('services.video_rendering.provider', 'moviepy')}")

# Configuração de provedores LLM
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_CALL_TIMEOUT_SECONDS = 120
LLM_MAX_RETRIES = 2
AUTO_GENERATE_MAX_SECONDS = 900
TEST_MODE_DEFAULT_DURATION_SEC = 45.0
TEST_MODE_MIN_DURATION_SEC = 30.0
TEST_MODE_MAX_DURATION_SEC = 59.0

# Mantemos Gemini para recursos auxiliares (TTS/Imagem), mas auto-generate agora usa OpenAI.
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
gemini_client = None
if GOOGLE_API_KEY:
    gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
    print(f"  ✅ Gemini Client inicializado")

if OPENAI_API_KEY:
    print(f"  ✅ OpenAI LLM configurado ({OPENAI_MODEL})")
else:
    print("  ⚠️ OPENAI_API_KEY ausente: auto-generate LLM ficará indisponível até configurar .env")

# --- Configuração da Aplicação ---
app = FastAPI(title=get_config("app.name", "SaaS VSL Generator MVP - Real AI"))

# Estrutura de Pastas (usa config ou defaults)
STATIC_DIR = os.path.join(BASE_DIR, get_config("output.static_dir", "static"))
TEMP_DIR = os.path.join(BASE_DIR, get_config("output.temp_dir", "temp"))
RUNS_DIR = os.path.join(BASE_DIR, "runs")
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(RUNS_DIR, exist_ok=True)
init_job_db()


def get_render_settings(profile: str = "production") -> dict:
    use_test_profile = str(profile or "production").strip().lower() == "test"
    moviepy_cfg = get_config("services.video_rendering.options.moviepy", {}) or {}
    moviepy_test_cfg = get_config("services.video_rendering.options.moviepy_test", {}) or {}
    cfg = moviepy_test_cfg if use_test_profile else moviepy_cfg
    prefix = "VIDEO_TEST_OUTPUT_" if use_test_profile else "VIDEO_OUTPUT_"

    width = int(os.getenv(f"{prefix}WIDTH") or cfg.get("output_width") or moviepy_cfg.get("output_width") or 1920)
    height = int(os.getenv(f"{prefix}HEIGHT") or cfg.get("output_height") or moviepy_cfg.get("output_height") or 1080)
    fps = int(os.getenv(f"{prefix}FPS") or cfg.get("fps") or moviepy_cfg.get("fps") or 24)
    codec = str(os.getenv(f"{prefix}CODEC") or cfg.get("codec") or moviepy_cfg.get("codec") or "libx264")
    audio_codec = str(os.getenv(f"{prefix}AUDIO_CODEC") or cfg.get("audio_codec") or moviepy_cfg.get("audio_codec") or "aac")
    preset = str(os.getenv(f"{prefix}PRESET") or cfg.get("preset") or moviepy_cfg.get("preset") or "fast")
    crf = str(os.getenv(f"{prefix}CRF") or cfg.get("crf") or moviepy_cfg.get("crf") or "18")
    threads = int(os.getenv(f"{prefix}THREADS") or cfg.get("threads") or moviepy_cfg.get("threads") or 4)
    return {
        "profile": "test" if use_test_profile else "production",
        "width": width,
        "height": height,
        "fps": fps,
        "codec": codec,
        "audio_codec": audio_codec,
        "preset": preset,
        "crf": crf,
        "threads": threads,
    }


def get_audio_mix_settings() -> dict:
    audio_cfg = get_config("services.audio_post", {}) or {}
    bg_path = os.getenv("YT_AUTOMATOR_BG_MUSIC_PATH") or audio_cfg.get("background_music_path") or ""
    bg_catalog = os.getenv("YT_AUTOMATOR_BG_MUSIC_CATALOG") or audio_cfg.get("background_music_catalog") or os.path.join("assets", "music", "catalog.json")
    bg_gain_db = float(os.getenv("YT_AUTOMATOR_BG_MUSIC_GAIN_DB") or audio_cfg.get("background_music_gain_db") or -15.0)
    ducking_threshold = float(os.getenv("YT_AUTOMATOR_BG_DUCK_THRESHOLD") or audio_cfg.get("ducking_threshold") or 0.035)
    ducking_ratio = float(os.getenv("YT_AUTOMATOR_BG_DUCK_RATIO") or audio_cfg.get("ducking_ratio") or 8.0)
    target_lufs = float(os.getenv("YT_AUTOMATOR_TARGET_LUFS") or audio_cfg.get("target_lufs") or -16.0)
    target_lra = float(os.getenv("YT_AUTOMATOR_TARGET_LRA") or audio_cfg.get("target_lra") or 7.0)
    hum_cut_hz = int(os.getenv("YT_AUTOMATOR_VOICE_HUM_CUT_HZ") or audio_cfg.get("voice_hum_cut_hz") or 85)
    voice_lowpass_hz = int(os.getenv("YT_AUTOMATOR_VOICE_LOWPASS_HZ") or audio_cfg.get("voice_lowpass_hz") or 9500)
    denoise_floor_db = float(os.getenv("YT_AUTOMATOR_VOICE_DENOISE_FLOOR_DB") or audio_cfg.get("voice_denoise_floor_db") or -20.0)
    compressor_threshold = float(os.getenv("YT_AUTOMATOR_VOICE_COMP_THRESHOLD") or audio_cfg.get("voice_compressor_threshold") or 0.09)
    compressor_ratio = float(os.getenv("YT_AUTOMATOR_VOICE_COMP_RATIO") or audio_cfg.get("voice_compressor_ratio") or 2.0)
    return {
        "background_music_path": str(bg_path).strip(),
        "background_music_catalog": str(bg_catalog).strip(),
        "background_music_gain_db": bg_gain_db,
        "ducking_threshold": ducking_threshold,
        "ducking_ratio": ducking_ratio,
        "target_lufs": target_lufs,
        "target_lra": target_lra,
        "voice_hum_cut_hz": hum_cut_hz,
        "voice_lowpass_hz": voice_lowpass_hz,
        "voice_denoise_floor_db": denoise_floor_db,
        "voice_compressor_threshold": compressor_threshold,
        "voice_compressor_ratio": compressor_ratio,
    }


def _resolve_config_path(path_value: str) -> str:
    if not path_value:
        return ""
    if os.path.isabs(path_value):
        return path_value
    return os.path.join(BASE_DIR, path_value)


def _normalize_music_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").lower()
    cleaned = re.sub(r"[^a-z0-9]+", " ", normalized)
    return {token for token in cleaned.split() if len(token) >= 3}


def _infer_music_moods(context_text: str) -> list[str]:
    tokens = _normalize_music_tokens(context_text)
    moods = ["corporate", "focused"]

    finance_tokens = {
        "reserva", "emergencia", "dinheiro", "financeiro", "financas",
        "salario", "renda", "divida", "economia", "investimento", "planejamento",
    }
    urgent_tokens = {"urgente", "crise", "medo", "ansiedade", "alerta", "problema"}
    hopeful_tokens = {"solucao", "crescer", "melhorar", "seguranca", "liberdade", "estabilidade"}

    if tokens & finance_tokens:
        moods.extend(["calm", "hopeful"])
    if tokens & urgent_tokens:
        moods.append("tense")
    if tokens & hopeful_tokens:
        moods.append("uplifting")

    # remover duplicados preservando ordem
    return list(dict.fromkeys(moods))


def _load_music_catalog() -> dict:
    settings = get_audio_mix_settings()
    catalog_path = _resolve_config_path(settings["background_music_catalog"])
    if not catalog_path or not os.path.exists(catalog_path):
        return {"tracks": []}
    try:
        with open(catalog_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {"tracks": []}
    except Exception:
        return {"tracks": []}


def _resolve_catalog_track_path(track: dict) -> str:
    local_path = str(track.get("local_path") or "").strip()
    if not local_path:
        return ""
    if os.path.isabs(local_path):
        return local_path
    return os.path.join(BASE_DIR, "assets", "music", local_path)


def _score_music_track(track: dict, context_text: str, moods: list[str]) -> int:
    score = 0
    tokens = _normalize_music_tokens(context_text)
    track_tokens = set()
    for key in ("title", "description", "energy"):
        track_tokens |= _normalize_music_tokens(str(track.get(key) or ""))
    for key in ("moods", "themes", "tags"):
        values = track.get(key) or []
        if isinstance(values, list):
            for value in values:
                track_tokens |= _normalize_music_tokens(str(value))

    track_moods = {str(value).strip().lower() for value in (track.get("moods") or []) if str(value).strip()}
    track_themes = {str(value).strip().lower() for value in (track.get("themes") or []) if str(value).strip()}

    for mood in moods:
        if mood in track_moods:
            score += 8
    score += len(tokens & track_tokens) * 3
    score += len(tokens & track_themes) * 4

    if str(track.get("energy") or "").strip().lower() in {"low", "medium"}:
        score += 2
    if track.get("content_id_registered") is False:
        score += 1
    return score


def _resolve_background_music_path(context_text: str) -> str:
    settings = get_audio_mix_settings()
    env_path = _resolve_config_path(settings["background_music_path"])
    if env_path and os.path.exists(env_path):
        return env_path

    catalog = _load_music_catalog()
    tracks = catalog.get("tracks") or []
    if not isinstance(tracks, list):
        return ""

    moods = _infer_music_moods(context_text)
    best_path = ""
    best_score = -1
    best_title = ""

    for track in tracks:
        if not isinstance(track, dict):
            continue
        candidate_path = _resolve_catalog_track_path(track)
        if not candidate_path or not os.path.exists(candidate_path):
            continue
        score = _score_music_track(track, context_text, moods)
        if score > best_score:
            best_score = score
            best_path = candidate_path
            best_title = str(track.get("title") or os.path.basename(candidate_path))

    if best_path:
        print(f"[Audio] Trilha selecionada: {best_title} ({os.path.basename(best_path)})")
    return best_path


def _build_audio_context(scenes: list["Scene"]) -> str:
    parts: list[str] = []
    for scene in scenes[:24]:
        for value in (scene.caption_line, scene.texto_narracao, scene.beat, scene.prop_family, scene.pose_family):
            if value:
                parts.append(str(value))
    return " ".join(parts)[:1200]

# Montar pasta static
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Configuração de CORS (usa config ou default ["*"])
origins = get_config("server.cors_origins", ["*"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Modelos de Dados ---

class Scene(BaseModel):
    id: int
    # Suporte para ambos os formatos (legado e novo)
    description: Optional[str] = None
    visual_prompt: Optional[str] = None
    duration_est: Optional[float] = None
    
    # Campos do formato brasileiro (roteiro.json)
    texto_narracao: Optional[str] = None
    prompt_visual: Optional[str] = None
    duracao_estimada: Optional[float] = 5.0
    tipo_transicao: Optional[str] = "cut"  # zoom_in, zoom_out, crossfade, cut
    style_instructions: Optional[str] = None

    # --- Nick-style layers mode (opcional) ---
    template: Optional[str] = None  # ex: avatar_left_prop_right, icons_with_red_x
    avatar_pose: Optional[str] = None  # ex: neutral_arms_crossed
    props: Optional[List[str]] = None  # ex: ["moneybag", "red_x"]
    motion: Optional[str] = None  # ex: slow_zoom_in, pop_in_prop
    beat: Optional[str] = None
    caption_line: Optional[str] = None
    pose_family: Optional[str] = None
    prop_family: Optional[str] = None
    
    @property
    def get_narration(self) -> str:
        """Retorna texto de narração (suporta ambos formatos)"""
        return self.texto_narracao or self.description or ""
    
    @property
    def get_visual_prompt(self) -> str:
        """Retorna prompt visual (suporta ambos formatos)"""
        return self.prompt_visual or self.visual_prompt or ""
    
    @property
    def get_duration(self) -> float:
        """Retorna duração estimada (suporta ambos formatos)"""
        return self.duracao_estimada or self.duration_est or 5.0
    
    @property
    def get_transition(self) -> str:
        """Retorna tipo de transição"""
        return self.tipo_transicao or "cut"

class VideoGenerationRequest(BaseModel):
    script: str
    scenes: List[Scene]
    voice_id: str
    narration_style: Optional[str] = "Normal"
    reference_image_b64: Optional[str] = None
    mode: Optional[str] = "images"  # images | layers
    title: Optional[str] = None
    brief: Optional[str] = None

class VideoResponse(BaseModel):
    status: str
    video_url: Optional[str] = None
    subtitles_url: Optional[str] = None
    message: str
    elapsed_seconds: Optional[float] = None


# --- Auto-generate (Brief -> Nick BR JSON -> Render) ---

class AutoGenerateRequest(BaseModel):
    brief: Optional[str] = None
    tema: Optional[str] = None
    voice_id: Optional[str] = "Antonio"
    mode: Optional[str] = "layers"  # force layers to avoid image-gen API calls
    test_mode: Optional[bool] = False
    target_duration_sec: Optional[float] = None
    validation_mode: Optional[str] = "auto"  # auto | strict | relaxed

class AutoGenerateResponse(BaseModel):
    status: str
    title: Optional[str] = None
    description: Optional[str] = None
    scene_plan: Optional[dict] = None
    video_url: Optional[str] = None
    subtitles_url: Optional[str] = None
    message: str
    job_id: Optional[str] = None
    run_dir: Optional[str] = None
    elapsed_seconds: Optional[float] = None


class JobSubmitResponse(BaseModel):
    status: str
    job_id: str
    queue_status: str
    route_name: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    job_type: str
    route_name: str
    queue_status: str
    stage: str
    attempts: int
    max_attempts: int
    worker_id: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    progress: Optional[dict] = None
    result: Optional[dict] = None


class SmokeBatchRequest(AutoGenerateRequest):
    count: int = 3


def _read_text_file(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _safe_json_extract(text: str) -> dict:
    """Best-effort JSON extraction: direct parse, or extract fenced blocks."""
    text = (text or '').strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    import re
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    a = text.find('{')
    b = text.rfind('}')
    if a != -1 and b != -1 and b > a:
        try:
            return json.loads(text[a:b+1])
        except Exception:
            pass
    raise Exception('LLM output is not valid JSON')


def _load_layers_asset_catalog() -> dict:
    catalog = {"avatar_poses": [], "props": [], "templates": [
        "avatar_center",
        "avatar_left_prop_right",
        "avatar_right_prop_left",
        "icons_with_red_x",
        "metaphor_single_prop",
    ]}
    try:
        avatar_meta = os.path.join(BASE_DIR, 'assets', 'avatars', 'marcos_avatar', 'whisk_pack_v1_png', '_meta.json')
        if os.path.exists(avatar_meta):
            data = json.loads(_read_text_file(avatar_meta))
            catalog['avatar_poses'] = sorted(list((data.get('files') or {}).keys()))
            print(f"[AutoGenerate] Loaded {len(catalog['avatar_poses'])} avatar poses")
    except Exception as e:
        print(f"[AutoGenerate] Failed loading avatar catalog: {e}, allowed_poses will be empty")
    try:
        props_meta = os.path.join(BASE_DIR, 'assets', 'props', 'whisk_pack_v1_png', '_meta.json')
        if os.path.exists(props_meta):
            data = json.loads(_read_text_file(props_meta))
            catalog['props'] = sorted(list((data.get('files') or {}).keys()))
    except Exception as e:
        print(f"[AutoGenerate] Failed loading props catalog: {e}")
    print(f"[AutoGenerate] Catalog: {len(catalog.get('avatar_poses', []))} poses, {len(catalog.get('props', []))} props, {len(catalog.get('templates', []))} templates")
    return catalog


BEAT_LIBRARY: dict[str, dict[str, object]] = {
    "hook": {
        "template_candidates": ["avatar_center", "avatar_left_prop_right"],
        "pose_candidates": ["surprised", "thinking_hand_chin", "pointing"],
        "prop_candidates": ["question_mark", "brain", "warning_sign"],
        "pose_family": "hook",
        "prop_family": "question",
        "motion": "zoom_in",
    },
    "problem": {
        "template_candidates": ["avatar_left_prop_right", "icons_with_red_x"],
        "pose_candidates": ["worried", "frustrated", "shaking_no"],
        "prop_candidates": ["warning_sign", "red_x", "chart_down"],
        "pose_family": "problem",
        "prop_family": "warning",
        "motion": "zoom_in",
    },
    "belief_break": {
        "template_candidates": ["avatar_center", "avatar_right_prop_left"],
        "pose_candidates": ["pointing", "explaining_hand_up", "neutral_arms_crossed"],
        "prop_candidates": ["red_x", "warning_sign", "brain"],
        "pose_family": "authority",
        "prop_family": "warning",
        "motion": "cut",
    },
    "authority": {
        "template_candidates": ["avatar_center", "avatar_left_prop_right"],
        "pose_candidates": ["neutral_arms_crossed", "explaining_hand_up", "pointing"],
        "prop_candidates": ["briefcase", "calendar", "contract"],
        "pose_family": "authority",
        "prop_family": "credibility",
        "motion": "zoom_out",
    },
    "number": {
        "template_candidates": ["avatar_left_prop_right", "avatar_right_prop_left"],
        "pose_candidates": ["pointing", "explaining_hand_up", "thinking_hand_chin"],
        "prop_candidates": ["coin_stack", "moneybag", "piggy_bank"],
        "pose_family": "proof",
        "prop_family": "money",
        "motion": "cut",
    },
    "analogy": {
        "template_candidates": ["metaphor_single_prop", "avatar_right_prop_left"],
        "pose_candidates": ["explaining_hand_up", "thinking_hand_chin", "pointing"],
        "prop_candidates": ["coin_stack", "chart_up", "calendar"],
        "pose_family": "explain",
        "prop_family": "metaphor",
        "motion": "zoom_out",
    },
    "proof": {
        "template_candidates": ["avatar_right_prop_left", "avatar_left_prop_right"],
        "pose_candidates": ["pointing", "smiling", "explaining_hand_up"],
        "prop_candidates": ["chart_up", "green_check", "up_arrow"],
        "pose_family": "proof",
        "prop_family": "growth",
        "motion": "zoom_in",
    },
    "mindset": {
        "template_candidates": ["avatar_center", "avatar_left_prop_right"],
        "pose_candidates": ["thinking_hand_chin", "relieved_exhale", "neutral_arms_crossed"],
        "prop_candidates": ["brain", "green_check", "calendar"],
        "pose_family": "mindset",
        "prop_family": "mindset",
        "motion": "crossfade",
    },
    "practical": {
        "template_candidates": ["avatar_left_prop_right", "avatar_right_prop_left"],
        "pose_candidates": ["explaining_hand_up", "pointing", "smiling"],
        "prop_candidates": ["piggy_bank", "receipt", "coin_stack"],
        "pose_family": "practical",
        "prop_family": "action",
        "motion": "cut",
    },
    "warning": {
        "template_candidates": ["icons_with_red_x", "avatar_left_prop_right"],
        "pose_candidates": ["shaking_no", "frustrated", "worried"],
        "prop_candidates": ["red_x", "warning_sign", "chart_down"],
        "pose_family": "warning",
        "prop_family": "warning",
        "motion": "zoom_in",
    },
    "close": {
        "template_candidates": ["avatar_center", "avatar_right_prop_left"],
        "pose_candidates": ["smiling", "relieved_exhale", "neutral_arms_crossed"],
        "prop_candidates": ["green_check", "up_arrow", "calendar"],
        "pose_family": "close",
        "prop_family": "resolution",
        "motion": "zoom_out",
    },
}

BEAT_KEYWORDS: list[tuple[list[str], str]] = [
    (["não faça isso", "aqui é onde", "erram", "estragam", "cuidado"], "warning"),
    (["meu nome", "eu passo tempo", "psicologia financeira", "me chamo"], "authority"),
    (["número", "numero", "regra", "marco", "r$", "%"], "number"),
    (["bola de neve", "gravidade", "dominó", "metáfora", "pedra"], "analogy"),
    (["modo sobrevivência", "ansiedade", "confiança", "escassez", "crescimento"], "mindset"),
    (["mercado", "carro", "restaurante", "demissão", "emergência", "casa"], "practical"),
    (["maioria das pessoas", "estão erradas", "ninguém explica"], "belief_break"),
    (["problema", "erro", "perda", "prejuízo", "dívida"], "problem"),
    (["resultado", "subir", "crescer", "lucro", "acelera"], "proof"),
    (["futuro", "trajetória", "decisão hoje", "amanhã"], "close"),
]


def _normalize_scene_text(text: str, max_sentences: int = 2, max_chars: int = 240) -> str:
    text = _clean_caption_text(text)
    if not text:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if len(sentences) > max_sentences:
        text = " ".join(sentences[:max_sentences]).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _derive_caption_line(text: str, max_chars: int = 56) -> str:
    text = _normalize_scene_text(text, max_sentences=1, max_chars=max_chars)
    if not text:
        return ""
    text = text.rstrip(".!? ")
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _prepare_tts_scene_text(scene: Scene | dict) -> str:
    if isinstance(scene, dict):
        raw = scene.get("texto_narracao") or scene.get("description") or ""
        beat = (scene.get("beat") or "").strip().lower()
    else:
        raw = scene.get_narration
        beat = (scene.beat or "").strip().lower()

    text = _normalize_scene_text(raw, max_sentences=2, max_chars=260)
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*([,;:])\s*", r"\1 ", text)
    text = re.sub(r"\s+(mas|porque|entao|então|agora|depois|por isso|so que|só que|enquanto)\s+", r", \1 ", text, flags=re.I)
    text = re.sub(r",\s*,+", ", ", text)
    if text[-1] not in ".!?":
        text = text.rstrip(",;: ") + "."

    pause_map = {
        "hook": "...",
        "problem": "...",
        "belief_break": ".",
        "authority": ".",
        "number": ".",
        "analogy": "...",
        "proof": ".",
        "mindset": "...",
        "practical": ".",
        "warning": "...",
        "close": ".",
    }
    pause = pause_map.get(beat, ".")
    bridge_map = {
        "hook": " Agora presta atencao.",
        "problem": " Respira.",
        "belief_break": " E aqui vira o jogo.",
        "authority": " Olha isso.",
        "number": " Anota esse numero.",
        "analogy": " Pensa nisso.",
        "proof": " E melhora rapido.",
        "mindset": " Sem pressa.",
        "practical": " Faz assim.",
        "warning": " Nao pula essa parte.",
        "close": " E continua daqui.",
    }
    bridge = bridge_map.get(beat, "")
    return f"{text}{bridge} {pause}".strip()


def _build_tts_script(scenes: List[Scene]) -> str:
    chunks = []
    for scene in scenes:
        chunk = _prepare_tts_scene_text(scene)
        if chunk:
            chunks.append(chunk)
    return "\n\n\n".join(chunks).strip()


def _build_target_beats(scene_count: int, short_form: bool) -> list[str]:
    if scene_count <= 0:
        return []
    if short_form:
        base = ["hook", "problem", "number", "proof", "practical", "warning", "close"]
    else:
        base = ["hook", "problem", "belief_break", "authority", "number", "analogy", "proof", "mindset", "practical", "warning", "close"]
    if scene_count <= len(base):
        return [base[min(int(i * len(base) / scene_count), len(base) - 1)] for i in range(scene_count)]

    beats = []
    for idx in range(scene_count):
        ratio = idx / max(scene_count - 1, 1)
        if ratio < 0.08:
            beats.append("hook")
        elif ratio < 0.18:
            beats.append("problem")
        elif ratio < 0.28:
            beats.append("belief_break")
        elif ratio < 0.38:
            beats.append("authority")
        elif ratio < 0.50:
            beats.append("number")
        elif ratio < 0.62:
            beats.append("analogy")
        elif ratio < 0.75:
            beats.append("proof")
        elif ratio < 0.86:
            beats.append("mindset" if idx % 2 == 0 else "practical")
        elif ratio < 0.94:
            beats.append("warning")
        else:
            beats.append("close")
    return beats


def _infer_scene_beat(text: str, idx: int, total_scenes: int, short_form: bool = False) -> str:
    normalized = unicodedata.normalize("NFD", (text or "").lower())
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = " ".join(normalized.split())
    for keywords, beat in BEAT_KEYWORDS:
        normalized_keywords = []
        for keyword in keywords:
            nk = unicodedata.normalize("NFD", keyword.lower())
            nk = "".join(ch for ch in nk if unicodedata.category(ch) != "Mn")
            normalized_keywords.append(" ".join(nk.split()))
        if any(keyword in normalized for keyword in normalized_keywords):
            return beat
    target_beats = _build_target_beats(total_scenes or 1, short_form=short_form)
    if idx < len(target_beats):
        return target_beats[idx]
    return target_beats[-1] if target_beats else "proof"


def _pick_first_allowed(candidates: list[str], allowed: set[str], fallback: str) -> str:
    for candidate in candidates:
        if not allowed or candidate in allowed:
            return candidate
    return fallback


def _scene_defaults_for_beat(beat: str, catalog: dict) -> dict:
    beat_config = BEAT_LIBRARY.get(beat) or BEAT_LIBRARY["proof"]
    allowed_templates = set(catalog.get("templates") or [])
    allowed_poses = set(catalog.get("avatar_poses") or [])
    allowed_props = set(catalog.get("props") or [])
    template = _pick_first_allowed(
        list(beat_config["template_candidates"]),
        allowed_templates,
        "avatar_left_prop_right",
    )
    pose = _pick_first_allowed(
        list(beat_config["pose_candidates"]),
        allowed_poses,
        "neutral_arms_crossed",
    )
    props = []
    for candidate in beat_config["prop_candidates"]:
        if not allowed_props or candidate in allowed_props:
            props.append(candidate)
        if len(props) >= 2:
            break
    return {
        "template": template,
        "avatar_pose": pose,
        "props": props,
        "motion": beat_config.get("motion"),
        "pose_family": beat_config.get("pose_family"),
        "prop_family": beat_config.get("prop_family"),
    }


def _validate_scene_plan(plan: dict, catalog: dict | None = None) -> dict:
    if not isinstance(plan, dict):
        raise Exception('scene_plan must be an object')

    title = plan.get('title')
    description = plan.get('description')
    script = plan.get('script')
    scenes = plan.get('scenes')

    if not isinstance(title, str) or len(title.strip()) < 5:
        raise Exception('Missing/invalid title')
    if not isinstance(description, str) or len(description.strip()) < 10:
        raise Exception('Missing/invalid description')
    if not isinstance(script, str) or len(script.strip()) < 30:
        raise Exception('Missing/invalid script')
    if not isinstance(scenes, list) or len(scenes) < 4:
        raise Exception('Missing/invalid scenes array')

    if catalog is None:
        catalog = _load_layers_asset_catalog()
    allowed_templates = set(catalog['templates'])
    allowed_poses = set(catalog['avatar_poses'])
    allowed_props = set(catalog['props'])
    short_form = len(scenes) <= 12

    normalized_scenes = []
    for i, sc in enumerate(scenes, start=1):
        if not isinstance(sc, dict):
            raise Exception(f'Scene {i} must be an object')

        texto = _normalize_scene_text(sc.get('texto_narracao') or sc.get('description') or '')
        if len(texto) < 20:
            raise Exception(f'Scene {i} missing texto_narracao')

        dur = sc.get('duracao_estimada') or sc.get('duration_est') or 5.0
        try:
            dur = float(dur)
        except Exception:
            dur = 5.0
        if dur < 2.5: dur = 2.5
        if dur > 6.0: dur = 6.0

        beat = (sc.get('beat') or '').strip().lower() or _infer_scene_beat(texto, i - 1, len(scenes), short_form=short_form)
        defaults = _scene_defaults_for_beat(beat, catalog)

        template = (sc.get('template') or defaults['template'] or 'avatar_left_prop_right').strip()
        if template not in allowed_templates:
            template = str(defaults['template'])

        pose = (sc.get('avatar_pose') or defaults['avatar_pose'] or 'neutral_arms_crossed').strip()
        if allowed_poses and pose not in allowed_poses and len(allowed_poses) > 0:
            pose = str(defaults['avatar_pose'])

        props = sc.get('props') or defaults['props'] or []
        if not isinstance(props, list):
            props = list(defaults['props'] or [])
        props2 = []
        for p in props[:3]:
            if isinstance(p, str) and (not allowed_props or (len(allowed_props) > 0 and p in allowed_props)):
                props2.append(p)
        if template == 'icons_with_red_x' and 'red_x' not in props2:
            props2 = (props2 + ['red_x'])[:3]

        caption_line = _derive_caption_line(sc.get('caption_line') or texto)
        if not caption_line:
            raise Exception(f'Scene {i} missing caption_line')

        normalized_scenes.append({
            'id': i,
            'texto_narracao': texto,
            'prompt_visual': (sc.get('prompt_visual') or sc.get('visual_prompt') or '').strip() or None,
            'duracao_estimada': dur,
            'tipo_transicao': (sc.get('tipo_transicao') or 'cut'),
            'template': template,
            'avatar_pose': pose,
            'props': props2,
            'motion': (sc.get('motion') or defaults.get('motion') or None),
            'beat': beat,
            'caption_line': caption_line,
            'pose_family': (sc.get('pose_family') or defaults.get('pose_family') or beat),
            'prop_family': (sc.get('prop_family') or defaults.get('prop_family') or beat),
        })

    return {
        'title': title.strip(),
        'description': description.strip(),
        'script': " ".join(_scene_narration_list({"scenes": normalized_scenes})) or script.strip(),
        'scenes': normalized_scenes,
    }


def _validate_script_structure(plan: dict, min_sections_required: int = 5) -> tuple[bool, list[str]]:
    """Valida estrutura do metaprompt no script e retorna (is_valid, errors)."""
    errors: list[str] = []

    if not isinstance(plan, dict):
        return False, ["plan must be a dict"]

    script = (plan.get("script") or "").strip()
    if not script:
        scenes = plan.get("scenes") or []
        if isinstance(scenes, list):
            script = " ".join(
                (sc.get("texto_narracao") or sc.get("description") or "").strip()
                for sc in scenes
                if isinstance(sc, dict)
            ).strip()

    if not script:
        return False, ["Script vazio: não foi possível validar estrutura"]

    def _normalize_text(value: str) -> str:
        value = value.lower()
        value = "".join(
            ch for ch in unicodedata.normalize("NFD", value)
            if unicodedata.category(ch) != "Mn"
        )
        return " ".join(value.split())

    required_sections: list[tuple[str, list[str]]] = [
        ("ABERTURA COM CENA EMOCIONAL", ["você já", "imagina que", "3 da manhã", "olhando para", "desconforto", "dúvida"]),
        ("QUEBRA DE CRENÇA", ["maioria das pessoas", "a maioria", "muita gente", "todo mundo", "mas estão erradas", "ninguem explica"]),
        ("APRESENTAÇÃO PESSOAL", ["meu nome", "me chamo", "sou o", "sou a", "eu passo tempo", "psicologia financeira"]),
        ("NÚMERO CENTRAL", ["número", "numero", "valor", "regra", "marco", "regra simples", "a oqui está motivo"]),
        ("ANALOGIA FÍSICA", ["bola de neve", "bola", "neve", "acumulando", "pedra subindo", "gravidade", "dominó", "metáfora"]),
        ("PROGRESSÃO MATEMÁTICA", ["primeiro", "primeira", "início", "começo", "segundo", "terceiro", "anos", "acelera"]),
        ("MUDANÇA PSICOLÓGICA", ["modo sobrevivência", "sobrevivencia", "sobreviver", "falta", "modo crescimento", "opção", "ansiedade", "confiança"]),
        ("APLICAÇÃO PRÁTICA", ["carro", "veículo", "automóvel", "transporte", "restaurante", "mercado", "emergência", "demissão", "promoção", "casa"]),
        ("ALERTA", ["aqui é onde", "aqui que", "é aqui", "momento", "erram", "estragam", "não faça isso"]),
        ("FECHAMENTO", ["não é sobre ficar rico", "não é riqueza", "riqueza rápida", "ficar rico", "mudança de trajetória", "decisão hoje", "futuro"]),
    ]

    normalized_script = _normalize_text(script)
    cursor = 0
    matched_sections = 0
    missing_sections: list[str] = []
    for section_name, keywords in required_sections:
        normalized_keywords = [_normalize_text(k) for k in keywords]
        matches = [normalized_script.find(k, cursor) for k in normalized_keywords]
        valid_matches = [pos for pos in matches if pos != -1]
        if not valid_matches:
            missing_sections.append(
                f"{section_name} (keywords esperadas: {', '.join(keywords)})"
            )
            continue
        matched_sections += 1
        cursor = min(valid_matches) + 1

    if matched_sections < min_sections_required:
        errors.append(
            f"Estrutura insuficiente: {matched_sections}/{len(required_sections)} seções detectadas "
            f"(mínimo: {min_sections_required}). Ausentes ou fora de ordem: {'; '.join(missing_sections)}"
        )

    lines = script.splitlines()
    bullet_line_re = re.compile(r"^\s*[-*]\s+")
    bullet_lines = [idx + 1 for idx, line in enumerate(lines) if bullet_line_re.match(line)]
    if bullet_lines:
        errors.append(f"PROIBIÇÃO: listas com '-' ou '*' detectadas nas linhas {bullet_lines}")

    subtitle_lines = [
        idx + 1 for idx, line in enumerate(lines)
        if "##" in line or "###" in line
    ]
    if subtitle_lines:
        errors.append(f"PROIBIÇÃO: subtítulos '##/###' detectados nas linhas {subtitle_lines}")

    extra_bullets = len([line for line in lines if re.match(r"^\s*[-*•]\s+", line)])
    if extra_bullets > 2:
        errors.append(f"PROIBIÇÃO: bullet points em excesso ({extra_bullets} encontrados)")

    return (len(errors) == 0, errors)


def _validate_required_beats(plan: dict, short_form: bool = False) -> tuple[bool, list[str]]:
    scenes = plan.get("scenes") or []
    beats = [str(sc.get("beat") or "").strip().lower() for sc in scenes if isinstance(sc, dict)]
    required = ["hook", "number", "practical", "close"] if short_form else [
        "hook",
        "belief_break",
        "authority",
        "number",
        "analogy",
        "mindset",
        "practical",
        "warning",
        "close",
    ]
    missing = [beat for beat in required if beat not in beats]
    if missing:
        return False, [f"Beat coverage insuficiente: faltando {', '.join(missing)}"]
    return True, []


# ---------------------------------------------------------------------------
# Validador de coerência visual (sem LLM, keyword-based)
# ---------------------------------------------------------------------------

_SEMANTIC_RULES: list[tuple[list[str], list[str], list[str]]] = [
    # (keywords na narração, props esperados, poses esperadas)
    (['dinheiro', 'valor', 'preço', 'custo', 'real', 'reais', 'salário', 'renda'],
     ['moneybag', 'coin_stack', 'piggy_bank'],
     ['explaining_hand_up', 'pointing']),
    (['problema', 'erro', 'perda', 'prejuízo', 'dívida', 'perder', 'cuidado'],
     ['warning_sign', 'red_x', 'chart_down'],
     ['worried', 'frustrated', 'shaking_no']),
    (['solução', 'ganho', 'crescimento', 'lucro', 'resultado', 'subir', 'crescer'],
     ['chart_up', 'green_check', 'up_arrow'],
     ['smiling', 'relieved_exhale']),
    (['trabalho', 'carreira', 'emprego', 'profissão', 'empresa'],
     ['briefcase', 'contract', 'calendar'],
     ['neutral_arms_crossed', 'thinking_hand_chin']),
    (['luxo', 'gasto', 'comprar', 'gastar', 'consumo'],
     ['car', 'house', 'airplane'],
     ['surprised', 'pushing_pose']),
    (['economia', 'poupança', 'poupar', 'guardar', 'economizar', 'reserva'],
     ['piggy_bank', 'savings_jar', 'coin_stack'],
     ['explaining_hand_up', 'thinking_hand_chin']),
    (['pergunta', 'dúvida', 'por que', 'como', 'será que'],
     ['question_mark', 'brain'],
     ['thinking_hand_chin', 'surprised']),
]


def _validate_scene_coherence(scenes: list[dict], catalog: dict) -> list[dict]:
    """Valida e corrige coerência visual das cenas pós-LLM.

    1. Corrige props incoerentes com a narração (keyword matching)
    2. Elimina repetição consecutiva de pose+prop
    3. Valida timing: narração longa demais para duração curta
    """
    allowed_props = set(catalog.get('props') or [])
    allowed_poses = set(catalog.get('avatar_poses') or [])
    fixes = []

    for i, sc in enumerate(scenes):
        texto = (sc.get('texto_narracao') or '').lower()
        beat = (sc.get('beat') or _infer_scene_beat(texto, i, len(scenes), short_form=len(scenes) <= 12)).strip().lower()
        defaults = _scene_defaults_for_beat(beat, catalog)
        current_props = sc.get('props') or []
        current_pose = sc.get('avatar_pose') or 'neutral_arms_crossed'
        current_template = sc.get('template') or defaults['template']

        sc['caption_line'] = _derive_caption_line(sc.get('caption_line') or sc.get('texto_narracao') or '')
        sc['beat'] = beat
        sc['pose_family'] = sc.get('pose_family') or defaults.get('pose_family') or beat
        sc['prop_family'] = sc.get('prop_family') or defaults.get('prop_family') or beat

        # --- 1) Keyword → prop matching ---
        best_rule = None
        best_score = 0
        for keywords, rule_props, rule_poses in _SEMANTIC_RULES:
            score = sum(1 for kw in keywords if kw in texto)
            if score > best_score:
                best_score = score
                best_rule = (rule_props, rule_poses)

        if best_rule and best_score >= 1:
            suggested_props, suggested_poses = best_rule
            # Se nenhum prop atual está na lista sugerida, corrigir
            if not any(p in suggested_props for p in current_props):
                # Pegar o primeiro prop sugerido que existe no catálogo
                for sp in suggested_props:
                    if not allowed_props or sp in allowed_props:
                        sc['props'] = [sp] + [p for p in current_props if p != sp][:2]
                        fixes.append(f"Cena {sc.get('id', i+1)}: props corrigidos → {sc['props']}")
                        break

            # Se a pose não combina, sugerir (só se existe no catálogo)
            if current_pose not in suggested_poses:
                for sp in suggested_poses:
                    if not allowed_poses or sp in allowed_poses:
                        sc['avatar_pose'] = sp
                        fixes.append(f"Cena {sc.get('id', i+1)}: pose corrigida → {sp}")
                        break

        # --- 2) Anti-repetição consecutiva ---
        if i > 0:
            prev = scenes[i - 1]
            same_pose = sc.get('avatar_pose') == prev.get('avatar_pose')
            same_props = sc.get('props') == prev.get('props')
            same_template = current_template == prev.get('template')
            if same_pose and same_props and best_rule:
                # Rotacionar para próxima pose/prop disponível
                _, rule_poses = best_rule
                alt_poses = [p for p in rule_poses if p != sc.get('avatar_pose') and (not allowed_poses or p in allowed_poses)]
                if alt_poses:
                    sc['avatar_pose'] = alt_poses[0]
                    fixes.append(f"Cena {sc.get('id', i+1)}: pose anti-repetição → {alt_poses[0]}")
            if same_template:
                template_candidates = list((BEAT_LIBRARY.get(beat) or BEAT_LIBRARY["proof"])["template_candidates"])
                alt_templates = [tpl for tpl in template_candidates if tpl != current_template and tpl in (catalog.get("templates") or [])]
                if alt_templates:
                    sc['template'] = alt_templates[0]
                    fixes.append(f"Cena {sc.get('id', i+1)}: template anti-repetição → {alt_templates[0]}")

        # --- 3) Timing: chars vs duração ---
        texto_len = len(sc.get('texto_narracao') or '')
        dur = sc.get('duracao_estimada', 5.0)
        # ~15 chars/segundo é um ritmo rápido mas legível
        min_dur_needed = texto_len / 15.0
        if min_dur_needed > dur + 1.0:
            sc['duracao_estimada'] = round(min(min_dur_needed, 6.0), 1)
            fixes.append(f"Cena {sc.get('id', i+1)}: duração ajustada {dur}s → {sc['duracao_estimada']}s (texto longo)")

    if fixes:
        print(f"[CoherenceValidator] {len(fixes)} correções aplicadas:")
        for f in fixes:
            print(f"  → {f}")
    else:
        print("[CoherenceValidator] ✅ Todas as cenas coerentes")

    return scenes


def _scene_narration_list(plan: dict) -> list[str]:
    scenes = plan.get('scenes') or []
    narrations = []
    for sc in scenes:
        if isinstance(sc, dict):
            text = (sc.get('texto_narracao') or sc.get('description') or '').strip()
            if text:
                narrations.append(text)
    return narrations


def _fit_short_form_plan(plan: dict, target_duration_sec: float) -> dict:
    scenes = [dict(sc) for sc in (plan.get('scenes') or []) if isinstance(sc, dict)]
    if not scenes:
        return plan

    target_duration_sec = max(TEST_MODE_MIN_DURATION_SEC, min(TEST_MODE_MAX_DURATION_SEC, float(target_duration_sec)))
    target_scene_count = max(6, min(12, int(round(target_duration_sec / 5.0))))
    base_scene_duration = round(target_duration_sec / target_scene_count, 1)
    base_scene_duration = max(3.5, min(6.0, base_scene_duration))

    if len(scenes) > target_scene_count:
        scenes = scenes[:target_scene_count]

    filler_idx = 0
    while len(scenes) < target_scene_count:
        base = dict(scenes[filler_idx % len(scenes)])
        base['id'] = len(scenes) + 1
        extra = " Mantem o ritmo e prepara a próxima virada."
        text = (base.get('texto_narracao') or '').strip()
        if text and extra.strip() not in text:
            base['texto_narracao'] = f"{text}{extra}"
        scenes.append(base)
        filler_idx += 1

    for idx, sc in enumerate(scenes, start=1):
        text = (sc.get('texto_narracao') or '').strip()
        if text:
            sc['texto_narracao'] = _normalize_scene_text(text, max_sentences=2, max_chars=220)
        sc['id'] = idx
        sc['duracao_estimada'] = base_scene_duration
        sc['beat'] = (sc.get('beat') or _infer_scene_beat(sc.get('texto_narracao') or '', idx - 1, len(scenes), short_form=True))
        sc['caption_line'] = _derive_caption_line(sc.get('caption_line') or sc.get('texto_narracao') or '')
        sc['pose_family'] = sc.get('pose_family') or sc['beat']
        sc['prop_family'] = sc.get('prop_family') or sc['beat']

    plan['scenes'] = scenes
    plan['script'] = " ".join(_scene_narration_list(plan))
    return plan


def _normalize_validation_mode(value: str | None, *, test_mode: bool = False) -> str:
    normalized = (value or "auto").strip().lower()
    if normalized not in {"auto", "strict", "relaxed"}:
        normalized = "auto"
    if normalized == "auto":
        return "relaxed" if test_mode else "strict"
    return normalized


def _build_fallback_scene_plan(brief: str, long_form: bool = True, target_duration_sec: float | None = None) -> dict:
    """Plano de contingência quando o LLM não retorna JSON válido."""
    catalog = _load_layers_asset_catalog()
    template = (catalog.get('templates') or ['avatar_left_prop_right'])[0]
    pose = (catalog.get('avatar_poses') or ['neutral_arms_crossed'])[0]

    theme = (brief or '').strip()
    if 'Tema principal:' in theme:
        theme = theme.split('Tema principal:', 1)[1].split('.', 1)[0].strip()
    if not theme:
        theme = 'como sair do modo sobrevivência financeira'

    if long_form:
        scene_count = 120
        dur = 6.0
    else:
        target_duration_sec = target_duration_sec or TEST_MODE_DEFAULT_DURATION_SEC
        target_duration_sec = max(TEST_MODE_MIN_DURATION_SEC, min(TEST_MODE_MAX_DURATION_SEC, float(target_duration_sec)))
        scene_count = max(6, min(12, int(round(target_duration_sec / 5.0))))
        dur = round(target_duration_sec / scene_count, 1)
        dur = max(3.5, min(6.0, dur))

    beat_copy = {
        "hook": [
            "Você trabalha, recebe e mesmo assim sente que o dinheiro some rápido demais.",
            "Tem alguma coisa invisível puxando você para trás.",
        ],
        "problem": [
            "O problema não é só ganhar pouco.",
            "É repetir decisões que mantêm você no modo sobrevivência.",
        ],
        "belief_break": [
            "A maioria das pessoas acha que precisa de um salto gigante de renda.",
            "Mas quase sempre o jogo vira antes disso.",
        ],
        "authority": [
            "Eu passo tempo demais estudando dinheiro e comportamento.",
            "E existe um padrão que aparece toda vez.",
        ],
        "number": [
            "Existe um número que muda sua sensação de sufoco.",
            "Quando ele aparece, você para de decidir no desespero.",
        ],
        "analogy": [
            "É como empurrar uma bola no começo da descida.",
            "No início parece pesado, depois ela ganha tração sozinha.",
        ],
        "proof": [
            "O primeiro marco demora mais.",
            "Depois o progresso acelera e o esforço começa a render.",
        ],
        "mindset": [
            "Sua cabeça sai da escassez e entra em estratégia.",
            "Você começa a escolher melhor, não só reagir.",
        ],
        "practical": [
            "Isso aparece no mercado, no carro, na conta surpresa e até numa demissão.",
            "Você ganha margem para respirar e agir.",
        ],
        "warning": [
            "Aqui é onde muita gente estraga tudo.",
            "Ela melhora um pouco e volta a inflar o padrão de vida.",
        ],
        "close": [
            "Não é sobre ficar rico rápido.",
            "É sobre mudar sua trajetória com consistência a partir de agora.",
        ],
    }

    scenes = []
    target_beats = _build_target_beats(scene_count, short_form=not long_form)
    for i, beat in enumerate(target_beats):
        defaults = _scene_defaults_for_beat(beat, catalog)
        pieces = beat_copy.get(beat) or beat_copy["proof"]
        texto_base = " ".join(pieces)
        scenes.append({
            'id': i + 1,
            'texto_narracao': _normalize_scene_text(texto_base),
            'prompt_visual': None,
            'duracao_estimada': dur,
            'tipo_transicao': defaults.get('motion') or 'cut',
            'template': defaults.get('template') or template,
            'avatar_pose': defaults.get('avatar_pose') or pose,
            'props': list(defaults.get('props') or []),
            'motion': defaults.get('motion'),
            'beat': beat,
            'caption_line': _derive_caption_line(texto_base),
            'pose_family': defaults.get('pose_family') or beat,
            'prop_family': defaults.get('prop_family') or beat,
        })

    return {
        'title': f"{theme[:72]}".strip().capitalize(),
        'description': 'Plano prático em linguagem simples para melhorar decisão e execução financeira.',
        'script': ' '.join(s['texto_narracao'] for s in scenes),
        'scenes': scenes,
    }


def _enforce_target_duration(plan: dict, min_sec: float = 480.0, max_sec: float = 900.0) -> dict:
    scenes = plan.get('scenes') or []
    if not scenes:
        return plan

    # FIX 4: Expansões variadas relacionadas a finanças pessoais
    INTELLIGENT_EXPANSIONS = [
        "Respira e observa: se você aplicar isso por 30 dias, a diferença aparece no caixa e na sua decisão.",
        "Muita gente ignora esse passo, mas é exatamente ele que separa quem sai do vermelho de quem fica preso.",
        "Quando você domina isso, percebe que o problema nunca foi falta de dinheiro, mas falta de método.",
        "Esse conceito parece óbvio, mas 9 em cada 10 pessoas não fazem na prática.",
        "Se você só lembrar de uma coisa desse vídeo, lembra disso: consistência vence inteligência sem execução.",
        "Eu sei que parece simples demais, mas os resultados provam que funciona melhor que qualquer atalho.",
        "Agora aplica isso na sua rotina hoje. Começa pequeno, mas começa agora.",
        "O erro clássico é querer o resultado sem mudar o processo. Aqui você muda o processo.",
        "Isso não é teoria de livro: é o que realmente funciona quando você testa na vida real.",
        "Presta atenção nesse detalhe, porque ele vai economizar meses de tentativa e erro.",
        "Quando você internaliza isso, as decisões financeiras ficam automáticas e menos estressantes.",
        "Se você chegou até aqui, já está na frente de quem desiste no primeiro obstáculo.",
    ]

    total = sum(float(s.get('duracao_estimada') or 0) for s in scenes)

    # alonga quando está curto
    if total < min_sec and total > 0:
        factor = min_sec / total
        for i, s in enumerate(scenes):
            d = float(s.get('duracao_estimada') or 5.0)
            s['duracao_estimada'] = max(5.0, min(6.0, d * factor))
            
            # FIX 4: Expansão inteligente baseada no contexto
            txt = (s.get('texto_narracao') or '').strip()
            if txt and len(txt) < 180:
                expansion = INTELLIGENT_EXPANSIONS[i % len(INTELLIGENT_EXPANSIONS)]
                s['texto_narracao'] = _normalize_scene_text(f"{txt} {expansion}", max_sentences=2, max_chars=240)
                s['caption_line'] = _derive_caption_line(s['texto_narracao'])
        
        total = sum(float(s.get('duracao_estimada') or 0) for s in scenes)

    # se ainda estiver curto, duplica cenas até bater mínimo
    i = 0
    while total < min_sec and len(scenes) < 120:
        base = scenes[i % len(scenes)].copy()
        base['id'] = len(scenes) + 1
        
        # FIX 4: Usar expansões variadas ao duplicar
        expansion = INTELLIGENT_EXPANSIONS[(i + 3) % len(INTELLIGENT_EXPANSIONS)]
        base['texto_narracao'] = _normalize_scene_text((base.get('texto_narracao') or '').strip() + f" {expansion}", max_sentences=2, max_chars=240)
        base['duracao_estimada'] = max(4.0, min(6.0, float(base.get('duracao_estimada') or 7.0)))
        base['caption_line'] = _derive_caption_line(base.get('caption_line') or base['texto_narracao'])
        scenes.append(base)
        total += float(base['duracao_estimada'])
        i += 1

    # comprime quando está muito longo
    if total > max_sec and total > 0:
        factor = max_sec / total
        for s in scenes:
            d = float(s.get('duracao_estimada') or 7.0)
            s['duracao_estimada'] = max(5.0, min(12.0, d * factor))

    plan['scenes'] = scenes
    plan['script'] = ' '.join((s.get('texto_narracao') or '') for s in scenes)
    return plan


def _build_nick_br_prompt_v2(brief: str, short_form: bool = False, target_duration_sec: float | None = None) -> str:
    prompt_path = os.path.join(BASE_DIR, 'prompts', 'metaprompt_nick.md')
    meta = _read_text_file(prompt_path) if os.path.exists(prompt_path) else ''
    catalog = _load_layers_asset_catalog()

    # FIX 3: Regras semânticas de matching visual
    visual_matching_rules = """
# REGRAS DE MATCHING VISUAL (obrigatório)
- Quando a narração fala de DINHEIRO/VALOR/PREÇO → props: moneybag, coin_stack, piggy_bank | poses: explaining_hand_up, pointing
- Quando fala de PROBLEMA/ERRO/PERDA → props: warning_sign, red_x, chart_down | poses: worried, frustrated, shaking_no
- Quando fala de SOLUÇÃO/GANHO/CRESCIMENTO → props: chart_up, green_check, up_arrow | poses: smiling, relieved_exhale
- Quando fala de TRABALHO/CARREIRA → props: briefcase, contract, calendar | poses: neutral_arms_crossed, thinking_hand_chin
- Quando fala de LUXO/GASTO → props: car, house, airplane | poses: surprised, pushing_pose
- Quando fala de ECONOMIA/POUPANÇA → props: piggy_bank, savings_jar, coin_stack | poses: explaining_hand_up, thinking_hand_chin
- Quando fala de COMIDA/BÁSICO → props: ramen, coffee, receipt | poses: worried, frustrated
- Quando fala de PERGUNTA/DÚVIDA → props: question_mark, brain | poses: thinking_hand_chin, surprised
- NUNCA repetir a mesma combinação template+pose+prop em cenas consecutivas
- Variar entre os 5 templates disponíveis ao longo do vídeo
"""

    mode_rules = ""
    if short_form:
        target_duration_sec = target_duration_sec or TEST_MODE_DEFAULT_DURATION_SEC
        target_duration_sec = max(TEST_MODE_MIN_DURATION_SEC, min(TEST_MODE_MAX_DURATION_SEC, float(target_duration_sec)))
        scene_count = max(6, min(12, int(round(target_duration_sec / 5.0))))
        mode_rules = f"""
# TEST MODE (obrigatório)
- Gere um vídeo curto de aproximadamente {int(target_duration_sec)} segundos.
- Use entre {scene_count - 1} e {scene_count + 1} cenas.
- Cada cena deve ter no máximo 1 ou 2 frases bem curtas.
- Foque em uma única ideia central do brief, sem tentar cobrir toda a arquitetura longa.
- Priorize hook forte, clareza, ritmo e retenção.
"""

    return f"""{meta}\n\n# INPUT BRIEF\n{brief.strip()}\n\n# AVAILABLE LAYERS ASSETS (STRICT)\nTemplates: {catalog['templates']}\nAvatar poses: {catalog['avatar_poses'][:30]}{' ...' if len(catalog['avatar_poses'])>30 else ''}\nProps: {catalog['props'][:60]}{' ...' if len(catalog['props'])>60 else ''}\n\n{visual_matching_rules}\n{mode_rules}\n# OUTPUT FORMAT\nReturn ONLY valid JSON with keys: title, description, script, scenes.\n- scenes must be an array of objects with: texto_narracao (MAX 2 frases curtas por cena), caption_line (1 linha curta estilo CapCut), beat, duracao_estimada (4-6), template, avatar_pose, props (0-3), pose_family, prop_family.
- IMPORTANTE: cada cena deve ter no MÁXIMO 2 frases curtas. Cenas rápidas e dinâmicas. Max 6 segundos por cena.\n- Use Portuguese (PT-BR), Nick BR tone: rápido, direto, \"papo reto\", com exemplos, números, e um final com CTA suave.\n- NO markdown, NO comments, NO trailing commas.\n"""


def _llm_generate_scene_plan(
    brief: str,
    short_form: bool = False,
    target_duration_sec: float | None = None,
    validation_mode: str = "strict",
) -> dict:
    """Gera plano de cenas via OpenAI (primário para auto-generate)."""
    prompt = _build_nick_br_prompt_v2(brief, short_form=short_form, target_duration_sec=target_duration_sec)
    # Vídeos longos (8-15min) exigem JSON maior; evita truncar saída.
    is_long_form = not short_form and (('8-15' in brief or '8–15' in brief) or (target_duration_sec or 0) >= 480)
    max_tokens = 12000 if is_long_form else 4096

    max_attempts = LLM_MAX_RETRIES + 1
    last_error = None

    def _finalize_plan(raw: dict, allow_relaxed_script: bool = False, allow_plan_fallback: bool = False) -> dict:
        catalog = _load_layers_asset_catalog()
        try:
            plan = _validate_scene_plan(raw, catalog)
        except Exception:
            if not allow_plan_fallback:
                raise
            print("[AutoGenerate] JSON utilizável, mas plano inválido. Aplicando fallback determinístico.")
            plan = _build_fallback_scene_plan(
                brief,
                long_form=not short_form,
                target_duration_sec=target_duration_sec,
            )

        min_sections_required = 2 if short_form else (8 if validation_mode == "strict" else 5)
        is_valid_script, script_errors = _validate_script_structure(
            plan,
            min_sections_required=min_sections_required,
        )
        has_beats, beat_errors = _validate_required_beats(plan, short_form=short_form)
        if not is_valid_script:
            if not allow_relaxed_script:
                raise Exception(f"Estrutura do roteiro inválida: {script_errors}")
            print(f"[AutoGenerate] Validação estrutural relaxada: {script_errors}")
            plan['script'] = " ".join(_scene_narration_list(plan))
        if not has_beats:
            if not allow_relaxed_script:
                raise Exception(f"Estrutura por beats inválida: {beat_errors}")
            print(f"[AutoGenerate] Validação de beats relaxada: {beat_errors}")

        plan['scenes'] = _validate_scene_coherence(plan.get('scenes') or [], catalog)
        if short_form:
            plan = _fit_short_form_plan(
                plan,
                target_duration_sec or TEST_MODE_DEFAULT_DURATION_SEC,
            )
        elif is_long_form:
            plan = _enforce_target_duration(plan, min_sec=480.0, max_sec=900.0)
        return plan

    def _generate_via_openclaw_agent() -> dict:
        agent_id = os.getenv("OPENCLAW_AGENT_ID", "loki").strip() or "loki"
        timeout_seconds = max(LLM_CALL_TIMEOUT_SECONDS + 60, 180)
        proc = subprocess.run(
            [
                "openclaw",
                "agent",
                "--agent", agent_id,
                "--message", prompt,
                "--json",
                "--timeout", str(timeout_seconds),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 30,
            check=False,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            raise Exception(f"OpenClaw agent falhou (exit {proc.returncode}): {stderr or stdout[:600]}")

        data = json.loads((proc.stdout or "").strip() or "{}")
        payloads = (((data.get("result") or {}).get("payloads")) or [])
        text = ""
        for item in payloads:
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text").strip():
                text = item["text"]
                break
        if not text:
            raise Exception("OpenClaw agent retornou sem payload textual")
        return _safe_json_extract(text)

    if OPENAI_API_KEY:
        for attempt in range(max_attempts):
            try:
                resp = requests.post(
                    f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": OPENAI_MODEL,
                        "messages": [
                            {
                                "role": "system",
                                "content": "Você é um gerador de roteiro JSON estrito. Responda apenas JSON válido sem markdown."
                            },
                            {
                                "role": "user",
                                "content": prompt,
                            },
                        ],
                        "temperature": 0.6,
                        "max_completion_tokens": max_tokens,
                        "response_format": {"type": "json_object"},
                    },
                    timeout=LLM_CALL_TIMEOUT_SECONDS,
                )

                if resp.status_code >= 400:
                    raise Exception(f"HTTP {resp.status_code}: {resp.text[:400]}")

                data = resp.json()
                text = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
                raw = _safe_json_extract(text)
                if validation_mode == "relaxed":
                    return _finalize_plan(raw, allow_relaxed_script=True, allow_plan_fallback=True)

                try:
                    return _finalize_plan(raw)
                except Exception as strict_error:
                    print(f"[AutoGenerate] LLM retornou JSON mas falhou na validação estrita: {strict_error}")
                    if validation_mode == "strict":
                        raise
                    return _finalize_plan(raw, allow_relaxed_script=True, allow_plan_fallback=True)
            except Exception as e:
                last_error = e
                if attempt < max_attempts - 1:
                    backoff_seconds = 2 ** attempt
                    print(f"[AutoGenerate] LLM tentativa {attempt + 1}/{max_attempts} falhou: {e}. Retry em {backoff_seconds}s.")
                    time.sleep(backoff_seconds)
                    continue
                print(f"[AutoGenerate] OpenAI indisponível, tentando fallback OpenClaw agent: {e}")
                break

    try:
        raw = _generate_via_openclaw_agent()
        if validation_mode == "relaxed":
            return _finalize_plan(raw, allow_relaxed_script=True, allow_plan_fallback=True)
        try:
            return _finalize_plan(raw)
        except Exception as strict_error:
            print(f"[AutoGenerate] OpenClaw strict falhou, tentando modo relaxado: {strict_error}")
            if validation_mode == "strict":
                raise
            return _finalize_plan(raw, allow_relaxed_script=True, allow_plan_fallback=True)
    except Exception as openclaw_error:
        print(f"[AutoGenerate] OpenClaw indisponível, usando fallback de roteiro local: {openclaw_error}")
        try:
            fallback = _build_fallback_scene_plan(
                brief,
                long_form=not short_form,
                target_duration_sec=target_duration_sec,
            )
            return _finalize_plan(fallback, allow_relaxed_script=True, allow_plan_fallback=True)
        except Exception as fallback_error:
            detail = f"Falha no LLM OpenAI/OpenClaw: openai={last_error}; openclaw={openclaw_error}; fallback={fallback_error}"
            raise HTTPException(status_code=502, detail=detail) from fallback_error


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.time())


async def _wait_with_deadline(awaitable, operation: str, deadline: float, cap_seconds: float):
    remaining = _remaining_seconds(deadline)
    if remaining <= 0:
        raise TimeoutError(f"Tempo limite global excedido antes de: {operation}")

    timeout = max(0.1, min(cap_seconds, remaining))
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"{operation} excedeu {timeout:.1f}s (limite global /auto-generate: {AUTO_GENERATE_MAX_SECONDS}s).") from e

# --- Serviços Reais ---

async def service_gemini_script_refinement(script: str):
    """
    Usa o Gemini 1.5 Flash para refinar o roteiro e garantir que ele seja visualmente rico.
    (Opcional, se o usuário mandar o roteiro cru, mas aqui assumimos que o roteiro já vem ok ou refinamos).
    """
    if not GOOGLE_API_KEY:
        print("⚠️ Sem GOOGLE_API_KEY. Pulando refinamento Gemini.")
        return script

    try:
        model = genai.GenerativeModel('gemini-2.0-flash')
        response = model.generate_content(f"Melhore este roteiro para um vídeo VSL de alta conversão. Mantenha o mesmo tamanho aprox: {script}")
        return response.text
    except Exception as e:
        print(f"Erro Gemini Script: {e}")
        return script

async def upload_image_to_kie(image_path: str, api_key: str) -> str:
    """Faz upload de uma imagem para a Kie.ai e retorna a URL pública."""
    try:
        if not os.path.exists(image_path):
            return None
            
        with open(image_path, "rb") as f:
            image_data = f.read()
            
        image_b64 = base64.b64encode(image_data).decode("utf-8")
        ext = os.path.splitext(image_path)[1].lower()
        mime_type = "image/png" if ext == ".png" else "image/jpeg"
        filename = os.path.basename(image_path)
        
        url = "https://kieai.redpandaai.co/api/file-base64-upload"
        
        payload = {
            "base64Data": f"data:{mime_type};base64,{image_b64}",
            "uploadPath": "images/consistency",
            "fileName": filename
        }
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        print(f"    📤 Enviando imagem de referência para Kie.ai...")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload, headers=headers)
            result = response.json()
            
            if result.get("code") == 200:
                data = result.get("data", {})
                download_url = data.get("downloadUrl") or data.get("fileUrl") or data.get("url")
                if download_url:
                    print(f"    ✅ Upload concluído: {download_url[:50]}...")
                    return download_url
            
            print(f"    ⚠️ Erro no upload: {result}")
            return None
            
    except Exception as e:
        print(f"    ❌ Erro ao fazer upload da imagem: {e}")
        return None

async def poll_kie_task_status(task_id: str, api_key: str, timeout: int = 120, interval: int = 2) -> dict:
    """
    Faz polling do status de uma task da API Kie.ai até conclusão.
    Retorna o resultado ou lança Exception em caso de erro/timeout.
    """
    base_url = get_config("services.image_generation.options.seedream.base_url", "https://api.kie.ai/api/v1/jobs")
    url = f"{base_url}/recordInfo?taskId={task_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    start_time = time.time()
    async with httpx.AsyncClient(timeout=30) as client:
        while (time.time() - start_time) < timeout:
            try:
                response = await client.get(url, headers=headers)
                data = response.json()
                
                # Debug log
                # print(f"    [DEBUG] Poll Response: {data}")
                
                if data.get("code") != 200:
                    print(f"    ❌ Erro no polling: {data}")
                    raise Exception(f"Kie.ai API error: {data.get('msg', 'Unknown error')}")
                
                task_data = data.get("data", {})
                state = task_data.get("state", "").lower()
                status = task_data.get("status", "").lower()
                
                # Combine state and status for compatibility
                current_state = state or status
                
                if current_state and current_state != "pending" and current_state != "waiting" and current_state != "queuing" and current_state != "generating":
                    print(f"    📋 Estado: {current_state} (Task {task_id})")
                
                if current_state in ["success", "completed"]:
                    return task_data
                elif current_state in ["failed", "error", "fail"]:
                    error_msg = task_data.get("failMsg") or task_data.get("error") or "Unknown error"
                    raise Exception(f"Task failed: {error_msg}")
                
                # Ainda processando, aguardar
                await asyncio.sleep(interval)
                
            except httpx.RequestError as e:
                print(f"    ⚠️ Request error: {e}, retrying...")
                await asyncio.sleep(interval)
    
    raise Exception(f"Timeout após {timeout}s aguardando task {task_id}")


async def upload_image_for_kie(image_path: str, api_key: str) -> str:
    """
    Faz upload de uma imagem para o serviço de arquivos da Kie.ai.
    Retorna a URL pública da imagem.
    
    NOTA: Como a Kie.ai pode não ter endpoint de upload público,
    usamos uma estratégia de base64 inline no prompt ou um serviço externo.
    Por enquanto, retornamos o caminho local para inclusão via base64.
    """
    # Para a Kie.ai, vamos converter a imagem para base64 e incluir no prompt
    # já que eles suportam image_urls no payload
    return image_path  # Será processado na função de geração


async def generate_single_image_seedream(scene: Scene, img_config: dict, reference_image_path: str = None, pre_uploaded_ref_url: str = None) -> tuple:
    """
    Gera uma única imagem com Kie.ai Seedream 4.5 API.
    Suporta imagem de referência para consistência de personagem.
    API assíncrona: cria task → polling até conclusão → download da imagem.
    """
    try:
        api_key = os.getenv("KIE_API_KEY")
        if not api_key:
            raise Exception("KIE_API_KEY não configurada. Obtenha em https://kie.ai/api-key")
        
        seedream_config = get_config("services.image_generation.options.seedream", {})
        base_url = seedream_config.get("base_url", "https://api.kie.ai/api/v1/jobs")
        model = seedream_config.get("model", "seedream/4.5-text-to-image")
        quality = seedream_config.get("quality", "basic")
        aspect_ratio = seedream_config.get("aspect_ratio", "16:9")
        timeout = seedream_config.get("timeout_seconds", 120)
        interval = seedream_config.get("polling_interval_seconds", 2)
        
        # Extrair prompt visual
        visual_prompt = scene.get_visual_prompt
        if "POSITIVE PROMPT:" in visual_prompt:
            start = visual_prompt.find("POSITIVE PROMPT:") + len("POSITIVE PROMPT:")
            end = visual_prompt.find("NEGATIVE PROMPT:") if "NEGATIVE PROMPT:" in visual_prompt else len(visual_prompt)
            visual_prompt = visual_prompt[start:end].strip()
        
        print(f"  🎨 Cena {scene.id} (Seedream 4.5): {visual_prompt[:50]}...")
        
        # --- CACHE SYSTEM ---
        # Gerar hash único baseado no prompt e imagem de referência
        cache_key = f"{visual_prompt}_{reference_image_path or ''}_{pre_uploaded_ref_url or ''}"
        prompt_hash = hashlib.md5(cache_key.encode("utf-8")).hexdigest()
        cache_filename = f"cache_{prompt_hash}.png"
        cache_filepath = os.path.join(TEMP_DIR, cache_filename)
        
        if os.path.exists(cache_filepath):
            print(f"  ♻️ Cena {scene.id}: Imagem em cache encontrada! Economizando créditos.")
            return (scene.id, cache_filepath)
        # --------------------
        
        # Construir payload da API
        payload = {
            "model": model,
            "input": {
                "prompt": visual_prompt,
                "aspect_ratio": aspect_ratio,
                "quality": quality
            }
        }
        
        # Se tiver imagem de referência
        if pre_uploaded_ref_url:
            # Usar URL já carregada (Otimização para múltiplas cenas)
            payload["input"]["image_urls"] = [pre_uploaded_ref_url]
            character_instruction = (
                "Maintain EXACT character consistency with the provided image reference. "
                "Same face, hair, clothing, and style. "
            )
            payload["input"]["prompt"] = character_instruction + visual_prompt
            print(f"    📷 Referência (URL Cache): {pre_uploaded_ref_url[:30]}...")
            
        elif reference_image_path and os.path.exists(reference_image_path):
            # Primeiro fazer upload da imagem para obter URL válida (Fallback local)
            ref_url = await upload_image_to_kie(reference_image_path, api_key)
            
            if ref_url:
                payload["input"]["image_urls"] = [ref_url]
                character_instruction = (
                    "Maintain EXACT character consistency with the provided image reference. "
                    "Same face, hair, clothing, and style. "
                )
                payload["input"]["prompt"] = character_instruction + visual_prompt
                print(f"    📷 Referência (Nova URL): {os.path.basename(reference_image_path)}")
            else:
                print("    ⚠️ Falha no upload da referência, usando apenas prompt.")

        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        # Criar task
        async with httpx.AsyncClient(timeout=30) as client:
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    response = await client.post(f"{base_url}/createTask", json=payload, headers=headers)
                    result = response.json()
                    
                    if result.get("code") == 200:
                        task_id = result.get("data", {}).get("taskId")
                        if task_id:
                            print(f"    📋 Task criada: {task_id}")
                            break
                    
                    # Se der erro de servidor, tentar novamente uma vez
                    if "Server exception" in str(result.get("msg", "")) and attempt < max_retries - 1:
                        print(f"    ⚠️ Erro de servidor Kie.ai (tentativa {attempt+1}), tentando novamente...")
                        await asyncio.sleep(2)
                        continue
                        
                    raise Exception(f"Kie.ai API error: {result.get('msg', 'Unknown error')}")
                except (httpx.RequestError, json.JSONDecodeError) as e:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
                        continue
                    raise e
            else:
                raise Exception("Falha ao criar task após retries")
        
        # Polling até conclusão
        task_result = await poll_kie_task_status(task_id, api_key, timeout, interval)
        
        # Extrair URL do resultJson (padrão Common API) ou output (padrão antigo)
        image_url = None
        result_json_str = task_result.get("resultJson")
        
        if result_json_str:
            try:
                result_data = json.loads(result_json_str)
                urls = result_data.get("resultUrls") or result_data.get("image_urls")
                if urls and isinstance(urls, list) and len(urls) > 0:
                    image_url = urls[0]
            except:
                pass
        
        if not image_url:
            output = task_result.get("output", {})
            if isinstance(output, dict):
                image_url = output.get("image_url") or output.get("imageUrl") or output.get("url")
            elif isinstance(output, list) and len(output) > 0:
                image_url = output[0] if isinstance(output[0], str) else output[0].get("url")
        
        if not image_url:
            print(f"    ⚠️ Resposta sem URL de imagem. Keys: {list(task_result.keys())}")
            return (scene.id, None)
        
        # Download da imagem
        async with httpx.AsyncClient(timeout=60) as client:
            img_response = await client.get(image_url)
            if img_response.status_code == 200:
                filename = f"scene_{int(time.time())}_{scene.id}.png"
                filepath = os.path.join(TEMP_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(img_response.content)
                
                # Salvar também no cache
                with open(cache_filepath, "wb") as f:
                    f.write(img_response.content)
                    
                print(f"  ✅ Cena {scene.id} gerada (Seedream 4.5)")
                return (scene.id, filepath)
            else:
                raise Exception(f"Erro ao baixar imagem: HTTP {img_response.status_code}")
        
    except Exception as e:
        print(f"  ❌ Cena {scene.id} erro Seedream: {e}")
        return (scene.id, None)


    # [REMOVED] generate_single_image_nanobanana (Pollinations) - instável, HTTP 530
    # [REMOVED] generate_single_image_pollinations - removido por instabilidade
    # Provider único: Seedream 4.5 via Kie.ai (generate_single_image_seedream)

async def service_generate_images(scenes: List[Scene], reference_image_path: str = None):
    """
    Gera imagens em paralelo usando o provider configurado (apenas Seedream agora).
    Usa semáforo para limitar concorrência e upload único de referência.
    """
    provider = get_config("services.image_generation.provider", "seedream")
    
    print(f"[Orchestrator] Gerando {len(scenes)} imagens com {provider} (PARALELO)...")
    
    # 1. Otimização: Upload único da referência no início
    pre_uploaded_ref_url = None
    if reference_image_path:
        print(f"  📷 Preparando personagem de referência: {reference_image_path}")
        api_key = os.getenv("KIE_API_KEY")
        if api_key:
            pre_uploaded_ref_url = await upload_image_to_kie(reference_image_path, api_key)
            if pre_uploaded_ref_url:
                print(f"  ✅ Referência carregada em cache para reutilização.")
    
    # 2. Configurar Semáforo para controlar concorrência (ex: 5 requests simultâneos)
    CONCURRENCY_LIMIT = 5
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    
    async def generate_with_limit(scene):
        async with semaphore:
            # Usar exclusivamente Seedream 4.5 com a URL pré-carregada
            return await generate_single_image_seedream(scene, {}, reference_image_path, pre_uploaded_ref_url)

    # 3. Disparar tarefas em paralelo
    tasks = [generate_with_limit(scene) for scene in scenes]
    results = await asyncio.gather(*tasks)
    
    # 4. Processar resultados
    all_results = {}
    for result in results:
        if isinstance(result, tuple):
            scene_id, filepath = result
            all_results[scene_id] = filepath

    image_paths = [all_results.get(scene.id) for scene in scenes]
    
    success_count = sum(1 for p in image_paths if p)
    print(f"[Orchestrator] {success_count}/{len(scenes)} imagens geradas com sucesso")
    
    if success_count == 0:
        print("  🚨 CRÍTICO: Nenhuma imagem foi gerada. Interrompendo processo.")
        raise Exception("Falha na geração de imagens (Kie.ai). Processo interrompido para economizar créditos.")
    
    return image_paths
    
    return image_paths

def _ensure_minimum_audio_duration(audio_path: str, min_sec: float = 480.0) -> tuple[bool, float]:
    """
    FIX 2: Verifica se o áudio gerado tem duração mínima.
    
    Retorna:
        (is_valid, actual_duration)
    """
    try:
        audio_clip = AudioFileClip(audio_path)
        duration = audio_clip.duration
        audio_clip.close()
        
        is_valid = duration >= min_sec
        
        if not is_valid:
            print(f"  ⚠️ Duração do áudio abaixo do mínimo: {duration:.1f}s < {min_sec:.1f}s")
        else:
            print(f"  ✅ Duração do áudio válida: {duration:.1f}s >= {min_sec:.1f}s")
        
        return (is_valid, duration)
    except Exception as e:
        print(f"  ❌ Erro ao verificar duração do áudio: {e}")
        return (False, 0.0)


def _resplit_scenes_by_audio_duration(scenes: List[Scene], audio_duration: float, target_sec_per_scene: float = 6.0) -> List[Scene]:
    """
    Reescala as cenas baseadas na duração real do áudio.
    
    Lógica: roteiro/6 = número de cenas
    - Pega o script completo de todas as cenas
    - Divide igualmente pelo número de cenas calculado
    """
    # Concatenar todo o script
    full_script = ""
    for sc in scenes:
        narr = sc.get_narration or ""
        if narr:
            full_script += " " + narr
    full_script = full_script.strip()
    
    if not full_script:
        return scenes
    
    # Calcular número de cenas = áudio / 6 segundos
    num_scenes = max(1, int(round(audio_duration / target_sec_per_scene)))
    
    # Não exceder 200 cenas (limite razoável)
    num_scenes = min(num_scenes, 150)
    
    print(f"  → Reescalando {len(scenes)} cenas → {num_scenes} cenas ({audio_duration:.1f}s / {target_sec_per_scene}s por cena)")
    
    # Dividir o script em partes iguais
    words = full_script.split()
    chunk_size = max(1, len(words) // num_scenes)
    
    catalog = _load_layers_asset_catalog()
    templates = catalog.get('templates') or ['avatar_left_prop_right']
    poses = catalog.get('avatar_poses') or ['neutral_arms_crossed']
    props_list = catalog.get('props') or []
    
    new_scenes = []
    target_beats = _build_target_beats(num_scenes, short_form=audio_duration < 90)
    for i in range(num_scenes):
        start = i * chunk_size
        end = len(words) if i == num_scenes - 1 else min(len(words), (i + 1) * chunk_size)
        chunk = " ".join(words[start:end])
        beat = target_beats[i] if i < len(target_beats) else "proof"
        defaults = _scene_defaults_for_beat(beat, catalog)
        
        new_scenes.append(Scene(
            id=i + 1,
            texto_narracao=_normalize_scene_text(chunk),
            prompt_visual=None,
            duracao_estimada=target_sec_per_scene,
            tipo_transicao='cut',
            template=defaults.get('template') or templates[i % len(templates)],
            avatar_pose=defaults.get('avatar_pose') or poses[i % len(poses)],
            props=list(defaults.get('props') or ([props_list[i % len(props_list)]] if props_list else [])),
            motion=defaults.get('motion'),
            beat=beat,
            caption_line=_derive_caption_line(chunk),
            pose_family=defaults.get('pose_family') or beat,
            prop_family=defaults.get('prop_family') or beat,
        ))
    
    return new_scenes


def validate_audio_quality(audio_path: str, min_duration_sec: float = 30.0) -> tuple[bool, str]:
    """
    Task 2: Validador de qualidade de áudio pré-render.
    
    Verificações:
    1. Arquivo existe e tem tamanho mínimo (não está vazio)
    2. Duração mínima (áudio não pode ser muito curto)
    3. Detecção de silêncio/chiado (percentual de áudio muito baixo)
    
    Retorna:
        (is_valid, error_message)
    """
    # 1. Verificar se arquivo existe
    if not os.path.exists(audio_path):
        return (False, "Áudio não encontrado")
    
    # 2. Verificar tamanho mínimo do arquivo (mínimo 5KB para áudio real)
    file_size = os.path.getsize(audio_path)
    if file_size < 5000:
        return (False, f"Áudio muito pequeno ({file_size} bytes) - possivelmente vazio")
    
    try:
        # Carregar áudio com moviepy
        audio_clip = AudioFileClip(audio_path)
        duration = audio_clip.duration
        sample_rate = audio_clip.fps
        
        # 3. Verificar duração mínima
        if duration < min_duration_sec:
            audio_clip.close()
            return (False, f"Áudio muito curto: {duration:.1f}s < {min_duration_sec}s")
        
        # 4. Detecção de silêncio/chiado
        # Usar pydub para análise de frames se disponível, senãomoviepy
        try:
            from pydub import AudioSegment
            audio_seg = AudioSegment.from_file(audio_path)
            
            # Calcular percentagem de frames silenciosos (abaixo de -50dB)
            silent_frames = sum(1 for frame in audio_seg if frame.dBFS < -50)
            total_frames = len(audio_seg)
            silent_percent = (silent_frames / total_frames * 100) if total_frames > 0 else 0
            
            if silent_percent > 70:
                audio_clip.close()
                return (False, f"Áudio com muito silêncio: {silent_percent:.0f}%")
            
            # 5. Detectar volume anormal (muito baixo ou muito alto)
            max_dBFS = max(frame.dBFS for frame in audio_seg)
            min_dBFS = min(frame.dBFS for frame in audio_seg)
            
            if max_dBFS < -30:
                audio_clip.close()
                return (False, f"Áudio com volume muito baixo: {max_dBFS:.1f} dB")
            
            if min_dBFS > -10:
                # Isso pode indicar distorção ou áudio saturado
                print(f"  ⚠️ Atenção: áudio com volume muito alto ({min_dBFS:.1f} dB)")
            
            print(f"  ✅ Áudio validado: {duration:.1f}s, {silent_percent:.1f}% silêncio, {max_dBFS:.1f}dB max")
            
        except ImportError:
            # Sem pydub, fazer validação básica com moviepy
            print(f"  ⚠️ pydub não disponível, usando validação básica")
            if duration < min_duration_sec:
                audio_clip.close()
                return (False, f"Áudio muito curto: {duration:.1f}s < {min_duration_sec}s")
            print(f"  ✅ Áudio validado (básico): {duration:.1f}s")
        
        audio_clip.close()
        return (True, "")
        
    except Exception as e:
        return (False, f"Erro ao validar áudio: {str(e)}")


def _ffmpeg_run(args: list[str], timeout: int = 180) -> None:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise Exception((proc.stderr or proc.stdout or "ffmpeg error")[:1200])


def _ffmpeg_run_capture(args: list[str], timeout: int = 180) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise Exception((proc.stderr or proc.stdout or "ffmpeg error")[:1200])
    return f"{proc.stdout or ''}\n{proc.stderr or ''}"


def _voice_cleanup_filter(settings: dict) -> str:
    hum_cut = int(settings["voice_hum_cut_hz"])
    lowpass = int(settings["voice_lowpass_hz"])
    denoise_floor = float(settings["voice_denoise_floor_db"])
    compressor_threshold = float(settings["voice_compressor_threshold"])
    compressor_ratio = float(settings["voice_compressor_ratio"])
    return (
        f"highpass=f={hum_cut},"
        f"lowpass=f={lowpass},"
        f"afftdn=nf={denoise_floor}:tn=1,"
        f"acompressor=threshold={compressor_threshold}:ratio={compressor_ratio}:attack=18:release=180:makeup=1.4:knee=2.5,"
        "aresample=48000"
    )


def _measure_voice_loudnorm(audio_path: str, settings: dict) -> dict | None:
    target_lufs = settings["target_lufs"]
    target_lra = settings["target_lra"]
    filter_chain = (
        f"{_voice_cleanup_filter(settings)},"
        f"loudnorm=I={target_lufs}:TP=-2.0:LRA={target_lra}:print_format=json"
    )
    output = _ffmpeg_run_capture(
        [
            "ffmpeg",
            "-y",
            "-i",
            audio_path,
            "-vn",
            "-af",
            filter_chain,
            "-f",
            "null",
            "-",
        ],
        timeout=240,
    )
    match = re.search(r"\{\s*\"input_i\".*?\}", output, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _master_voice_audio(audio_path: str) -> str:
    output_path = os.path.join(TEMP_DIR, f"voice_master_{int(time.time()*1000)}.m4a")
    settings = get_audio_mix_settings()
    target_lufs = settings["target_lufs"]
    target_lra = settings["target_lra"]
    cleanup_filter = _voice_cleanup_filter(settings)
    measured = _measure_voice_loudnorm(audio_path, settings)
    if measured:
        loudnorm_filter = (
            f"loudnorm=I={target_lufs}:TP=-2.0:LRA={target_lra}:"
            f"measured_I={measured['input_i']}:"
            f"measured_LRA={measured['input_lra']}:"
            f"measured_TP={measured['input_tp']}:"
            f"measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}:linear=true:print_format=summary"
        )
    else:
        loudnorm_filter = f"loudnorm=I={target_lufs}:TP=-2.0:LRA={target_lra}"

    _ffmpeg_run(
        [
            "ffmpeg",
            "-y",
            "-i",
            audio_path,
            "-vn",
            "-af",
            f"{cleanup_filter},{loudnorm_filter},alimiter=limit=-2.0dB,volume=-1.2dB",
            "-c:a",
            "aac",
            "-b:a", "160k",
            "-ar", "48000",
            "-ac", "1",
            output_path,
        ],
        timeout=240,
    )
    return output_path


def _mix_background_music(voice_audio_path: str, total_duration: float, context_text: str = "") -> str:
    settings = get_audio_mix_settings()
    bg_path = _resolve_background_music_path(context_text)
    if not bg_path or not os.path.exists(bg_path):
        return voice_audio_path

    output_path = os.path.join(TEMP_DIR, f"mix_master_{int(time.time()*1000)}.m4a")
    gain_linear = round(10 ** (float(settings["background_music_gain_db"]) / 20.0), 4)
    threshold = settings["ducking_threshold"]
    ratio = settings["ducking_ratio"]
    fade_out_start = max(0.0, float(total_duration) - 2.0)
    filter_complex = (
        f"[1:a]atrim=0:{float(total_duration):.3f},"
        "aresample=48000,highpass=f=40,lowpass=f=7000,"
        "loudnorm=I=-30:TP=-2.0:LRA=9,"
        f"volume={gain_linear},"
        f"afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out_start:.3f}:d=2[bg];"
        f"[bg][0:a]sidechaincompress=threshold={threshold}:ratio={ratio}:attack=15:release=250[ducked];"
        f"[0:a][ducked]amix=inputs=2:weights=1 1:normalize=0[aout]"
    )
    _ffmpeg_run(
        [
            "ffmpeg",
            "-y",
            "-i",
            voice_audio_path,
            "-stream_loop",
            "-1",
            "-i",
            bg_path,
            "-filter_complex",
            filter_complex,
            "-map",
            "[aout]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            output_path,
        ],
        timeout=300,
    )
    return output_path


def _postprocess_audio_for_video(audio_path: str, total_duration: float, context_text: str = "") -> str:
    mastered = _master_voice_audio(audio_path)
    return _mix_background_music(mastered, total_duration, context_text=context_text)


async def service_generate_audio(scenes: List[Scene], voice_alias: str):
    """
    Gera áudio usando Gemini TTS (áudio natural) com fallback para Edge TTS.
    Modelo: gemini-2.5-flash-preview-tts
    
    FIX 2: Valida duração real do áudio após geração.
    """
    if not gemini_client:
        print("  ⚠️ Gemini client não disponível. Usando Edge TTS...")
        return await service_generate_audio_edge_fallback(scenes, voice_alias)
    
    print(f"[Orchestrator] Gerando áudio com Gemini TTS ({len(scenes)} cenas)...")
    
    full_script = _build_tts_script(scenes)
    
    if not full_script.strip():
        raise Exception("Nenhum texto de narração encontrado nas cenas.")
    
    print(f"  📝 Script: {len(full_script)} caracteres, {len(scenes)} cenas")
    
    # Vozes do Gemini TTS
    voice_map = {
        "voice_1": "Puck",
        "voice_2": "Kore",
        "Puck": "Puck",
        "Charon": "Charon",
        "Fenrir": "Fenrir",
        "Aoede": "Aoede",
        "Kore": "Kore",
        "Enceladus": "Enceladus",
        "Orus": "Orus",
        "Zephyr": "Zephyr",
        "Leda": "Leda",
        "Erinome": "Erinome",
        "Iapetus": "Iapetus",
        "Algenib": "Algenib",
        "Harpalyke": "Harpalyke",
        "Mneme": "Mneme"
    }
    target_voice = voice_map.get(voice_alias, "Puck")
    
    output_path = os.path.join(TEMP_DIR, f"audio_{int(time.time())}.wav")
    
    try:
        # Gerar áudio com Gemini TTS
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash-preview-tts",
            contents=full_script,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=target_voice,
                        )
                    ),
                ),
            )
        )
        
        # Extrair dados de áudio
        audio_data = response.candidates[0].content.parts[0].inline_data.data
        
        # Salvar como WAV (24kHz, mono, 16-bit)
        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(audio_data)
        
        file_size = os.path.getsize(output_path)
        print(f"  ✅ Áudio Gemini TTS gerado: {target_voice} ({file_size / 1024:.1f} KB)")
        
        # FIX 2: Validar duração do áudio gerado (comentado - agora usa validate_audio_quality)
        # is_valid, actual_duration = _ensure_minimum_audio_duration(output_path, min_sec=480.0)
        # if not is_valid:
        #     print(f"  ⚠️ Áudio muito curto ({actual_duration:.1f}s). Considere adicionar mais conteúdo nas cenas.")
        
        return output_path
        
    except Exception as e:
        print(f"  ⚠️ Erro Gemini TTS: {e}")
        print("  ⚠️ Usando Edge TTS como fallback...")
        return await service_generate_audio_edge_fallback(scenes, voice_alias)


async def service_generate_audio_edge_fallback(scenes: List[Scene], voice_alias: str):
    """Fallback: Gera áudio usando Edge TTS."""
    full_script = _build_tts_script(scenes)
    
    voice_map = {
        # Preferências Edge (PT-BR)
        "Antonio": "pt-BR-AntonioNeural",
        "Fabio": "pt-BR-FabioNeural",
        "Francisca": "pt-BR-FranciscaNeural",
        "Thalita": "pt-BR-ThalitaNeural",

        # Aliases herdados do Gemini (mantém compat com UI antiga)
        "Puck": "pt-BR-AntonioNeural",
        "Charon": "pt-BR-FabioNeural",
        "Kore": "pt-BR-ThalitaNeural",
        "Fenrir": "pt-BR-AntonioNeural",
        "Aoede": "pt-BR-FranciscaNeural",
        "Enceladus": "pt-BR-AntonioNeural",
        "Orus": "pt-BR-FabioNeural",
        "Zephyr": "pt-BR-FranciscaNeural",
        "Leda": "pt-BR-ThalitaNeural",
        "Erinome": "pt-BR-ThalitaNeural",
        "Iapetus": "pt-BR-AntonioNeural",
        "Algenib": "pt-BR-FabioNeural",
        "Harpalyke": "pt-BR-FranciscaNeural",
        "Mneme": "pt-BR-ThalitaNeural"
    }
    target_voice = voice_map.get(voice_alias, "pt-BR-AntonioNeural")
    
    output_path = os.path.join(TEMP_DIR, f"audio_{int(time.time())}.mp3")
    
    communicate = edge_tts.Communicate(full_script, target_voice)
    await communicate.save(output_path)
    
    file_size = os.path.getsize(output_path)
    print(f"  ✅ Áudio Edge TTS gerado: {target_voice} ({file_size / 1024:.1f} KB)")
    
    # FIX 2: Validar duração do áudio gerado (comentado - agora usa validate_audio_quality)
    # is_valid, actual_duration = _ensure_minimum_audio_duration(output_path, min_sec=480.0)
    # if not is_valid:
    #     print(f"  ⚠️ Áudio muito curto ({actual_duration:.1f}s). Considere adicionar mais conteúdo nas cenas.")
    
    return output_path

# Função legada removida - agora temos Gemini TTS + Edge TTS fallback

def apply_zoom_effect(clip, zoom_type: str, duration: float):
    """
    Aplica efeito de zoom (Ken Burns) ao clip.
    """
    return clip

def resize_to_fill(clip, target_width, target_height):
    """Redimensiona o clip para preencher a tela (crop) mantendo aspect ratio"""
    w, h = clip.size
    ratio_clip = w / h
    ratio_target = target_width / target_height
    
    if ratio_clip > ratio_target:
        # Imagem mais larga que o alvo: ajustar pela altura e cortar laterais
        new_height = target_height
        new_width = int(new_height * ratio_clip)
        clip = clip.resize(height=new_height)
        # Centralizar (crop automático no CompositeVideoClip ou manual)
        # Mas para garantir, vamos fazer crop manual centralizado
        x_center = new_width / 2
        clip = clip.crop(x1=x_center - target_width/2, width=target_width)
    else:
        # Imagem mais alta que o alvo (ou igual): ajustar pela largura e cortar topo/baixo
        new_width = target_width
        new_height = int(new_width / ratio_clip)
        clip = clip.resize(width=new_width)
        y_center = new_height / 2
        clip = clip.crop(y1=y_center - target_height/2, height=target_height)
        
    return clip

def _asset_path_avatar(avatar_pose: str) -> str:
    # Preferir pack processado (png com alpha)
    return os.path.join(
        BASE_DIR,
        "assets",
        "avatars",
        "marcos_avatar",
        "whisk_pack_v1_png",
        f"{avatar_pose}.png",
    )


def _asset_path_prop(prop_name: str) -> str:
    return os.path.join(
        BASE_DIR,
        "assets",
        "props",
        "whisk_pack_v1_png",
        f"{prop_name}.png",
    )


def _load_rgba(path: str) -> Image.Image:
    im = Image.open(path)
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    return im


def _resample_lanczos():
    # Pillow 10+: Image.Resampling.LANCZOS
    try:
        return Image.Resampling.LANCZOS
    except Exception:
        return Image.LANCZOS


def _parse_anchor(anchor) -> tuple[str, str]:
    '''Normaliza anchor em (ax, ay).

    Aceita:
    - 'left top' | 'center bottom' | 'right center'
    - ('left','top')
    '''
    if isinstance(anchor, (tuple, list)) and len(anchor) == 2:
        ax, ay = anchor
    else:
        s = str(anchor or 'center center').strip().lower().replace('-', ' ')
        parts = [p for p in s.split() if p]
        if len(parts) == 1:
            ax, ay = parts[0], 'center'
        else:
            ax, ay = parts[0], parts[1]

    ax = ax if ax in {'left', 'center', 'right'} else 'center'
    ay = ay if ay in {'top', 'center', 'bottom'} else 'center'
    return ax, ay


def _box_inset(box: tuple[int, int, int, int], dx: int, dy=None) -> tuple[int, int, int, int]:
    if dy is None:
        dy = dx
    x1, y1, x2, y2 = [int(v) for v in box]
    return (x1 + int(dx), y1 + int(dy), x2 - int(dx), y2 - int(dy))


def _bbox_area(b: tuple[int, int, int, int]) -> int:
    x1, y1, x2, y2 = b
    return max(0, x2 - x1) * max(0, y2 - y1)


def _bbox_intersection_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    x1 = max(ax1, bx1)
    y1 = max(ay1, by1)
    x2 = min(ax2, bx2)
    y2 = min(ay2, by2)
    return max(0, x2 - x1) * max(0, y2 - y1)


def _bbox_overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ia = _bbox_intersection_area(a, b)
    if ia <= 0:
        return 0.0
    denom = min(_bbox_area(a), _bbox_area(b))
    return (ia / denom) if denom > 0 else 0.0


def _predict_fit_bbox(
    dst_size: tuple[int, int],
    src_size: tuple[int, int],
    box: tuple[int, int, int, int],
    anchor='center center',
    max_upscale: float = 1.0,
    allow_downscale: bool = True,
    safe_margin: int = 6,
) -> tuple[int, int, int, int]:
    '''Calcula bbox final (sem desenhar).'''
    W, H = dst_size
    x1, y1, x2, y2 = [int(v) for v in box]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(W, x2), min(H, y2)

    m = max(0, int(safe_margin))
    x1m, y1m, x2m, y2m = x1 + m, y1 + m, x2 - m, y2 - m
    if x2m <= x1m:
        x1m, x2m = x1, x2
    if y2m <= y1m:
        y1m, y2m = y1, y2

    box_w = max(1, x2m - x1m)
    box_h = max(1, y2m - y1m)
    sw, sh = src_size
    sw = max(1, int(sw))
    sh = max(1, int(sh))

    fit_scale = min(box_w / sw, box_h / sh)
    scale = min(float(max_upscale), fit_scale)
    if not allow_downscale:
        scale = max(1.0, scale)

    rw = max(1, int(sw * scale))
    rh = max(1, int(sh * scale))
    if allow_downscale:
        rw = min(rw, box_w)
        rh = min(rh, box_h)

    ax, ay = _parse_anchor(anchor)
    if ax == 'left':
        px = x1m
    elif ax == 'right':
        px = x2m - rw
    else:
        px = int(x1m + (box_w - rw) / 2)

    if ay == 'top':
        py = y1m
    elif ay == 'bottom':
        py = y2m - rh
    else:
        py = int(y1m + (box_h - rh) / 2)

    px = max(0, min(int(px), W - rw))
    py = max(0, min(int(py), H - rh))

    return (px, py, px + rw, py + rh)


def paste_fit(
    dst_img: Image.Image,
    src_img: Image.Image,
    box: tuple[int, int, int, int],
    anchor='center center',
    max_upscale: float = 1.0,
    allow_downscale: bool = True,
):
    '''Cola src dentro de box preservando aspect ratio.

    - Fit-to-box + safe margins.
    - LANCZOS.
    - clamp para não estourar o box.
    - não permite upscale acima de max_upscale.

    Retorna bbox final (x1,y1,x2,y2).
    '''
    W, H = dst_img.size
    x1, y1, x2, y2 = [int(v) for v in box]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(W, x2), min(H, y2)

    SAFE_MARGIN = 6
    m = max(0, int(SAFE_MARGIN))
    x1m, y1m, x2m, y2m = x1 + m, y1 + m, x2 - m, y2 - m
    if x2m <= x1m:
        x1m, x2m = x1, x2
    if y2m <= y1m:
        y1m, y2m = y1, y2

    box_w = max(1, x2m - x1m)
    box_h = max(1, y2m - y1m)

    sw, sh = src_img.size
    sw = max(1, int(sw))
    sh = max(1, int(sh))

    fit_scale = min(box_w / sw, box_h / sh)
    scale = min(float(max_upscale), fit_scale)
    if not allow_downscale:
        scale = max(1.0, scale)

    rw = max(1, int(sw * scale))
    rh = max(1, int(sh * scale))

    resample = _resample_lanczos()
    resized = src_img.resize((rw, rh), resample=resample) if (rw, rh) != src_img.size else src_img

    # clamp final size to box (cropping se necessário)
    final_w = min(rw, box_w)
    final_h = min(rh, box_h)

    ax, ay = _parse_anchor(anchor)

    crop_x = 0
    if rw > final_w:
        if ax == 'left':
            crop_x = 0
        elif ax == 'right':
            crop_x = rw - final_w
        else:
            crop_x = int((rw - final_w) / 2)

    crop_y = 0
    if rh > final_h:
        if ay == 'top':
            crop_y = 0
        elif ay == 'bottom':
            crop_y = rh - final_h
        else:
            crop_y = int((rh - final_h) / 2)

    cropped = resized.crop((crop_x, crop_y, crop_x + final_w, crop_y + final_h)) if (crop_x or crop_y or final_w != rw or final_h != rh) else resized

    slack_x = box_w - final_w
    slack_y = box_h - final_h

    if ax == 'left':
        px = x1m
    elif ax == 'right':
        px = x1m + slack_x
    else:
        px = int(x1m + slack_x / 2)

    if ay == 'top':
        py = y1m
    elif ay == 'bottom':
        py = y1m + slack_y
    else:
        py = int(y1m + slack_y / 2)

    px = max(0, min(int(px), W - final_w))
    py = max(0, min(int(py), H - final_h))

    dst_img.alpha_composite(cropped, (px, py))
    return (px, py, px + final_w, py + final_h)


def validate_layout(
    placements: list[dict],
    canvas_size: tuple[int, int],
    clip_margin: int = 2,
    overlap_threshold: float = 0.20,
) -> list[str]:
    '''Validação mínima de layout (warning-only).'''
    W, H = canvas_size
    warnings: list[str] = []

    for it in placements:
        bbox = it.get('bbox')
        if not bbox:
            continue
        x1, y1, x2, y2 = bbox
        if x1 < -clip_margin or y1 < -clip_margin or x2 > W + clip_margin or y2 > H + clip_margin:
            warnings.append(f"[Layout] CLIP: {it.get('kind')}:{it.get('name')} bbox={bbox} canvas={(W,H)}")

    for i in range(len(placements)):
        a = placements[i]
        ab = a.get('bbox')
        if not ab:
            continue
        for j in range(i + 1, len(placements)):
            b = placements[j]
            bb = b.get('bbox')
            if not bb:
                continue
            ka = a.get('kind')
            kb = b.get('kind')
            # overlays são feitos para sobrepor, não entram no threshold
            if ka == 'overlay' or kb == 'overlay':
                continue
            r = _bbox_overlap_ratio(ab, bb)
            if r > overlap_threshold:
                warnings.append(
                    f"[Layout] OVERLAP>{overlap_threshold:.0%}: {ka}:{a.get('name')} x {kb}:{b.get('name')} r={r:.0%} a={ab} b={bb}"
                )

    for w in warnings:
        print(w)
    return warnings


def _paste_center(dst: Image.Image, src: Image.Image, center_xy: tuple[int, int], scale: float = 1.0):
    if scale != 1.0:
        w = max(1, int(src.size[0] * scale))
        h = max(1, int(src.size[1] * scale))
        src = src.resize((w, h), Image.LANCZOS)
    x = int(center_xy[0] - src.size[0] / 2)
    y = int(center_xy[1] - src.size[1] / 2)
    dst.alpha_composite(src, (x, y))


def _render_layer_scene_to_png(scene: Scene, out_path: str, size=(1280, 720)) -> str:
    """Renderiza 1 cena (layers) em um PNG (fundo branco) usando avatar + props.

    MVP: fit-to-box (evita recorte/pixelização), z-order por template,
    icons_with_red_x com red_x como overlay, e validação mínima.
    """
    W, H = size
    canvas = Image.new("RGBA", (W, H), (255, 255, 255, 255))

    template = (scene.template or "avatar_center").strip().lower()
    avatar_pose = (scene.avatar_pose or "neutral_arms_crossed").strip()
    props = scene.props or []

    # --- load assets ---
    avatar_path = _asset_path_avatar(avatar_pose)
    if not os.path.exists(avatar_path):
        raise Exception(f"Avatar pose não encontrada: {avatar_pose} ({avatar_path})")
    avatar = _load_rgba(avatar_path)

    def load_prop(name: str) -> Image.Image:
        prop_path = _asset_path_prop(name)
        if not os.path.exists(prop_path):
            raise Exception(f"Prop não encontrado: {name} ({prop_path})")
        return _load_rgba(prop_path)

    # --- template boxes ---
    def boxes_for(tpl: str):
        if tpl == 'avatar_center':
            # Avatar central/inferior (evita recorte: avatar costuma ser 768px de altura)
            return {
                'avatar': (int(W * 0.22), int(H * 0.03), int(W * 0.78), int(H * 0.99)),
                'prop_1': (int(W * 0.02), int(H * 0.35), int(W * 0.28), int(H * 0.82)),
                'prop_2': (int(W * 0.72), int(H * 0.10), int(W * 0.98), int(H * 0.38)),
            }
        if tpl == 'avatar_left_prop_right':
            return {
                'avatar': (int(W * 0.02), int(H * 0.03), int(W * 0.52), int(H * 0.99)),
                'prop_1': (int(W * 0.52), int(H * 0.18), int(W * 0.98), int(H * 0.92)),
                'prop_2': (int(W * 0.62), int(H * 0.05), int(W * 0.96), int(H * 0.30)),
            }
        if tpl == 'avatar_right_prop_left':
            return {
                'avatar': (int(W * 0.48), int(H * 0.03), int(W * 0.98), int(H * 0.99)),
                'prop_1': (int(W * 0.02), int(H * 0.18), int(W * 0.48), int(H * 0.92)),
                'prop_2': (int(W * 0.04), int(H * 0.05), int(W * 0.38), int(H * 0.30)),
            }
        if tpl == 'metaphor_single_prop':
            return {
                'prop_1': (int(W * 0.32), int(H * 0.10), int(W * 0.98), int(H * 0.92)),
                'avatar': (int(W * 0.02), int(H * 0.20), int(W * 0.38), int(H * 0.99)),
            }
        if tpl == 'icons_with_red_x':
            col = (int(W * 0.04), int(H * 0.18), int(W * 0.24), int(H * 0.86))
            x1, y1, x2, y2 = col
            col_h = y2 - y1
            gap = int(col_h * 0.04)
            cell_h = int((col_h - 2 * gap) / 3)
            icons = []
            for i in range(3):
                yy1 = y1 + i * (cell_h + gap)
                yy2 = yy1 + cell_h
                icons.append((x1, yy1, x2, yy2))
            return {
                'avatar': (int(W * 0.28), int(H * 0.03), int(W * 0.98), int(H * 0.99)),
                'icons': icons,
            }

        return {
            'avatar': (int(W * 0.35), int(H * 0.03), int(W * 0.98), int(H * 0.99)),
            'prop_1': (int(W * 0.05), int(H * 0.32), int(W * 0.38), int(H * 0.78)),
        }

    boxes = boxes_for(template)
    placements: list[dict] = []

    def paste_with_overlap_guard(name: str, kind: str, im: Image.Image, box, anchor, avoid=None):
        avoid = avoid or []
        max_tries = 7
        inset = 0
        for _ in range(max_tries):
            b = _box_inset(box, inset)
            if b[2] <= b[0] + 10 or b[3] <= b[1] + 10:
                break
            pred = _predict_fit_bbox((W, H), im.size, b, anchor=anchor, max_upscale=1.0, allow_downscale=True)
            worst = 0.0
            for ab in avoid:
                worst = max(worst, _bbox_overlap_ratio(pred, ab))
            if worst <= 0.20:
                box = b
                break
            inset += int(0.05 * min((box[2]-box[0]), (box[3]-box[1]))) or 8

        final_bbox = paste_fit(canvas, im, box, anchor=anchor, max_upscale=1.0, allow_downscale=True)
        placements.append({'name': name, 'kind': kind, 'bbox': final_bbox, 'box': box})
        return final_bbox

    # --- render by template + z-order ---
    if template == 'icons_with_red_x':
        avatar_bbox = paste_fit(canvas, avatar, boxes['avatar'], anchor='center bottom', max_upscale=1.0, allow_downscale=True)
        placements.append({'name': avatar_pose, 'kind': 'avatar', 'bbox': avatar_bbox, 'box': boxes['avatar']})

        has_red_x = any((p or '').strip().lower() == 'red_x' for p in props)
        icon_names = [p.strip() for p in props if isinstance(p, str) and p.strip() and p.strip().lower() != 'red_x']
        # garantir 2-3 ícones base (MVP)
        if len(icon_names) < 2:
            for fb in ['coin_stack', 'chart_up', 'calendar', 'piggy_bank', 'moneybag', 'warning_sign']:
                if fb in icon_names:
                    continue
                if os.path.exists(_asset_path_prop(fb)):
                    icon_names.append(fb)
                if len(icon_names) >= 2:
                    break
        icon_names = icon_names[:3]

        icon_bboxes = []
        for i, icon_name in enumerate(icon_names):
            try:
                icon_im = load_prop(icon_name)
            except Exception as e:
                print(f"[Layers] Icon prop inválido: {icon_name}: {e}")
                continue
            box = boxes['icons'][i] if i < len(boxes['icons']) else boxes['icons'][-1]
            bb = paste_fit(canvas, icon_im, box, anchor='center center', max_upscale=1.0, allow_downscale=True)
            icon_bboxes.append(bb)
            placements.append({'name': icon_name, 'kind': 'icon', 'bbox': bb, 'box': box})

        if has_red_x and icon_bboxes:
            redx = load_prop('red_x')
            for k, ib in enumerate(icon_bboxes):
                ov_box = _box_inset(ib, int(0.08 * min(ib[2]-ib[0], ib[3]-ib[1])))
                bb = paste_fit(canvas, redx, ov_box, anchor='center center', max_upscale=1.0, allow_downscale=True)
                placements.append({'name': f'red_x_{k}', 'kind': 'overlay', 'bbox': bb, 'box': ov_box})

    elif template == 'metaphor_single_prop':
        prop_names = [p for p in props if isinstance(p, str)][:2]
        if prop_names:
            p1 = load_prop(prop_names[0])
            p1_bbox = paste_fit(canvas, p1, boxes['prop_1'], anchor='center center', max_upscale=1.0, allow_downscale=True)
            placements.append({'name': prop_names[0], 'kind': 'prop', 'bbox': p1_bbox, 'box': boxes['prop_1']})

        avatar_bbox = paste_fit(canvas, avatar, boxes['avatar'], anchor='center bottom', max_upscale=1.0, allow_downscale=True)
        placements.append({'name': avatar_pose, 'kind': 'avatar', 'bbox': avatar_bbox, 'box': boxes['avatar']})

    else:
        if template not in {'avatar_center', 'avatar_left_prop_right', 'avatar_right_prop_left'}:
            template = 'avatar_center'
            boxes = boxes_for(template)

        avatar_bbox = paste_fit(canvas, avatar, boxes['avatar'], anchor='center bottom', max_upscale=1.0, allow_downscale=True)
        placements.append({'name': avatar_pose, 'kind': 'avatar', 'bbox': avatar_bbox, 'box': boxes['avatar']})

        prop_names = [p for p in props if isinstance(p, str)][:2]
        if prop_names:
            p1 = load_prop(prop_names[0])
            paste_with_overlap_guard(prop_names[0], 'prop', p1, boxes.get('prop_1', boxes['avatar']), 'center center', avoid=[avatar_bbox])
        if len(prop_names) > 1 and boxes.get('prop_2'):
            p2 = load_prop(prop_names[1])
            paste_with_overlap_guard(prop_names[1], 'prop', p2, boxes['prop_2'], 'center center', avoid=[avatar_bbox])

    validate_layout(placements, (W, H), clip_margin=2, overlap_threshold=0.20)

    canvas.convert("RGB").save(out_path, format="PNG", optimize=True)
    return out_path


async def service_generate_layer_images(scenes: List[Scene], render_cfg: dict | None = None):
    """Gera um PNG por cena usando o motor de layers (assets locais)."""
    print(f"[Orchestrator] Gerando {len(scenes)} cenas em modo layers (assets locais)...")
    render_cfg = render_cfg or get_render_settings()
    size = (render_cfg["width"], render_cfg["height"])
    out_paths = []
    for scene in scenes:
        out_path = os.path.join(TEMP_DIR, f"layer_scene_{int(time.time()*1000)}_{scene.id}.png")
        _render_layer_scene_to_png(scene, out_path, size=size)
        out_paths.append(out_path)
    return out_paths


def _clean_caption_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _caption_for_scene(scene: Scene, max_chars: int = 54) -> str:
    """CapCut-ish: 1 linha, curta, pegando o melhor pedaço da fala da cena."""
    raw = _clean_caption_text((scene.caption_line if scene else "") or (scene.get_narration if scene else ""))
    if not raw:
        return ""
    first = re.split(r"(?<=[\.!\?])\s+", raw, maxsplit=1)[0].strip()
    if len(first) > max_chars:
        first = first[: max_chars - 1].rstrip() + "…"
    return first


def _format_srt_timestamp(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def _build_scene_timeline(scenes: List[Scene], total_audio_duration: float) -> list[dict]:
    if not scenes:
        return []
    total_scene_duration = sum(scene.get_duration for scene in scenes) or float(len(scenes))
    scale_factor = total_audio_duration / total_scene_duration if total_scene_duration > 0 else 1.0
    timeline = []
    cursor = 0.0
    for idx, scene in enumerate(scenes, start=1):
        duration = max(0.5, float(scene.get_duration) * scale_factor)
        start = cursor
        end = min(total_audio_duration, start + duration)
        if idx == len(scenes):
            end = total_audio_duration
        timeline.append(
            {
                "index": idx,
                "start": max(0.0, start),
                "end": max(start + 0.2, end),
                "caption": _caption_for_scene(scene),
            }
        )
        cursor = end
    return timeline


def _write_srt(entries: list[dict], out_path: str) -> str:
    lines = []
    for entry in entries:
        caption = _clean_caption_text(entry.get("caption") or "")
        if not caption:
            continue
        lines.extend(
            [
                str(entry["index"]),
                f"{_format_srt_timestamp(entry['start'])} --> {_format_srt_timestamp(entry['end'])}",
                caption,
                "",
            ]
        )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    return out_path


def _render_caption_png(text: str, out_path: str, size=(1280, 720)) -> str:
    """Renderiza uma legenda 1-linha em PNG (sem ImageMagick)."""
    W, H = size
    text = _clean_caption_text(text)
    if not text:
        # cria PNG transparente vazio
        Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(out_path, format="PNG")
        return out_path

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Fonte padrão do sistema (Ubuntu)
    font = None
    for fp in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        try:
            if os.path.exists(fp):
                font = ImageFont.truetype(fp, 54)
                break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    # garante 1 linha (sem wrap). se estourar, corta.
    max_chars = 54
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"

    # posição: centro inferior com padding
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=6)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = int((W - tw) / 2)
    y = max(0, int(H * 0.87 - (th / 2)))

    # stroke (preto) + fill (branco)
    draw.text(
        (x, y),
        text,
        font=font,
        fill=(255, 255, 255, 255),
        stroke_width=8,
        stroke_fill=(0, 0, 0, 220),
    )

    img.save(out_path, format="PNG", optimize=True)
    return out_path


async def service_render_video(
    image_paths: List[str],
    audio_path: str,
    scenes: List[Scene],
    static_base_url: str | None = None,
    render_cfg: dict | None = None,
):
    """
    Compõe imagens e áudio usando MoviePy com suporte a:
    - Transições (zoom_in, zoom_out, crossfade, cut)
    - Duração sincronizada por cena
    """
    print("[Orchestrator] Renderizando vídeo com MoviePy...")
    
    if not audio_path or not os.path.exists(audio_path):
        raise Exception("Arquivo de áudio não encontrado.")

    render_cfg = render_cfg or get_render_settings()
    width = render_cfg["width"]
    height = render_cfg["height"]

    # Carregar áudio para saber a duração total
    source_audio_clip = AudioFileClip(audio_path)
    total_audio_duration = source_audio_clip.duration
    source_audio_clip.close()

    final_audio_path = _postprocess_audio_for_video(
        audio_path,
        total_audio_duration,
        context_text=_build_audio_context(scenes),
    )
    audio_clip = AudioFileClip(final_audio_path)
    total_audio_duration = audio_clip.duration
    
    # Filtrar imagens válidas e parear com cenas
    valid_pairs = []
    for i, img_path in enumerate(image_paths):
        if img_path and os.path.exists(img_path):
            scene = scenes[i] if i < len(scenes) else None
            valid_pairs.append((img_path, scene))
    
    if not valid_pairs:
        raise Exception("Nenhuma imagem válida gerada.")
    
    paired_scenes = [scene for _, scene in valid_pairs if scene]
    timeline = _build_scene_timeline(paired_scenes, total_audio_duration)
    
    clips = []
    current_time = 0
    
    for i, (img_path, scene) in enumerate(valid_pairs):
        # Duração da cena sincronizada com áudio
        if scene and i < len(timeline):
            scene_duration = timeline[i]["end"] - timeline[i]["start"]
        else:
            scene_duration = total_audio_duration / len(valid_pairs)
        
        # Criar clip base
        clip = ImageClip(img_path).set_duration(scene_duration)
        
        # FIX: Usar resize_to_fill em vez de apenas resize height
        # Isso garante que imagens quadradas/retangulares preencham 16:9 sem barras pretas
        try:
            clip = resize_to_fill(clip, width, height)
        except Exception as e:
            print(f"Erro no resize_to_fill: {e}, usando resize padrão")
            clip = clip.resize(height=height)
            
        clip = clip.set_position("center")
        
        # Aplicar apenas transições estáveis para evitar tremido.
        transition = None
        if scene:
            transition = scene.get_transition

        if transition in ["zoom_in", "zoom_out"]:
            transition = "cut"

        if transition == "crossfade":
            # Crossfade será aplicado na concatenação
            clip = clip.crossfadein(0.5) if i > 0 else clip
            if scene:
                print(f"  🎬 Cena {scene.id}: crossfade ({scene_duration:.1f}s)")
        else:
            if scene:
                print(f"  🎬 Cena {scene.id}: cut ({scene_duration:.1f}s)")

        # Legenda 1 linha (CapCut-ish)
        try:
            caption = _caption_for_scene(scene) if scene else ""
            if caption:
                cap_path = os.path.join(TEMP_DIR, f"caption_{int(time.time()*1000)}_{i}.png")
                _render_caption_png(caption, cap_path, size=(width, height))
                cap_clip = ImageClip(cap_path).set_duration(scene_duration).set_position((0, 0))
                clip = CompositeVideoClip([clip, cap_clip])
        except Exception as e:
            print(f"  ⚠️ Legenda falhou na cena {i+1}: {e}")

        clips.append(clip)
        current_time += scene_duration
    
    # Concatenar com método compose para suportar crossfades
    final_video = concatenate_videoclips(clips, method="compose")
    final_video = final_video.set_audio(audio_clip)

    # FIX: libx264 + yuv420p exige dimensões pares (divisíveis por 2)
    # Garante que width e height são pares para evitar "height not divisible by 2"
    fw, fh = final_video.size
    new_fw = fw if fw % 2 == 0 else fw - 1
    new_fh = fh if fh % 2 == 0 else fh - 1
    if new_fw != fw or new_fh != fh:
        print(f"[Orchestrator] FIX: ajustando dimensões {fw}x{fh} → {new_fw}x{new_fh} (par p/ libx264)")
        final_video = final_video.resize((new_fw, new_fh))

    output_filename = f"vsl_final_{int(time.time())}.mp4"
    output_path = os.path.join(STATIC_DIR, output_filename)
    subtitles_filename = output_filename.rsplit(".", 1)[0] + ".srt"
    subtitles_path = os.path.join(STATIC_DIR, subtitles_filename)
    
    print(f"[Orchestrator] Renderizando {len(clips)} cenas ({total_audio_duration:.1f}s total)...")
    
    # Renderizar (qualidade ok sem ficar "ultrafast")
    final_video.write_videofile(
        output_path,
        fps=render_cfg["fps"],
        codec=render_cfg["codec"],
        audio_codec=render_cfg["audio_codec"],
        temp_audiofile=os.path.join(TEMP_DIR, "temp-audio.m4a"),
        remove_temp=True,
        logger=None,
        preset=render_cfg["preset"],
        ffmpeg_params=["-crf", str(render_cfg["crf"]), "-pix_fmt", "yuv420p"],
        threads=render_cfg["threads"],
    )
    
    print(f"  ✅ Vídeo renderizado: {output_filename}")
    base_url = (static_base_url or get_static_base_url()).rstrip("/") + "/"
    if timeline:
        _write_srt(timeline, subtitles_path)
        print(f"  ✅ Legenda SRT gerada: {subtitles_filename}")
    return {
        "video_url": f"{base_url}{output_filename}",
        "subtitles_url": f"{base_url}{subtitles_filename}" if os.path.exists(subtitles_path) else None,
        "subtitles_path": subtitles_path if os.path.exists(subtitles_path) else None,
    }

@app.get("/config")
async def get_public_config():
    """Retorna configurações públicas (preços, flags)"""
    return {
        "pricing": get_config("pricing", {}),
        "app": get_config("app", {})
    }

# --- Endpoint Principal ---

def _new_run_id() -> str:
    return uuid.uuid4().hex[:10]


def _persist_run_artifacts(run_id: str, payload: dict, plan: dict, response: dict):
    run_dir = os.path.join(RUNS_DIR, f"run_{run_id}")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "request.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "scene_plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(response, f, ensure_ascii=False, indent=2)
    return run_dir


def _job_to_status_response(job: dict) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=job["id"],
        job_type=job["job_type"],
        route_name=job["route_name"],
        queue_status=job["status"],
        stage=job["stage"],
        attempts=int(job.get("attempts") or 0),
        max_attempts=int(job.get("max_attempts") or 1),
        worker_id=job.get("worker_id"),
        error=job.get("error_text"),
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
        progress=job.get("progress") or {},
        result=job.get("result_payload"),
    )


def _parse_iso_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _seconds_between(start_value: str | None, end_value: str | None) -> float | None:
    start_dt = _parse_iso_dt(start_value)
    end_dt = _parse_iso_dt(end_value)
    if not start_dt or not end_dt:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def _dir_stats(path: str) -> dict:
    total_bytes = 0
    file_count = 0
    if os.path.exists(path):
        for root, _, files in os.walk(path):
            for filename in files:
                file_count += 1
                full_path = os.path.join(root, filename)
                try:
                    total_bytes += os.path.getsize(full_path)
                except OSError:
                    continue
    return {"path": path, "files": file_count, "bytes": total_bytes}


def _safe_unlink(path: str) -> bool:
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except OSError:
        return False
    return False


def _cleanup_old_files(
    directory: str,
    *,
    older_than_seconds: int,
    suffixes: tuple[str, ...] | None = None,
) -> dict:
    now = time.time()
    removed_files = 0
    removed_bytes = 0
    if older_than_seconds <= 0 or not os.path.exists(directory):
        return {"path": directory, "removed_files": 0, "removed_bytes": 0}

    for root, _, files in os.walk(directory):
        for filename in files:
            if suffixes and not filename.lower().endswith(suffixes):
                continue
            full_path = os.path.join(root, filename)
            try:
                age = now - os.path.getmtime(full_path)
                if age < older_than_seconds:
                    continue
                size = os.path.getsize(full_path)
            except OSError:
                continue
            if _safe_unlink(full_path):
                removed_files += 1
                removed_bytes += size
    return {"path": directory, "removed_files": removed_files, "removed_bytes": removed_bytes}


def _ops_cleanup() -> dict:
    temp_hours = int(os.getenv("YT_AUTOMATOR_TEMP_RETENTION_HOURS", "12"))
    runs_days = int(os.getenv("YT_AUTOMATOR_RUNS_RETENTION_DAYS", "14"))
    static_days = int(os.getenv("YT_AUTOMATOR_STATIC_RETENTION_DAYS", "0"))
    results = [
        _cleanup_old_files(
            TEMP_DIR,
            older_than_seconds=temp_hours * 3600,
            suffixes=(".png", ".mp3", ".wav", ".m4a", ".json"),
        ),
        _cleanup_old_files(
            RUNS_DIR,
            older_than_seconds=runs_days * 86400,
            suffixes=(".json",),
        ),
    ]
    if static_days > 0:
        results.append(
            _cleanup_old_files(
                STATIC_DIR,
                older_than_seconds=static_days * 86400,
                suffixes=(".mp4", ".srt"),
            )
        )
    return {
        "cleanup": results,
        "after": {
            "temp": _dir_stats(TEMP_DIR),
            "static": _dir_stats(STATIC_DIR),
            "runs": _dir_stats(RUNS_DIR),
        },
    }


def _ops_dashboard(limit: int = 100) -> dict:
    recent_jobs = list_jobs(limit=max(1, min(limit, 500)))
    completed_jobs = [job for job in recent_jobs if job.get("status") == "completed"]
    failed_jobs = [job for job in recent_jobs if job.get("status") == "failed"]
    job_durations = [
        seconds
        for job in completed_jobs
        if (seconds := _seconds_between(job.get("started_at"), job.get("finished_at"))) is not None
    ]
    failure_buckets: dict[str, int] = {}
    for job in failed_jobs[:25]:
        error = (job.get("error_text") or "unknown").strip().splitlines()[0][:120]
        failure_buckets[error] = failure_buckets.get(error, 0) + 1
    recent_view = []
    for job in recent_jobs[:10]:
        recent_view.append(
            {
                "job_id": job["id"],
                "route_name": job["route_name"],
                "status": job["status"],
                "stage": job["stage"],
                "created_at": job["created_at"],
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "elapsed_seconds": _seconds_between(job.get("started_at"), job.get("finished_at")),
                "error": (job.get("error_text") or "")[:160] or None,
            }
        )
    return {
        "queue": queue_stats(),
        "storage": {
            "temp": _dir_stats(TEMP_DIR),
            "static": _dir_stats(STATIC_DIR),
            "runs": _dir_stats(RUNS_DIR),
        },
        "recent_jobs": recent_view,
        "performance": {
            "sample_size": len(completed_jobs),
            "completed_elapsed_min": round(min(job_durations), 2) if job_durations else None,
            "completed_elapsed_avg": round(sum(job_durations) / len(job_durations), 2) if job_durations else None,
            "completed_elapsed_max": round(max(job_durations), 2) if job_durations else None,
        },
        "failures": {
            "sample_size": len(failed_jobs),
            "top_errors": sorted(
                ({"error": error, "count": count} for error, count in failure_buckets.items()),
                key=lambda item: item["count"],
                reverse=True,
            )[:10],
        },
    }


def _video_path_from_url(video_url: str) -> str:
    filename = (video_url or "").split("/static/")[-1].strip()
    return os.path.join(STATIC_DIR, filename)


def _probe_duration_seconds(path: str) -> float:
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            return 0.0
        return float((proc.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _probe_video_stream_info(path: str) -> dict:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            return {"width": 0, "height": 0}
        data = json.loads((proc.stdout or "").strip() or "{}")
        stream = ((data.get("streams") or [{}])[0]) if isinstance(data, dict) else {}
        return {
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
        }
    except Exception:
        return {"width": 0, "height": 0}


def _validate_video_quality(video_url: str, min_seconds: float = 30.0, min_bytes: int = 150_000, min_width: int = 1280, min_height: int = 720):
    video_path = _video_path_from_url(video_url)
    if not os.path.exists(video_path):
        raise Exception("Quality gate: arquivo de vídeo não encontrado")
    size = os.path.getsize(video_path)
    if size < min_bytes:
        raise Exception(f"Quality gate: arquivo muito pequeno ({size} bytes)")
    duration = _probe_duration_seconds(video_path)
    if duration < min_seconds:
        raise Exception(f"Quality gate: duração muito curta ({duration:.1f}s < {min_seconds:.1f}s)")
    stream_info = _probe_video_stream_info(video_path)
    if stream_info["width"] < min_width or stream_info["height"] < min_height:
        raise Exception(
            f"Quality gate: resolução abaixo do mínimo ({stream_info['width']}x{stream_info['height']} < {min_width}x{min_height})"
        )
    return {
        "video_path": video_path,
        "bytes": size,
        "duration": round(duration, 2),
        "width": stream_info["width"],
        "height": stream_info["height"],
    }


def _send_telegram_alert(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4096]},
            timeout=10,
        )
    except Exception as e:
        print(f"[Alert] Telegram falhou: {e}")


def _notify_mission_control(
    title: str,
    brief: str,
    run_id: str,
    event: str = "video_generated",
    status: str = "completed",
    video_url: str = "",
    quality_status: str = "unknown",
):
    payload = {
        "event": event,
        "status": status,
        "video_url": video_url,
        "title": title,
        "brief": brief,
        "run_id": run_id,
        "quality_status": quality_status,
    }
    raw_endpoints = os.getenv("MISSION_CONTROL_ENDPOINTS", "").strip()
    legacy_endpoint = os.getenv("MISSION_CONTROL_NOTIFY_URL", "").strip()
    allow_local_fallback = os.getenv("MISSION_CONTROL_ALLOW_LOCAL_FALLBACK", "true").strip().lower() not in {"0", "false", "no"}

    if raw_endpoints:
        endpoints = [item.strip() for item in raw_endpoints.split(",") if item.strip()]
    elif legacy_endpoint:
        endpoints = [legacy_endpoint]
        if allow_local_fallback:
            endpoints.append("http://localhost:3000/api/video-event")
    else:
        endpoints = [
            "http://100.99.151.85:3000/api/video-event",
            "http://localhost:3000/api/video-event",
        ]
    last_error = None
    for idx, endpoint in enumerate(endpoints):
        try:
            resp = requests.post(endpoint, json=payload, timeout=5)
            if resp.status_code < 400:
                print(f"[Run {run_id}] Mission Control notify ok via {endpoint} ({resp.status_code})")
                return
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            print(f"[Run {run_id}] Mission Control notify failed via {endpoint}: {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"[Run {run_id}] Mission Control notify exception via {endpoint}: {e}")
        if idx == 0:
            print(f"[Run {run_id}] Tentando fallback Mission Control em localhost...")
    if last_error:
        print(f"[Run {run_id}] Mission Control notify failed after fallback: {last_error}")


async def _run_auto_generate(
    payload: AutoGenerateRequest,
    request: Request | None,
    route_name: str = "/auto-generate",
    stage_callback: Optional[Callable[[str, dict[str, Any]], None]] = None,
):
    # 1-click flow: brief -> Nick BR plan (JSON) -> render via internal pipeline (layers + Antonio)
    run_id = _new_run_id()
    started = time.time()
    deadline = started + AUTO_GENERATE_MAX_SECONDS
    print(f"[Run {run_id}] {route_name} iniciado")

    def push_stage(stage: str, **progress_updates):
        if stage_callback:
            stage_callback(stage, progress_updates)

    try:
        brief = (payload.brief or payload.tema or '').strip()
        if len(brief) < 10:
            raise HTTPException(status_code=400, detail='brief/tema muito curto. Explique o tema, promessa e público-alvo (>=10 chars).')

        # Force constraints: no image-gen API calls
        mode = 'layers'
        voice_id = payload.voice_id or 'Antonio'
        test_mode = bool(payload.test_mode)
        validation_mode = _normalize_validation_mode(payload.validation_mode, test_mode=test_mode)
        target_duration_sec = payload.target_duration_sec
        if target_duration_sec is None:
            target_duration_sec = TEST_MODE_DEFAULT_DURATION_SEC if test_mode else 480.0
        if test_mode:
            target_duration_sec = max(TEST_MODE_MIN_DURATION_SEC, min(TEST_MODE_MAX_DURATION_SEC, float(target_duration_sec)))
        else:
            target_duration_sec = max(120.0, float(target_duration_sec))

        push_stage(
            "llm",
            route_name=route_name,
            test_mode=test_mode,
            validation_mode=validation_mode,
            target_duration_sec=target_duration_sec,
            llm_ok=False,
            script_ok=False,
            audio_ok=False,
            render_ok=False,
        )

        plan = await _wait_with_deadline(
            asyncio.to_thread(
                _llm_generate_scene_plan,
                brief,
                test_mode,
                target_duration_sec,
                validation_mode,
            ),
            operation="Geração de roteiro LLM",
            deadline=deadline,
            cap_seconds=(LLM_CALL_TIMEOUT_SECONDS * (LLM_MAX_RETRIES + 1)) + 10,
        )

        scenes_objs = [Scene(**sc) for sc in plan.get('scenes', [])]
        if not scenes_objs:
            raise Exception("Plano de cenas vazio após LLM.")

        push_stage(
            "script_ready",
            llm_ok=True,
            script_ok=True,
            scene_count=len(scenes_objs),
            title=plan.get("title"),
        )

        # Pré-check de áudio para erro precoce sem iniciar render pesado.
        push_stage("audio", llm_ok=True, script_ok=True, audio_ok=False, scene_count=len(scenes_objs))
        test_audio = await _wait_with_deadline(
            service_generate_audio(scenes_objs, voice_id),
            operation="Pré-check de áudio",
            deadline=deadline,
            cap_seconds=300,
        )
        is_valid, actual_duration = _ensure_minimum_audio_duration(test_audio, min_sec=target_duration_sec)

        # Reescalar cenas baseado na duração real do áudio: roteiro/5 = cenas
        if is_valid and actual_duration > (20.0 if test_mode else 60.0):
            scenes_objs = _resplit_scenes_by_audio_duration(scenes_objs, actual_duration, target_sec_per_scene=5.0)
            # Atualizar o script no plan com a concatenação das novas cenas
            plan['scenes'] = [s.model_dump() for s in scenes_objs]
            plan['script'] = " ".join(s.get_narration or "" for s in scenes_objs)

        push_stage(
            "render",
            llm_ok=True,
            script_ok=True,
            audio_ok=is_valid,
            audio_duration_sec=round(actual_duration, 1),
            scene_count=len(scenes_objs),
            render_ok=False,
        )

        video_req = VideoGenerationRequest(
            script=plan.get('script', ''),
            scenes=scenes_objs,
            voice_id=voice_id,
            narration_style='Normal',
            reference_image_b64=None,
            mode=mode,
            title=plan.get('title', ''),
            brief=brief,
        )

        video_resp = await _wait_with_deadline(
            generate_video(video_req, request=request),
            operation="Renderização de vídeo",
            deadline=deadline,
            cap_seconds=520,
        )
        if video_resp.status != 'completed':
            raise Exception(video_resp.message)

        message = 'Auto-generate concluído.'
        if not is_valid:
            label = f"{int(target_duration_sec)}s" if test_mode else "8min"
            message = f'Auto-generate concluído com duração de {actual_duration:.1f}s (abaixo do alvo de {label}).'
        elif test_mode:
            message = f'Auto-generate TEST MODE concluído com duração de {actual_duration:.1f}s.'

        elapsed = time.time() - started
        print(f"[Run {run_id}] {route_name} concluído em {elapsed:.1f}s")
        response_payload = {
            "status": "completed",
            "title": plan['title'],
            "description": plan['description'],
            "scene_plan": plan,
            "video_url": video_resp.video_url,
            "subtitles_url": video_resp.subtitles_url,
            "message": f"{message} | run_id={run_id}",
            "job_id": None,
            "elapsed_seconds": round(elapsed, 2),
        }
        run_dir = _persist_run_artifacts(
            run_id,
            {
                "brief": payload.brief,
                "tema": payload.tema,
                "voice_id": voice_id,
                "mode": mode,
                "test_mode": test_mode,
                "target_duration_sec": target_duration_sec,
                "validation_mode": validation_mode,
                "render_profile": render_profile,
                "render_settings": render_cfg,
            },
            plan,
            response_payload,
        )
        response_payload["run_dir"] = run_dir
        response_payload["message"] += f" | run_dir={run_dir}"
        push_stage(
            "completed",
            llm_ok=True,
            script_ok=True,
            audio_ok=is_valid,
            render_ok=True,
            run_id=run_id,
            run_dir=run_dir,
            render_profile=render_profile,
            video_url=video_resp.video_url,
            subtitles_url=video_resp.subtitles_url,
        )
        _send_telegram_alert(
            f"✅ yt-automator concluído\nrun_id={run_id}\nvideo={response_payload.get('video_url')}"
        )
        return AutoGenerateResponse(**response_payload)

    except HTTPException as e:
        raise e
    except TimeoutError as e:
        elapsed = time.time() - started
        print(f"[Run {run_id}] {route_name} timeout após {elapsed:.1f}s: {e}")
        push_stage("failed", error=str(e), timeout=True)
        _send_telegram_alert(f"❌ yt-automator timeout\nrun_id={run_id}\nerro={str(e)[:800]}")
        return AutoGenerateResponse(
            status='error',
            message=f"run_id={run_id} | timeout | {str(e)}",
            elapsed_seconds=round(elapsed, 2),
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        elapsed = time.time() - started
        print(f"[Run {run_id}] {route_name} erro após {elapsed:.1f}s: {e}")
        push_stage("failed", error=str(e))
        _send_telegram_alert(f"❌ yt-automator falhou\nrun_id={run_id}\nerro={str(e)[:800]}")
        return AutoGenerateResponse(
            status='error',
            message=f"run_id={run_id} | {str(e)}",
            elapsed_seconds=round(elapsed, 2),
        )


def _enqueue_auto_generate_job(payload: AutoGenerateRequest, route_name: str) -> JobSubmitResponse:
    brief = (payload.brief or payload.tema or "").strip()
    if len(brief) < 10:
        raise HTTPException(status_code=400, detail='brief/tema muito curto. Explique o tema, promessa e público-alvo (>=10 chars).')

    job = enqueue_job(
        "auto_generate",
        route_name,
        payload.model_dump(),
        max_attempts=1,
    )
    return JobSubmitResponse(
        status="accepted",
        job_id=job["id"],
        queue_status=job["status"],
        route_name=route_name,
        message=f"Job enfileirado. Consulte /jobs/{job['id']}",
    )


@app.post('/auto-generate', response_model=JobSubmitResponse)
async def auto_generate(payload: AutoGenerateRequest, request: Request):
    return _enqueue_auto_generate_job(payload, route_name="/auto-generate")


@app.post('/auto-generate/smoke', response_model=JobSubmitResponse)
async def auto_generate_smoke(payload: AutoGenerateRequest, request: Request):
    smoke_payload = payload.model_copy(
        update={
            "test_mode": True,
            "target_duration_sec": payload.target_duration_sec or TEST_MODE_DEFAULT_DURATION_SEC,
            "validation_mode": payload.validation_mode or "relaxed",
            "mode": "layers",
        }
    )
    return _enqueue_auto_generate_job(smoke_payload, route_name="/auto-generate/smoke")


@app.post('/auto-generate/smoke-batch', response_model=list[JobSubmitResponse])
async def auto_generate_smoke_batch(payload: SmokeBatchRequest, request: Request):
    count = max(1, min(int(payload.count), 20))
    smoke_payload = payload.model_copy(
        update={
            "test_mode": True,
            "target_duration_sec": payload.target_duration_sec or TEST_MODE_DEFAULT_DURATION_SEC,
            "validation_mode": payload.validation_mode or "relaxed",
            "mode": "layers",
        }
    )
    normalized_payload = AutoGenerateRequest(**smoke_payload.model_dump())
    responses = []
    for _ in range(count):
        responses.append(_enqueue_auto_generate_job(normalized_payload, route_name="/auto-generate/smoke-batch"))
    return responses


@app.get("/jobs", response_model=list[JobStatusResponse])
async def jobs_index(limit: int = 20):
    return [_job_to_status_response(job) for job in list_jobs(limit=limit)]


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_to_status_response(job)


@app.get("/jobs/{job_id}/result")
async def job_result(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "job_id": job["id"],
        "queue_status": job["status"],
        "result": job.get("result_payload"),
        "error": job.get("error_text"),
    }


@app.get("/ops/dashboard")
async def ops_dashboard(limit: int = 100):
    return _ops_dashboard(limit=limit)


@app.post("/ops/cleanup")
async def ops_cleanup():
    return _ops_cleanup()

@app.post("/generate-video", response_model=VideoResponse)
async def generate_video(payload: VideoGenerationRequest, request: Request):
    run_id = _new_run_id()
    started = time.time()
    print(f"[Run {run_id}] /generate-video iniciado")
    try:
        # 1. Processar cenas
        scenes_to_process = payload.scenes
        
        # 2. Determinar script: usar payload.script OU extrair das cenas
        script_to_use = payload.script
        
        if scenes_to_process and (not script_to_use or script_to_use.strip() == ""):
            # Extrair script concatenando todas as narrações das cenas
            narrations = [scene.get_narration for scene in scenes_to_process if scene.get_narration]
            script_to_use = " ".join(narrations)
            print(f"[Orchestrator] Script extraído de {len(narrations)} cenas ({len(script_to_use)} chars)")
        
        if not script_to_use:
            return VideoResponse(
                status="error",
                message="Nenhum script ou narração de cenas fornecido."
            )

        # Se o usuário já enviou um script completo, usar esse texto para narração
        # mesmo quando as cenas vierem sem texto_narracao/description.
        if scenes_to_process and script_to_use and script_to_use.strip():
            scenes_missing_narration = [s for s in scenes_to_process if not (s.get_narration or "").strip()]
            if scenes_missing_narration:
                words = script_to_use.strip().split()
                chunk_size = max(1, len(words) // len(scenes_to_process))
                chunks = []
                for idx in range(len(scenes_to_process)):
                    start = idx * chunk_size
                    end = len(words) if idx == len(scenes_to_process) - 1 else min(len(words), (idx + 1) * chunk_size)
                    chunk = " ".join(words[start:end]).strip()
                    chunks.append(chunk)

                fallback_chunk = chunks[-1] if chunks and chunks[-1] else script_to_use.strip()
                for idx, scene in enumerate(scenes_to_process):
                    if not (scene.get_narration or "").strip():
                        text = (chunks[idx] if idx < len(chunks) else "").strip() or fallback_chunk
                        scene.texto_narracao = text

                print(f"[Orchestrator] Script do payload distribuído em {len(scenes_to_process)} cenas (sem LLM).")

        # Cria/atualiza task no Mission Control quando a geração começa.
        _notify_mission_control(
            title=(payload.title or f"Video {run_id}").strip(),
            brief=(payload.brief or script_to_use[:500]).strip(),
            run_id=run_id,
            event="video_started",
            status="in_progress",
            video_url="",
            quality_status="pending",
        )

        print(f"--- Iniciando Processamento ({len(script_to_use)} chars, {len(scenes_to_process)} cenas) ---")
        
        # 3. Se não houver cenas, criar uma dummy
        if not scenes_to_process:
            scenes_to_process = [Scene(
                id=1, 
                description=script_to_use[:100], 
                visual_prompt=f"A cinematic representation of: {script_to_use[:50]}", 
                duration_est=10
            )]

        # 4. Processar imagem de referência (personagem fixo)
        reference_image_path = None
        if payload.reference_image_b64:
            try:
                ref_image_data = base64.b64decode(payload.reference_image_b64)
                reference_image_path = os.path.join(TEMP_DIR, f"reference_{int(time.time())}.png")
                with open(reference_image_path, "wb") as f:
                    f.write(ref_image_data)
                print(f"  📷 Imagem de referência salva: {reference_image_path}")
            except Exception as e:
                print(f"  ⚠️ Erro ao processar imagem de referência: {e}")
        
        render_profile = "test" if test_mode else "production"
        render_cfg = get_render_settings(render_profile)

        # 5. Gerar imagens (modo images x modo layers)
        if (payload.mode or "images").lower() == "layers":
            # Render local por assets (avatar + props)
            image_paths = await asyncio.wait_for(
                service_generate_layer_images(scenes_to_process, render_cfg=render_cfg),
                timeout=360,
            )
        else:
            # Geração por IA (Seedream) - modo legado
            image_paths = await asyncio.wait_for(
                service_generate_images(scenes_to_process, reference_image_path),
                timeout=600,
            )
        
        # 6. Gerar Audio com Gemini TTS (áudio natural) com fallback Edge
        audio_path = await asyncio.wait_for(
            service_generate_audio(scenes_to_process, payload.voice_id),
            timeout=300,
        )
        
        # Task 2: Validar áudio antes de renderizar
        # Para vídeos curtos (≤3 cenas), permitir mínimo de 8s; para longos, 30s
        min_audio_sec = 8.0 if len(scenes_to_process) <= 3 else 30.0
        print(f"[Run {run_id}] Validando áudio (mínimo: {min_audio_sec}s para {len(scenes_to_process)} cenas)...")
        audio_valid, audio_error = validate_audio_quality(audio_path, min_duration_sec=min_audio_sec)
        if not audio_valid:
            raise Exception(f"Áudio inválido: {audio_error}. Pipeline abortado antes de renderizar.")
        
        # 7. Renderizar Vídeo (retry curto)
        last_render_error = None
        video_url = None
        subtitles_url = None
        is_layers = (payload.mode or "images").lower() == "layers"
        min_bytes_quality = 100_000 if is_layers else 150_000
        min_seconds_quality = 3.0 if (is_layers and len(scenes_to_process) <= 3) else 8.0
        for render_attempt in range(2):
            try:
                render_result = await asyncio.wait_for(
                    service_render_video(
                        image_paths,
                        audio_path,
                        scenes_to_process,
                        static_base_url=get_public_static_base_url(request),
                        render_cfg=render_cfg,
                    ),
                    timeout=900,
                )
                video_url = render_result.get("video_url")
                subtitles_url = render_result.get("subtitles_url")
                # Pular quality gate para vídeos remotos (sem acesso local ao arquivo).
                is_local = video_url and ("localhost" in video_url or "127.0.0.1" in video_url)
                skip_this = not is_local
                quality_status = "skipped_remote" if skip_this else "pending"
                if skip_this:
                    print(f"[Run {run_id}] Pulando quality gate")
                else:
                    quality = _validate_video_quality(
                        video_url,
                        min_seconds=min_seconds_quality,
                        min_bytes=min_bytes_quality,
                        min_width=render_cfg["width"],
                        min_height=render_cfg["height"],
                    )
                    print(f"[Run {run_id}] quality_gate ok: {quality}")
                break
            except Exception as render_err:
                last_render_error = render_err
                print(f"[Run {run_id}] render tentativa {render_attempt + 1} falhou: {render_err}")
                if render_attempt == 1:
                    raise
                await asyncio.sleep(1)

        if not video_url:
            raise Exception(f"Falha no render após retry: {last_render_error}")

        _notify_mission_control(
            title=(payload.title or f"Video {run_id}").strip(),
            brief=(payload.brief or script_to_use[:500]).strip(),
            run_id=run_id,
            event="video_ready",
            status="review",
            video_url=video_url,
            quality_status=quality_status,
        )

        elapsed = time.time() - started
        print(f"[Run {run_id}] /generate-video concluído em {elapsed:.1f}s")

        return VideoResponse(
            status="completed",
            video_url=video_url,
            subtitles_url=subtitles_url,
            message=f"Vídeo gerado com sucesso! ({len(scenes_to_process)} cenas) | run_id={run_id}",
            elapsed_seconds=round(elapsed, 2),
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        elapsed = time.time() - started
        print(f"[Run {run_id}] /generate-video erro após {elapsed:.1f}s: {e}")
        return VideoResponse(
            status="error",
            message=f"Falha na geração (run_id={run_id}): {str(e)}",
            elapsed_seconds=round(elapsed, 2),
        )

@app.get("/health")
def health_check():
    return {
        "status": "backend_v1_ready",
        "ai_engine": "seedream_edge_gemini",
        "queue": queue_stats(),
    }


@app.get("/list-videos")
def list_videos():
    """Lista vídeos gerados no diretório static."""
    # STATIC_DIR already points to the correct location
    static_dir = STATIC_DIR
    if not os.path.exists(static_dir):
        return {"videos": [], "message": "Diretório static não encontrado"}
    
    videos = []
    for fname in os.listdir(static_dir):
        if fname.endswith(".mp4"):
            fpath = os.path.join(static_dir, fname)
            stat = os.stat(fpath)
            videos.append({
                "filename": fname,
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                "url": f"/static/{fname}"
            })
    
    # Sort by date, newest first
    videos.sort(key=lambda v: v["created_at"], reverse=True)
    return {"videos": videos}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=str(os.getenv("SERVER_HOST") or get_config("server.host", "0.0.0.0")),
        port=int(os.getenv("SERVER_PORT") or get_config("server.port", 8000)),
    )
