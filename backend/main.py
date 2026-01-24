from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
import os
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

class VideoResponse(BaseModel):
    status: str
    video_url: Optional[str] = None
    message: str

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
            contents = [visual_prompt]
            
            if reference_image_path and os.path.exists(reference_image_path):
                ref_image = PIL.Image.open(reference_image_path)
                contents = [visual_prompt, ref_image]
                print(f"    📷 Usando imagem de referência: {os.path.basename(reference_image_path)}")
            
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
    """Gera uma única imagem com Pollinations API - Wrapper Async"""
    def _generate_sync():
        try:
            visual_prompt = scene.get_visual_prompt
            if "POSITIVE PROMPT:" in visual_prompt:
                start = visual_prompt.find("POSITIVE PROMPT:") + len("POSITIVE PROMPT:")
                end = visual_prompt.find("NEGATIVE PROMPT:") if "NEGATIVE PROMPT:" in visual_prompt else len(visual_prompt)
                visual_prompt = visual_prompt[start:end].strip()
            
            base_url = img_config.get("base_url", "https://gen.pollinations.ai/image/")
            model = img_config.get("model", "turbo")
            width = img_config.get("width", 1280)
            height = img_config.get("height", 720)
            
            # API Key para autenticação (aumenta limites)
            api_key = os.getenv("POLLINATIONS_API_KEY", "")
            
            encoded_prompt = requests.utils.quote(visual_prompt)
            url = f"{base_url}{encoded_prompt}?width={width}&height={height}&model={model}&seed={scene.id}&nologo=true"
            
            # Adicionar imagem de referência se disponível (para consistência de personagem)
            if reference_image_path and os.path.exists(reference_image_path):
                # Converter imagem para base64 e adicionar como parâmetro
                import base64
                with open(reference_image_path, "rb") as img_file:
                    img_b64 = base64.b64encode(img_file.read()).decode('utf-8')
                    # Pollinations aceita URL de imagem - usar data URI
                    img_data_uri = f"data:image/png;base64,{img_b64}"
                    url += f"&image={requests.utils.quote(img_data_uri)}"
                    print(f"  📷 Usando imagem de referência para consistência")
            
            # Headers com Bearer token se API key disponível
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            
            print(f"  🎨 Cena {scene.id} ({model}): {visual_prompt[:50]}...")
            
            response = requests.get(url, headers=headers, timeout=120)
            if response.status_code == 200:
                # Detectar formato pelo content-type
                content_type = response.headers.get("content-type", "image/jpeg")
                ext = "png" if "png" in content_type else "jpg"
                filename = f"scene_{int(time.time())}_{scene.id}.{ext}"
                filepath = os.path.join(TEMP_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(response.content)
                print(f"  ✅ Cena {scene.id} gerada (Pollinations {model})")
                return (scene.id, filepath)
            else:
                print(f"  ❌ Cena {scene.id} falhou: HTTP {response.status_code}")
                return (scene.id, None)
        except Exception as e:
            print(f"  ❌ Cena {scene.id} erro: {e}")
            return (scene.id, None)

    return await asyncio.to_thread(_generate_sync)

async def service_generate_images(scenes: List[Scene], reference_image_path: str = None):
    """
    Gera imagens usando Nano Banana (Gemini 2.5 Flash) com fallback para Pollinations.
    OTIMIZAÇÃO: Geração com retry e fallback automático.
    """
    provider = get_config("services.image_generation.provider", "nanobanana")
    img_config = get_config(f"services.image_generation.options.pollinations", {})
    
    print(f"[Orchestrator] Gerando {len(scenes)} imagens com {provider}...")
    if reference_image_path:
        print(f"  📷 Personagem de referência: {reference_image_path}")
    
    all_results = {}
    
    for scene in scenes:
        filepath = None
        
        # Tentar Nano Banana primeiro (se configurado)
        if provider == "nanobanana":
            result = await generate_single_image_nanobanana(scene, {}, reference_image_path)
            if isinstance(result, tuple):
                scene_id, filepath = result
        
        # Fallback para Pollinations se Nano Banana falhou
        if not filepath:
            print(f"  🔄 Cena {scene.id}: Usando Pollinations como fallback...")
            result = await generate_single_image_pollinations(scene, img_config, reference_image_path)
            if isinstance(result, tuple):
                scene_id, filepath = result
        
        all_results[scene.id] = filepath
        
        # Delay maior para evitar rate limit (HTTP 429)
        await asyncio.sleep(3)
    
    # Ordenar resultados pela ordem original das cenas
    image_paths = [all_results.get(scene.id) for scene in scenes]
    
    success_count = sum(1 for p in image_paths if p)
    print(f"[Orchestrator] {success_count}/{len(scenes)} imagens geradas com sucesso")
    
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
        "Kore": "Kore"
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
        "Puck": "pt-BR-AntonioNeural",
        "Charon": "pt-BR-FabioNeural",
        "Kore": "pt-BR-ThalitaNeural",
        "Fenrir": "pt-BR-AntonioNeural",
        "Aoede": "pt-BR-FranciscaNeural"
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
    
    # Renderizar (preset ultrafast para MVP)
    final_video.write_videofile(
        output_path, 
        fps=24, 
        codec="libx264", 
        audio_codec="aac", 
        temp_audiofile=os.path.join(TEMP_DIR, "temp-audio.m4a"), 
        remove_temp=True, 
        logger=None,
        preset="ultrafast"
    )
    
    print(f"  ✅ Vídeo renderizado: {output_filename}")
    return f"http://localhost:8000/static/{output_filename}"

# --- Endpoint Principal ---

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
        
        # 5. Gerar Imagens com Nano Banana (Gemini 2.5 Flash Image)
        image_paths = await service_generate_images(scenes_to_process, reference_image_path)
        
        # 6. Gerar Audio com Gemini TTS (áudio natural)
        audio_path = await service_generate_audio(scenes_to_process, payload.voice_id)
        
        # 6. Renderizar Vídeo
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
