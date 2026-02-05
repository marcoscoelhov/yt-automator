from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
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
import requests
import wave
import io
from google import genai
from google.genai import types
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip, TextClip
import edge_tts
from dotenv import load_dotenv
import PIL.Image
from PIL import Image, ImageDraw
import hashlib  # Para cache de imagens
import httpx  # Para chamadas HTTP assíncronas (Kie.ai API)

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

print(f"[Config] Carregado: {CONFIG_PATH}")
print(f"  📷 Imagens: {get_config('services.image_generation.provider', 'pollinations')}")
print(f"  🎙️ TTS: {get_config('services.text_to_speech.provider', 'edge_tts')}")
print(f"  🎬 Render: {get_config('services.video_rendering.provider', 'moviepy')}")

# Configurar API Key do Google (Gemini) - Novo SDK
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
gemini_client = None
if GOOGLE_API_KEY:
    gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
    print(f"  ✅ Gemini Client inicializado")

# --- Configuração da Aplicação ---
app = FastAPI(title=get_config("app.name", "SaaS VSL Generator MVP - Real AI"))

# Estrutura de Pastas (usa config ou defaults)
STATIC_DIR = os.path.join(BASE_DIR, get_config("output.static_dir", "static"))
TEMP_DIR = os.path.join(BASE_DIR, get_config("output.temp_dir", "temp"))
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

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

class VideoResponse(BaseModel):
    status: str
    video_url: Optional[str] = None
    message: str


# --- Auto-generate (Brief -> Nick BR JSON -> Render) ---

class AutoGenerateRequest(BaseModel):
    brief: str
    voice_id: Optional[str] = "Antonio"
    mode: Optional[str] = "layers"  # force layers to avoid image-gen API calls

class AutoGenerateResponse(BaseModel):
    status: str
    title: Optional[str] = None
    description: Optional[str] = None
    scene_plan: Optional[dict] = None
    video_url: Optional[str] = None
    message: str


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
    except Exception as e:
        print(f"[AutoGenerate] Failed loading avatar catalog: {e}")
    try:
        props_meta = os.path.join(BASE_DIR, 'assets', 'props', 'whisk_pack_v1_png', '_meta.json')
        if os.path.exists(props_meta):
            data = json.loads(_read_text_file(props_meta))
            catalog['props'] = sorted(list((data.get('files') or {}).keys()))
    except Exception as e:
        print(f"[AutoGenerate] Failed loading props catalog: {e}")
    return catalog


def _validate_scene_plan(plan: dict) -> dict:
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

    catalog = _load_layers_asset_catalog()
    allowed_templates = set(catalog['templates'])
    allowed_poses = set(catalog['avatar_poses'])
    allowed_props = set(catalog['props'])

    normalized_scenes = []
    for i, sc in enumerate(scenes, start=1):
        if not isinstance(sc, dict):
            raise Exception(f'Scene {i} must be an object')

        texto = (sc.get('texto_narracao') or sc.get('description') or '').strip()
        if len(texto) < 5:
            raise Exception(f'Scene {i} missing texto_narracao')

        dur = sc.get('duracao_estimada') or sc.get('duration_est') or 5.0
        try:
            dur = float(dur)
        except Exception:
            dur = 5.0
        if dur < 2.5: dur = 2.5
        if dur > 9.0: dur = 9.0

        template = (sc.get('template') or 'avatar_left_prop_right').strip()
        if template not in allowed_templates:
            template = 'avatar_left_prop_right'

        pose = (sc.get('avatar_pose') or 'neutral_arms_crossed').strip()
        if allowed_poses and pose not in allowed_poses:
            pose = 'neutral_arms_crossed'

        props = sc.get('props') or []
        if not isinstance(props, list):
            props = []
        props2 = []
        for p in props[:3]:
            if isinstance(p, str) and (not allowed_props or p in allowed_props):
                props2.append(p)
        if template == 'icons_with_red_x' and 'red_x' not in props2:
            props2 = (props2 + ['red_x'])[:3]

        normalized_scenes.append({
            'id': i,
            'texto_narracao': texto,
            'prompt_visual': (sc.get('prompt_visual') or sc.get('visual_prompt') or '').strip() or None,
            'duracao_estimada': dur,
            'tipo_transicao': (sc.get('tipo_transicao') or 'cut'),
            'template': template,
            'avatar_pose': pose,
            'props': props2,
            'motion': (sc.get('motion') or None),
        })

    return {
        'title': title.strip(),
        'description': description.strip(),
        'script': script.strip(),
        'scenes': normalized_scenes,
    }


def _build_nick_br_prompt_v2(brief: str) -> str:
    prompt_path = os.path.join(BASE_DIR, 'prompts', 'nickinvests-br-meta-prompt.md')
    meta = _read_text_file(prompt_path) if os.path.exists(prompt_path) else ''
    catalog = _load_layers_asset_catalog()

    return f"""{meta}\n\n# INPUT BRIEF\n{brief.strip()}\n\n# AVAILABLE LAYERS ASSETS (STRICT)\nTemplates: {catalog['templates']}\nAvatar poses: {catalog['avatar_poses'][:30]}{' ...' if len(catalog['avatar_poses'])>30 else ''}\nProps: {catalog['props'][:60]}{' ...' if len(catalog['props'])>60 else ''}\n\n# OUTPUT FORMAT\nReturn ONLY valid JSON with keys: title, description, script, scenes.\n- scenes must be an array of objects with: texto_narracao, duracao_estimada (2.5-9), template, avatar_pose, props (0-3).\n- Use Portuguese (PT-BR), Nick BR tone: rápido, direto, \"papo reto\", com exemplos, números, e um final com CTA suave.\n- NO markdown, NO comments, NO trailing commas.\n"""


def _llm_generate_scene_plan(brief: str) -> dict:
    if not gemini_client:
        raise HTTPException(status_code=503, detail='GOOGLE_API_KEY não configurada. Configure GOOGLE_API_KEY no .env para usar o auto-generate.')

    prompt = _build_nick_br_prompt_v2(brief)

    try:
        resp = gemini_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.6,
                max_output_tokens=4096,
            )
        )
        text = resp.text or ''
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'Falha no LLM (Gemini): {e}')

    raw = _safe_json_extract(text)
    return _validate_scene_plan(raw)

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
        model = genai.GenerativeModel('gemini-1.5-flash')
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


async def generate_single_image_nanobanana(scene: Scene, img_config: dict, reference_image_path: str = None) -> tuple:
    """Gera uma única imagem com Nano Banana (Gemini 2.5 Flash Image) - Wrapper Async"""
    def _generate_sync():
        try:
            if not gemini_client:
                raise Exception("Gemini client não inicializado. Verifique GOOGLE_API_KEY.")
            
            visual_prompt = scene.get_visual_prompt
            # Extrair POSITIVE PROMPT
            if "POSITIVE PROMPT:" in visual_prompt:
                start = visual_prompt.find("POSITIVE PROMPT:") + len("POSITIVE PROMPT:")
                end = visual_prompt.find("NEGATIVE PROMPT:") if "NEGATIVE PROMPT:" in visual_prompt else len(visual_prompt)
                visual_prompt = visual_prompt[start:end].strip()
            
            print(f"  🎨 Cena {scene.id}: {visual_prompt[:60]}...")
            
            # Preparar conteúdo: prompt + imagem de referência (se existir)
            contents = []
            
            if reference_image_path and os.path.exists(reference_image_path):
                ref_image = PIL.Image.open(reference_image_path)
                # Instrução explícita para usar o personagem da referência
                character_instruction = (
                    "IMPORTANT: Use the character from the reference image below as the MAIN CHARACTER in this scene. "
                    "Keep the same character design, face, body proportions, clothing style, and colors. "
                    "The character must be clearly recognizable as the same person from the reference. "
                    "Reference image:"
                )
                contents = [character_instruction, ref_image, f"\n\nScene to generate: {visual_prompt}"]
                print(f"    📷 Usando personagem de referência: {os.path.basename(reference_image_path)}")
            else:
                contents = [visual_prompt]
            
            # Gerar imagem com Nano Banana
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash-image",
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                )
            )
            
            # Extrair imagem da resposta
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'inline_data') and part.inline_data:
                    filename = f"scene_{int(time.time())}_{scene.id}.png"
                    filepath = os.path.join(TEMP_DIR, filename)
                    
                    # Salvar imagem
                    image_data = part.inline_data.data
                    with open(filepath, "wb") as f:
                        f.write(image_data)
                    
                    print(f"  ✅ Cena {scene.id} gerada (Nano Banana)")
                    return (scene.id, filepath)
            
            print(f"  ⚠️ Cena {scene.id}: Nenhuma imagem retornada")
            return (scene.id, None)
            
        except Exception as e:
            print(f"  ❌ Cena {scene.id} erro: {e}")
            return (scene.id, None)

    return await asyncio.to_thread(_generate_sync)

async def generate_single_image_pollinations(scene: Scene, img_config: dict, reference_image_path: str = None) -> tuple:
    """Gera uma única imagem com Pollinations API - Wrapper Async com retry"""
    def _generate_sync():
        visual_prompt = scene.get_visual_prompt
        if "POSITIVE PROMPT:" in visual_prompt:
            start = visual_prompt.find("POSITIVE PROMPT:") + len("POSITIVE PROMPT:")
            end = visual_prompt.find("NEGATIVE PROMPT:") if "NEGATIVE PROMPT:" in visual_prompt else len(visual_prompt)
            visual_prompt = visual_prompt[start:end].strip()
        
        base_url = img_config.get("base_url", "https://gen.pollinations.ai/image/")
        model = img_config.get("model", "turbo")
        width = img_config.get("width", 1280)
        height = img_config.get("height", 720)
        api_key = os.getenv("POLLINATIONS_API_KEY", "")
        
        # Se tem referência, adicionar instrução detalhada no prompt
        # NOTA: Pollinations não suporta base64 inline (causa HTTP 414)
        if reference_image_path and os.path.exists(reference_image_path):
            # Adicionar instrução no prompt para manter consistência
            visual_prompt = f"Maintain consistent character design throughout. Scene: {visual_prompt}"
        
        encoded_prompt = requests.utils.quote(visual_prompt)
        url = f"{base_url}{encoded_prompt}?width={width}&height={height}&model={model}&seed={scene.id}&nologo=true"
        
        # Headers com Bearer token
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        
        # Retry com backoff exponencial para rate limit (429)
        max_retries = 3
        retry_delays = [5, 10, 20]  # Segundos entre tentativas
        
        for attempt in range(max_retries + 1):
            try:
                if attempt == 0:
                    print(f"  🎨 Cena {scene.id} ({model}): {visual_prompt[:45]}...")
                else:
                    print(f"  🔄 Cena {scene.id}: Retry {attempt}/{max_retries}...")
                
                response = requests.get(url, headers=headers, timeout=120)
                
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "image/jpeg")
                    ext = "png" if "png" in content_type else "jpg"
                    filename = f"scene_{int(time.time())}_{scene.id}.{ext}"
                    filepath = os.path.join(TEMP_DIR, filename)
                    with open(filepath, "wb") as f:
                        f.write(response.content)
                    print(f"  ✅ Cena {scene.id} gerada ({model})")
                    return (scene.id, filepath)
                
                elif response.status_code == 429:
                    # Rate limit - aguardar e tentar novamente
                    if attempt < max_retries:
                        delay = retry_delays[attempt]
                        print(f"  ⏳ Cena {scene.id}: Rate limit, aguardando {delay}s...")
                        time.sleep(delay)
                        continue
                    else:
                        print(f"  ❌ Cena {scene.id}: Rate limit persistente após {max_retries} tentativas")
                        return (scene.id, None)
                else:
                    print(f"  ❌ Cena {scene.id} falhou: HTTP {response.status_code}")
                    return (scene.id, None)
                    
            except requests.exceptions.Timeout:
                if attempt < max_retries:
                    print(f"  ⏳ Cena {scene.id}: Timeout, tentando novamente...")
                    continue
                print(f"  ❌ Cena {scene.id}: Timeout após {max_retries} tentativas")
                return (scene.id, None)
            except Exception as e:
                print(f"  ❌ Cena {scene.id} erro: {e}")
                return (scene.id, None)
        
        return (scene.id, None)

    return await asyncio.to_thread(_generate_sync)

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

async def service_generate_audio(scenes: List[Scene], voice_alias: str):
    """
    Gera áudio usando Gemini TTS (áudio natural) com fallback para Edge TTS.
    Modelo: gemini-2.5-flash-preview-tts
    """
    if not gemini_client:
        print("  ⚠️ Gemini client não disponível. Usando Edge TTS...")
        return await service_generate_audio_edge_fallback(scenes, voice_alias)
    
    print(f"[Orchestrator] Gerando áudio com Gemini TTS ({len(scenes)} cenas)...")
    
    # Concatenar todas as narrações das cenas
    narrations = []
    for scene in scenes:
        narration = scene.get_narration
        if narration:
            narrations.append(narration)
    
    full_script = " ".join(narrations)
    
    if not full_script.strip():
        raise Exception("Nenhum texto de narração encontrado nas cenas.")
    
    print(f"  📝 Script: {len(full_script)} caracteres, {len(narrations)} cenas")
    
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
        return output_path
        
    except Exception as e:
        print(f"  ⚠️ Erro Gemini TTS: {e}")
        print("  ⚠️ Usando Edge TTS como fallback...")
        return await service_generate_audio_edge_fallback(scenes, voice_alias)


async def service_generate_audio_edge_fallback(scenes: List[Scene], voice_alias: str):
    """Fallback: Gera áudio usando Edge TTS."""
    # Concatenar narrações
    full_script = " ".join([s.get_narration for s in scenes if s.get_narration])
    
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
    return output_path

# Função legada removida - agora temos Gemini TTS + Edge TTS fallback

def apply_zoom_effect(clip, zoom_type: str, duration: float):
    """
    Aplica efeito de zoom (Ken Burns) ao clip.
    """
    if zoom_type == "zoom_in":
        # Zoom in: começa em 100%, termina em 120%
        def zoom_in_effect(get_frame, t):
            scale = 1 + (0.2 * t / duration)  # 1.0 -> 1.2
            return get_frame(t)
        return clip.resize(lambda t: 1 + (0.15 * t / duration))
    elif zoom_type == "zoom_out":
        # Zoom out: começa em 120%, termina em 100%
        return clip.resize(lambda t: 1.15 - (0.15 * t / duration))
    else:
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


async def service_generate_layer_images(scenes: List[Scene]):
    """Gera um PNG por cena usando o motor de layers (assets locais)."""
    print(f"[Orchestrator] Gerando {len(scenes)} cenas em modo layers (assets locais)...")
    out_paths = []
    for scene in scenes:
        out_path = os.path.join(TEMP_DIR, f"layer_scene_{int(time.time()*1000)}_{scene.id}.png")
        _render_layer_scene_to_png(scene, out_path)
        out_paths.append(out_path)
    return out_paths


async def service_render_video(image_paths: List[str], audio_path: str, scenes: List[Scene]):
    """
    Compõe imagens e áudio usando MoviePy com suporte a:
    - Transições (zoom_in, zoom_out, crossfade, cut)
    - Duração sincronizada por cena
    """
    print("[Orchestrator] Renderizando vídeo com MoviePy...")
    
    if not audio_path or not os.path.exists(audio_path):
        raise Exception("Arquivo de áudio não encontrado.")

    # Carregar áudio para saber a duração total
    audio_clip = AudioFileClip(audio_path)
    total_audio_duration = audio_clip.duration
    
    # Filtrar imagens válidas e parear com cenas
    valid_pairs = []
    for i, img_path in enumerate(image_paths):
        if img_path and os.path.exists(img_path):
            scene = scenes[i] if i < len(scenes) else None
            valid_pairs.append((img_path, scene))
    
    if not valid_pairs:
        raise Exception("Nenhuma imagem válida gerada.")
    
    # Calcular durações
    total_scene_duration = sum(s.get_duration for _, s in valid_pairs if s)
    
    # Se a soma das durações for diferente do áudio, escalar proporcionalmente
    scale_factor = total_audio_duration / total_scene_duration if total_scene_duration > 0 else 1
    
    clips = []
    current_time = 0
    
    for i, (img_path, scene) in enumerate(valid_pairs):
        # Duração da cena (escalada para sincronizar com áudio)
        scene_duration = (scene.get_duration * scale_factor) if scene else (total_audio_duration / len(valid_pairs))
        
        # Criar clip base
        clip = ImageClip(img_path).set_duration(scene_duration)
        
        # FIX: Usar resize_to_fill em vez de apenas resize height
        # Isso garante que imagens quadradas/retangulares preencham 16:9 sem barras pretas
        try:
            clip = resize_to_fill(clip, 1280, 720)
        except Exception as e:
            print(f"Erro no resize_to_fill: {e}, usando resize padrão")
            clip = clip.resize(height=720)
            
        clip = clip.set_position("center")
        
        # Aplicar efeito de zoom baseado no tipo de transição
        if scene:
            transition = scene.get_transition
            if transition in ["zoom_in", "zoom_out"]:
                clip = apply_zoom_effect(clip, transition, scene_duration)
                print(f"  🎬 Cena {scene.id}: {transition} ({scene_duration:.1f}s)")
            elif transition == "crossfade":
                # Crossfade será aplicado na concatenação
                clip = clip.crossfadein(0.5) if i > 0 else clip
                print(f"  🎬 Cena {scene.id}: crossfade ({scene_duration:.1f}s)")
            else:
                print(f"  🎬 Cena {scene.id}: cut ({scene_duration:.1f}s)")
        
        clips.append(clip)
        current_time += scene_duration
    
    # Concatenar com método compose para suportar crossfades
    final_video = concatenate_videoclips(clips, method="compose")
    final_video = final_video.set_audio(audio_clip)
    
    output_filename = f"vsl_final_{int(time.time())}.mp4"
    output_path = os.path.join(STATIC_DIR, output_filename)
    
    print(f"[Orchestrator] Renderizando {len(clips)} cenas ({total_audio_duration:.1f}s total)...")
    
    # Renderizar (qualidade ok sem ficar "ultrafast")
    final_video.write_videofile(
        output_path,
        fps=24,
        codec="libx264",
        audio_codec="aac",
        temp_audiofile=os.path.join(TEMP_DIR, "temp-audio.m4a"),
        remove_temp=True,
        logger=None,
        preset="fast",
        ffmpeg_params=["-crf", "18", "-pix_fmt", "yuv420p"],
        threads=4,
    )
    
    print(f"  ✅ Vídeo renderizado: {output_filename}")
    return f"http://localhost:8000/static/{output_filename}"

@app.get("/config")
async def get_public_config():
    """Retorna configurações públicas (preços, flags)"""
    return {
        "pricing": get_config("pricing", {}),
        "app": get_config("app", {})
    }

# --- Endpoint Principal ---



@app.post('/auto-generate', response_model=AutoGenerateResponse)
async def auto_generate(payload: AutoGenerateRequest):
    # 1-click flow: brief -> Nick BR plan (JSON) -> render via internal pipeline (layers + Antonio)
    try:
        brief = (payload.brief or '').strip()
        if len(brief) < 10:
            raise HTTPException(status_code=400, detail='brief muito curto. Explique o tema, promessa e público-alvo (>=10 chars).')

        # Force constraints: no image-gen API calls
        mode = 'layers'
        voice_id = payload.voice_id or 'Antonio'

        plan = _llm_generate_scene_plan(brief)

        video_req = VideoGenerationRequest(
            script=plan.get('script', ''),
            scenes=[Scene(**sc) for sc in plan.get('scenes', [])],
            voice_id=voice_id,
            narration_style='Normal',
            reference_image_b64=None,
            mode=mode,
        )

        video_resp = await generate_video(video_req)
        if video_resp.status != 'completed':
            raise Exception(video_resp.message)

        return AutoGenerateResponse(
            status='completed',
            title=plan['title'],
            description=plan['description'],
            scene_plan=plan,
            video_url=video_resp.video_url,
            message='Auto-generate concluído.'
        )

    except HTTPException as e:
        raise e
    except Exception as e:
        import traceback
        traceback.print_exc()
        return AutoGenerateResponse(status='error', message=str(e))

@app.post("/generate-video", response_model=VideoResponse)
async def generate_video(payload: VideoGenerationRequest):
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
        
        # 5. Gerar imagens (modo images x modo layers)
        if (payload.mode or "images").lower() == "layers":
            # Render local por assets (avatar + props)
            image_paths = await service_generate_layer_images(scenes_to_process)
        else:
            # Geração por IA (Seedream) - modo legado
            image_paths = await service_generate_images(scenes_to_process, reference_image_path)
        
        # 6. Gerar Audio com Gemini TTS (áudio natural) com fallback Edge
        audio_path = await service_generate_audio(scenes_to_process, payload.voice_id)
        
        # 7. Renderizar Vídeo
        video_url = await service_render_video(image_paths, audio_path, scenes_to_process)
        
        return VideoResponse(
            status="completed",
            video_url=video_url,
            message=f"Vídeo gerado com sucesso! ({len(scenes_to_process)} cenas)"
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return VideoResponse(
            status="error",
            message=f"Falha na geração: {str(e)}"
        )

@app.get("/health")
def health_check():
    return {"status": "backend_v1_ready", "ai_engine": "pollinations_edge_gemini"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
