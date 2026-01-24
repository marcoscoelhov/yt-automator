import { useState, useMemo, useEffect } from 'react';
import { Video, FileText, Loader2, AlertCircle, CheckCircle2, Clock, Zap, Mic2, Image as ImageIcon, Upload, User, X, ChevronDown, Star, History, Trash2 } from 'lucide-react';

interface Scene {
  id: number;
  texto_narracao?: string;
  prompt_visual?: string;
  duracao_estimada?: number;
  tipo_transicao?: string;
}

interface ScriptHistory {
  id: string;
  name: string;
  script: string;
  date: string;
  scenes: number;
}

const VOICES = [
  { id: 'Puck', name: 'Puck', desc: 'Masculino • Energético' },
  { id: 'Kore', name: 'Kore', desc: 'Feminino • Jovem' },
  { id: 'Charon', name: 'Charon', desc: 'Masculino • Profundo' },
  { id: 'Aoede', name: 'Aoede', desc: 'Feminino • Melódico' },
  { id: 'Fenrir', name: 'Fenrir', desc: 'Masculino • Grave' },
];

const STORAGE_KEYS = {
  FAVORITE_VOICE: 'vsl_favorite_voice',
  SCRIPT_HISTORY: 'vsl_script_history',
};

export default function App() {
  // Carregar voz favorita do localStorage
  const [voice, setVoice] = useState(() => {
    const saved = localStorage.getItem(STORAGE_KEYS.FAVORITE_VOICE);
    return saved || 'Puck';
  });
  const [favoriteVoice, setFavoriteVoice] = useState(() => {
    return localStorage.getItem(STORAGE_KEYS.FAVORITE_VOICE) || '';
  });

  // Histórico de roteiros
  const [scriptHistory, setScriptHistory] = useState<ScriptHistory[]>(() => {
    const saved = localStorage.getItem(STORAGE_KEYS.SCRIPT_HISTORY);
    return saved ? JSON.parse(saved) : [];
  });
  const [showHistory, setShowHistory] = useState(false);

  const [scriptState, setScriptState] = useState('');
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string>('');
  const [progress, setProgress] = useState(0);
  const [currentStep, setCurrentStep] = useState(0);
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [errorQuery, setErrorQuery] = useState<string | null>(null);
  const [characterImage, setCharacterImage] = useState<File | null>(null);
  const [characterPreview, setCharacterPreview] = useState<string | null>(null);
  const [pricingConfig, setPricingConfig] = useState<any>(null);

  // Carregar configurações do backend
  useEffect(() => {
    fetch('http://localhost:8000/config')
      .then(res => res.json())
      .then(data => setPricingConfig(data.pricing))
      .catch(err => console.error("Falha ao carregar config:", err));
  }, []);

  // Salvar voz favorita
  const handleSetFavoriteVoice = () => {
    if (favoriteVoice === voice) {
      // Remover favorito
      localStorage.removeItem(STORAGE_KEYS.FAVORITE_VOICE);
      setFavoriteVoice('');
    } else {
      // Definir favorito
      localStorage.setItem(STORAGE_KEYS.FAVORITE_VOICE, voice);
      setFavoriteVoice(voice);
    }
  };

  // Adicionar roteiro ao histórico
  const addToHistory = (script: string, scenesCount: number) => {
    const newEntry: ScriptHistory = {
      id: Date.now().toString(),
      name: `Roteiro ${scriptHistory.length + 1}`,
      script: script,
      date: new Date().toLocaleDateString('pt-BR'),
      scenes: scenesCount,
    };
    const updated = [newEntry, ...scriptHistory.slice(0, 9)]; // Máximo 10 itens
    setScriptHistory(updated);
    localStorage.setItem(STORAGE_KEYS.SCRIPT_HISTORY, JSON.stringify(updated));
  };

  // Carregar roteiro do histórico
  const loadFromHistory = (item: ScriptHistory) => {
    setScriptState(item.script);
    setShowHistory(false);
  };

  // Remover do histórico
  const removeFromHistory = (id: string) => {
    const updated = scriptHistory.filter(h => h.id !== id);
    setScriptHistory(updated);
    localStorage.setItem(STORAGE_KEYS.SCRIPT_HISTORY, JSON.stringify(updated));
  };

  const parsedScenes: Scene[] = useMemo(() => {
    try {
      const json = JSON.parse(scriptState);
      if (Array.isArray(json)) return json;
      if (json.scenes) return json.scenes;
    } catch { }
    return [];
  }, [scriptState]);

  const totalDuration = useMemo(() => {
    return parsedScenes.reduce((acc, s) => acc + (s.duracao_estimada || 5), 0);
  }, [parsedScenes]);

  const steps = [
    { id: 1, label: 'Analisando', icon: FileText },
    { id: 2, label: 'Imagens', icon: ImageIcon },
    { id: 3, label: 'Narração', icon: Mic2 },
    { id: 4, label: 'Renderizando', icon: Video },
    { id: 5, label: 'Concluído', icon: CheckCircle2 },
  ];

  const handleGenerate = async () => {
    setLoading(true);
    setProgress(0);
    setCurrentStep(1);
    setErrorQuery(null);
    setVideoUrl(null);

    // Salvar roteiro no histórico
    if (scriptState.trim() && parsedScenes.length > 0) {
      addToHistory(scriptState, parsedScenes.length);
    }

    const progressInterval = setInterval(() => {
      setProgress(p => Math.min(p + 0.3, 95));
    }, 500);

    try {
      for (let i = 1; i <= 4; i++) {
        setCurrentStep(i);
        setStatus(steps[i - 1].label);
        await new Promise(r => setTimeout(r, 600));
      }

      let payload: any;
      try {
        const json = JSON.parse(scriptState);
        if (Array.isArray(json)) {
          payload = { script: "", scenes: json, voice_id: voice, narration_style: "", reference_image_b64: "" };
        } else {
          payload = { script: json.script || "", scenes: json.scenes || [], voice_id: json.voice_id || voice, narration_style: json.narration_style || "", reference_image_b64: "" };
        }
      } catch {
        payload = { script: scriptState, scenes: [], voice_id: voice, narration_style: "", reference_image_b64: "" };
      }

      if (characterImage && characterPreview) {
        const base64 = characterPreview.split(',')[1] || '';
        payload.reference_image_b64 = base64;
      }

      const response = await fetch('http://localhost:8000/generate-video', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      const data = await response.json();
      setCurrentStep(5);
      setProgress(100);

      if (data.status === 'completed' && data.video_url) {
        setVideoUrl(data.video_url);
      } else {
        throw new Error(data.message || 'Erro desconhecido');
      }
    } catch (err: any) {
      setErrorQuery(err.message.includes('Failed to fetch') ? "Backend offline" : err.message);
    } finally {
      clearInterval(progressInterval);
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-950 via-slate-900 to-indigo-950 text-white">
      {/* Background */}
      <div className="fixed inset-0 -z-10 overflow-hidden">
        <div className="absolute top-1/4 left-1/4 w-[500px] h-[500px] bg-purple-500/10 rounded-full blur-[120px] animate-pulse" />
        <div className="absolute bottom-1/4 right-1/4 w-[400px] h-[400px] bg-indigo-500/10 rounded-full blur-[100px] animate-pulse" style={{ animationDelay: '1s' }} />
      </div>

      <div className="max-w-6xl mx-auto p-4 lg:p-6">
        {/* Header */}
        <header className="mb-6">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="relative">
                <div className="absolute inset-0 bg-gradient-to-br from-indigo-500 to-purple-600 blur-lg opacity-50" />
                <div className="relative p-2.5 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 shadow-xl">
                  <Video className="w-5 h-5 text-white" />
                </div>
              </div>
              <div>
                <h1 className="text-xl font-bold text-white">VSL Generator</h1>
                <p className="text-xs text-slate-400">Pollinations + Gemini TTS</p>
              </div>
            </div>

            <button
              onClick={handleGenerate}
              disabled={loading || !scriptState.trim()}
              className="px-6 py-2.5 bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 active:scale-[0.98] rounded-xl font-semibold shadow-xl shadow-indigo-900/30 disabled:opacity-40 disabled:cursor-not-allowed transition-all flex items-center gap-2"
            >
              {loading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  <span>Gerando...</span>
                </>
              ) : (
                <>
                  <Zap className="w-4 h-4" />
                  <span>Gerar VSL</span>
                </>
              )}
            </button>
          </div>
        </header>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Painel Esquerdo */}
          <div className="space-y-4">
            {/* Voz e Personagem */}
            <div className="flex gap-3">
              {/* Seletor de Voz com Favorito */}
              <div className="flex-1 bg-slate-900/50 backdrop-blur-xl rounded-xl border border-white/5 p-3">
                <label className="flex items-center gap-1.5 text-xs font-medium text-slate-400 mb-2">
                  <Mic2 className="w-3 h-3" />
                  Narrador
                  {favoriteVoice && (
                    <span className="text-amber-400 text-[10px] ml-1">★ Favorito</span>
                  )}
                </label>
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <select
                      value={voice}
                      onChange={(e) => setVoice(e.target.value)}
                      className="w-full appearance-none bg-slate-800/70 border border-slate-700/50 rounded-lg px-3 py-2 text-sm text-white focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 outline-none cursor-pointer"
                    >
                      {VOICES.map((v) => (
                        <option key={v.id} value={v.id}>
                          {v.name} - {v.desc} {v.id === favoriteVoice ? '★' : ''}
                        </option>
                      ))}
                    </select>
                    <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400 pointer-events-none" />
                  </div>
                  <button
                    onClick={handleSetFavoriteVoice}
                    title={favoriteVoice === voice ? "Remover favorito" : "Definir como favorito"}
                    className={`p-2 rounded-lg border transition-all ${favoriteVoice === voice
                      ? 'bg-amber-500/20 border-amber-500/50 text-amber-400'
                      : 'bg-slate-800/50 border-slate-700/50 text-slate-400 hover:text-amber-400 hover:border-amber-500/30'
                      }`}
                  >
                    <Star className={`w-4 h-4 ${favoriteVoice === voice ? 'fill-current' : ''}`} />
                  </button>
                </div>
              </div>

              {/* Upload Personagem */}
              <div className="flex-1 bg-slate-900/50 backdrop-blur-xl rounded-xl border border-white/5 p-3">
                <label className="flex items-center gap-1.5 text-xs font-medium text-slate-400 mb-2">
                  <User className="w-3 h-3" />
                  Personagem
                </label>
                {characterPreview ? (
                  <div className="relative h-10 flex items-center gap-2 bg-slate-800/50 rounded-lg px-2">
                    <img src={characterPreview} alt="Ref" className="h-8 w-8 rounded object-cover" />
                    <span className="text-xs text-emerald-400 flex-1 truncate">Anexado</span>
                    <button
                      onClick={() => { setCharacterImage(null); setCharacterPreview(null); }}
                      className="p-1 rounded hover:bg-red-500/20 transition-colors"
                    >
                      <X className="w-3 h-3 text-red-400" />
                    </button>
                  </div>
                ) : (
                  <label className="flex items-center justify-center h-10 rounded-lg border border-dashed border-slate-700/50 hover:border-pink-500/50 bg-slate-800/30 cursor-pointer transition-all text-xs text-slate-400 hover:text-pink-300 gap-1.5">
                    <input
                      type="file"
                      accept="image/*"
                      className="hidden"
                      onChange={(e) => {
                        const file = e.target.files?.[0];
                        if (file) {
                          setCharacterImage(file);
                          const reader = new FileReader();
                          reader.onloadend = () => setCharacterPreview(reader.result as string);
                          reader.readAsDataURL(file);
                        }
                      }}
                    />
                    <Upload className="w-3 h-3" />
                    Upload
                  </label>
                )}
              </div>
            </div>

            {/* Roteiro */}
            <div className="bg-slate-900/50 backdrop-blur-xl rounded-xl border border-white/5 p-4">
              <div className="flex items-center justify-between mb-2">
                <label className="flex items-center gap-2 text-xs font-medium text-slate-400">
                  <FileText className="w-3 h-3" />
                  Roteiro (JSON)
                  {parsedScenes.length > 0 && (
                    <span className="text-indigo-300 flex items-center gap-1">
                      <Clock className="w-3 h-3" />
                      {parsedScenes.length} cenas • ~{Math.ceil(totalDuration / 60)}min
                    </span>
                  )}
                </label>
                {scriptHistory.length > 0 && (
                  <button
                    onClick={() => setShowHistory(!showHistory)}
                    className={`flex items-center gap-1 text-xs px-2 py-1 rounded-lg transition-all ${showHistory ? 'bg-indigo-500/20 text-indigo-300' : 'text-slate-400 hover:text-slate-300'
                      }`}
                  >
                    <History className="w-3 h-3" />
                    Histórico ({scriptHistory.length})
                  </button>
                )}
              </div>

              {/* Histórico Dropdown */}
              {showHistory && scriptHistory.length > 0 && (
                <div className="mb-3 p-2 bg-slate-800/50 rounded-lg border border-slate-700/30 max-h-40 overflow-y-auto">
                  {scriptHistory.map((item) => (
                    <div
                      key={item.id}
                      className="flex items-center justify-between p-2 rounded-lg hover:bg-slate-700/30 transition-colors group"
                    >
                      <button
                        onClick={() => loadFromHistory(item)}
                        className="flex-1 text-left"
                      >
                        <div className="text-xs text-white font-medium">{item.name}</div>
                        <div className="text-[10px] text-slate-400">{item.scenes} cenas • {item.date}</div>
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); removeFromHistory(item.id); }}
                        className="p-1 rounded opacity-0 group-hover:opacity-100 hover:bg-red-500/20 transition-all"
                      >
                        <Trash2 className="w-3 h-3 text-red-400" />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <textarea
                className="w-full h-48 bg-slate-950/50 border border-slate-700/50 rounded-lg p-3 text-sm font-mono text-slate-200 focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 outline-none transition-all placeholder:text-slate-600 resize-none"
                placeholder='Cole seu roteiro.json aqui...'
                value={scriptState}
                onChange={(e) => setScriptState(e.target.value)}
              />
            </div>
          </div>

          {/* Painel Direito */}
          <div className="space-y-4">
            {/* Estimativa de Custos (Novo) */}
            {parsedScenes.length > 0 && pricingConfig && (
              <div className="bg-slate-900/50 backdrop-blur-xl rounded-xl border border-white/5 p-4 flex items-center justify-between">
                <div>
                  <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1">Estimativa de Custo</h3>
                  <div className="flex items-baseline gap-1">
                    <span className="text-2xl font-bold text-emerald-400">
                      ${(parsedScenes.length * pricingConfig.image_unit_cost_usd).toFixed(2)}
                    </span>
                    <span className="text-xs text-slate-500">USD</span>
                  </div>
                </div>
                <div className="text-right text-xs text-slate-500">
                  <div>{parsedScenes.length} cenas x ${pricingConfig.image_unit_cost_usd}</div>
                  <div>TTS: Gratuito</div>
                </div>
              </div>
            )}

            {/* Progress */}
            {loading && (
              <div className="bg-slate-900/50 backdrop-blur-xl rounded-xl border border-white/5 p-4">
                <div className="flex items-center gap-2 mb-3">
                  <Loader2 className="w-4 h-4 text-indigo-400 animate-spin" />
                  <span className="text-sm font-medium text-slate-300">{status}</span>
                  <span className="ml-auto text-sm text-indigo-400">{Math.round(progress)}%</span>
                </div>
                <div className="h-1.5 bg-slate-800 rounded-full overflow-hidden mb-3">
                  <div
                    className="h-full bg-gradient-to-r from-indigo-500 via-purple-500 to-pink-500 transition-all duration-500 rounded-full"
                    style={{ width: `${progress}%` }}
                  />
                </div>
                <div className="flex gap-1">
                  {steps.map((step) => {
                    const Icon = step.icon;
                    const isActive = currentStep === step.id;
                    const isComplete = currentStep > step.id;
                    return (
                      <div
                        key={step.id}
                        className={`flex-1 flex flex-col items-center gap-0.5 p-1.5 rounded-lg transition-all ${isActive ? 'bg-indigo-500/20' : isComplete ? 'bg-emerald-500/10' : 'bg-slate-800/30'}`}
                      >
                        <Icon className={`w-3 h-3 ${isActive ? 'text-indigo-400' : isComplete ? 'text-emerald-400' : 'text-slate-500'}`} />
                        <span className={`text-[9px] ${isActive ? 'text-indigo-300' : isComplete ? 'text-emerald-300' : 'text-slate-500'}`}>
                          {step.label}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Video Preview */}
            <div className="relative bg-slate-900/30 backdrop-blur-xl rounded-xl border border-white/5 overflow-hidden shadow-xl min-h-[320px] flex items-center justify-center">
              {videoUrl ? (
                <div className="w-full">
                  <video src={videoUrl} controls className="w-full aspect-video bg-black" autoPlay />
                  <div className="p-3 flex items-center justify-between bg-slate-900/50">
                    <div className="flex items-center gap-2">
                      <CheckCircle2 className="w-4 h-4 text-emerald-400" />
                      <span className="text-sm text-emerald-300">Concluído!</span>
                    </div>
                    <a
                      href={videoUrl}
                      download
                      className="px-3 py-1.5 rounded-lg bg-indigo-500/20 text-indigo-300 text-xs font-medium hover:bg-indigo-500/30 transition-colors"
                    >
                      Download
                    </a>
                  </div>
                </div>
              ) : errorQuery ? (
                <div className="p-6 text-center">
                  <AlertCircle className="w-10 h-10 text-red-400 mx-auto mb-2" />
                  <p className="text-red-300 font-medium text-sm mb-1">Erro</p>
                  <p className="text-xs text-slate-400">{errorQuery}</p>
                </div>
              ) : (
                <div className="text-center p-6">
                  <div className="w-16 h-16 bg-slate-800/50 rounded-full mx-auto flex items-center justify-center mb-3">
                    <Video className="w-7 h-7 text-slate-600" />
                  </div>
                  <p className="text-slate-400 text-sm">Cole o roteiro e gere seu VSL</p>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
