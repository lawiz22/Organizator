import os

try:
    from dotenv import load_dotenv
    
    env_path = '.env'
    if os.path.exists(env_path):
        for enc in['utf-8', 'cp1251', 'utf-8-sig', 'utf-16']:
            try:
                load_dotenv(dotenv_path=env_path, encoding=enc)
                break
            except UnicodeDecodeError:
                continue
    else:
        load_dotenv()

except ImportError:
    pass

import argparse
import sys
import asyncio
import gc
import time
import json
import shutil
import hashlib
import urllib.parse
from urllib import request as urllib_request, error as urllib_error
import io
import sqlite3
import datetime
import concurrent.futures
import threading
import traceback
import faulthandler
import queue as _queue_mod
from collections import defaultdict
from pathlib import Path
import subprocess
import tempfile

# Внешние библиотеки
import cv2
import av
import numpy as np
import torch

import torch.nn.functional as F

# --- ПРОБРОС SAGE ATTENTION (РУЧНОЙ MONKEY PATCH) ---
try:
    from sageattention import sageattn
    
    original_sdpa = F.scaled_dot_product_attention

    def sage_wrapper(query, key, value, attn_mask=None, dropout_p=0.0, is_causal=False, scale=None, **kwargs):
        if attn_mask is not None:
            return original_sdpa(query, key, value, attn_mask=attn_mask, dropout_p=dropout_p, is_causal=is_causal, scale=scale, **kwargs)
        return sageattn(query, key, value, is_causal=is_causal)

    F.scaled_dot_product_attention = sage_wrapper
    # print("✅ SageAttention active et SDPA remplace avec succes !")

except Exception as e:
    print(f"⚠️ SageAttention indisponible. Utilisation de PyTorch SDPA standard.")
    print(f"   (Details: {e})")

# --- ИНТЕГРАЦИЯ INSIGHTFACE ---
try:
    import insightface
    from insightface.app import FaceAnalysis
    INSIGHTFACE_AVAILABLE = True
except ImportError:
    INSIGHTFACE_AVAILABLE = False
    print("⚠️ InsightFace indisponible. Installez insightface et onnxruntime-gpu pour la recherche par visage.")

from PIL import Image, ImageFile, ExifTags
from huggingface_hub import snapshot_download
from sentence_transformers import SentenceTransformer, CrossEncoder, util
from transformers import AutoImageProcessor, AutoModelForImageClassification, SiglipForImageClassification
from nicegui import app, ui, run
from fastapi.responses import FileResponse, Response
from fastapi import Body

# Локальная модель
from aesthetic_predictor_v2_5 import convert_v2_5_from_siglip

ImageFile.LOAD_TRUNCATED_IMAGES = True

current_dir = os.path.dirname(os.path.abspath(__file__))
# Перенаправляем загрузки моделей HF и PyTorch в папку "models"
os.environ["HF_HOME"] = os.path.join(current_dir, "models")
os.environ["TORCH_HOME"] = os.path.join(current_dir, "models")
CONFIG_FILE = os.path.join(current_dir, 'config.json')
CRASH_LOG_FILE = os.path.join(current_dir, 'crash_runtime.log')
OLLAMA_TRACE_LOG_FILE = os.path.join(current_dir, 'ollama_runtime.log')
THUMB_CACHE_DIR = os.path.join(current_dir, ".thumbs")
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)


def _append_crash_log(header, message):
    try:
        with open(CRASH_LOG_FILE, 'a', encoding='utf-8') as f:
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            f.write(f"[{ts}] {header}\n{message}\n\n")
    except Exception:
        pass


def _append_runtime_log(log_file, header, message):
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            f.write(f"[{ts}] {header}\n{message}\n\n")
    except Exception:
        pass


def _ollama_trace(stage: str, **fields):
    parts = []
    for key, value in fields.items():
        txt = str(value)
        if len(txt) > 300:
            txt = txt[:297] + '...'
        parts.append(f"{key}={txt}")
    line = f"[OLLAMA] {stage} {' | '.join(parts)}".rstrip()
    print(line)
    _append_runtime_log(OLLAMA_TRACE_LOG_FILE, f"OLLAMA::{stage}", ' | '.join(parts))


def _global_excepthook(exc_type, exc_value, exc_tb):
    formatted = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _append_crash_log('UNHANDLED EXCEPTION', formatted)
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _threading_excepthook(args):
    formatted = ''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    thread_name = getattr(args.thread, 'name', 'unknown')
    _append_crash_log(f'UNHANDLED THREAD EXCEPTION [{thread_name}]', formatted)
    if hasattr(threading, '__excepthook__'):
        threading.__excepthook__(args)


sys.excepthook = _global_excepthook
threading.excepthook = _threading_excepthook
try:
    _crash_fh = open(CRASH_LOG_FILE, 'a', encoding='utf-8')
    faulthandler.enable(_crash_fh)
except Exception:
    _crash_fh = None

SUPPORTED_IMAGES = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff')
SUPPORTED_VIDEOS = ('.mp4', '.avi', '.mov', '.mkv', '.webm')
SUPPORTED_TEXTS  = ('.txt', '.md', '.json', '.csv')
ITEMS_PER_PAGE = 50
DEFAULT_NSFW_MODEL = 'strangerguardhf/nsfw-image-detection'
NSFW_CACHE_VERSION = 'v14'
NSFW_THRESHOLD = 0.42  # Simple threshold for all modes
NSFW_SENSUAL_THRESHOLD = 0.30  # Fixed threshold for yellow tier
AVAILABLE_NSFW_MODELS = [
    'strangerguardhf/nsfw-image-detection',
    'prithivMLmods/siglip2-x256-explicit-content',
]

# ==========================================
# МЕНЕДЖМЕНТ КОНФИГУРАЦИЙ
# ==========================================
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception: pass
    return {}

def save_config(updates):
    config = load_config()
    config.update(updates)
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print(f"Erreur sauvegarde config : {e}")


def resolve_nsfw_profile(model_name_override=None, threshold_override=None):
    """Simple resolver: no presets, just model + threshold."""
    model_name = model_name_override or DEFAULT_NSFW_MODEL
    threshold = threshold_override if threshold_override is not None else NSFW_THRESHOLD
    return model_name, float(threshold)


# Label sets shared across classification and display
NSFW_SAFE_LABELS    = {'safe', 'sfw', 'normal', 'general', 'neutral', 'drawing',
                       'safe_content', 'anime picture', 'anime'}
NSFW_SENSUAL_LABELS = {'enticing or sensual', 'suggestive'}
# Everything else (pornography, hentai, explicit, nsfw, explicit content, …) = EXPLICIT

def classify_nsfw_tier(details, threshold, portrait_guard=False):
    """
    3-tier classification based directly on raw model label scores.
      SAIN     (green)  — safe content
            SENSUEL  (yellow) — enticing/suggestive score >= fixed 0.30
    EXPLICITE (red)   — pornographic/explicit score >= threshold and dominant over safe
        Threshold slider controls red boundary only.
    """
    if portrait_guard:
        return 'SAIN'

    threshold = float(threshold)
    sensual_threshold = float(NSFW_SENSUAL_THRESHOLD)
    safe_max = max((prob for lbl, prob in details.items()
                    if not lbl.startswith('_')
                    and lbl.lower() in NSFW_SAFE_LABELS), default=0.0)
    explicit_max = max((prob for lbl, prob in details.items()
                        if not lbl.startswith('_')
                        and lbl.lower() not in NSFW_SAFE_LABELS
                        and lbl.lower() not in NSFW_SENSUAL_LABELS), default=0.0)
    sensual_max  = max((prob for lbl, prob in details.items()
                        if lbl.lower() in NSFW_SENSUAL_LABELS), default=0.0)

    if explicit_max >= threshold:
        # If model still leans "safe/normal", avoid hard red.
        # Only keep yellow when sensual evidence is also meaningful.
        if explicit_max >= safe_max:
            return 'EXPLICITE'
        return 'SENSUEL' if sensual_max >= sensual_threshold else 'SAIN'
    if sensual_max >= sensual_threshold:
        return 'SENSUEL'
    return 'SAIN'

# Keep old name as alias so nothing else breaks
def classify_nsfw_label(danger_score, threshold=None, portrait_guard=False):
    return 'SAIN' if portrait_guard or (danger_score or 0) < (threshold or NSFW_THRESHOLD) else 'EXPLICITE'


def aesthetic_score_to_percent(score):
    """Convertit le score esthétique brut (~0..10) en pourcentage lisible."""
    try:
        value = float(score)
    except Exception:
        value = 0.0
    return max(0.0, min(100.0, value * 10.0))


def aesthetic_percent_level(percent):
    if percent >= 85:
        return 'Excellent'
    if percent >= 70:
        return 'Très bon'
    if percent >= 55:
        return 'Bon'
    if percent >= 40:
        return 'Moyen'
    return 'Faible'


def aesthetic_explain(avg_score, max_score, is_video):
    """Retourne des explications affichables du score esthétique."""
    avg_pct = aesthetic_score_to_percent(avg_score)
    max_pct = aesthetic_score_to_percent(max_score)
    delta = max(0.0, max_pct - avg_pct)

    if is_video:
        if delta <= 6:
            stability = 'stable'
        elif delta <= 15:
            stability = 'variable'
        else:
            stability = 'très variable'
        return {
            'avg_pct': avg_pct,
            'max_pct': max_pct,
            'level': aesthetic_percent_level(avg_pct),
            'method': 'Vidéo: moyenne des frames',
            'stability': stability,
            'delta': delta,
        }

    return {
        'avg_pct': avg_pct,
        'max_pct': max_pct,
        'level': aesthetic_percent_level(avg_pct),
        'method': 'Image: score direct du modèle',
        'stability': 'n/a',
        'delta': 0.0,
    }


def resolve_model_dir(repo_id):
    return os.path.join(current_dir, "models", repo_id.replace("/", "_"))


def is_local_model_ready(local_dir, required_files=None, required_any=None):
    if not os.path.isdir(local_dir):
        return False

    required_files = required_files or []
    required_any = required_any or []

    for name in required_files:
        if not os.path.exists(os.path.join(local_dir, name)):
            return False

    if required_any:
        return any(os.path.exists(os.path.join(local_dir, name)) for name in required_any)

    return True


def is_local_transformer_model_ready(local_dir):
    return is_local_model_ready(
        local_dir,
        required_files=["config.json", "tokenizer_config.json"],
        required_any=["model.safetensors", "pytorch_model.bin", "model.safetensors.index.json", "pytorch_model.bin.index.json"],
    )


def is_local_tagger_model_ready(local_dir):
    return is_local_model_ready(
        local_dir,
        required_files=["model.onnx"],
        required_any=["tags.csv", "tags.json", "tags.txt"],
    )


def has_child_entries(dir_path):
    return os.path.isdir(dir_path) and any(True for _ in os.scandir(dir_path))


def directory_contains_extension(dir_path, extension):
    if not os.path.isdir(dir_path):
        return False
    for root, _, files in os.walk(dir_path):
        if any(name.lower().endswith(extension.lower()) for name in files):
            return True
    return False


def is_local_aesthetic_ready():
    siglip_cache_dir = os.path.join(current_dir, "models", "models--google--siglip-so400m-patch14-384")
    siglip_snapshots_dir = os.path.join(siglip_cache_dir, "snapshots")
    predictor_head = os.path.join(current_dir, "models", "hub", "checkpoints", "aesthetic_predictor_v2_5.pth")
    return has_child_entries(siglip_snapshots_dir) and os.path.exists(predictor_head)


def is_local_insightface_ready():
    model_dir = os.path.join(current_dir, "models", "insightface", "models", "buffalo_l")
    return directory_contains_extension(model_dir, ".onnx")


def build_local_models_report_lines():
    model_checks = [
        ("Qwen Embedding 2B", resolve_model_dir("Qwen/Qwen3-VL-Embedding-2B"), is_local_transformer_model_ready(resolve_model_dir("Qwen/Qwen3-VL-Embedding-2B"))),
        ("Qwen Embedding 8B", resolve_model_dir("Qwen/Qwen3-VL-Embedding-8B"), is_local_transformer_model_ready(resolve_model_dir("Qwen/Qwen3-VL-Embedding-8B"))),
        ("Qwen Reranker 2B", resolve_model_dir("Qwen/Qwen3-VL-Reranker-2B"), is_local_transformer_model_ready(resolve_model_dir("Qwen/Qwen3-VL-Reranker-2B"))),
        ("Qwen Reranker 8B", resolve_model_dir("Qwen/Qwen3-VL-Reranker-8B"), is_local_transformer_model_ready(resolve_model_dir("Qwen/Qwen3-VL-Reranker-8B"))),
        ("NSFW Soft", resolve_model_dir("strangerguardhf/nsfw-image-detection"), is_local_model_ready(resolve_model_dir("strangerguardhf/nsfw-image-detection"), required_files=["config.json", "preprocessor_config.json"], required_any=["model.safetensors", "pytorch_model.bin", "model.safetensors.index.json", "pytorch_model.bin.index.json"])),
        ("NSFW Strict", resolve_model_dir("prithivMLmods/siglip2-x256-explicit-content"), is_local_model_ready(resolve_model_dir("prithivMLmods/siglip2-x256-explicit-content"), required_files=["config.json", "preprocessor_config.json"], required_any=["model.safetensors", "pytorch_model.bin", "model.safetensors.index.json", "pytorch_model.bin.index.json"])),
        ("Tags SwinV2", resolve_model_dir("SmilingWolf/wd-swinv2-tagger-v3"), is_local_tagger_model_ready(resolve_model_dir("SmilingWolf/wd-swinv2-tagger-v3"))),
        ("Tags ConvNeXt", resolve_model_dir("SmilingWolf/wd-convnext-tagger-v3"), is_local_tagger_model_ready(resolve_model_dir("SmilingWolf/wd-convnext-tagger-v3"))),
        ("InsightFace buffalo_l", os.path.join(current_dir, "models", "insightface", "models", "buffalo_l"), is_local_insightface_ready()),
        ("Aesthetic Predictor v2.5", os.path.join(current_dir, "models"), is_local_aesthetic_ready()),
    ]

    lines = []
    for name, path, is_ready in model_checks:
        status = "OK" if is_ready else "INCOMPLET"
        lines.append(f"[{status}] {name}")
        lines.append(f"  {path}")
    return lines

# ==========================================
# FastAPI РОУТИНГ ДЛЯ ПЛЕЕРА И МИНИАТЮР
# ==========================================
@app.get('/media/{file_path:path}')
def read_media(file_path: str):
    clean_path = urllib.parse.unquote(file_path)
    return FileResponse(clean_path)

@app.get('/thumb/{file_path:path}')
def read_thumb(file_path: str):
    clean_path = urllib.parse.unquote(file_path)
    path_hash = hashlib.md5(clean_path.encode('utf-8')).hexdigest()
    thumb_path = os.path.join(THUMB_CACHE_DIR, f"{path_hash}.jpg")

    if os.path.exists(thumb_path):
        return FileResponse(thumb_path)

    ext = os.path.splitext(clean_path)[1].lower()
    try:
        if ext in SUPPORTED_IMAGES:
            with Image.open(clean_path) as img:
                img.thumbnail((300, 300))
                img.convert('RGB').save(thumb_path, format="JPEG", quality=80)
        elif ext in SUPPORTED_VIDEOS:
            with av.open(clean_path) as container:
                for frame in container.decode(video=0):
                    img = frame.to_image()
                    img.thumbnail((300, 300))
                    img.convert('RGB').save(thumb_path, format="JPEG", quality=80)
                    break
        if os.path.exists(thumb_path):
            return FileResponse(thumb_path)
    except: pass
    return Response(status_code=404)


def _collect_llm_payload_from_cache(path: str) -> dict:
    """Collecte les signaux IA déjà indexés dans la base cache de MediaMind AI."""
    if not path:
        return {}

    try:
        c = search_engine.db_cache.conn.cursor()

        def safe_json(value):
            if value is None:
                return None
            try:
                return json.loads(value)
            except Exception:
                return None

        tags_row = c.execute(
            "SELECT model, tags FROM tags_cache WHERE path = ? ORDER BY rowid DESC LIMIT 1",
            (path,),
        ).fetchone()
        prompt_row = c.execute(
            "SELECT source, prompt FROM prompt_cache WHERE path = ? LIMIT 1",
            (path,),
        ).fetchone()
        detailed_prompt_row = c.execute(
            "SELECT source, prompt FROM detailed_prompt_cache WHERE path = ? LIMIT 1",
            (path,),
        ).fetchone()
        nsfw_row = c.execute(
            "SELECT model, top_label, danger_score, details FROM nsfw_cache WHERE path = ? ORDER BY rowid DESC LIMIT 1",
            (path,),
        ).fetchone()
        aes_row = c.execute(
            "SELECT model, avg_score, max_score FROM aes_cache WHERE path = ? ORDER BY rowid DESC LIMIT 1",
            (path,),
        ).fetchone()
        face_row = c.execute(
            "SELECT COUNT(*) FROM face_cache WHERE path = ? AND face_idx >= 0",
            (path,),
        ).fetchone()

        payload = {
            "source": "media_mind_ai_api_cache",
            "analyzed_at": datetime.datetime.now().isoformat(timespec='seconds'),
            "path": path,
        }

        if tags_row:
            payload["tags"] = {
                "model": tags_row[0],
                "values": safe_json(tags_row[1]) or {},
            }
        if prompt_row and prompt_row[1]:
            payload["prompt"] = {
                "source": prompt_row[0] or "image_metadata",
                "text": str(prompt_row[1]),
            }
        if detailed_prompt_row and detailed_prompt_row[1]:
            payload["detailed_prompt"] = {
                "source": detailed_prompt_row[0] or "heuristic",
                "text": str(detailed_prompt_row[1]),
            }
        if nsfw_row:
            payload["nsfw"] = {
                "model": nsfw_row[0],
                "top_label": nsfw_row[1],
                "danger_score": nsfw_row[2],
                "details": safe_json(nsfw_row[3]) or {},
            }
        if aes_row:
            payload["aesthetic"] = {
                "model": aes_row[0],
                "avg_score": aes_row[1],
                "max_score": aes_row[2],
            }
        if face_row and face_row[0] is not None:
            payload["faces"] = {"count": int(face_row[0])}

        return payload
    except Exception:
        return {}


@app.get('/api/llm/ping')
def api_llm_ping():
    return {
        "ok": True,
        "service": "media_mind_ai",
        "time": datetime.datetime.now().isoformat(timespec='seconds'),
    }


def _ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")


def _ollama_timeout_seconds() -> float:
    # Priority: env var -> config.json -> default.
    raw = os.getenv("OLLAMA_TIMEOUT_SEC", "").strip()
    if not raw:
        cfg = load_config()
        raw = str(cfg.get("ollama_timeout_sec", "240") or "240").strip()
    try:
        timeout = float(raw)
    except Exception:
        timeout = 240.0
    return max(10.0, timeout)


def _extract_first_json_object(text: str) -> dict:
    if not text:
        return {}
    raw = str(text).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _ollama_generate(model: str, prompt: str, system: str = "", image_path: str = "") -> str:
    req_id = f"g{time.time_ns() % 1000000000:09d}"
    started = time.monotonic()
    timeout_s = _ollama_timeout_seconds()
    # Encode l'image en base64 si fournie (pour les modèles de vision)
    images_b64 = []
    if image_path and os.path.isfile(image_path):
        try:
            import base64
            with open(image_path, 'rb') as _f:
                images_b64.append(base64.b64encode(_f.read()).decode('ascii'))
        except Exception as _e:
            _ollama_trace('generate.image_encode_error', req_id=req_id, error=repr(_e))
    _ollama_trace(
        'generate.start',
        req_id=req_id,
        base_url=_ollama_base_url(),
        model=model,
        prompt_len=len(prompt or ''),
        system_len=len(system or ''),
        timeout_s=f"{timeout_s:.1f}",
        has_image=bool(images_b64),
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
        },
    }
    if system:
        payload["system"] = system
    if images_b64:
        payload["images"] = images_b64
    req = urllib_request.Request(
        f"{_ollama_base_url()}/api/generate",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        _ollama_trace('generate.http_ok', req_id=req_id, raw_len=len(raw or ''))
        parsed = json.loads(raw) if raw else {}
        response_text = str(parsed.get("response", "") or "")
        _ollama_trace(
            'generate.done',
            req_id=req_id,
            elapsed_s=f"{(time.monotonic() - started):.2f}",
            response_len=len(response_text),
        )
        return response_text
    except Exception as e:
        _ollama_trace(
            'generate.error',
            req_id=req_id,
            elapsed_s=f"{(time.monotonic() - started):.2f}",
            error=repr(e),
        )
        raise


def _ollama_list_models() -> list[str]:
    req_id = f"m{time.time_ns() % 1000000000:09d}"
    started = time.monotonic()
    _ollama_trace('models.start', req_id=req_id, base_url=_ollama_base_url())
    req = urllib_request.Request(f"{_ollama_base_url()}/api/tags", method="GET")
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        parsed = json.loads(raw) if raw else {}
        models = parsed.get("models") or []
        names = []
        for item in models:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item.get("name")))
        _ollama_trace('models.done', req_id=req_id, elapsed_s=f"{(time.monotonic() - started):.2f}", count=len(names))
        return names
    except Exception as e:
        _ollama_trace('models.error', req_id=req_id, elapsed_s=f"{(time.monotonic() - started):.2f}", error=repr(e))
        raise


def _ollama_prompt_to_tags(model: str, prompt_text: str) -> dict:
    req_id = f"t{time.time_ns() % 1000000000:09d}"
    started = time.monotonic()
    _ollama_trace('prompt_to_tags.start', req_id=req_id, model=model, prompt_len=len(prompt_text or ''))
    system = (
        "Tu convertis un prompt d'image en tags courts utiles pour recherche média. "
        "Retourne uniquement un JSON valide au format {\"tags\": [\"tag_1\", \"tag_2\"]}. "
        "Pas d'explication, pas de markdown. Maximum 40 tags."
    )
    prompt = (
        "Convertis ce prompt en tags de recherche simples, compacts, sans phrases longues.\n\n"
        f"PROMPT:\n{prompt_text}"
    )
    raw = _ollama_generate(model, prompt, system=system)
    _ollama_trace('prompt_to_tags.raw', req_id=req_id, raw_len=len(raw or ''), raw_preview=(raw or '')[:220])
    parsed = _extract_first_json_object(raw)
    tag_list = parsed.get("tags") or []
    if not isinstance(tag_list, list):
        tag_list = []
    tags = {}
    for item in tag_list:
        tag = str(item or "").strip().lower().replace(" ", "_")
        if tag:
            tags[tag] = 1.0
    _ollama_trace('prompt_to_tags.done', req_id=req_id, elapsed_s=f"{(time.monotonic() - started):.2f}", tag_count=len(tags))
    return tags


def _ollama_detailed_prompt(model: str, prompt_text: str, tags_dict: dict, ai_payload: dict | None = None, path: str = "") -> str:
    req_id = f"d{time.time_ns() % 1000000000:09d}"
    started = time.monotonic()
    _ollama_trace(
        'detailed_prompt.start',
        req_id=req_id,
        model=model,
        prompt_len=len(prompt_text or ''),
        tags_count=len(tags_dict or {}),
        has_path=bool(path),
    )
    ai_payload = ai_payload or {}
    top_tags = ", ".join(tag for tag, _score in sorted((tags_dict or {}).items(), key=lambda x: -float(x[1]))[:20])
    nsfw_label = str(((ai_payload.get("nsfw") or {}).get("top_label") or "")).strip()
    aes_score = (ai_payload.get("aesthetic") or {}).get("avg_score")
    faces_count = (ai_payload.get("faces") or {}).get("count")
    has_image = bool(path and os.path.isfile(path))
    system = (
        "You write a detailed, clean, reusable image prompt by DESCRIBING THE ATTACHED IMAGE. "
        "Your primary source of truth is the image itself; the text inputs are only secondary context. "
        "Return ONLY the final prompt in plain text, with no title and no explanation. "
        "Never repeat the base prompt or tags verbatim. "
        "Start directly with the generated prompt. "
        "Output MUST be in English. "
        "Use natural prose (2 to 4 sentences), never a tag list. "
        "Do not use prefixes like 'Tags:' or comma-only lists. "
        "Describe ONLY what is actually visible in the image. Do not invent characters, actions, settings, or details not present. "
        "If the image is unclear, stay conservative and describe only what you can clearly see."
    )
    user_prompt = (
        ("Look at the attached image and describe it as a detailed reusable prompt.\n\n" if has_image
         else "No image available — use only the textual context below.\n\n")
        + f"Base prompt (context only, do not repeat): {prompt_text or 'none'}\n"
        f"Tags (hints only, may be wrong): {top_tags or 'none'}\n"
        f"NSFW: {nsfw_label or 'unknown'}\n"
        f"Aesthetic score: {aes_score if aes_score is not None else 'unknown'}\n"
        f"Faces: {faces_count if faces_count is not None else 'unknown'}\n"
        f"File: {os.path.basename(path) if path else 'unknown'}\n\n"
        + ("Now generate a coherent, visually rich detailed prompt in English DESCRIBING WHAT YOU SEE IN THE IMAGE. "
           if has_image else
           "Generate a coherent, visually rich detailed prompt in English from the textual context only. ")
        + "Do not start with the base prompt. Do not invent elements not in the image."
    )
    out = _ollama_generate(model, user_prompt, system=system, image_path=path).strip()
    # Strip echoed input prefix if the model repeated it despite instructions
    if prompt_text:
        stripped = prompt_text.strip()
        if out.lower().startswith(stripped.lower()):
            out = out[len(stripped):].lstrip(' .,;\n').strip()
    lowered = out.lower().strip()
    if lowered.startswith('dominant tags:') or lowered.startswith('tags dominants:') or lowered.startswith('tags:'):
        out = TagEngine._build_detailed_prompt_fallback(prompt_text, tags_dict, ai_payload, path)
    _ollama_trace('detailed_prompt.done', req_id=req_id, elapsed_s=f"{(time.monotonic() - started):.2f}", out_len=len(out or ''))
    return out


def _ollama_raw_prompt(model: str, prompt_text: str, tags_dict: dict, image_path: str = "") -> str:
    req_id = f"r{time.time_ns() % 1000000000:09d}"
    started = time.monotonic()
    _ollama_trace('raw_prompt.start', req_id=req_id, model=model, prompt_len=len(prompt_text or ''), tags_count=len(tags_dict or {}))
    top_tags = ", ".join(tag for tag, _score in sorted((tags_dict or {}).items(), key=lambda x: -float(x[1]))[:20])
    has_image = bool(image_path and os.path.isfile(image_path))
    system = (
        "You produce a raw image prompt that is directly usable, by DESCRIBING THE ATTACHED IMAGE. "
        "Your primary source of truth is the image itself; the text inputs are only secondary context. "
        "Return ONLY one single-line prompt, with no title and no explanation. "
        "Never repeat the input prompt verbatim. "
        "Start directly with the new prompt content. "
        "Output MUST be in English. "
        "Describe ONLY what is actually visible in the image. Do not invent characters, actions, settings, or details not present."
    )
    user_prompt = (
        ("Look at the attached image and describe it as a compact single-line prompt.\n\n" if has_image
         else "No image available — use only the textual context below.\n\n")
        + f"Input prompt (context only, do not repeat): {prompt_text or 'none'}\n"
        f"Useful tags (hints only, may be wrong): {top_tags or 'none'}\n\n"
        + ("Now generate a compact and readable raw prompt in English DESCRIBING WHAT YOU SEE IN THE IMAGE. "
           if has_image else
           "Generate a compact and readable raw prompt in English from the textual context only. ")
        + "Do not start with the input prompt."
    )
    out = _ollama_generate(model, user_prompt, system=system, image_path=image_path).strip()
    # Strip echoed input prefix if the model repeated it despite instructions
    if prompt_text:
        stripped = prompt_text.strip()
        if out.lower().startswith(stripped.lower()):
            out = out[len(stripped):].lstrip(' .,;\n').strip()
    _ollama_trace('raw_prompt.done', req_id=req_id, elapsed_s=f"{(time.monotonic() - started):.2f}", out_len=len(out or ''))
    return out


def _ollama_detect_ai(model: str, image_path: str) -> dict:
    """Demande à un modèle de vision Ollama de décider si l'image est IA-générée.

    Retourne dict {is_ai: bool, confidence: float (0..1), reason: str, raw: str}.
    En cas d'erreur ou de parsing impossible, is_ai=None.
    """
    req_id = f"ai{time.time_ns() % 1000000000:09d}"
    started = time.monotonic()
    _ollama_trace('detect_ai.start', req_id=req_id, model=model, path=image_path)
    system = (
        "You are a forensic image analyst. Look carefully at the attached image and decide whether "
        "it was generated by an AI image generator (Stable Diffusion, Midjourney, DALL-E, ComfyUI, "
        "NovelAI, Flux, etc.) or whether it is a real photograph (or a hand-made drawing/painting). "
        "Consider: anatomical errors (extra fingers, melted hands, wrong eye gaze), texture artifacts, "
        "background nonsense, plastic/airbrushed skin, hair physics, text rendering, perfect symmetry, "
        "lighting consistency. "
        "Respond with ONLY a single line of valid JSON, no markdown, no prose, in this exact shape: "
        '{\"is_ai\": true|false, \"confidence\": 0.0-1.0, \"reason\": \"short explanation\"}'
    )
    user_prompt = "Analyze the attached image and return the JSON verdict."
    raw = ""
    try:
        raw = _ollama_generate(model, user_prompt, system=system, image_path=image_path).strip()
    except Exception as e:
        _ollama_trace('detect_ai.error', req_id=req_id, error=repr(e))
        return {'is_ai': None, 'confidence': 0.0, 'reason': f'ollama_error: {e}', 'raw': ''}
    # Tentative parsing JSON; on tolère du texte autour
    parsed = None
    s = raw.strip()
    # Si du texte avant/après, isoler le premier objet {...}
    if s and s[:1] != '{':
        start = s.find('{')
        end = s.rfind('}')
        if start >= 0 and end > start:
            s = s[start:end + 1]
    try:
        parsed = json.loads(s)
    except Exception:
        parsed = None
    if not isinstance(parsed, dict):
        _ollama_trace('detect_ai.parse_failed', req_id=req_id, raw_snippet=raw[:200])
        # Heuristique de secours: chercher "ai" / "real" dans le texte brut
        low = raw.lower()
        if 'is_ai' in low and ('true' in low or 'yes' in low):
            return {'is_ai': True, 'confidence': 0.5, 'reason': 'parsed_from_text', 'raw': raw}
        if 'is_ai' in low and ('false' in low or 'no' in low):
            return {'is_ai': False, 'confidence': 0.5, 'reason': 'parsed_from_text', 'raw': raw}
        return {'is_ai': None, 'confidence': 0.0, 'reason': 'unparsable_response', 'raw': raw}
    is_ai_raw = parsed.get('is_ai')
    if isinstance(is_ai_raw, str):
        is_ai = is_ai_raw.strip().lower() in ('true', 'yes', '1', 'ai')
    else:
        is_ai = bool(is_ai_raw)
    try:
        conf = float(parsed.get('confidence', 0.0))
    except Exception:
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    reason = str(parsed.get('reason', '') or '')[:500]
    _ollama_trace('detect_ai.done', req_id=req_id, elapsed_s=f"{(time.monotonic() - started):.2f}", is_ai=is_ai, confidence=conf)
    return {'is_ai': is_ai, 'confidence': conf, 'reason': reason, 'raw': raw}


def run_ai_detection(path: str, ollama_model: str = "", use_ollama_fallback: bool = True) -> dict:
    """Pipeline 3-passes pour décider si une image est IA-générée.

    Passe 1 : signature de générateur dans les métadonnées image → IA confirmé.
    Passe 2 : EXIF appareil photo cohérent (Make/Model/Lens/Exposure) → photo réelle.
    Passe 3 : fallback vision Ollama (si modèle fourni et `use_ollama_fallback`).

    Retourne un dict prêt à sérialiser en sidecar `.ia` :
    {is_ai, confidence, method, detected_at, ai_metadata, exif, ollama_reasoning}
    """
    detected_at = time.strftime('%Y-%m-%dT%H:%M:%S')
    result = {
        'is_ai': None,
        'confidence': 0.0,
        'method': 'unknown',
        'detected_at': detected_at,
        'ai_metadata': {},
        'exif': {},
        'ollama_reasoning': '',
    }
    if not path or not os.path.isfile(path):
        result['method'] = 'file_missing'
        return result

    # Passe 1 : metadata
    meta = TagEngine._detect_ai_from_metadata(path)
    if meta.get('is_ai') is True:
        result.update({
            'is_ai': True,
            'confidence': float(meta.get('confidence', 0.9)),
            'method': f"metadata:{meta.get('source', '')}",
            'ai_metadata': meta.get('evidence', {}),
        })
        # On capture quand même l'EXIF pour info
        result['exif'] = TagEngine._extract_exif_camera_info(path)
        return result

    # Passe 2 : EXIF appareil photo
    exif_info = TagEngine._extract_exif_camera_info(path)
    result['exif'] = exif_info
    photo_check = TagEngine._detect_real_photo_from_exif(exif_info)
    if photo_check.get('is_photo'):
        result.update({
            'is_ai': False,
            'confidence': float(photo_check.get('confidence', 0.7)),
            'method': f"exif_camera:{','.join(photo_check.get('signals', []))}",
        })
        return result

    # Passe 3 : fallback vision Ollama
    if use_ollama_fallback and ollama_model:
        try:
            ai_resp = _ollama_detect_ai(ollama_model, path)
        except Exception as e:
            result['method'] = f'ollama_error:{e}'
            return result
        is_ai_v = ai_resp.get('is_ai')
        if is_ai_v is None:
            result['method'] = 'ollama_inconclusive'
            result['ollama_reasoning'] = ai_resp.get('reason', '') or ai_resp.get('raw', '')[:300]
            return result
        result.update({
            'is_ai': bool(is_ai_v),
            'confidence': float(ai_resp.get('confidence', 0.5)),
            'method': f'ollama:{ollama_model}',
            'ollama_reasoning': ai_resp.get('reason', '')[:500],
        })
        return result

    # Rien de concluant
    result['method'] = 'inconclusive_no_ollama'
    return result


@app.get('/api/llm/ollama_models')
def api_llm_ollama_models():
    _ollama_trace('api.ollama_models.start')
    try:
        models = _ollama_list_models()
        _ollama_trace('api.ollama_models.done', count=len(models))
        return {
            "ok": True,
            "base_url": _ollama_base_url(),
            "models": models,
        }
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError, OSError, ValueError) as e:
        _ollama_trace('api.ollama_models.error', error=repr(e))
        return {
            "ok": False,
            "base_url": _ollama_base_url(),
            "error": str(e),
            "models": [],
        }


@app.post('/api/llm/enrich')
def api_llm_enrich(payload: dict = Body(default={})):  # noqa: B008
    path = str(payload.get("path", "") or "")
    if not path:
        return {"ok": False, "error": "missing_path"}

    data = _collect_llm_payload_from_cache(path)
    has_signal = any(k in data for k in ("tags", "prompt", "detailed_prompt", "nsfw", "aesthetic", "faces"))
    return {
        "ok": True,
        "path": path,
        "has_signal": has_signal,
        "payload": data if has_signal else {},
    }


@app.post('/api/llm/prompt_to_tags')
def api_llm_prompt_to_tags(payload: dict = Body(default={})):  # noqa: B008
    path = str(payload.get("path", "") or "").strip()
    prompt_text = str(payload.get("prompt", "") or "").strip()
    provider = str(payload.get("provider", "local") or "local").strip().lower()
    model_name = str(payload.get("model", "") or "").strip()
    if not prompt_text and path:
        prompt_data = search_engine.db_cache.get_prompt(path)
        prompt_text = str((prompt_data or {}).get("text", "") or "").strip()
    if not prompt_text:
        return {"ok": False, "error": "missing_prompt"}

    _ollama_trace(
        'api.prompt_to_tags.start',
        provider=provider,
        model=model_name,
        has_path=bool(path),
        prompt_len=len(prompt_text),
    )

    if provider == "ollama":
        if not model_name:
            return {"ok": False, "error": "missing_ollama_model"}
        tags = _ollama_prompt_to_tags(model_name, prompt_text)
        engine_name = f"ollama:{model_name}"
    else:
        tags = TagEngine._prompt_text_to_tags(prompt_text)
        engine_name = "heuristic_fallback"
        model_name = "prompt_parser"

    if path and tags:
        search_engine.db_cache.save_tags(model_name, path, tags)
    _ollama_trace('api.prompt_to_tags.done', engine=engine_name, count=len(tags))
    return {
        "ok": True,
        "engine": engine_name,
        "path": path,
        "count": len(tags),
        "payload": {
            "tags": {
                "model": model_name,
                "values": tags,
            }
        },
    }


@app.post('/api/llm/generate_detailed_prompt')
def api_llm_generate_detailed_prompt(payload: dict = Body(default={})):  # noqa: B008
    path = str(payload.get("path", "") or "").strip()
    base_prompt = str(payload.get("prompt", "") or "").strip()
    provider = str(payload.get("provider", "local") or "local").strip().lower()
    model_name = str(payload.get("model", "") or "").strip()
    if not base_prompt and path:
        prompt_data = search_engine.db_cache.get_prompt(path)
        base_prompt = str((prompt_data or {}).get("text", "") or "").strip()

    cache_payload = _collect_llm_payload_from_cache(path) if path else {}
    tags_values = ((payload.get("tags") or {}) if isinstance(payload.get("tags"), dict) else {})
    if not tags_values:
        tags_values = (((cache_payload.get("tags") or {}).get("values") or {}))

    _ollama_trace(
        'api.generate_prompt.start',
        provider=provider,
        model=model_name,
        has_path=bool(path),
        prompt_len=len(base_prompt),
        tags_count=len(tags_values),
    )

    if provider == "ollama":
        if not model_name:
            return {"ok": False, "error": "missing_ollama_model"}
        detailed_prompt = _ollama_detailed_prompt(model_name, base_prompt, tags_values, cache_payload, path)
        engine_name = f"ollama:{model_name}"
    else:
        detailed_prompt = TagEngine._build_detailed_prompt_fallback(
            prompt_text=base_prompt,
            tags_dict=tags_values,
            ai_payload=cache_payload,
            path=path,
        )
        engine_name = "heuristic_fallback"
    if not detailed_prompt:
        return {"ok": False, "error": "insufficient_context"}

    if path:
        search_engine.db_cache.save_detailed_prompt(path, detailed_prompt, source=engine_name)

    _ollama_trace('api.generate_prompt.done', engine=engine_name, out_len=len(detailed_prompt))

    return {
        "ok": True,
        "engine": engine_name,
        "path": path,
        "payload": {
            "detailed_prompt": {
                "source": engine_name,
                "text": detailed_prompt,
            }
        },
    }


# File d'attente d'analyse à la demande (POST /api/llm/request_analysis)
_on_demand_queue: _queue_mod.Queue = _queue_mod.Queue()
_on_demand_lock = threading.Lock()
_on_demand_thread: threading.Thread | None = None


def _on_demand_worker():
    """Thread de fond: analyse les images soumises via /api/llm/request_analysis."""
    while True:
        try:
            path = _on_demand_queue.get(timeout=5)
        except _queue_mod.Empty:
            continue
        try:
            if not path or not os.path.isfile(path):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext not in SUPPORTED_IMAGES:
                continue
            previous_processing_state = bool(getattr(state, "is_processing", False))
            try:
                state.is_processing = True
                # Aesthetic
                try:
                    aesthetic_engine.evaluate_media(None, SUPPORTED_IMAGES, override_files=[path])
                except Exception:
                    pass
                # NSFW — utilise le modèle courant ou le premier disponible
                try:
                    _nsfw_model = getattr(nsfw_engine, "current_model_name", None) or "strangerguardhf/nsfw-image-detection"
                    nsfw_engine.evaluate_media(os.path.dirname(path), _nsfw_model, SUPPORTED_IMAGES, override_files=[path])
                except Exception:
                    pass
                # Tags
                try:
                    _tag_model = getattr(tag_engine, "model_name", None) or "SmilingWolf/wd-vit-tagger-v3"
                    tag_engine.evaluate_media(None, _tag_model, SUPPORTED_IMAGES, override_files=[path])
                except Exception:
                    pass

                payload = _collect_llm_payload_from_cache(path)
                has_required = any(k in payload for k in ("aesthetic", "nsfw", "tags", "prompt"))
                present = [k for k in ("aesthetic", "nsfw", "tags", "prompt", "faces") if k in payload]
                if has_required:
                    state.add_log(
                        f"✅ Indexation MediaMind AI terminée : {os.path.basename(path)}"
                        f" | signaux: {', '.join(present) if present else 'aucun'}"
                    )
                else:
                    state.add_log(
                        f"⚠️ Indexation incomplète (aucun signal requis) : {os.path.basename(path)}"
                        f" | signaux: {', '.join(present) if present else 'aucun'}"
                    )
            finally:
                state.is_processing = previous_processing_state
        except Exception:
            pass
        finally:
            _on_demand_queue.task_done()


def _ensure_on_demand_thread():
    global _on_demand_thread
    with _on_demand_lock:
        if _on_demand_thread is None or not _on_demand_thread.is_alive():
            _on_demand_thread = threading.Thread(target=_on_demand_worker, daemon=True, name="on_demand_analysis")
            _on_demand_thread.start()


@app.post('/api/llm/request_analysis')
def api_llm_request_analysis(payload: dict = Body(default={})):  # noqa: B008
    paths = payload.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    unique_paths = []
    seen = set()
    for p in paths:
        norm = os.path.normpath(str(p or "").strip())
        if norm and norm not in seen:
            seen.add(norm)
            unique_paths.append(norm)
    queued = 0
    skipped = []
    for p in unique_paths:
        if p and os.path.isfile(p):
            _on_demand_queue.put_nowait(p)
            queued += 1
        elif p:
            skipped.append(os.path.basename(p))
    if skipped:
        state.add_log(f"⚠️ request_analysis: {len(skipped)} fichier(s) introuvable(s) sur disque : {', '.join(skipped)}")
    _ensure_on_demand_thread()
    return {"ok": True, "queued": queued, "skipped": len(skipped)}

# ==========================================
# 1. БАЗЫ ДАННЫХ И КЭШ
# ==========================================
class DatabaseCache:
    def __init__(self, db_path='image_cache.db'):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)

        # Включаем WAL (Write-Ahead Logging) для быстрой работы без блокировок
        self.conn.execute("PRAGMA journal_mode=WAL") 
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # Выделяем 256 МБ ОЗУ под кэш SQLite (по умолчанию там смешные крохи)
        self.conn.execute("PRAGMA cache_size=-262144") 
        # Разрешаем проецировать базу в оперативную память (до 2 ГБ)
        self.conn.execute("PRAGMA mmap_size=2147483648") 
        self.conn.execute("PRAGMA temp_store=MEMORY")

        self._init_tables()

    def _init_tables(self):
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS emb_cache (model TEXT, path TEXT, features BLOB, PRIMARY KEY (model, path))''')
        c.execute('''CREATE TABLE IF NOT EXISTS rerank_cache_v2 (model TEXT, query TEXT, path TEXT, score REAL, PRIMARY KEY (model, query, path))''')
        c.execute('''CREATE TABLE IF NOT EXISTS aes_cache (model TEXT, path TEXT, avg_score REAL, max_score REAL, PRIMARY KEY (model, path))''')
        c.execute('''CREATE TABLE IF NOT EXISTS sim_cache (model TEXT, query TEXT, path TEXT, score REAL, PRIMARY KEY (model, query, path))''')
        c.execute('''CREATE TABLE IF NOT EXISTS nsfw_cache (model TEXT, path TEXT, top_label TEXT, danger_score REAL, details TEXT, PRIMARY KEY (model, path))''')
        c.execute('''CREATE TABLE IF NOT EXISTS face_cache (path TEXT, face_idx INTEGER, embedding BLOB, PRIMARY KEY (path, face_idx))''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS tags_cache (model TEXT, path TEXT, tags TEXT, PRIMARY KEY (model, path))''')
        c.execute('''CREATE TABLE IF NOT EXISTS prompt_cache (path TEXT PRIMARY KEY, source TEXT, prompt TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS detailed_prompt_cache (path TEXT PRIMARY KEY, source TEXT, prompt TEXT)''')
        # Cache détection IA: un seul état par path (verdict + métadonnées JSON)
        c.execute('''CREATE TABLE IF NOT EXISTS ai_detection_cache (path TEXT PRIMARY KEY, is_ai INTEGER, confidence REAL, method TEXT, detection TEXT, updated_at REAL)''')
        
        c.execute('CREATE INDEX IF NOT EXISTS idx_nsfw_path ON nsfw_cache(path)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_emb_path ON emb_cache(path)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_face_path ON face_cache(path)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_tags_path ON tags_cache(path)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_prompt_path ON prompt_cache(path)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_detailed_prompt_path ON detailed_prompt_cache(path)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_ai_detection_path ON ai_detection_cache(path)')
        self.conn.commit()

    # --- Danbooru Tags ---
    def get_tags(self, model_name, path):
        c = self.conn.cursor()
        c.execute("SELECT tags FROM tags_cache WHERE model=? AND path=?", (model_name, path))
        row = c.fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def save_tags(self, model_name, path, tags_dict):
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO tags_cache (model, path, tags) VALUES (?, ?, ?)", 
                  (model_name, path, json.dumps(tags_dict)))
        self.conn.commit()

    def save_tags_batch(self, batch_data):
        if not batch_data: return
        c = self.conn.cursor()
        data =[(m, p, json.dumps(t)) for m, p, t in batch_data]
        c.executemany("INSERT OR REPLACE INTO tags_cache (model, path, tags) VALUES (?, ?, ?)", data)
        self.conn.commit()

    def get_prompt(self, path):
        c = self.conn.cursor()
        c.execute("SELECT source, prompt FROM prompt_cache WHERE path=?", (path,))
        row = c.fetchone()
        if not row:
            return None
        text = str(row[1] or '').strip()
        # Auto-réparation : si garbage en DB, on le nettoie en place
        if text and not TagEngine._is_valid_prompt_text(text):
            c.execute("UPDATE prompt_cache SET prompt='' WHERE path=?", (path,))
            self.conn.commit()
            text = ''
        return {"source": row[0], "text": text}

    def save_prompt(self, path, prompt_text, source="image_metadata"):
        # Validation centrale : n'importe quel garbage est réduit à ''
        prompt_text = str(prompt_text or '').strip()
        if prompt_text and not TagEngine._is_valid_prompt_text(prompt_text):
            prompt_text = ''
        c = self.conn.cursor()
        c.execute("SELECT source, prompt FROM prompt_cache WHERE path=?", (path,))
        existing = c.fetchone()
        existing_source = str((existing[0] if existing else '') or '').strip().lower()
        new_source = str(source or '').strip()

        # Guardrail: never replace a prompt extracted from the image metadata
        # with a generated/raw prompt. Generated text is redirected to the
        # detailed prompt cache instead, leaving the embedded source intact.
        if existing_source.startswith('image_metadata') and not str(new_source).lower().startswith('image_metadata'):
            c.execute(
                "INSERT OR REPLACE INTO detailed_prompt_cache (path, source, prompt) VALUES (?, ?, ?)",
                (path, f"raw:{new_source or 'cache'}", prompt_text),
            )
            self.conn.commit()
            self._write_prompt_sidecar(path)
            return

        c.execute(
            "INSERT OR REPLACE INTO prompt_cache (path, source, prompt) VALUES (?, ?, ?)",
            (path, new_source, prompt_text),
        )
        self.conn.commit()
        self._write_prompt_sidecar(path)

    def _write_prompt_sidecar(self, path):
        """Ecrit {stem}_prompt.txt avec priorite detailed > raw."""
        try:
            p = Path(path)
            best = ''
            d = self.get_detailed_prompt(path)
            if d and str(d.get('text', '') or '').strip():
                best = str(d['text']).strip()
            else:
                c = self.conn.cursor()
                c.execute("SELECT prompt FROM prompt_cache WHERE path=?", (path,))
                row = c.fetchone()
                if row and str(row[0] or '').strip():
                    best = str(row[0]).strip()
            if not best:
                return
            p.with_name(f"{p.stem}_prompt.txt").write_text(best, encoding='utf-8')
        except Exception as e:
            try: state.add_log(f"[PROMPT-SIDECAR] err {path}: {e}")
            except Exception: pass

    def get_detailed_prompt(self, path):
        c = self.conn.cursor()
        c.execute("SELECT source, prompt FROM detailed_prompt_cache WHERE path=?", (path,))
        row = c.fetchone()
        if not row:
            return None
        text = str(row[1] or '').strip()
        # Auto-réparation : si garbage en DB, on le nettoie en place
        if text and not TagEngine._is_valid_prompt_text(text):
            c.execute("UPDATE detailed_prompt_cache SET prompt='' WHERE path=?", (path,))
            self.conn.commit()
            text = ''
        return {"source": row[0], "text": text}

    def save_detailed_prompt(self, path, prompt_text, source="heuristic"):
        # Validation centrale : n'importe quel garbage est réduit à ''
        prompt_text = str(prompt_text or '').strip()
        if prompt_text and not TagEngine._is_valid_prompt_text(prompt_text):
            prompt_text = ''
        c = self.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO detailed_prompt_cache (path, source, prompt) VALUES (?, ?, ?)",
            (path, source, prompt_text),
        )
        self.conn.commit()
        self._write_prompt_sidecar(path)

    # --- Détection IA ---
    def get_ai_detection(self, path):
        c = self.conn.cursor()
        c.execute("SELECT is_ai, confidence, method, detection, updated_at FROM ai_detection_cache WHERE path=?", (path,))
        row = c.fetchone()
        if not row:
            return None
        try:
            det = json.loads(row[3]) if row[3] else {}
        except Exception:
            det = {}
        return {
            "is_ai": bool(row[0]) if row[0] is not None else None,
            "confidence": float(row[1] or 0.0),
            "method": str(row[2] or ''),
            "detection": det if isinstance(det, dict) else {},
            "updated_at": float(row[4] or 0.0),
        }

    def save_ai_detection(self, path, is_ai, confidence, method, detection):
        c = self.conn.cursor()
        try:
            det_json = json.dumps(detection or {}, ensure_ascii=False, default=str)
        except Exception:
            det_json = '{}'
        c.execute(
            "INSERT OR REPLACE INTO ai_detection_cache (path, is_ai, confidence, method, detection, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (path, 1 if is_ai else 0, float(confidence or 0.0), str(method or ''), det_json, float(time.time())),
        )
        self.conn.commit()
        # Auto-ecriture du sidecar {stem}.ia
        if is_ai is not None:
            try:
                payload = dict(detection) if isinstance(detection, dict) else {}
                payload.setdefault('is_ai', bool(is_ai))
                payload.setdefault('confidence', float(confidence or 0.0))
                payload.setdefault('method', str(method or ''))
                payload.setdefault('detected_at', datetime.datetime.now(datetime.timezone.utc).isoformat())
                Path(path).with_suffix('.ia').write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                    encoding='utf-8',
                )
            except Exception as e:
                try: state.add_log(f"[IA-SIDECAR] err {path}: {e}")
                except Exception: pass

    def delete_ai_detection(self, path):
        c = self.conn.cursor()
        c.execute("DELETE FROM ai_detection_cache WHERE path=?", (path,))
        self.conn.commit()

    # --- Face ---
    def get_face_embeddings(self, path):
        c = self.conn.cursor()
        c.execute("SELECT embedding FROM face_cache WHERE path=?", (path,))
        rows = c.fetchall()
        if not rows: return None
        embs =[]
        for r in rows:
            if len(r[0]) > 0:
                embs.append(np.frombuffer(r[0], dtype=np.float32))
        return embs

    def save_face_embeddings(self, path, embeddings):
        c = self.conn.cursor()
        c.execute("DELETE FROM face_cache WHERE path=?", (path,))
        if not embeddings:
            c.execute("INSERT INTO face_cache (path, face_idx, embedding) VALUES (?, ?, ?)", (path, -1, b''))
        else:
            data =[(path, i, emb.tobytes()) for i, emb in enumerate(embeddings)]
            c.executemany("INSERT INTO face_cache (path, face_idx, embedding) VALUES (?, ?, ?)", data)
        self.conn.commit()

    def save_face_embeddings_batch(self, batch_data):
        """Sauvegarde par lot pour le batching InsightFace (acceleration SQLite)."""
        if not batch_data: return
        c = self.conn.cursor()
        
        # 1. Удаляем старые записи для всего батча разом
        paths = [(item[0],) for item in batch_data]
        c.executemany("DELETE FROM face_cache WHERE path=?", paths)
        
        # 2. Подготавливаем новые векторы
        insert_data =[]
        for path, embs in batch_data:
            if not embs:
                insert_data.append((path, -1, b''))
            else:
                for i, emb in enumerate(embs):
                    insert_data.append((path, i, emb.tobytes()))
                    
        # 3. Сохраняем всё одним запросом
        c.executemany("INSERT INTO face_cache (path, face_idx, embedding) VALUES (?, ?, ?)", insert_data)
        self.conn.commit()

    # --- NSFW ---
    def get_nsfw_score(self, model_name, path):
        c = self.conn.cursor()
        c.execute("SELECT top_label, danger_score, details FROM nsfw_cache WHERE model=? AND path=?", (model_name, path))
        return c.fetchone()

    def save_nsfw_score(self, model_name, path, top_label, danger_score, details):
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO nsfw_cache (model, path, top_label, danger_score, details) VALUES (?, ?, ?, ?, ?)", 
                  (model_name, path, top_label, danger_score, json.dumps(details)))
        self.conn.commit()
        # Auto-ecriture {stem}_validation.json (skip cas auto/pseudo)
        try:
            if top_label in ('error', 'no_person', 'portrait'):
                return
            det = details if isinstance(details, dict) else {}
            real_keys = [k for k in det.keys() if not str(k).startswith('_')]
            if not real_keys:
                return
            p = Path(path)
            numeric_details = {}
            for k, v in det.items():
                if str(k).startswith('_') or v is None:
                    continue
                try: numeric_details[k] = float(v)
                except (ValueError, TypeError):
                    try: numeric_details[k] = int(v)
                    except (ValueError, TypeError): numeric_details[k] = str(v)
            try: expl_thr = float(getattr(state, 'nsfw_threshold', 0.5))
            except Exception: expl_thr = 0.5
            try: sens_thr = float(NSFW_SENSUAL_THRESHOLD)
            except Exception: sens_thr = 0.3
            payload = {
                'schema': 'organizador.nsfw.validation.v1',
                'validated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'source_file': str(p),
                'file_name': p.name,
                'result': {
                    'tier': top_label,
                    'danger': float(danger_score),
                    'model': model_name,
                    'raw_top_label': str(det.get('_raw_top_label', top_label)),
                    'explicit_threshold': expl_thr,
                    'sensual_threshold': sens_thr,
                    'details': numeric_details,
                },
            }
            with open(p.with_name(f"{p.stem}_validation.json"), 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            try: state.add_log(f"[NSFW-SIDECAR] err {path}: {e}")
            except Exception: pass

    def clear_nsfw_cache(self):
        """Vide complètement le cache NSFW pour forcer réanalyse."""
        c = self.conn.cursor()
        c.execute("DELETE FROM nsfw_cache")
        self.conn.commit()

    # --- Общие ---
    def get_query_sims(self, model_name, query):
        c = self.conn.cursor()
        c.execute("SELECT path, score FROM sim_cache WHERE model=? AND query=?", (model_name, query))
        return {row[0]: row[1] for row in c.fetchall()}

    def save_query_sims(self, model_name, query, paths, scores):
        c = self.conn.cursor()
        data =[(model_name, query, p, s) for p, s in zip(paths, scores)]
        c.executemany("INSERT OR REPLACE INTO sim_cache (model, query, path, score) VALUES (?, ?, ?, ?)", data)
        self.conn.commit()

    def get_aesthetic_score(self, model_name, path):
        c = self.conn.cursor()
        c.execute("SELECT avg_score, max_score FROM aes_cache WHERE model=? AND path=?", (model_name, path))
        return c.fetchone()

    def save_aesthetic_score(self, model_name, path, avg_score, max_score):
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO aes_cache (model, path, avg_score, max_score) VALUES (?, ?, ?, ?)", 
                  (model_name, path, avg_score, max_score))
        self.conn.commit()
        # Auto-ecriture {stem}_aesthetic.json (skip si tout a zero, signal d'erreur)
        try:
            if not (float(avg_score) == 0.0 and float(max_score) == 0.0):
                p = Path(path)
                payload = {
                    'schema': 'organizador.aesthetic.v1',
                    'validated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'source_file': str(p),
                    'file_name': p.name,
                    'result': {
                        'avg_score': float(avg_score),
                        'max_score': float(max_score),
                        'model': model_name,
                    },
                }
                with open(p.with_name(f"{p.stem}_aesthetic.json"), 'w', encoding='utf-8') as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            try: state.add_log(f"[AES-SIDECAR] err {path}: {e}")
            except Exception: pass

    def get_image_features(self, model_name, path):
        c = self.conn.cursor()
        c.execute("SELECT features FROM emb_cache WHERE model=? AND path=?", (model_name, path))
        result = c.fetchone()
        if result is not None:
            return torch.load(io.BytesIO(result[0]), weights_only=False)
        return None

    def save_image_features(self, model_name, path, features):
        c = self.conn.cursor()
        features_bytes = io.BytesIO()
        torch.save(features, features_bytes)
        c.execute("INSERT OR REPLACE INTO emb_cache (model, path, features) VALUES (?, ?, ?)", 
                  (model_name, path, features_bytes.getvalue()))
        self.conn.commit()

    def get_rerank_score(self, model_name, query, path):
        c = self.conn.cursor()
        c.execute("SELECT score FROM rerank_cache_v2 WHERE model=? AND query=? AND path=?", (model_name, query, path))
        result = c.fetchone()
        return result[0] if result is not None else None

    def save_rerank_score(self, model_name, query, path, score):
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO rerank_cache_v2 (model, query, path, score) VALUES (?, ?, ?, ?)", 
                  (model_name, query, path, score))
        self.conn.commit()

    def get_max_danger_score(self, path):
        c = self.conn.cursor()
        # Ищем файл в кэше любых NSFW-моделей и берем максимальную оценку опасности
        c.execute("SELECT MAX(danger_score) FROM nsfw_cache WHERE path=?", (path,))
        res = c.fetchone()
        return res[0] if res and res[0] is not None else -1.0  # -1.0 означает, что файла нет в базе

    def get_all_models(self):
        c = self.conn.cursor()
        models = set()
        for table in['emb_cache', 'rerank_cache_v2', 'aes_cache', 'sim_cache', 'nsfw_cache']:
            try:
                c.execute(f"SELECT DISTINCT model FROM {table}")
                models.update([r[0] for r in c.fetchall() if r[0]])
            except: pass
            
        # Искусственно добавляем пункт для лиц, если в кэше лиц есть хотя бы одна запись
        try:
            c.execute("SELECT 1 FROM face_cache LIMIT 1")
            if c.fetchone() is not None:
                models.add("InsightFace (Visages)")
        except: pass
        
        return list(models)

    def clear_model_cache(self, model_name=None):
        c = self.conn.cursor()
        tables =['emb_cache', 'rerank_cache_v2', 'aes_cache', 'sim_cache', 'nsfw_cache', 'face_cache', 'tags_cache']
        if model_name:
            if model_name == "InsightFace (Visages)":
                c.execute("DELETE FROM face_cache")
            else:
                for table in tables:
                    if table == 'face_cache': continue 
                    c.execute(f"DELETE FROM {table} WHERE model=?", (model_name,))
        else:
            for table in tables:
                c.execute(f"DELETE FROM {table}")
        self.conn.commit()
        self.conn.execute("VACUUM")

    def get_all_paths(self):
        c = self.conn.cursor()
        paths = set()
        for table in['emb_cache', 'aes_cache', 'nsfw_cache', 'face_cache', 'tags_cache']:
            try:
                c.execute(f"SELECT DISTINCT path FROM {table}")
                paths.update([r[0] for r in c.fetchall() if r[0]])
            except: pass
        return list(paths)

    def remove_paths(self, paths_to_remove):
        if not paths_to_remove: return
        c = self.conn.cursor()
        tables = ['emb_cache', 'rerank_cache_v2', 'aes_cache', 'sim_cache', 'nsfw_cache', 'face_cache', 'tags_cache', 'prompt_cache', 'detailed_prompt_cache', 'ai_detection_cache']
        chunk_size = 900
        for i in range(0, len(paths_to_remove), chunk_size):
            chunk = paths_to_remove[i:i+chunk_size]
            placeholders = ','.join(['?'] * len(chunk))
            for table in tables:
                try:
                    c.execute(f"DELETE FROM {table} WHERE path IN ({placeholders})", chunk)
                except Exception:
                    pass
        self.conn.commit()

    def close(self): self.conn.close()

class FilesCache:
    FILE_NAME = 'dir_cache.json'
    def __init__(self):
        self._data = self._load_cache()
    
    def _load_cache(self):
        if not os.path.isfile(self.FILE_NAME): return {}
        try:
            with open(self.FILE_NAME, 'r', encoding='utf-8') as f: return json.load(f)
        except json.JSONDecodeError: return {}

    def save_cache(self):
        # Écriture compacte : pour de gros dossiers (75k+ fichiers) un indent=4
        # peut faire un fichier de plusieurs Mo et bloquer 1-2s à chaque save.
        with open(self.FILE_NAME, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, separators=(',', ':'))

    def list_files(self, directory):
        return self._data.get(directory, None)

class MediaCache:
    def __init__(self):
        self.enabled = False
        self.compress = False
        self.cache = {}
        self.last_image_error = ""

    def clear(self):
        self.cache.clear()
        gc.collect()

    def _compress_img(self, img):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def _decompress_img(self, bytes_data):
        return Image.open(io.BytesIO(bytes_data)).convert("RGB")

    def _get_bucket_size(self, w, h, max_dim, patch_size=28):
        scale = min(max_dim / w, max_dim / h)
        if scale > 1.0: scale = 1.0
        new_w = max(patch_size, int(round((w * scale) / patch_size) * patch_size))
        new_h = max(patch_size, int(round((h * scale) / patch_size) * patch_size))
        return new_w, new_h

    def get_image(self, path, max_dim):
        cache_key = (path, max_dim)
        if self.enabled and cache_key in self.cache:
            cached = self.cache[cache_key]
            return self._decompress_img(cached) if self.compress else cached
        self.last_image_error = ""
        for attempt in range(3):
            try:
                with Image.open(path) as pil_image:
                    pil_image.load()
                    image = pil_image.convert("RGB")
            except Exception as pil_error:
                try:
                    raw_data = np.fromfile(path, dtype=np.uint8)
                    if raw_data.size == 0:
                        raise ValueError("fichier vide")
                    decoded = cv2.imdecode(raw_data, cv2.IMREAD_UNCHANGED)
                    if decoded is None:
                        raise ValueError("cv2.imdecode a renvoye None")
                    if len(decoded.shape) == 2:
                        decoded = cv2.cvtColor(decoded, cv2.COLOR_GRAY2RGB)
                    elif decoded.shape[2] == 4:
                        decoded = cv2.cvtColor(decoded, cv2.COLOR_BGRA2RGB)
                    else:
                        decoded = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
                    image = Image.fromarray(decoded)
                except Exception as cv_error:
                    self.last_image_error = f"PIL={pil_error}; OpenCV={cv_error}"
                    image = None

            if image is not None:
                try:
                    new_w, new_h = self._get_bucket_size(image.width, image.height, max_dim)
                    resized = image.resize((new_w, new_h), Image.Resampling.BILINEAR)
                    if self.enabled:
                        self.cache[cache_key] = self._compress_img(resized) if self.compress else resized
                    return resized
                except Exception as resize_error:
                    self.last_image_error = f"resize={resize_error}"

            if attempt < 2:
                time.sleep(0.15)

        if not self.last_image_error:
            self.last_image_error = "lecture image impossible"
        return None

    def get_video_frames(self, path, max_dim, video_frames):
        cache_key = (path, max_dim, video_frames)
        if self.enabled and cache_key in self.cache:
            cached = self.cache[cache_key]
            return[self._decompress_img(b) for b in cached] if self.compress else cached
        try:
            frames =[]
            with av.open(path) as container:
                stream = container.streams.video[0]
                total_frames = stream.frames or 100
                num_extract = max(1, video_frames)
                step = max(1, total_frames // num_extract)
                target_indices = {min(i * step, total_frames - 1) for i in range(num_extract)}
                
                extracted =[]
                for i, frame in enumerate(container.decode(video=0)):
                    if i in target_indices:
                        extracted.append(frame.to_image().convert("RGB"))
                        target_indices.remove(i)
                    if not target_indices: break
                    
            if not extracted: return None
            while len(extracted) < num_extract: extracted.append(extracted[-1])
            new_w, new_h = self._get_bucket_size(extracted[0].width, extracted[0].height, max_dim)
            resized =[img.resize((new_w, new_h), Image.Resampling.BILINEAR) for img in extracted]
            if self.enabled:
                self.cache[cache_key] =[self._compress_img(img) for img in resized] if self.compress else resized
            return resized
        except Exception:
            return None

media_cache = MediaCache()

# ==========================================
# 2. ДВИЖОК ПОИСКА
# ==========================================
class SearchEngine:
    def __init__(self, log_callback, progress_callback, db_cache=None):
        self.log = log_callback
        self.progress = progress_callback
        self.files_cache = FilesCache()
        self.db_cache = db_cache if db_cache is not None else DatabaseCache()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model_kwargs = {"torch_dtype": torch.bfloat16, "attn_implementation": "sdpa"} if self.device == "cuda" else {}
        self.embedding_model = None
        self.current_emb_model_state = None
        self.emb_size = 512
        self.rerank_size = 800
        self.video_frames = 4
        self.quant_mode = "None"
        self.cancel_flag = False

    def cancel(self): self.cancel_flag = True

    def _download_model(self, model_name):
        local_dir = resolve_model_dir(model_name)
        self.log(f"Répertoire local modèle : {local_dir}")
        if not is_local_transformer_model_ready(local_dir):
            self.log(f"Modèle local incomplet ou absent, téléchargement vers : {local_dir}")
            snapshot_download(repo_id=model_name, local_dir=local_dir, local_dir_use_symlinks=False)
            if not is_local_transformer_model_ready(local_dir):
                raise RuntimeError(f"Le modèle téléchargé dans {local_dir} est incomplet.")
        else:
            self.log("Modèle local valide détecté, téléchargement ignoré.")
        return local_dir

    def _unload_embedding_model(self):
        if self.embedding_model is not None:
            self.log(f"Déchargement du modèle d'embeddings de la VRAM...")
            del self.embedding_model
            self.embedding_model = None
            self.current_emb_model_state = None
            gc.collect()
            if torch.cuda.is_available(): torch.cuda.empty_cache()

    def _apply_quantization(self, kwargs):
        if self.quant_mode != "None" and self.device == "cuda":
            kwargs["device_map"] = {"": self.device}
            try:
                from transformers import BitsAndBytesConfig
                if self.quant_mode == "8-bit":
                    kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
                elif self.quant_mode == "4-bit":
                    kwargs["quantization_config"] = BitsAndBytesConfig(
                        load_in_4bit=True,
                        bnb_4bit_compute_dtype=torch.bfloat16
                    )
            except ImportError:
                self.log("⚠️ ERREUR : Pour la quantification, installez : pip install bitsandbytes accelerate")
        return kwargs

    def _get_embedding_model(self, model_name):
        current_state = f"{model_name}_{self.quant_mode}"
        if self.current_emb_model_state != current_state or self.embedding_model is None:
            self._unload_embedding_model()
            kwargs = dict(self.model_kwargs)
            kwargs = self._apply_quantization(kwargs)
            
            local_model_path = self._download_model(model_name)
            self.log(f"Chargement du modèle {model_name} en VRAM...")
            self.embedding_model = SentenceTransformer(local_model_path, device=self.device, model_kwargs=kwargs, trust_remote_code=True)
            self.current_emb_model_state = current_state
        return self.embedding_model

    def _gather_files(self, dir_path, allowed_exts, force_rescan: bool = False):
        cached_list = None if force_rescan else self.files_cache.list_files(dir_path)
        all_supported = SUPPORTED_IMAGES + SUPPORTED_VIDEOS + SUPPORTED_TEXTS

        if cached_list is None:
            self.log(f"Indexation du système de fichiers : {dir_path}{' (rescan forcé)' if force_rescan else ''}...")
            files_list = []
            for root, dirs, files in os.walk(dir_path):
                if self.cancel_flag: break
                for file in files:
                    if file.lower().endswith(all_supported):
                        files_list.append(os.path.join(root, file))
            if not self.cancel_flag:
                self.files_cache._data[dir_path] = files_list
                self.files_cache.save_cache()
                self.log(f"Fichiers supportés trouvés : {len(files_list)}")
        else:
            # Walk disque (1 passe) pour détecter à la fois les disparus ET les nouveaux arrivants
            disk_set = set()
            walk_cancelled = False
            for root, dirs, files in os.walk(dir_path):
                if self.cancel_flag:
                    walk_cancelled = True
                    break
                for file in files:
                    if file.lower().endswith(all_supported):
                        disk_set.add(os.path.join(root, file))
            if walk_cancelled:
                # Annulation : on ne touche pas au cache, on rend la liste cache telle quelle
                files_list = cached_list
            else:
                cached_set = set(cached_list)
                removed = cached_set - disk_set
                added = disk_set - cached_set
                if removed or added:
                    # Préserve l'ordre des entrées existantes, ajoute les nouveautés à la fin
                    files_list = [f for f in cached_list if f in disk_set] + sorted(added)
                    if removed:
                        self.log(f"[dir_cache] {len(removed)} fichier(s) disparu(s) retiré(s) ({dir_path})")
                    if added:
                        self.log(f"[dir_cache] {len(added)} nouveau(x) fichier(s) détecté(s) ({dir_path})")
                    self.files_cache._data[dir_path] = files_list
                    self.files_cache.save_cache()
                else:
                    files_list = cached_list
        return [f for f in files_list if f.lower().endswith(allowed_exts)]

    def _load_and_prep_file(self, file_path, phase='embedding'):
        ext = os.path.splitext(file_path)[1].lower()
        size_val = self.emb_size if phase == 'embedding' else self.rerank_size
        if ext in SUPPORTED_IMAGES:
            img = media_cache.get_image(file_path, size_val)
            if img:
                return img, f"{img.width}x{img.height}", 1
            return None, None, 0
        elif ext in SUPPORTED_VIDEOS:
            frames = media_cache.get_video_frames(file_path, size_val, self.video_frames)
            if frames:
                new_w, new_h = frames[0].width, frames[0].height
                stacked = np.stack([np.array(f) for f in frames])
                return {"video": stacked}, f"{new_w}x{new_h}", self.video_frames
            return None, None, 0
        elif ext in SUPPORTED_TEXTS:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    text = f.read()[:2000]
                    if text.strip(): return text, "text", 1
            except: pass
        return None, None, 0

    def prepare_query(self, raw_query):
        if os.path.isfile(raw_query):
            doc_emb, _, _ = self._load_and_prep_file(raw_query, phase='embedding')
            doc_rerank, _, _ = self._load_and_prep_file(raw_query, phase='rerank')
            return doc_emb, doc_rerank
        return raw_query, raw_query

    def build_cache(self, dir_path, emb_model_name, batch_size, allowed_exts, override_files=None):
        """Pre-cache tous les medias d'un dossier (sans lancer de recherche)."""
        self.cancel_flag = False
        files_list = self._gather_files(dir_path, allowed_exts) if override_files is None else[f for f in override_files if f.lower().endswith(allowed_exts)]
        cache_key = emb_model_name if self.emb_size == 512 else f"{emb_model_name}_{self.emb_size}"
        
        paths_to_compute =[]
        for i, fp in enumerate(files_list):
            if self.cancel_flag: break
            if self.db_cache.get_image_features(cache_key, fp) is None:
                paths_to_compute.append(fp)
                
        if not paths_to_compute or self.cancel_flag:
            self.log("Cache d'embeddings entièrement à jour.")
            return

        # LAZY LOADING: Загружаем модель только если есть новые файлы для обработки
        model = self._get_embedding_model(emb_model_name)

        self.log(f"Mise en cache : traitement de {len(paths_to_compute)} nouveaux fichiers...")
        processed_count, total = 0, len(paths_to_compute)
        preload_chunk = max(64, batch_size * 4) 
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4) * 2)) as executor:
            for i in range(0, total, preload_chunk):
                if self.cancel_flag: break
                chunk_paths = paths_to_compute[i:i + preload_chunk]
                futures = {executor.submit(self._load_and_prep_file, p, 'embedding'): p for p in chunk_paths}
                
                buckets = defaultdict(list)
                for fut in concurrent.futures.as_completed(futures):
                    path = futures[fut]
                    doc, size_key, weight = fut.result()
                    if doc is not None: buckets[size_key].append((path, doc, weight))
                
                for size_key, items in buckets.items():
                    if self.cancel_flag: break
                    c_paths, c_docs = [],[]
                    c_weight = 0
                    
                    for path, doc, weight in items:
                        if c_weight + weight > batch_size and len(c_docs) > 0:
                            try:
                                feats_batch = model.encode(c_docs, batch_size=len(c_docs), convert_to_tensor=True).cpu()
                                for p, feats in zip(c_paths, feats_batch):
                                    self.db_cache.save_image_features(cache_key, p, feats)
                            except Exception as e: self.log(f"Erreur lot embeddings : {e}")
                            
                            processed_count += len(c_paths)
                            self.progress(processed_count / total, f"Cache embeddings ({processed_count}/{total})...")
                            c_paths, c_docs = [],[]
                            c_weight = 0
                            
                        c_paths.append(path)
                        c_docs.append(doc)
                        c_weight += weight
                        
                    if len(c_docs) > 0 and not self.cancel_flag:
                        try:
                            feats_batch = model.encode(c_docs, batch_size=len(c_docs), convert_to_tensor=True).cpu()
                            for p, feats in zip(c_paths, feats_batch):
                                self.db_cache.save_image_features(cache_key, p, feats)
                        except Exception as e: self.log(f"Erreur lot embeddings : {e}")
                            
                        processed_count += len(c_paths)
                        self.progress(processed_count / total, f"Cache embeddings ({processed_count}/{total})...")

    def phase1_recall(self, dir_path, raw_query, query_input, top_k, emb_model_name, batch_size, allowed_exts):
        self.cancel_flag = False
        files_list = self._gather_files(dir_path, allowed_exts)
        
        results_phase1 =[]
        cache_key = emb_model_name if self.emb_size == 512 else f"{emb_model_name}_{self.emb_size}"
        cached_sims = self.db_cache.get_query_sims(cache_key, raw_query)
        
        self.log(f"Filtrage de {len(files_list)} fichiers via le cache...")
        paths_needing_sims =[]
        paths_needing_features =[]
        
        for i, file_path in enumerate(files_list):
            if self.cancel_flag: break
            if file_path in cached_sims:
                results_phase1.append((cached_sims[file_path], file_path))
            else:
                paths_needing_sims.append(file_path)
                
            if i % 500 == 0: 
                prog = 0.1 * (i / max(1, len(files_list)))
                self.progress(prog, f"Lecture cache ({i}/{len(files_list)})...")
                
        # ⚡ Если все результаты уже в кэше — возвращаем мгновенно, не трогая VRAM!
        if not paths_needing_sims or self.cancel_flag:
            self.log("⚡ Requête entièrement en cache ! Chargement du modèle ignoré.")
            results_phase1.sort(key=lambda x: x[0], reverse=True)
            self.progress(0.8, "Recherche terminée.") 
            return results_phase1[:top_k]

        # Иначе загружаем модель для создания вектора (эмбеддинга) запроса
        model = self._get_embedding_model(emb_model_name)
        self.log("Conversion de la requête en embedding...")
        query_emb = model.encode(query_input, convert_to_tensor=True).cpu()

        # Проверяем, есть ли уже фичи картинок для тех файлов, где нет симиларов
        sims_to_save_paths = []
        sims_to_save_scores =[]
        
        for file_path in paths_needing_sims:
            if self.cancel_flag: break
            features = self.db_cache.get_image_features(cache_key, file_path)
            if features is not None:
                sim = float(util.cos_sim(query_emb, features).item())
                results_phase1.append((sim, file_path))
                
                # Собираем данные в списки вместо сохранения по одному
                sims_to_save_paths.append(file_path)
                sims_to_save_scores.append(sim)
            else: 
                paths_needing_features.append(file_path)

        # Сохраняем все вычисленные симилары ОДНИМ запросом к диску (ускорение в 100+ раз)
        if sims_to_save_paths and not self.cancel_flag:
            self.db_cache.save_query_sims(cache_key, raw_query, sims_to_save_paths, sims_to_save_scores)

        if not paths_needing_features or self.cancel_flag:
            results_phase1.sort(key=lambda x: x[0], reverse=True)
            self.progress(0.8, "Recherche terminée.") 
            return results_phase1[:top_k]

        # Если дошли сюда, значит есть файлы, для которых нужно инференсить фичи
        self.log(f"Traitement IA des nouveaux fichiers : {len(paths_needing_features)}...")
        processed_count, total = 0, len(paths_needing_features)
        preload_chunk = max(64, batch_size * 4) 
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4) * 2)) as executor:
            for i in range(0, total, preload_chunk):
                if self.cancel_flag: break
                chunk_paths = paths_needing_features[i:i + preload_chunk]
                futures = {executor.submit(self._load_and_prep_file, p, 'embedding'): p for p in chunk_paths}
                
                buckets = defaultdict(list)
                for fut in concurrent.futures.as_completed(futures):
                    path = futures[fut]
                    doc, size_key, weight = fut.result()
                    if doc is not None: 
                        buckets[size_key].append((path, doc, weight))
                    else:
                        self.db_cache.save_query_sims(cache_key, raw_query, [path], [0.0])
                
                for size_key, items in buckets.items():
                    if self.cancel_flag: break
                    c_paths, c_docs = [],[]
                    c_weight = 0
                    
                    for path, doc, weight in items:
                        if c_weight + weight > batch_size and len(c_docs) > 0:
                            try:
                                feats_batch = model.encode(c_docs, batch_size=len(c_docs), convert_to_tensor=True).cpu()
                                sims_to_save =[]
                                for p, feats in zip(c_paths, feats_batch):
                                    self.db_cache.save_image_features(cache_key, p, feats)
                                    sim = float(util.cos_sim(query_emb, feats).item())
                                    sims_to_save.append(sim)
                                    results_phase1.append((sim, p))
                                self.db_cache.save_query_sims(cache_key, raw_query, c_paths, sims_to_save)
                            except Exception as e: self.log(f"Erreur lot : {e}")
                            
                            processed_count += len(c_paths)
                            self.progress(0.1 + 0.7 * (processed_count / total), f"Inférence ({processed_count}/{total})...")
                            c_paths, c_docs = [],[]
                            c_weight = 0
                            
                        c_paths.append(path)
                        c_docs.append(doc)
                        c_weight += weight
                        
                    if len(c_docs) > 0 and not self.cancel_flag:
                        try:
                            feats_batch = model.encode(c_docs, batch_size=len(c_docs), convert_to_tensor=True).cpu()
                            sims_to_save =[]
                            for p, feats in zip(c_paths, feats_batch):
                                self.db_cache.save_image_features(cache_key, p, feats)
                                sim = float(util.cos_sim(query_emb, feats).item())
                                sims_to_save.append(sim)
                                results_phase1.append((sim, p))
                            self.db_cache.save_query_sims(cache_key, raw_query, c_paths, sims_to_save)
                        except Exception as e: self.log(f"Erreur lot (reste) : {e}")
                            
                        processed_count += len(c_paths)
                        self.progress(0.1 + 0.7 * (processed_count / total), f"Inférence ({processed_count}/{total})...")

        results_phase1.sort(key=lambda x: x[0], reverse=True)
        return results_phase1[:top_k]

    def phase2_rerank(self, raw_query, query_input, top_candidates, min_score, rerank_model_name):
        if not top_candidates or self.cancel_flag: return top_candidates
        cache_key = rerank_model_name if self.rerank_size == 800 else f"{rerank_model_name}_{self.rerank_size}"
        
        final_results =[]
        docs_to_compute, paths_to_compute =[],[]
        for i, (score, fp) in enumerate(top_candidates):
            if self.cancel_flag: break
            cached_score = self.db_cache.get_rerank_score(cache_key, raw_query, fp)
            if cached_score is not None:
                if cached_score >= min_score: final_results.append((cached_score, fp))
            else:
                doc, _, _ = self._load_and_prep_file(fp, 'rerank') 
                if doc is not None:
                    docs_to_compute.append(doc)
                    paths_to_compute.append(fp)
                    
        if docs_to_compute and not self.cancel_flag:
            self._unload_embedding_model() # LAZY UNLOAD: освобождаем память только если нужен Reranker
            self.log(f"Reranker : traitement approfondi de {len(docs_to_compute)} candidats...")
            kwargs = dict(self.model_kwargs)
            kwargs = self._apply_quantization(kwargs)
            
            local_path = self._download_model(rerank_model_name)
            reranker = CrossEncoder(local_path, device=self.device, model_kwargs=kwargs, trust_remote_code=True)
            chunk_size = 4
            processed, len_total = 0, len(docs_to_compute)
            
            for i in range(0, len_total, chunk_size):
                if self.cancel_flag: break
                c_docs, c_paths = docs_to_compute[i:i+chunk_size], paths_to_compute[i:i+chunk_size]
                rankings = reranker.rank(query_input, c_docs, batch_size=len(c_docs))
                for rank in rankings:
                    s = float(rank['score'])
                    fp = c_paths[rank['corpus_id']]
                    self.db_cache.save_rerank_score(cache_key, raw_query, fp, s)
                    if s >= min_score: final_results.append((s, fp))
                    
                processed += len(c_docs)
                self.progress(0.8 + 0.2 * (processed / len_total), f"Rerank ({processed}/{len_total})...")
                
            del reranker
            gc.collect()
            if torch.cuda.is_available(): torch.cuda.empty_cache()

        final_results.sort(key=lambda x: x[0], reverse=True)
        return final_results

# ==========================================
# 3. ДВИЖКИ ЭСТЕТИКИ, NSFW, ЛИЦ И ТЕГОВ
# ==========================================
def _compute_sharpness(path: str, max_dim: int = 512) -> float:
    """Laplacian variance of greyscale image. Higher = sharper. Returns 0.0 on error."""
    try:
        img = Image.open(path).convert('L')
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        arr = np.array(img, dtype=np.float32)
        lap = (arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:] - 4 * arr[1:-1, 1:-1])
        return float(lap.var())
    except Exception:
        return 0.0


class AestheticEngine:
    def __init__(self, search_engine):
        self.se = search_engine
        self.db_cache = search_engine.db_cache
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.dtype = torch.bfloat16 if self.device == "cuda" else torch.float32
        self.model = None
        self.preprocessor = None
        self.batch_size = 16
        self.max_dim = 512
        self.video_frames = 4
        self.quant_mode = "None"
        self.current_model_state = None

    def load_model(self):
        current_state = f"v2_5_{self.quant_mode}"
        if self.model is None or self.current_model_state != current_state:
            self.unload()
            state.add_log(f"Chargement du modele Aesthetic Predictor sur {self.device}...")
            state.add_log(f"Répertoire cache esthétique : {os.path.join(current_dir, 'models')}")
            if is_local_aesthetic_ready():
                state.add_log("Cache esthétique local valide détecté.")
            else:
                state.add_log("Cache esthétique incomplet ou absent, Hugging Face complètera dans le dossier models.")
            # Принудительная скачка модели в локальную папку моделей
            kwargs = {
                "low_cpu_mem_usage": True, 
                "trust_remote_code": True,
                "cache_dir": os.path.join(current_dir, "models"),
                "torch_dtype": self.dtype
            }
            if self.device == "cuda":
                kwargs["attn_implementation"] = "sdpa"
                
            if self.quant_mode != "None" and self.device == "cuda":
                kwargs["device_map"] = {"": self.device}
                try:
                    from transformers import BitsAndBytesConfig
                    # Ignore la tete personnalisee 'layers', chargee separement via load_state_dict.
                    bnb_kwargs = {"llm_int8_skip_modules": ["layers"]} 
                    
                    if self.quant_mode == "8-bit":
                        kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True, **bnb_kwargs)
                    elif self.quant_mode == "4-bit":
                        kwargs["quantization_config"] = BitsAndBytesConfig(
                            load_in_4bit=True,
                            bnb_4bit_compute_dtype=self.dtype,
                            **bnb_kwargs
                        )
                except ImportError:
                    state.add_log("⚠️ ERREUR : Pour la quantification, installez : pip install bitsandbytes accelerate")
                
            self.model, self.preprocessor = convert_v2_5_from_siglip(**kwargs)
            
            if self.quant_mode == "None":
                self.model = self.model.to(self.dtype).to(self.device)
            else:
                # Перекидываем пропущенную голову в нужный формат вручную
                if hasattr(self.model, "layers"):
                    self.model.layers.to(self.dtype).to(self.device)
                elif hasattr(self.model, "mlp"):
                    self.model.mlp.to(self.dtype).to(self.device)
                
            self.model.eval()
            self.current_model_state = current_state

    def unload(self):
        if self.model is not None:
            state.add_log(f"Déchargement Aesthetic Predictor de la VRAM...")
            del self.model
            del self.preprocessor
            self.model = None
            self.preprocessor = None
            self.current_model_state = None
            gc.collect()
            if torch.cuda.is_available(): torch.cuda.empty_cache()

    def evaluate_media(self, directory_path, allowed_exts, override_files=None):
        all_files = self.se._gather_files(directory_path, allowed_exts) if override_files is None else[f for f in override_files if f.lower().endswith(allowed_exts)]
        image_paths =[p for p in all_files if p.lower().endswith(SUPPORTED_IMAGES)]
        video_paths =[p for p in all_files if p.lower().endswith(SUPPORTED_VIDEOS)]
        
        state.add_log(f"Trouvé pour évaluation : {len(image_paths)} images, {len(video_paths)} vidéos.")
        results =[]
        cache_key_img = "v2_5_siglip"
        cache_key_vid = "v2_5_siglip_vid_" + str(self.video_frames)

        # Подготовка: фильтрация через кэш (LAZY LOADING)
        images_to_process =[]
        for p in image_paths:
            cached = self.db_cache.get_aesthetic_score(cache_key_img, p)
            if cached is not None:
                results.append((cached[0], p, cached[1]))
            else:
                images_to_process.append(p)
                
        videos_to_process =[]
        for p in video_paths:
            cached = self.db_cache.get_aesthetic_score(cache_key_vid, p)
            if cached is not None:
                results.append((cached[0], p, cached[1]))
            else:
                videos_to_process.append(p)
                
        # Если все есть в базе, модель даже не грузим в VRAM
        if images_to_process or videos_to_process:
            self.load_model()
        else:
            results.sort(key=lambda x: x[0], reverse=True)
            return results

        # --- ОБРАБОТКА ИЗОБРАЖЕНИЙ ---
        batch_images, batch_paths =[],[]
        for i, img_path in enumerate(images_to_process):
            if not state.is_processing: break
            state.status_text = f"Préparation photo : {Path(img_path).name} ({i+1}/{len(images_to_process)})"
            try:
                image = media_cache.get_image(img_path, self.max_dim)
                if image:
                    batch_images.append(image)
                    batch_paths.append(img_path)
                else:
                    self.db_cache.save_aesthetic_score(cache_key_img, img_path, 0.0, 0.0)
                    results.append((0.0, img_path, 0.0))
            except Exception as e: 
                state.add_log(f"Erreur {img_path} : {e}")
                self.db_cache.save_aesthetic_score(cache_key_img, img_path, 0.0, 0.0)
                results.append((0.0, img_path, 0.0))
                
            if len(batch_images) >= self.batch_size or (i == len(images_to_process) - 1 and batch_images):
                state.progress = (i + 1) / max(1, len(images_to_process))
                state.status_text = f"Inférence photos ({i+1}/{len(images_to_process)})..."
                try:
                    pixel_values = self.preprocessor(images=batch_images, return_tensors="pt").pixel_values.to(self.dtype).to(self.device)
                    with torch.inference_mode():
                        logits = self.model(pixel_values).logits.flatten().float().cpu().tolist()
                    for score, p in zip(logits, batch_paths):
                        self.db_cache.save_aesthetic_score(cache_key_img, p, score, score)
                        results.append((score, p, score))
                except Exception as e: state.add_log(f"Erreur d'inférence : {e}")
                batch_images, batch_paths = [],[]

        # --- ОБРАБОТКА ВИДЕО ---
        batch_images, batch_frame_counts, batch_paths =[], [],[]
        for i, vid_path in enumerate(videos_to_process):
            time.sleep(0.002)
            if not state.is_processing: break
            state.status_text = f"Préparation vidéo : {Path(vid_path).name} ({i+1}/{len(videos_to_process)})"
            try:
                frames = media_cache.get_video_frames(vid_path, self.max_dim, self.video_frames)
                if frames:
                    batch_images.extend(frames)
                    batch_paths.append(vid_path)
                    batch_frame_counts.append(len(frames))
                else:
                    self.db_cache.save_aesthetic_score(cache_key_vid, vid_path, 0.0, 0.0)
                    results.append((0.0, vid_path, 0.0))
            except Exception as e: 
                state.add_log(f"Erreur lecture {vid_path} : {e}")
                self.db_cache.save_aesthetic_score(cache_key_vid, vid_path, 0.0, 0.0)
                results.append((0.0, vid_path, 0.0))
                
            if len(batch_images) >= self.batch_size or (i == len(videos_to_process) - 1 and batch_images):
                state.progress = (i + 1) / max(1, len(videos_to_process))
                state.status_text = f"Inférence vidéos ({i+1}/{len(videos_to_process)})..."
                try:
                    all_scores =[]
                    for k in range(0, len(batch_images), self.batch_size):
                        chunk = batch_images[k:k+self.batch_size]
                        pixel_values = self.preprocessor(images=chunk, return_tensors="pt").pixel_values.to(self.dtype).to(self.device)
                        with torch.inference_mode():
                            logits = self.model(pixel_values).logits.flatten().float().cpu().tolist()
                        all_scores.extend(logits)
                        
                    idx = 0
                    for path, count in zip(batch_paths, batch_frame_counts):
                        vid_scores = all_scores[idx : idx + count]
                        idx += count
                        if vid_scores:
                            avg_s = sum(vid_scores) / len(vid_scores)
                            max_s = max(vid_scores)
                            self.db_cache.save_aesthetic_score(cache_key_vid, path, avg_s, max_s)
                            results.append((avg_s, path, max_s))
                except Exception as e: state.add_log(f"Erreur d'inférence : {e}")
                batch_images, batch_frame_counts, batch_paths = [],[],[]

        results.sort(key=lambda x: x[0], reverse=True)
        return results

class NsfwEngine:
    def __init__(self, search_engine):
        self.se = search_engine
        self.db_cache = search_engine.db_cache
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.dtype = torch.bfloat16 if self.device == "cuda" else torch.float32
        self.model = None
        self.processor = None
        self.current_model_name = None
        self.batch_size = 16
        self.max_dim = 512
        self.video_frames = 4
        self.quant_mode = "None"
        self.current_model_state = None
        self._portrait_cascade = None
        self._portrait_cascade_alt = None
        self._portrait_cascade_alt3 = None
        self._portrait_cascade_profile = None
        self._hog_person = None
        self._body_detector_enabled = not (sys.platform == 'win32' and sys.version_info >= (3, 13))
        self._body_detector_warning_logged = False

    def _nsfw_checkpoint_path(self, directory_path, model_name):
        safe_model = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '_' for ch in model_name.split('/')[-1])
        return Path(directory_path) / f'.organizator_nsfw_{safe_model}_{self.video_frames}_{NSFW_CACHE_VERSION}.json'

    def _serialize_nsfw_result(self, item):
        danger, path, label, details = item
        return {
            'danger': float(danger),
            'path': str(path),
            'label': str(label),
            'details': details if isinstance(details, dict) else {},
        }

    def _atomic_write_json(self, file_path, payload):
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False, dir=str(file_path.parent), prefix=file_path.stem + '.', suffix='.tmp') as tmp_file:
            json.dump(payload, tmp_file, indent=2, ensure_ascii=False)
            tmp_name = tmp_file.name
        os.replace(tmp_name, file_path)

    def _load_nsfw_checkpoint(self, directory_path, model_name):
        checkpoint_path = self._nsfw_checkpoint_path(directory_path, model_name)
        if not checkpoint_path.exists():
            return None
        try:
            with open(checkpoint_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if data.get('schema') != 'organizador.nsfw.scan.checkpoint.v1':
                return None
            if data.get('source_dir') != str(Path(directory_path)):
                return None
            if data.get('model') != model_name:
                return None
            if int(data.get('video_frames', self.video_frames)) != int(self.video_frames):
                return None
            if data.get('cache_version') != NSFW_CACHE_VERSION:
                return None

            results = []
            for item in data.get('results', []):
                if not isinstance(item, dict):
                    continue
                try:
                    details = item.get('details', {})
                    results.append((
                        float(item.get('danger', 0.0)),
                        str(item.get('path', '')),
                        str(item.get('label', '')),
                        details if isinstance(details, dict) else {},
                    ))
                except Exception:
                    continue

            return {
                'processed_count': int(data.get('processed_count', len(results))),
                'total_count': int(data.get('total_count', len(results))),
                'results': results,
                'checkpoint_path': str(checkpoint_path),
                'completed': bool(data.get('completed', False)),
            }
        except Exception as e:
            state.add_log(f"[NSFW] Impossible de relire le checkpoint: {e}")
            return None

    def _write_nsfw_checkpoint(self, directory_path, model_name, threshold, results, total_count, completed=False):
        checkpoint_path = self._nsfw_checkpoint_path(directory_path, model_name)
        payload = {
            'schema': 'organizador.nsfw.scan.checkpoint.v1',
            'source_dir': str(Path(directory_path)),
            'model': model_name,
            'video_frames': int(self.video_frames),
            'cache_version': NSFW_CACHE_VERSION,
            'threshold': float(threshold),
            'processed_count': len(results),
            'total_count': int(total_count),
            'completed': bool(completed),
            'written_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'results': [self._serialize_nsfw_result(item) for item in results],
        }
        self._atomic_write_json(checkpoint_path, payload)

    def load_model(self, model_name):
        current_state = f"{model_name}_{self.quant_mode}"
        if self.model is None or self.current_model_state != current_state:
            self.unload()
            state.add_log(f"Chargement du modèle NSFW {model_name} sur {self.device}...")
            
            local_dir = resolve_model_dir(model_name)
            state.add_log(f"Répertoire local NSFW : {local_dir}")
            model_ready = is_local_model_ready(
                local_dir,
                required_files=["config.json", "preprocessor_config.json"],
                required_any=["model.safetensors", "pytorch_model.bin", "model.safetensors.index.json", "pytorch_model.bin.index.json"],
            )
            if not model_ready:
                state.add_log(f"Modèle local incomplet ou absent, téléchargement vers : {local_dir}")
                snapshot_download(repo_id=model_name, local_dir=local_dir, local_dir_use_symlinks=False)
                model_ready = is_local_model_ready(
                    local_dir,
                    required_files=["config.json", "preprocessor_config.json"],
                    required_any=["model.safetensors", "pytorch_model.bin", "model.safetensors.index.json", "pytorch_model.bin.index.json"],
                )
                if not model_ready:
                    raise RuntimeError(f"Le modèle NSFW téléchargé dans {local_dir} est incomplet.")
            else:
                state.add_log("Modèle NSFW local valide détecté, téléchargement ignoré.")

            if any(name in model_name.lower() for name in ["strangerguardhf", "prithivmlmods"]):
                for item in os.listdir(local_dir):
                    if item.startswith("checkpoint-"):
                        chk_path = os.path.join(local_dir, item)
                        if os.path.isdir(chk_path):
                            try:
                                shutil.rmtree(chk_path)
                                state.add_log(f"Checkpoint inutile supprimé : {item}")
                            except Exception as e:
                                state.add_log(f"Impossible de supprimer {item} : {e}")
            
            kwargs = {
                "torch_dtype": self.dtype,
            }
            if self.quant_mode != "None" and self.device == "cuda":
                kwargs["device_map"] = {"": self.device}
                try:
                    from transformers import BitsAndBytesConfig
                    if self.quant_mode == "8-bit":
                        kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
                    elif self.quant_mode == "4-bit":
                        kwargs["quantization_config"] = BitsAndBytesConfig(
                            load_in_4bit=True,
                            bnb_4bit_compute_dtype=self.dtype
                        )
                except ImportError:
                    state.add_log("⚠️ ERREUR : Pour la quantification, installez : pip install bitsandbytes accelerate")

            self.processor = AutoImageProcessor.from_pretrained(local_dir)
            if "siglip" in model_name.lower():
                self.model = SiglipForImageClassification.from_pretrained(local_dir, **kwargs)
            else:
                self.model = AutoModelForImageClassification.from_pretrained(local_dir, **kwargs)
                
            if self.quant_mode == "None":
                self.model.to(self.dtype).to(self.device)
            self.model.eval()
            self.current_model_name = model_name
            self.current_model_state = current_state

    def unload(self):
        if self.model is not None:
            state.add_log(f"Déchargement du modèle NSFW {self.current_model_name} de la VRAM...")
            del self.model
            del self.processor
            self.model = None
            self.processor = None
            self.current_model_name = None
            self.current_model_state = None
            gc.collect()
            if torch.cuda.is_available(): torch.cuda.empty_cache()

    def _is_portrait_image(self, image):
        """Detecte un portrait via cascades Haar: frontal + profil gauche + profil droit."""
        try:
            data = cv2.data.haarcascades
            if self._portrait_cascade is None:
                self._portrait_cascade = cv2.CascadeClassifier(
                    os.path.join(data, 'haarcascade_frontalface_default.xml'))
            if self._portrait_cascade_alt is None:
                self._portrait_cascade_alt = cv2.CascadeClassifier(
                    os.path.join(data, 'haarcascade_frontalface_alt.xml'))
            if self._portrait_cascade_alt3 is None:
                self._portrait_cascade_alt3 = cv2.CascadeClassifier(
                    os.path.join(data, 'haarcascade_frontalface_alt2.xml'))
            if self._portrait_cascade_profile is None:
                self._portrait_cascade_profile = cv2.CascadeClassifier(
                    os.path.join(data, 'haarcascade_profileface.xml'))

            gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
            # Slightly stricter settings reduce landscape false positives.
            detect_params = dict(scaleFactor=1.08, minNeighbors=3, minSize=(28, 28))

            all_faces = []
            for cascade in (
                self._portrait_cascade,
                self._portrait_cascade_alt,
                self._portrait_cascade_alt3,
                self._portrait_cascade_profile,
            ):
                if cascade is None or cascade.empty():
                    continue
                faces = cascade.detectMultiScale(gray, **detect_params)
                all_faces.extend(faces)

            # Check mirrored image for right-facing profiles
            gray_flip = cv2.flip(gray, 1)
            if not (self._portrait_cascade_profile is None or self._portrait_cascade_profile.empty()):
                all_faces.extend(self._portrait_cascade_profile.detectMultiScale(gray_flip, **detect_params))

            return all_faces
        except Exception:
            return []

    def _has_person_body(self, image):
        """Detecte un corps humain (meme sans visage) avec HOG people detector."""
        if not self._body_detector_enabled:
            if not self._body_detector_warning_logged:
                self._body_detector_warning_logged = True
                state.add_log("[NSFW] Détecteur corps OpenCV désactivé sur Windows/Python 3.13 pour éviter un crash natif.")
            return False
        try:
            if self._hog_person is None:
                self._hog_person = cv2.HOGDescriptor()
                self._hog_person.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

            bgr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            h, w = bgr.shape[:2]
            if h <= 0 or w <= 0:
                return False

            # Keep detection stable and reasonably fast.
            scale = min(1.0, 640.0 / max(h, w))
            if scale < 1.0:
                bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

            rects, _weights = self._hog_person.detectMultiScale(
                bgr,
                winStride=(8, 8),
                padding=(8, 8),
                scale=1.05,
            )
            return len(rects) > 0
        except Exception:
            return False

    def _is_face_only(self, image):
        """Retourne True pour un vrai gros plan visage (pas une personne/corps)."""
        try:
            faces = self._is_portrait_image(image)
            if not faces:
                return False
            if self._has_person_body(image):
                return False
            
            # Calcule le ratio de face vs image
            img_array = np.array(image)
            img_height, img_width = img_array.shape[:2]
            img_area = img_height * img_width
            
            max_face_area = max((w * h for _x, _y, w, h in faces), default=0)
            face_ratio = max_face_area / img_area if img_area > 0 else 0.0
            
            # Use largest face only; avoids multi-cascade overlap overcount.
            return face_ratio > 0.40
        except Exception:
            return False

    def _has_person(self, image):
        """Vérifier si l'image contient une personne (visage OU corps)."""
        faces = self._is_portrait_image(image)
        if len(faces) > 0:
            return True
        if not self._body_detector_enabled:
            # Fallback conservateur: on laisse le modèle NSFW analyser l'image
            # plutôt que d'étiqueter SAIN via un préfiltre instable.
            return True
        return self._has_person_body(image)

    def compute_danger(self, details, nsfw_mode=None):
        """
        danger = max(explicit_labels) + 0.3 * max(neutral_labels)
        
        'Enticing or Sensual' is NEUTRAL: it boosts danger slightly if explicit content
        is also present, but can't trigger EXPLICITE alone (a sunset at 50% enticing
        with 0% pornography gives danger = 0 + 0.3*0.50 = 0.15 → SAIN at threshold 0.42).
        """
        safe_labels    = {'safe', 'sfw', 'normal', 'general', 'neutral', 'drawing',
                          'safe_content', 'anime picture', 'anime'}
        neutral_labels = {'enticing or sensual', 'suggestive'}

        explicit_scores = [prob for lbl, prob in details.items()
                           if lbl.lower() not in safe_labels
                           and lbl.lower() not in neutral_labels
                           and not lbl.startswith('_')]
        neutral_scores  = [prob for lbl, prob in details.items()
                           if lbl.lower() in neutral_labels]

        explicit_max = max(explicit_scores, default=0.0)
        neutral_max  = max(neutral_scores, default=0.0)

        # Neutral alone can't exceed threshold; only boosts when explicit content present
        danger = explicit_max + 0.3 * neutral_max
        return min(1.0, max(0.0, danger))

    def present_label(self, details, portrait_guard=False):
        # Respect manual override when user corrected the category from the UI.
        manual_tier = str((details or {}).get('_manual_tier', '')).upper() if isinstance(details, dict) else ''
        if manual_tier in {'SAIN', 'SENSUEL', 'EXPLICITE'}:
            return manual_tier
        return classify_nsfw_tier(details, state.nsfw_threshold, portrait_guard)

    def evaluate_media(self, directory_path, model_name, allowed_exts, override_files=None, nsfw_mode=None):
        if override_files is None:
            # NSFW scan uses a fresh recursive walk to avoid stale dir_cache misses.
            all_files = []
            for root, _dirs, files in os.walk(directory_path):
                if self.se.cancel_flag:
                    break
                for file in files:
                    full_path = os.path.join(root, file)
                    if full_path.lower().endswith(allowed_exts):
                        all_files.append(full_path)
        else:
            all_files = [f for f in override_files if f.lower().endswith(allowed_exts)]
        image_paths =[p for p in all_files if p.lower().endswith(SUPPORTED_IMAGES)]
        video_paths =[p for p in all_files if p.lower().endswith(SUPPORTED_VIDEOS)]
        
        state.add_log(f"Trouvé pour le détecteur NSFW : {len(image_paths)} images, {len(video_paths)} vidéos.")
        results =[]
        checkpoint = self._load_nsfw_checkpoint(directory_path, model_name)
        checkpoint_by_path = {}
        if checkpoint and checkpoint.get('results'):
            results = list(checkpoint['results'])
            checkpoint_by_path = {p: (d, p, l, dt) for d, p, l, dt in results if p}
            state.add_log(f"[NSFW] Checkpoint repris: {len(results)} résultat(s) déjà sauvegardé(s)")
        # Cache key: model + frames + version (threshold affects display, not raw scores)
        cache_key = f"{model_name}_{self.video_frames}_{NSFW_CACHE_VERSION}"
        total_candidates = len(image_paths) + len(video_paths)
        checkpoint_every = max(4, self.batch_size // 2)
        checkpoint_dirty = 0

        def safe_write_checkpoint(completed=False):
            nonlocal checkpoint_dirty
            try:
                self._write_nsfw_checkpoint(
                    directory_path,
                    model_name,
                    getattr(state, 'nsfw_threshold', NSFW_THRESHOLD),
                    results,
                    total_candidates,
                    completed=completed,
                )
                checkpoint_dirty = 0
            except Exception as e:
                state.add_log(f"[NSFW] Checkpoint non écrit: {e}")

        def record_result(item):
            nonlocal checkpoint_dirty
            results.append(item)
            checkpoint_dirty += 1
            if checkpoint_dirty >= checkpoint_every:
                safe_write_checkpoint(completed=False)

        def flush_checkpoint(completed=False):
            safe_write_checkpoint(completed=completed)

        # Подготовка: фильтрация через кэш (LAZY LOADING)
        images_to_process =[]
        for p in image_paths:
            cached = self.db_cache.get_nsfw_score(cache_key, p)
            if p in checkpoint_by_path:
                continue
            if cached is not None:
                details_dict = json.loads(cached[2]) if cached[2] else {}
                details_dict.setdefault('_raw_top_label', cached[0])
                portrait_guard = bool(details_dict.get('_portrait_guard', 0.0))
                record_result((cached[1], p, self.present_label(details_dict, portrait_guard), details_dict))
            else:
                images_to_process.append(p)

        videos_to_process =[]
        for p in video_paths:
            cached = self.db_cache.get_nsfw_score(cache_key, p)
            if p in checkpoint_by_path:
                continue
            if cached is not None:
                details_dict = json.loads(cached[2]) if cached[2] else {}
                details_dict.setdefault('_raw_top_label', cached[0])
                record_result((cached[1], p, self.present_label(details_dict, False), details_dict))
            else:
                videos_to_process.append(p)

        cached_count = (len(image_paths) - len(images_to_process)) + (len(video_paths) - len(videos_to_process))
        if cached_count > 0:
            state.add_log(f"[NSFW] Cache réutilisé: {cached_count} fichiers")
        state.add_log(f"[NSFW] À analyser: {len(images_to_process)} images, {len(videos_to_process)} vidéos")
                
        # Если все есть в базе, модель не грузим в VRAM
        if images_to_process or videos_to_process:
            self.load_model(model_name)
        else:
            results.sort(key=lambda x: x[0], reverse=True)
            return results

        # --- ИЗОБРАЖЕНИЯ ---
        batch_images, batch_paths, batch_orig_dims = [], [], []
        for i, img_path in enumerate(images_to_process):
            if not state.is_processing: break
            state.status_text = f"NSFW Photo : {Path(img_path).name} ({i+1}/{len(images_to_process)})"
            try:
                image = media_cache.get_image(img_path, self.max_dim)
                if image:
                    # PRÉ-FILTRE 1: vérifier s'il y a une personne dans l'image
                    if not self._has_person(image):
                        # Pas de personne → SAIN d'office, skip NSFW model
                        state.add_log(f"[NSFW] Pas de personne: {Path(img_path).name} → SAIN (paysage/objet)")
                        self.db_cache.save_nsfw_score(cache_key, img_path, "no_person", 0.0, {"_auto_sain": 1.0})
                        record_result((0.0, img_path, 'SAIN', {"_auto_sain": 1.0}))
                        continue
                    
                    # PRÉ-FILTRE 2: vérifier si c'est un portrait/visage (close-up ou détecté)
                    try:
                        from PIL import Image as _PIL
                        with _PIL.open(img_path) as _raw:
                            _ow, _oh = _raw.size
                    except Exception:
                        _ow, _oh = 0, 0
                    
                    fname_lower = os.path.basename(img_path).lower()
                    is_face_crop = (
                        (fname_lower.startswith('tmp') and fname_lower.endswith('.png'))
                        or (_ow > 0 and _oh > 0 and _ow <= 600 and _oh <= 600
                            and abs(_ow - _oh) <= max(_ow, _oh) * 0.30)
                    )
                    
                    if is_face_crop or self._is_face_only(image):
                        # Portrait/visage seul (close-up) → SAIN d'office, skip NSFW model
                        state.add_log(f"[NSFW] Portrait seul: {Path(img_path).name} → SAIN (pas analyse NSFW)")
                        self.db_cache.save_nsfw_score(cache_key, img_path, "portrait", 0.0, {"_portrait_skip": 1.0})
                        record_result((0.0, img_path, 'SAIN', {"_portrait_skip": 1.0}))
                        continue
                    
                    # Personne détectée + pas portrait → analyser au modèle NSFW
                    batch_images.append(image)
                    batch_paths.append(img_path)
                    batch_orig_dims.append((_ow, _oh))
                else:
                    state.add_log(f"[NSFW] Impossible de charger l'image: {Path(img_path).name}")
                    self.db_cache.save_nsfw_score(cache_key, img_path, "error", 0.0, {"error": 1.0})
                    record_result((0.0, img_path, "error", {"error": 1.0}))
            except Exception as e: 
                state.add_log(f"Erreur {img_path} : {e}")
                self.db_cache.save_nsfw_score(cache_key, img_path, "error", 0.0, {"error": 1.0})
                record_result((0.0, img_path, "error", {"error": 1.0}))
                
            if len(batch_images) >= self.batch_size or (i == len(images_to_process) - 1 and batch_images):
                state.progress = (i + 1) / max(1, len(images_to_process))
                state.status_text = f"Inférence NSFW photos ({i+1}/{len(images_to_process)})..."
                try:
                    inputs = self.processor(images=batch_images, return_tensors="pt")
                    inputs = {k: v.to(self.dtype).to(self.device) if v.is_floating_point() else v.to(self.device) for k, v in inputs.items()}
                    with torch.inference_mode():
                        logits = self.model(**inputs).logits
                    probs = torch.nn.functional.softmax(logits, dim=-1).cpu()
                    
                    # Log model labels once so we can verify safe_labels alignment
                    if not getattr(self, '_labels_logged', False):
                        self._labels_logged = True
                        id2label = getattr(getattr(self.model, 'config', None), 'id2label', {})
                        state.add_log(f"[NSFW] Labels modèle: {id2label}")

                    for j, p in enumerate(batch_paths):
                        try:
                            prob_dist = probs[j]
                            top_idx = prob_dist.argmax(-1).item()
                            raw_top_label = self.model.config.id2label[top_idx]
                            details = {self.model.config.id2label[idx]: float(val) for idx, val in enumerate(prob_dist)}
                            # No portrait_guard here: already filtered in pre-filter (PRÉ-FILTRE 2)
                            # These images passed the face-only check, so analyze with full model results
                            portrait_guard = False
                            danger = self.compute_danger(details)

                            # Log detailed scores
                            detail_str = " | ".join([f"{lbl}: {prob:.2%}" for lbl, prob in sorted(details.items()) if not lbl.startswith('_')])
                            state.add_log(f"[NSFW] {Path(p).name} → {detail_str}")

                            details['_raw_top_label'] = raw_top_label
                            details['_portrait_guard'] = 1.0 if portrait_guard else 0.0

                            self.db_cache.save_nsfw_score(cache_key, p, raw_top_label, danger, details)
                            record_result((danger, p, self.present_label(details, portrait_guard), details))
                        except Exception as item_err:
                            state.add_log(f"[NSFW] Erreur image {Path(p).name} : {item_err}")
                            self.db_cache.save_nsfw_score(cache_key, p, "error", 0.0, {"error": 1.0})
                            record_result((0.0, p, "error", {"error": 1.0}))
                except Exception as e: state.add_log(f"Erreur d'inférence : {e}")
                batch_images, batch_paths, batch_orig_dims = [], [], []
                flush_checkpoint()

        # Final safety flush: handle remaining batched images when loop ends with `continue` paths.
        if batch_images:
            state.progress = 1.0
            state.status_text = f"Inférence NSFW photos (final flush: {len(batch_images)} images)..."
            try:
                inputs = self.processor(images=batch_images, return_tensors="pt")
                inputs = {k: v.to(self.dtype).to(self.device) if v.is_floating_point() else v.to(self.device) for k, v in inputs.items()}
                with torch.inference_mode():
                    logits = self.model(**inputs).logits
                probs = torch.nn.functional.softmax(logits, dim=-1).cpu()

                if not getattr(self, '_labels_logged', False):
                    self._labels_logged = True
                    id2label = getattr(getattr(self.model, 'config', None), 'id2label', {})
                    state.add_log(f"[NSFW] Labels modèle: {id2label}")

                for j, p in enumerate(batch_paths):
                    try:
                        prob_dist = probs[j]
                        top_idx = prob_dist.argmax(-1).item()
                        raw_top_label = self.model.config.id2label[top_idx]
                        details = {self.model.config.id2label[idx]: float(val) for idx, val in enumerate(prob_dist)}
                        portrait_guard = False
                        danger = self.compute_danger(details)

                        detail_str = " | ".join([f"{lbl}: {prob:.2%}" for lbl, prob in sorted(details.items()) if not lbl.startswith('_')])
                        state.add_log(f"[NSFW] {Path(p).name} → {detail_str}")

                        details['_raw_top_label'] = raw_top_label
                        details['_portrait_guard'] = 1.0 if portrait_guard else 0.0

                        self.db_cache.save_nsfw_score(cache_key, p, raw_top_label, danger, details)
                        record_result((danger, p, self.present_label(details, portrait_guard), details))
                    except Exception as item_err:
                        state.add_log(f"[NSFW] Erreur image {Path(p).name} : {item_err}")
                        self.db_cache.save_nsfw_score(cache_key, p, "error", 0.0, {"error": 1.0})
                        record_result((0.0, p, "error", {"error": 1.0}))
            except Exception as e:
                state.add_log(f"Erreur d'inférence (final flush) : {e}")

            batch_images, batch_paths, batch_orig_dims = [], [], []
            flush_checkpoint()

        # --- ВИДЕО ---
        batch_images, batch_frame_counts, batch_paths =[], [],[]
        for i, vid_path in enumerate(videos_to_process):
            time.sleep(0.002)
            if not state.is_processing: break
            state.status_text = f"NSFW Vidéo : {Path(vid_path).name} ({i+1}/{len(videos_to_process)})"
            try:
                frames = media_cache.get_video_frames(vid_path, self.max_dim, self.video_frames)
                if frames:
                    # PRÉ-FILTRE 1: vérifier si au moins 1 frame contient une personne
                    has_person = any(self._has_person(frame) for frame in frames)
                    if not has_person:
                        # Aucune personne détectée → SAIN d'office
                        state.add_log(f"[NSFW] Vidéo sans personne: {Path(vid_path).name} → SAIN (paysage/objet)")
                        self.db_cache.save_nsfw_score(cache_key, vid_path, "no_person", 0.0, {"_auto_sain": 1.0})
                        record_result((0.0, vid_path, 'SAIN', {"_auto_sain": 1.0}))
                        continue
                    
                    # PRÉ-FILTRE 2: vérifier si c'est un portrait seul (au moins 1 frame)
                    is_portrait_video = any(self._is_face_only(frame) for frame in frames)
                    if is_portrait_video:
                        # Au moins 1 frame portrait seul → SAIN d'office
                        state.add_log(f"[NSFW] Vidéo portrait seul: {Path(vid_path).name} → SAIN (pas analyse NSFW)")
                        self.db_cache.save_nsfw_score(cache_key, vid_path, "portrait", 0.0, {"_portrait_skip": 1.0})
                        record_result((0.0, vid_path, 'SAIN', {"_portrait_skip": 1.0}))
                        continue
                    
                    # Personne détectée + pas portrait → analyser au modèle NSFW
                    batch_images.extend(frames)
                    batch_paths.append(vid_path)
                    batch_frame_counts.append(len(frames))
                else:
                    self.db_cache.save_nsfw_score(cache_key, vid_path, "error", 0.0, {"error": 1.0})
                    record_result((0.0, vid_path, "error", {"error": 1.0}))
            except Exception as e: 
                state.add_log(f"Erreur lecture {vid_path} : {e}")
                self.db_cache.save_nsfw_score(cache_key, vid_path, "error", 0.0, {"error": 1.0})
                record_result((0.0, vid_path, "error", {"error": 1.0}))

            if len(batch_images) >= self.batch_size or (i == len(videos_to_process) - 1 and batch_images):
                state.progress = (i + 1) / max(1, len(videos_to_process))
                state.status_text = f"Inférence NSFW vidéos ({i+1}/{len(videos_to_process)})..."
                try:
                    all_probs =[]
                    for k in range(0, len(batch_images), self.batch_size):
                        chunk = batch_images[k:k+self.batch_size]
                        inputs = self.processor(images=chunk, return_tensors="pt")
                        inputs = {k: v.to(self.dtype).to(self.device) if v.is_floating_point() else v.to(self.device) for k, v in inputs.items()}
                        with torch.inference_mode():
                            logits = self.model(**inputs).logits
                        all_probs.extend(torch.nn.functional.softmax(logits, dim=-1).cpu())
                    
                    idx = 0
                    for p, count in zip(batch_paths, batch_frame_counts):
                        vid_probs = torch.stack(all_probs[idx : idx + count])
                        idx += count
                        avg_probs = vid_probs.mean(dim=0)
                        top_idx = avg_probs.argmax(-1).item()
                        raw_top_label = self.model.config.id2label[top_idx]
                        details = {self.model.config.id2label[k]: float(val) for k, val in enumerate(avg_probs)}
                        danger = self.compute_danger(details)
                        details['_raw_top_label'] = raw_top_label
                        details['_portrait_guard'] = 0.0
                        
                        self.db_cache.save_nsfw_score(cache_key, p, raw_top_label, danger, details)
                        record_result((danger, p, self.present_label(details, False), details))
                except Exception as e: state.add_log(f"Erreur d'inférence : {e}")
                batch_images, batch_frame_counts, batch_paths = [], [],[]
                flush_checkpoint()

            flush_checkpoint(completed=True)

        results.sort(key=lambda x: x[0], reverse=True)
        return results

class FaceEngine:
    def __init__(self, search_engine):
        self.se = search_engine
        self.db_cache = search_engine.db_cache
        self.app = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if self.device == "cuda" else ['CPUExecutionProvider']
        self._force_cpu = (self.device != "cuda")
        self.batch_size = 16

    def _switch_to_cpu(self, reason: str = ""):
        if self._force_cpu:
            return
        self._force_cpu = True
        self.providers = ['CPUExecutionProvider']
        self.unload()
        msg = "⚠️ InsightFace CUDA indisponible, bascule automatique sur CPU."
        if reason:
            msg += f" ({reason})"
        state.add_log(msg)

    def load_model(self):
        global INSIGHTFACE_AVAILABLE
        if not INSIGHTFACE_AVAILABLE:
            raise Exception("InsightFace n'est pas installé ! Installez : pip install insightface onnxruntime")
            
        if self.app is None:
            model_dir = os.path.join(current_dir, "models", "insightface")
            os.makedirs(model_dir, exist_ok=True)

            providers = ['CPUExecutionProvider'] if self._force_cpu else self.providers
            use_cuda = (not self._force_cpu and 'CUDAExecutionProvider' in providers)
            state.add_log(f"Chargement InsightFace (buffalo_l) sur {'cuda' if use_cuda else 'cpu'}...")

            try:
                self.app = FaceAnalysis(name='buffalo_l', root=model_dir, providers=providers)
                self.app.prepare(ctx_id=0 if use_cuda else -1, det_size=(640, 640))
            except Exception as e:
                # Fallback immédiat si provider CUDA incompatible avec le GPU.
                if use_cuda:
                    self._switch_to_cpu(str(e))
                    self.app = FaceAnalysis(name='buffalo_l', root=model_dir, providers=['CPUExecutionProvider'])
                    self.app.prepare(ctx_id=-1, det_size=(640, 640))
                else:
                    raise

    def unload(self):
        if self.app is not None:
            state.add_log(f"Déchargement InsightFace de la VRAM...")
            del self.app
            self.app = None
            gc.collect()
            if torch.cuda.is_available(): torch.cuda.empty_cache()

    def extract_faces(self, img_path):
        try:
            # Lire via PIL pour éviter les problèmes de chemins Unicode.
            img = Image.open(img_path).convert('RGB')
            # Réduire les très grandes images pour limiter les risques OOM.
            img.thumbnail((1920, 1920))
            img_arr = np.array(img)
            img_bgr = cv2.cvtColor(img_arr, cv2.COLOR_RGB2BGR)
            faces = self.app.get(img_bgr)
            return [f.embedding for f in faces]
        except Exception as e:
            err_text = str(e)
            cuda_markers = (
                "cudaErrorNoKernelImageForDevice",
                "CUDAExecutionProvider",
                "no kernel image is available",
            )
            if (not self._force_cpu) and any(m in err_text for m in cuda_markers):
                self._switch_to_cpu("provider CUDA incompatible")
                try:
                    self.load_model()
                    img = Image.open(img_path).convert('RGB')
                    img.thumbnail((1920, 1920))
                    img_arr = np.array(img)
                    img_bgr = cv2.cvtColor(img_arr, cv2.COLOR_RGB2BGR)
                    faces = self.app.get(img_bgr)
                    return [f.embedding for f in faces]
                except Exception as retry_e:
                    state.add_log(f"Erreur extraction visages {Path(img_path).name} (retry CPU) : {retry_e}")
                    return []
            state.add_log(f"Erreur extraction visages {Path(img_path).name} : {e}")
            return []

    def search_faces(self, ref_img_path, directory_path, allowed_exts, threshold, override_files=None):
        self.load_model()
        
        ref_embs = self.extract_faces(ref_img_path)
        if not ref_embs:
            raise Exception("Aucun visage détecté sur la photo de référence !")
        
        ref_emb = ref_embs[0]  # Utiliser le premier visage détecté comme référence.
        ref_n = ref_emb / np.linalg.norm(ref_emb)

        all_files = self.se._gather_files(directory_path, allowed_exts) if override_files is None else[f for f in override_files if f.lower().endswith(allowed_exts)]
        
        results = []
        images_to_process =[]
        
        # 1. Быстрый проход через кэш
        for p in all_files:
            cached = self.db_cache.get_face_embeddings(p)
            if cached is not None:
                if len(cached) > 0:
                    max_sim = -1.0
                    for emb in cached:
                        emb_n = emb / np.linalg.norm(emb)
                        sim = np.dot(emb_n, ref_n)
                        if sim > max_sim: max_sim = sim
                    if max_sim >= threshold:
                        results.append((float(max_sim), p))
            else:
                images_to_process.append(p)
                
        # 2. Обработка новых файлов
        if images_to_process:
            state.add_log(f"Extraction des visages pour {len(images_to_process)} nouveaux fichiers...")
            batch_paths =[]
            for i, p in enumerate(images_to_process):
                if not state.is_processing: break
                
                batch_paths.append(p)
                
                if len(batch_paths) >= self.batch_size or i == len(images_to_process) - 1:
                    state.progress = (i + 1) / max(1, len(images_to_process))
                    state.status_text = f"Analyse des visages ({i+1}/{len(images_to_process)})..."
                    
                    batch_db_data =[]
                    for path in batch_paths:
                        ext = os.path.splitext(path)[1].lower()
                        if ext in SUPPORTED_IMAGES:
                            embs = self.extract_faces(path)
                        elif ext in SUPPORTED_VIDEOS:
                            # Pour les vidéos, on prend le premier frame.
                            frames = media_cache.get_video_frames(path, 640, 1)
                            if frames and len(frames) > 0:
                                img_arr = np.array(frames[0])
                                img_bgr = cv2.cvtColor(img_arr, cv2.COLOR_RGB2BGR)
                                faces = self.app.get(img_bgr)
                                embs =[f.embedding for f in faces]
                            else:
                                embs =[]
                        else:
                            embs =[]
                            
                        batch_db_data.append((path, embs))
                        
                        if embs:
                            max_sim = -1.0
                            for emb in embs:
                                emb_n = emb / np.linalg.norm(emb)
                                sim = np.dot(emb_n, ref_n)
                                if sim > max_sim: max_sim = sim
                            if max_sim >= threshold:
                                results.append((float(max_sim), path))
                                
                    self.db_cache.save_face_embeddings_batch(batch_db_data)
                    batch_paths = []
                        
        results.sort(key=lambda x: x[0], reverse=True)
        return results

    def build_cache(self, directory_path, allowed_exts, override_files=None):
        """Pre-cache des visages."""
        self.load_model()
        all_files = self.se._gather_files(directory_path, allowed_exts) if override_files is None else[f for f in override_files if f.lower().endswith(allowed_exts)]
        
        images_to_process =[]
        for p in all_files:
            if self.db_cache.get_face_embeddings(p) is None:
                images_to_process.append(p)
                
        if not images_to_process:
            return
            
        state.add_log(f"Mise en cache des visages pour {len(images_to_process)} fichiers...")
        batch_paths =[]
        for i, p in enumerate(images_to_process):
            if not state.is_processing: break
            
            batch_paths.append(p)
            
            if len(batch_paths) >= self.batch_size or i == len(images_to_process) - 1:
                state.progress = (i + 1) / max(1, len(images_to_process))
                state.status_text = f"Cache visages ({i+1}/{len(images_to_process)})..."
                
                batch_db_data =[]
                for path in batch_paths:
                    ext = os.path.splitext(path)[1].lower()
                    if ext in SUPPORTED_IMAGES:
                        embs = self.extract_faces(path)
                    elif ext in SUPPORTED_VIDEOS:
                        frames = media_cache.get_video_frames(path, 640, 1)
                        if frames and len(frames) > 0:
                            img_arr = np.array(frames[0])
                            img_bgr = cv2.cvtColor(img_arr, cv2.COLOR_RGB2BGR)
                            faces = self.app.get(img_bgr)
                            embs = [f.embedding for f in faces]
                        else:
                            embs =[]
                    else:
                        embs =[]
                        
                    batch_db_data.append((path, embs))
                    
                self.db_cache.save_face_embeddings_batch(batch_db_data)
                batch_paths =[]

# --- ДВИЖОК ТЕГИРОВАНИЯ DANBOORU ---
class TagEngine:
    def __init__(self, search_engine):
        self.se = search_engine
        self.db_cache = search_engine.db_cache
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._force_cpu = (self.device != "cuda")
        self.session = None
        self.tag_names =[]
        self.model_name = None
        self.target_size = 448
        self.batch_size = 16
        self.video_frames = 4
        self.min_save_threshold = 0.1

    @staticmethod
    def _collect_prompt_strings(value):
        texts = []
        if value is None:
            return texts
        if isinstance(value, str):
            raw = value.strip()
            if raw:
                texts.append(raw)
                if raw[:1] in "[{":
                    try:
                        texts.extend(TagEngine._collect_prompt_strings(json.loads(raw)))
                    except Exception:
                        pass
            return texts
        if isinstance(value, dict):
            for key, subvalue in value.items():
                key_l = str(key).strip().lower()
                if key_l in {
                    "prompt", "positive", "positive_prompt", "positiveprompt", "text",
                    "parameters", "comment", "description", "caption", "keywords",
                    "usercomment", "imagedescription",
                }:
                    texts.extend(TagEngine._collect_prompt_strings(subvalue))
                elif isinstance(subvalue, (dict, list)):
                    texts.extend(TagEngine._collect_prompt_strings(subvalue))
            return texts
        if isinstance(value, list):
            for item in value:
                texts.extend(TagEngine._collect_prompt_strings(item))
        return texts

    @staticmethod
    def _extract_comfyui_positive_prompt(data) -> str:
        """
        Extract only the positive prompt text from a ComfyUI workflow or prompt JSON.
        Supports both full workflow (node graph) and simplified prompt formats.
        """
        if isinstance(data, str):
            raw = data.strip()
            if raw[:1] in '{[':
                try:
                    data = json.loads(raw)
                except Exception:
                    return ""
        if not isinstance(data, dict):
            return ""

        # Detect ComfyUI format: most values should be dicts with 'inputs' or 'class_type'
        node_values = [v for v in list(data.values())[:8] if isinstance(v, dict)]
        if not node_values:
            return ""
        comfy_like = sum(1 for v in node_values if 'class_type' in v or 'inputs' in v)
        if comfy_like < max(1, len(node_values) // 2):
            return ""

        # Build the set of negative node IDs from multiple signals
        negative_node_ids = set()

        # Signal A: title-based detection (English + Chinese + French)
        _neg_title_keywords = ('negative', 'neg ', 'neg_', 'bad prompt', 'unwanted',
                               '负面', '负向', '反向', '不要', '坏')
        for node_id, node in data.items():
            if not isinstance(node, dict):
                continue
            meta = node.get('_meta') or {}
            title = str(meta.get('title') or '').lower()
            if any(kw in title for kw in _neg_title_keywords) or title.startswith('neg'):
                negative_node_ids.add(str(node_id))

        # Signal B: follow every KSampler's 'negative' input wire (language-independent)
        for node_id, node in data.items():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get('class_type') or '').lower()
            if 'sampler' in class_type or 'ksampler' in class_type:
                inputs = node.get('inputs') or {}
                neg_ref = inputs.get('negative')
                if isinstance(neg_ref, list) and neg_ref:
                    negative_node_ids.add(str(neg_ref[0]))

        # Strategy 1: find node whose _meta.title contains "positive" (not "negative")
        _pos_title_keywords = ('positive', 'pos ', 'pos_', 'prompt', '正向', '正面', '提示词')
        for node_id, node in data.items():
            if not isinstance(node, dict):
                continue
            meta = node.get('_meta') or {}
            title = str(meta.get('title') or '').lower()
            inputs = node.get('inputs') or {}
            raw_text = inputs.get('text')
            text = raw_text.strip() if isinstance(raw_text, str) else ''
            if (text and str(node_id) not in negative_node_ids
                    and any(kw in title for kw in _pos_title_keywords)):
                return text

        # Strategy 2: follow KSampler's 'positive' input reference to source node
        for node_id, node in data.items():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get('class_type') or '').lower()
            if 'sampler' in class_type:
                inputs = node.get('inputs') or {}
                positive_ref = inputs.get('positive')
                if isinstance(positive_ref, list) and positive_ref:
                    ref_id = str(positive_ref[0])
                    ref_node = data.get(ref_id) or {}
                    ref_inputs = ref_node.get('inputs') or {}
                    raw_text = ref_inputs.get('text')
                    text = raw_text.strip() if isinstance(raw_text, str) else ''
                    if text and ref_id not in negative_node_ids:
                        return text

        # Strategy 3: first CLIPTextEncode node that is NOT a known negative node
        for node_id, node in data.items():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get('class_type') or '').lower()
            if 'clip' in class_type and 'encode' in class_type and str(node_id) not in negative_node_ids:
                inputs = node.get('inputs') or {}
                raw_text = inputs.get('text')
                text = raw_text.strip() if isinstance(raw_text, str) else ''
                if text:
                    return text

        return ""

    @staticmethod
    def _is_valid_prompt_text(text: str) -> bool:
        """Vérifie qu'un texte est un vrai prompt.
        Rejette : chiffres seuls, listes Python, seeds, hashes hex (SHA/MD5/UUID),
        valeurs techniques ComfyUI/SD, et les prompts négatifs purs.
        """
        t = text.strip()
        if not t:
            return False

        # --- Blocklist des valeurs techniques ComfyUI / SD (jamais des prompts) ---
        _TECH = {
            'auto', 'image', 'latent', 'conditioning', 'mask', 'model', 'clip', 'vae',
            'control_net', 'controlnet', 'string', 'int', 'float', 'boolean',
            'none', 'null', 'true', 'false', 'undefined',
            'euler', 'euler_ancestral', 'dpm', 'dpm2', 'lms', 'heun', 'ddpm', 'ddim',
            'plms', 'unipc', 'karras', 'exponential', 'simple', 'linear', 'normal',
            'empty', 'default', 'n/a', 'na', 'unknown',
        }
        if t.lower() in _TECH:
            return False

        # --- Rejeter les noms de fichiers modèle (single token avec extension) ---
        _MODEL_EXTS = ('.safetensors', '.ckpt', '.pt', '.pth', '.bin', '.gguf',
                       '.onnx', '.sft', '.vae', '.lora')
        t_lower_full = t.lower()
        if ' ' not in t and ',' not in t and '\n' not in t:
            if any(t_lower_full.endswith(ext) for ext in _MODEL_EXTS):
                return False

        # --- Rejeter le code Python / expressions (nœuds math ComfyUI) ---
        _CODE_PATTERNS = (
            'round(', 'int(', 'float(', 'str(', 'abs(', 'min(', 'max(',
            'lambda ', 'def ', 'return ', 'import ', '== ', '!= ', '>= ', '<= ',
        )
        if len(t) < 120 and any(p in t_lower_full for p in _CODE_PATTERNS):
            return False

        # --- Rejeter les chaînes purement hexadécimales (SHA-1/256, MD5, UUID…) ---
        hex_only = t.replace('-', '').replace('_', '').replace(' ', '').lower()
        if len(hex_only) >= 8 and all(c in '0123456789abcdef' for c in hex_only):
            return False

        # --- Marqueurs de prompt négatif ---
        # Forts (1 seul suffit pour rejeter)
        _strong_neg_en = (
            'worst quality', 'low quality', 'bad quality', 'normal quality',
            'jpeg artifact', 'jpeg compress',
            'bad anatomy', 'bad hands', 'bad feet', 'poorly drawn hands',
            'extra fingers', 'fused fingers', 'missing fingers',
            'extra limb', 'missing limb', 'missing arm', 'missing leg',
            'disney pixar', 'pixar type',
        )
        _strong_neg_zh = (
            '最差质量', '低质量', '丑陋', '畸形', '毁容', 'jpeg压缩',
            '字幕', '水印', '模糊不清', '画得不好', '手指融合', '多余的手指',
        )
        t_lower = t.lower()
        for kw in _strong_neg_en:
            if kw in t_lower:
                return False
        for kw in _strong_neg_zh:
            if kw in t:
                return False

        # Faibles (2+ requis pour rejeter — évite les faux positifs sur mots isolés)
        _weak_neg = (
            'blurry', 'blurred', 'out of focus', 'overexposed', 'underexposed',
            'ugly', 'deformed', 'malformed', 'disfigured',
            'watermark', 'text overlay', 'subtitle', 'cropped', 'duplicate',
            'lowres', 'low res', 'pixelated', 'cartoon style',
            '过曝', '整体发灰', '静止不动', '杂乱',
        )
        weak_hits = sum(1 for kw in _weak_neg if kw in t_lower)
        weak_hits += sum(1 for kw in ('过曝', '整体发灰', '静止不动', '杂乱') if kw in t)
        if weak_hits >= 2:
            return False

        # --- Doit contenir au moins 2 lettres consécutives ---
        prev_alpha = False
        for c in t:
            if c.isalpha():
                if prev_alpha:
                    return True
                prev_alpha = True
            else:
                prev_alpha = False
        return False

    @staticmethod
    def _extract_positive_prompt_text(text: str) -> str:
        if not text:
            return ""

        normalized = str(text).replace("\r", "\n")
        for marker in ("Negative prompt:", "Negative Prompt:", "negative prompt:"):
            if marker in normalized:
                normalized = normalized.split(marker, 1)[0]
        for marker in ("\nSteps:", " Steps:", "\nSampler:"):
            if marker in normalized:
                normalized = normalized.split(marker, 1)[0]

        cleaned = normalized.strip()
        cleaned = cleaned.replace("\x00", " ")
        cleaned = "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())
        result = cleaned[:12000]
        # Rejeter les artefacts non-textuels (chiffres, listes Python, seeds, etc.)
        if not TagEngine._is_valid_prompt_text(result):
            return ""
        return result

    @staticmethod
    def _prompt_text_to_tags(text: str) -> dict:
        prompt_text = TagEngine._extract_positive_prompt_text(text)
        if not prompt_text:
            return {}

        raw_chunks = []
        for line in prompt_text.splitlines():
            for part in line.replace(";", ",").replace("|", ",").split(","):
                chunk = part.strip()
                if chunk:
                    raw_chunks.append(chunk)

        ignored_terms = {
            "masterpiece", "best quality", "high quality", "absurdres", "newest",
            "rating_safe", "score_9", "score_8_up", "score_7_up",
        }
        tags = {}
        for chunk in raw_chunks:
            cleaned = chunk.strip()
            cleaned = cleaned.replace("BREAK", " ")
            cleaned = cleaned.replace("(", " ").replace(")", " ")
            cleaned = cleaned.replace("[", " ").replace("]", " ")
            cleaned = cleaned.replace("{", " ").replace("}", " ")
            cleaned = cleaned.replace("<", " ").replace(">", " ")
            cleaned = cleaned.replace("\\", " ").replace("/", " ")
            cleaned = " ".join(cleaned.split())
            lower = cleaned.lower()

            if not lower:
                continue
            if lower.startswith(("lora:", "embedding:", "negative prompt:", "steps:", "sampler:", "cfg scale:", "seed:")):
                continue
            if any(token in lower for token in ("model hash", "clip skip", "size:", "denoising strength", "version:")):
                continue
            if len(lower) < 2 or len(lower) > 80:
                continue
            if lower in ignored_terms:
                continue

            words = [w for w in lower.split() if w]
            if len(words) > 8:
                continue

            tag = "_".join(words)
            if len(tag) < 2:
                continue
            tags[tag] = max(tags.get(tag, 0.0), 1.0)
        return tags

    @staticmethod
    def _build_detailed_prompt_fallback(prompt_text: str, tags_dict: dict, ai_payload: dict | None = None, path: str = "") -> str:
        ai_payload = ai_payload or {}
        base_prompt = TagEngine._extract_positive_prompt_text(prompt_text)
        top_tags = []
        if isinstance(tags_dict, dict):
            top_tags = [tag for tag, _score in sorted(tags_dict.items(), key=lambda x: -float(x[1]))[:24]]

        parts = []
        if base_prompt:
            parts.append(base_prompt)
        if top_tags:
            human_tags = ", ".join(tag.replace("_", " ") for tag in top_tags[:18])
            parts.append(f"Description guided by detected visual signals: {human_tags}")

        nsfw = ai_payload.get("nsfw") or {}
        if nsfw.get("top_label"):
            parts.append(f"Safety context: {nsfw.get('top_label')}")

        aes = ai_payload.get("aesthetic") or {}
        if aes.get("avg_score") is not None:
            try:
                parts.append(f"Perceived aesthetic quality: {float(aes.get('avg_score')):.2f}/10")
            except Exception:
                pass

        faces = ai_payload.get("faces") or {}
        if faces.get("count") is not None:
            parts.append(f"Detected faces: {faces.get('count')}")

        if path:
            ext = os.path.splitext(path)[1].lower()
            if ext:
                parts.append(f"Media type: {ext.lstrip('.')} image")

        if not parts:
            return ""

        return ". ".join(part.strip().rstrip('.') for part in parts if part).strip() + "."

    @staticmethod
    def _read_sidecar_prompt_txt(path: str) -> str:
        """Lit un .txt sidecar à côté du média si présent.

        Conventions reconnues (dans l'ordre): `{stem}_prompt.txt` puis `{stem}.txt`
        (convention SD WebUI / kohya). Retourne le texte nettoyé, ou '' si rien
        de valide.
        """
        try:
            p = Path(path)
            for candidate in (p.with_name(f"{p.stem}_prompt.txt"), p.with_suffix('.txt')):
                if candidate.exists() and candidate.is_file():
                    try:
                        txt = candidate.read_text(encoding='utf-8', errors='ignore').strip()
                    except Exception:
                        continue
                    if txt and TagEngine._is_valid_prompt_text(txt):
                        return txt
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # Détection IA (sidecar .ia + extraction signatures + EXIF caméra)
    # ------------------------------------------------------------------
    # Clés metadata typiques des générateurs IA connus
    _AI_METADATA_SIGNATURES = (
        # PNG tEXt chunks
        ("info", "parameters"),               # Automatic1111 / Forge / SD WebUI
        ("info", "prompt"),                   # ComfyUI
        ("info", "workflow"),                 # ComfyUI
        ("info", "sd-metadata"),              # InvokeAI legacy
        ("info", "invokeai_metadata"),        # InvokeAI v3+
        ("info", "invokeai"),                 # InvokeAI
        ("info", "software"),                 # NovelAI, Fooocus parfois
        ("info", "comment"),                  # NovelAI
        ("info", "title"),                    # NovelAI parfois
        ("info", "description"),              # divers
        ("info", "dream"),                    # vieux Stable Diffusion
        ("info", "novelai"),                  # NovelAI
        ("info", "extras"),                   # divers
    )

    # Mots-clés textuels (case-insensitive) qui prouvent une origine IA
    _AI_TEXT_MARKERS = (
        "stable diffusion", "stablediffusion", "automatic1111", "a1111",
        "comfyui", "comfy ui", "invokeai", "invoke ai", "novelai", "novel ai",
        "fooocus", "easydiffusion", "stable-diffusion-webui", "sdxl",
        "diffusers", "midjourney", "dall-e", "dall·e", "dalle", "leonardo.ai",
        "stability ai", "stability.ai", "kohya", "lora:", "<lora:",
        "negative prompt:", "sampler:", "cfg scale:", "steps:", "denoising strength",
        "model hash:", "vae hash:", "schedule type:", "scheduler:",
    )

    # Marqueurs EXIF d'appareil photo réel
    _CAMERA_EXIF_KEYS = (
        "make", "model", "lensmake", "lensmodel", "focallength", "fnumber",
        "exposuretime", "isospeedratings", "photographicsensitivity",
        "datetimeoriginal", "gpsinfo",
    )

    @staticmethod
    def _read_sidecar_ia(path: str) -> dict:
        """Lit un .ia JSON sidecar (`{stem}.ia`) si présent. Retourne dict ou {}."""
        try:
            p = Path(path)
            candidate = p.with_suffix('.ia')
            if candidate.exists() and candidate.is_file():
                try:
                    data = json.loads(candidate.read_text(encoding='utf-8', errors='ignore'))
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        except Exception:
            pass
        return {}

    @staticmethod
    def _write_sidecar_ia(path: str, detection: dict) -> bool:
        """Écrit le sidecar `{stem}.ia` (JSON UTF-8). True si OK."""
        try:
            p = Path(path)
            candidate = p.with_suffix('.ia')
            payload = json.dumps(detection or {}, ensure_ascii=False, indent=2, default=str)
            candidate.write_text(payload, encoding='utf-8')
            return True
        except Exception:
            return False

    @staticmethod
    def _extract_exif_camera_info(path: str) -> dict:
        """Extrait les champs EXIF typiques d'un APN. Retourne un dict (vide si rien)."""
        out = {}
        try:
            with Image.open(path) as img:
                exif = img.getexif() or {}
                for key, value in exif.items():
                    name = str(ExifTags.TAGS.get(key, key)).strip()
                    name_l = name.lower()
                    if name_l in TagEngine._CAMERA_EXIF_KEYS:
                        try:
                            if isinstance(value, bytes):
                                try:
                                    value = value.decode('utf-8', errors='ignore').strip('\x00').strip()
                                except Exception:
                                    value = str(value)
                            out[name] = value if isinstance(value, (str, int, float)) else str(value)
                        except Exception:
                            continue
                # Software field aussi
                soft = exif.get(305)  # Software
                if soft and 'Software' not in out:
                    out['Software'] = soft if isinstance(soft, str) else str(soft)
        except Exception:
            pass
        # Nettoyer chaînes vides
        return {k: v for k, v in out.items() if v not in (None, '', b'')}

    @staticmethod
    def _detect_ai_from_metadata(path: str) -> dict:
        """Inspecte le PNG info + EXIF pour détecter une signature de générateur IA.

        Retourne dict: {is_ai: bool|None, confidence: float, source: str, evidence: dict}
        - is_ai=True si signature trouvée
        - is_ai=None si aucune métadonnée concluante (à fallback Ollama)
        """
        evidence = {}
        try:
            with Image.open(path) as img:
                info = getattr(img, 'info', {}) or {}
                # Chercher clés signatures
                for _, key_name in TagEngine._AI_METADATA_SIGNATURES:
                    if key_name in info:
                        val = info[key_name]
                        if val is None:
                            continue
                        sval = val if isinstance(val, str) else str(val)
                        sval_l = sval.lower()
                        # Si le contenu mentionne explicitement un outil IA → confiance max
                        for marker in TagEngine._AI_TEXT_MARKERS:
                            if marker in sval_l:
                                return {
                                    'is_ai': True,
                                    'confidence': 0.99,
                                    'source': f'png_info:{key_name}',
                                    'evidence': {key_name: sval[:1000]},
                                }
                        # Clé typiquement IA (parameters, prompt ComfyUI, sd-metadata) → forte présomption
                        if key_name in {'parameters', 'sd-metadata', 'invokeai_metadata', 'invokeai', 'novelai', 'dream'}:
                            return {
                                'is_ai': True,
                                'confidence': 0.95,
                                'source': f'png_info:{key_name}',
                                'evidence': {key_name: sval[:1000]},
                            }
                        # Workflow/prompt ComfyUI : valider que c'est bien du JSON ComfyUI
                        if key_name in {'workflow', 'prompt'} and sval.strip()[:1] in '{[':
                            try:
                                parsed = json.loads(sval)
                                if isinstance(parsed, dict) and parsed:
                                    return {
                                        'is_ai': True,
                                        'confidence': 0.95,
                                        'source': f'png_info:{key_name}',
                                        'evidence': {key_name: 'ComfyUI workflow JSON détecté'},
                                    }
                            except Exception:
                                pass
                        evidence[key_name] = sval[:500]

                # Chercher dans EXIF (UserComment etc.) des marqueurs textuels IA
                try:
                    exif = img.getexif() or {}
                    blob_parts = []
                    for key, value in exif.items():
                        name = str(ExifTags.TAGS.get(key, key)).strip().lower()
                        if name in {'usercomment', 'imagedescription', 'xpkeywords', 'xpcomment', 'software'}:
                            if isinstance(value, bytes):
                                for enc in ('utf-8', 'utf-16', 'utf-16-le', 'latin-1'):
                                    try:
                                        value = value.decode(enc, errors='ignore')
                                        break
                                    except Exception:
                                        continue
                            if value:
                                blob_parts.append(str(value))
                    blob = ' \n '.join(blob_parts).lower()
                    for marker in TagEngine._AI_TEXT_MARKERS:
                        if marker in blob:
                            return {
                                'is_ai': True,
                                'confidence': 0.9,
                                'source': 'exif_text',
                                'evidence': {'exif_snippet': blob[:500]},
                            }
                except Exception:
                    pass
        except Exception as e:
            return {'is_ai': None, 'confidence': 0.0, 'source': 'error', 'evidence': {'error': str(e)}}

        return {'is_ai': None, 'confidence': 0.0, 'source': 'metadata_inconclusive', 'evidence': evidence}

    @staticmethod
    def _detect_real_photo_from_exif(exif_info: dict) -> dict:
        """Évalue la probabilité que ce soit une photo réelle d'après l'EXIF caméra.

        Retourne {is_photo: bool, confidence: float, signals: list[str]}.
        """
        if not exif_info:
            return {'is_photo': False, 'confidence': 0.0, 'signals': []}
        signals = []
        has_make = any(k.lower() == 'make' for k in exif_info)
        has_model = any(k.lower() == 'model' for k in exif_info)
        has_lens = any(k.lower() in ('lensmake', 'lensmodel') for k in exif_info)
        has_exposure = any(k.lower() in ('exposuretime', 'fnumber', 'focallength') for k in exif_info)
        has_gps = any(k.lower() == 'gpsinfo' for k in exif_info)
        has_dt = any(k.lower() == 'datetimeoriginal' for k in exif_info)
        if has_make: signals.append('Make')
        if has_model: signals.append('Model')
        if has_lens: signals.append('Lens')
        if has_exposure: signals.append('ExposureSettings')
        if has_gps: signals.append('GPS')
        if has_dt: signals.append('DateTimeOriginal')
        score = 0.0
        if has_make and has_model: score += 0.5
        if has_lens: score += 0.15
        if has_exposure: score += 0.2
        if has_gps: score += 0.1
        if has_dt: score += 0.05
        # Logiciel d'édition typique → ne casse pas le verdict mais on note
        soft = str(exif_info.get('Software', '')).lower()
        if any(s in soft for s in ('stable diffusion', 'comfy', 'invoke', 'novelai', 'midjourney', 'dall')):
            return {'is_photo': False, 'confidence': 0.9, 'signals': signals + [f'Software:{soft}']}
        is_photo = score >= 0.5
        return {'is_photo': is_photo, 'confidence': min(score, 0.95), 'signals': signals}

    @staticmethod
    def _extract_prompt_from_image_metadata(path: str) -> str:
        try:
            candidates = []
            with Image.open(path) as img:
                info = getattr(img, "info", {}) or {}
                for key, value in info.items():
                    key_l = str(key).strip().lower()
                    # ComfyUI keys: try dedicated extractor first (avoids picking up negative prompts)
                    if key_l in {"workflow", "prompt"}:
                        raw = value
                        parsed_as_dict = False
                        if isinstance(raw, str):
                            raw = raw.strip()
                            if raw[:1] in '{[':
                                try:
                                    raw = json.loads(raw)
                                except Exception:
                                    raw = value
                        if isinstance(raw, dict):
                            parsed_as_dict = True
                            comfy_text = TagEngine._extract_comfyui_positive_prompt(raw)
                            if comfy_text and TagEngine._is_valid_prompt_text(comfy_text):
                                return comfy_text
                        # Fall back to generic ONLY if not a ComfyUI dict
                        # (avoids harvesting model filenames, code, negative prompts from workflow)
                        if not parsed_as_dict and isinstance(value, str):
                            candidates.extend(TagEngine._collect_prompt_strings(value))
                    elif key_l in {
                        "parameters", "comment", "description",
                        "caption", "keywords",
                    }:
                        candidates.extend(TagEngine._collect_prompt_strings(value))

                try:
                    exif = img.getexif() or {}
                    for key, value in exif.items():
                        key_name = str(ExifTags.TAGS.get(key, key)).strip().lower()
                        if key_name in {
                            "usercomment", "imagedescription", "xpkeywords", "xpcomment",
                            "comment", "keywords", "artist",
                        }:
                            if isinstance(value, bytes):
                                for enc in ("utf-8", "utf-16", "utf-16-le", "latin-1"):
                                    try:
                                        value = value.decode(enc, errors="ignore")
                                        break
                                    except Exception:
                                        continue
                            candidates.extend(TagEngine._collect_prompt_strings(value))
                except Exception:
                    pass

            for candidate in candidates:
                stripped = str(candidate).strip()
                # Skip raw JSON blobs that slipped through – not a usable prompt string
                if stripped[:1] in '{[':
                    continue
                # Skip XMP/XML metadata blobs (Adobe, Dublin Core, RDF, etc.)
                if stripped.startswith(('<?xpacket', '<x:xmpmeta', '<?xml', '<rdf:', '<dc:')):
                    continue
                if stripped.startswith('<') and 'xmlns:' in stripped[:500]:
                    continue
                prompt_text = TagEngine._extract_positive_prompt_text(stripped)
                if prompt_text:
                    return prompt_text
            return ""
        except Exception:
            return ""

    def _switch_to_cpu(self, reason: str = ""):
        if self._force_cpu:
            return
        self._force_cpu = True
        self.unload()
        msg = "⚠️ Tags CUDA indisponible, bascule automatique sur CPU."
        if reason:
            msg += f" ({reason})"
        state.add_log(msg)

    def load_model(self, model_repo):
        if self.model_name == model_repo and self.session is not None:
            return

        self.unload()
        state.add_log(f"Chargement du modèle de tags {model_repo} sur {self.device}...")
        
        try:
            import onnxruntime as rt
            import pandas as pd
            from huggingface_hub import hf_hub_download
        except ImportError:
            raise Exception("Installez les dépendances : pip install onnxruntime pandas huggingface_hub")

        local_dir = resolve_model_dir(model_repo)
        os.makedirs(local_dir, exist_ok=True)
        state.add_log(f"Répertoire local tags : {local_dir}")

        # 1. Загрузка CSV тегов
        from huggingface_hub import list_repo_files, hf_hub_download
        
        # 1. Умный поиск файла тегов и ONNX модели
        csv_path = os.path.join(local_dir, "tags.csv")
        json_path = os.path.join(local_dir, "tags.json")
        txt_path = os.path.join(local_dir, "tags.txt")
        onnx_path = os.path.join(local_dir, "model.onnx")
        
        # Миграция со старых версий файлов
        old_csv = os.path.join(local_dir, "selected_tags.csv")
        if os.path.exists(old_csv) and not os.path.exists(csv_path): os.rename(old_csv, csv_path)
        old_json = os.path.join(local_dir, "tag_mapping.json")
        if os.path.exists(old_json) and not os.path.exists(json_path): os.rename(old_json, json_path)
        old_txt = os.path.join(local_dir, "top_tags.txt")
        if os.path.exists(old_txt) and not os.path.exists(txt_path): os.rename(old_txt, txt_path)

        repo_files =[]
        if not is_local_tagger_model_ready(local_dir):
            state.add_log(f"Modèle tags incomplet ou absent, téléchargement vers : {local_dir}")
            try:
                repo_files = list_repo_files(repo_id=model_repo)
            except Exception as e:
                raise Exception(f"Impossible d'obtenir la liste des fichiers du dépôt {model_repo} : {e}")
        else:
            state.add_log("Modèle tags local valide détecté, téléchargement ignoré.")

        # --- ЗАГРУЗКА ТЕГОВ ---
        if not os.path.exists(csv_path) and not os.path.exists(json_path) and not os.path.exists(txt_path):
            tag_filename = None
            # Priorite 1: fichiers CSV avec 'tag' ou 'class'
            for f in repo_files:
                if f.endswith('.csv') and ('tag' in f.lower() or 'class' in f.lower()):
                    tag_filename = f; break
            # Priorite 2: fichiers JSON avec 'tag' ou 'metadata'
            if not tag_filename:
                for f in repo_files:
                    if f.endswith('.json') and ('tag' in f.lower() or 'metadata' in f.lower()) and 'config' not in f.lower():
                        tag_filename = f; break
            # Priorite 3: fichiers TXT avec 'tag' (ex: modeles type joytag)
            if not tag_filename:
                for f in repo_files:
                    if f.endswith('.txt') and 'tag' in f.lower():
                        tag_filename = f; break
                        
            if not tag_filename:
                raise Exception(f"Fichier de tags (.csv, .json ou .txt) introuvable dans le dépôt {model_repo}")
                
            downloaded_path = hf_hub_download(repo_id=model_repo, filename=tag_filename, local_dir=local_dir)
            if downloaded_path.endswith('.csv'):
                os.rename(downloaded_path, csv_path)
            elif downloaded_path.endswith('.json'):
                os.rename(downloaded_path, json_path)
            elif downloaded_path.endswith('.txt'):
                os.rename(downloaded_path, txt_path)

        if os.path.exists(csv_path):
            df = pd.read_csv(csv_path)
            if 'name' in df.columns:
                self.tag_names = df['name'].fillna('unknown_tag').astype(str).tolist()
            else:
                self.tag_names = df.iloc[:, 0].fillna('unknown_tag').astype(str).tolist()
        elif os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            extracted_names =[]
            
            def extract_from_list(lst):
                names = []
                if len(lst) > 0 and isinstance(lst[0], dict):
                    if any(key in lst[0] for key in['id', 'tag_id', 'tag_index']):
                        max_id = max((int(item.get('id', item.get('tag_id', item.get('tag_index', -1)))) for item in lst if isinstance(item, dict) and str(item.get('id', item.get('tag_id', item.get('tag_index', '')))).lstrip('-').isdigit()), default=-1)
                        if max_id >= 0:
                            names = ['unknown_tag'] * (max_id + 1)
                            for item in lst:
                                if isinstance(item, dict):
                                    val = str(item.get('id', item.get('tag_id', item.get('tag_index', '-1'))))
                                    if val.lstrip('-').isdigit():
                                        idx = int(val)
                                        if idx >= 0: names[idx] = str(item.get('name', item.get('tag', str(item))))
                            return names
                    for item in lst:
                        if isinstance(item, dict):
                            names.append(str(item.get('name', item.get('tag', str(item)))))
                        else:
                            names.append(str(item))
                else:
                    names =[str(x) for x in lst]
                return names

            if isinstance(data, dict):
                # Уникальная структура для Camie-Tagger v2
                if "dataset_info" in data and isinstance(data["dataset_info"], dict):
                    mapping = data["dataset_info"].get("tag_mapping", {})
                    if isinstance(mapping, dict):
                        if "idx_to_tag" in mapping: data = mapping["idx_to_tag"]
                        elif "tag_to_idx" in mapping: data = mapping["tag_to_idx"]

                if isinstance(data, dict):
                    for key in["tags", "tag_names", "classes", "labels"]:
                        if key in data and isinstance(data[key], (list, dict)):
                            data = data[key]
                            break

            if isinstance(data, list):
                extracted_names = extract_from_list(data)
            elif isinstance(data, dict):
                keys_are_ints = all(str(k).isdigit() for k in data.keys() if k != "meta")
                if keys_are_ints:
                    int_keys =[int(k) for k in data.keys() if str(k).isdigit()]
                    if int_keys:
                        max_idx = max(int_keys)
                        extracted_names = ['unknown_tag'] * (max_idx + 1)
                        for k, v in data.items():
                            if not str(k).isdigit(): continue
                            idx = int(k)
                            if isinstance(v, dict):
                                extracted_names[idx] = str(v.get('name', v.get('tag', str(v))))
                            else:
                                extracted_names[idx] = str(v)
                else:
                    is_name_to_id = any(isinstance(v, int) or str(v).lstrip('-').isdigit() for v in data.values())
                    if is_name_to_id:
                        valid_pairs =[(int(v), str(k)) for k, v in data.items() if isinstance(v, (int, str)) and str(v).lstrip('-').isdigit()]
                        if valid_pairs:
                            max_idx = max(idx for idx, _ in valid_pairs)
                            extracted_names = ['unknown_tag'] * (max_idx + 1)
                            for idx, name in valid_pairs:
                                if idx >= 0: extracted_names[idx] = name
                    
                    if not extracted_names:
                        for k, v in data.items():
                            if isinstance(v, list) and len(v) > 50:
                                extracted_names = extract_from_list(v)
                                break
                            elif isinstance(v, dict) and len(v) > 50:
                                valid_vals =[int(val) for val in v.values() if isinstance(val, int) or str(val).lstrip('-').isdigit()]
                                if valid_vals:
                                    max_val = max(valid_vals)
                                    if max_val >= 0:
                                        extracted_names = ['unknown_tag'] * (max_val + 1)
                                        for tk, tv in v.items():
                                            if isinstance(tv, int) or str(tv).lstrip('-').isdigit():
                                                idx = int(tv)
                                                if idx >= 0: extracted_names[idx] = str(tk)
                                        break

            if extracted_names:
                self.tag_names = extracted_names
            else:
                self.tag_names = ['unknown_tag'] * 100000
                state.add_log(f"⚠️ ERREUR LECTURE TAGS ! Structure inconnue. 200 premiers caracteres: {str(data)[:200]}")
        elif os.path.exists(txt_path):
            with open(txt_path, 'r', encoding='utf-8') as f:
                self.tag_names =[line.strip() for line in f if line.strip()]

        # --- ЗАГРУЗКА ONNX ---
        if not os.path.exists(onnx_path):
            onnx_filename = "model.onnx"
            if "model.onnx" not in repo_files:
                for f in repo_files:
                    if f.endswith(".onnx"):
                        onnx_filename = f; break
            try:
                downloaded_onnx = hf_hub_download(repo_id=model_repo, filename=onnx_filename, local_dir=local_dir)
                if downloaded_onnx != onnx_path:
                    os.rename(downloaded_onnx, onnx_path)
            except Exception:
                raise Exception(f"Impossible de telecharger le modele ONNX depuis le depot {model_repo}")

        if not is_local_tagger_model_ready(local_dir):
            raise Exception(f"Le modèle de tags téléchargé dans {local_dir} est incomplet.")

        providers = ['CPUExecutionProvider'] if self._force_cpu else (['CUDAExecutionProvider', 'CPUExecutionProvider'] if self.device == "cuda" else ['CPUExecutionProvider'])
        use_cuda = (not self._force_cpu and self.device == "cuda")
        try:
            self.session = rt.InferenceSession(onnx_path, providers=providers)
        except Exception as e:
            if use_cuda:
                self._switch_to_cpu(str(e))
                self.session = rt.InferenceSession(onnx_path, providers=['CPUExecutionProvider'])
            else:
                raise
        self.model_name = model_repo

        # Динамическое определение размера входа (NCHW или NHWC)
        input_shape = self.session.get_inputs()[0].shape
        if len(input_shape) == 4:
            self.target_size = input_shape[2] if input_shape[1] == 3 else input_shape[1]
            if not isinstance(self.target_size, int): self.target_size = 448
        else:
            self.target_size = 448

    def unload(self):
        if self.session is not None:
            state.add_log(f"Dechargement du modele Tag {self.model_name} de la VRAM...")
            del self.session
            self.session = None
            self.model_name = None
            self.tag_names =[]
            gc.collect()

    def evaluate_media(self, directory_path, model_name, allowed_exts, override_files=None):
        all_files = self.se._gather_files(directory_path, allowed_exts) if override_files is None else[f for f in override_files if f.lower().endswith(allowed_exts)]
        image_paths =[p for p in all_files if p.lower().endswith(SUPPORTED_IMAGES)]
        video_paths =[p for p in all_files if p.lower().endswith(SUPPORTED_VIDEOS)]
        
        state.add_log(f"Trouve pour tagging: {len(image_paths)} photos, {len(video_paths)} videos.")
        cache_key = f"{model_name}_{self.video_frames}"
        
        images_to_process, videos_to_process = [],[]
        metadata_prompted = 0
        
        for p in image_paths:
            cached_tags = self.db_cache.get_tags(cache_key, p)
            # Skip only if tags already exist in cache. Having a cached prompt is NOT a reason to skip tag generation.
            if cached_tags is None or len(cached_tags) == 0:
                # 1) Prompt embarqué dans les métadonnées de l'image (ComfyUI/A1111)
                embedded_prompt = self._extract_prompt_from_image_metadata(p)
                if embedded_prompt:
                    self.db_cache.save_prompt(p, embedded_prompt, source="image_metadata_positive_prompt")
                    metadata_prompted += 1
                else:
                    # 2) Fallback: sidecar .txt à côté du fichier (SD WebUI / kohya / _prompt.txt)
                    existing = self.db_cache.get_prompt(p) or {}
                    if not str(existing.get('text') or '').strip():
                        txt_prompt = self._read_sidecar_prompt_txt(p)
                        if txt_prompt:
                            self.db_cache.save_prompt(p, txt_prompt, source="file_sidecar")
                            metadata_prompted += 1
                # 3) Sidecar .ia : pré-population du cache détection IA (sans Ollama)
                try:
                    if self.db_cache.get_ai_detection(p) is None:
                        ia_side = self._read_sidecar_ia(p)
                        if ia_side and ia_side.get('is_ai') is not None:
                            self.db_cache.save_ai_detection(
                                p,
                                bool(ia_side.get('is_ai')),
                                float(ia_side.get('confidence', 0.0) or 0.0),
                                str(ia_side.get('method', 'sidecar')),
                                ia_side,
                            )
                except Exception:
                    pass
                images_to_process.append(p)
        for p in video_paths:
            cached_tags = self.db_cache.get_tags(cache_key, p)
            # Reprocess if missing OR previously failed and stored as empty dict.
            if cached_tags is None or len(cached_tags) == 0:
                videos_to_process.append(p)

        if metadata_prompted:
            state.add_log(
                f"📝 {metadata_prompted} image(s) avec prompt embarqué détecté. Modèle de tags ignoré pour celles-ci."
            )

        if images_to_process or videos_to_process:
            self.load_model(model_name)
        else:
            return

        def _refresh_session_io():
            input_name_local = self.session.get_inputs()[0].name
            input_shape_local = self.session.get_inputs()[0].shape
            is_nchw_local = (len(input_shape_local) == 4 and input_shape_local[1] == 3)
            return input_name_local, is_nchw_local

        input_name, is_nchw = _refresh_session_io()

        def _prepare_batch_inputs(imgs, is_nchw_local):
            img_arrs =[]
            for img in imgs:
                max_dim = max(img.width, img.height)
                padded = Image.new('RGB', (max_dim, max_dim), (255, 255, 255))
                padded.paste(img, ((max_dim - img.width) // 2, (max_dim - img.height) // 2))
                padded = padded.resize((self.target_size, self.target_size), Image.Resampling.BICUBIC)
                arr = np.array(padded, dtype=np.float32)[:, :, ::-1]  # BGR
                if is_nchw_local:
                    arr = arr.transpose(2, 0, 1)
                img_arrs.append(arr)
            return np.stack(img_arrs)

        def _infer_with_auto_fallback(imgs, context_label="tags"):
            nonlocal input_name, is_nchw
            batch_inputs = _prepare_batch_inputs(imgs, is_nchw)
            try:
                probs_batch_local = self.session.run(None, {input_name: batch_inputs})[0]
                probs_batch_local = np.array(probs_batch_local, dtype=np.float32)
                if probs_batch_local.max() > 1.0 or probs_batch_local.min() < 0.0:
                    probs_batch_local = 1 / (1 + np.exp(-np.clip(probs_batch_local, -100, 100)))
                return probs_batch_local
            except Exception as e:
                err_text = str(e)
                cuda_markers = (
                    "cudaErrorNoKernelImageForDevice",
                    "CUDAExecutionProvider",
                    "no kernel image is available",
                )
                if (not self._force_cpu) and any(m.lower() in err_text.lower() for m in cuda_markers):
                    self._switch_to_cpu("provider CUDA incompatible")
                    self.load_model(model_name)
                    input_name, is_nchw = _refresh_session_io()
                    state.add_log(f"[TAGS] Retry {context_label} sur CPU...")

                    batch_inputs = _prepare_batch_inputs(imgs, is_nchw)
                    probs_batch_local = self.session.run(None, {input_name: batch_inputs})[0]
                    probs_batch_local = np.array(probs_batch_local, dtype=np.float32)
                    if probs_batch_local.max() > 1.0 or probs_batch_local.min() < 0.0:
                        probs_batch_local = 1 / (1 + np.exp(-np.clip(probs_batch_local, -100, 100)))
                    return probs_batch_local
                raise

        def process_batch(imgs, paths):
            try:
                probs_batch = _infer_with_auto_fallback(imgs, context_label="images")
                
                db_data =[]
                for j, p in enumerate(paths):
                    probs = probs_batch[j]
                    tags_dict = {str(tag): float(prob) for tag, prob in zip(self.tag_names, probs) if float(prob) >= self.min_save_threshold}
                    
                    if len(tags_dict) == 0:
                        state.add_log(f"⚠️ Debug: Pour le fichier {Path(p).name}, aucun tag >= {self.min_save_threshold}. Probabilite max modele: {float(probs.max()):.3f}")
                        
                    db_data.append((cache_key, p, tags_dict))
                    
                self.db_cache.save_tags_batch(db_data)
                
            except Exception as e:
                state.add_log(f"⚠️ Erreur inference tags (process_batch): {e}")
                self.db_cache.save_tags_batch([(cache_key, p, {}) for p in paths])

        # --- ОБРАБОТКА ИЗОБРАЖЕНИЙ ---
        batch_images, batch_paths = [],[]
        for i, img_path in enumerate(images_to_process):
            if not state.is_processing: break
            state.status_text = f"Tags photo: {Path(img_path).name} ({i+1}/{len(images_to_process)})"
            
            try:
                image = media_cache.get_image(img_path, self.target_size)
                if image:
                    batch_images.append(image)
                    batch_paths.append(img_path)
                else:
                    error_detail = getattr(media_cache, "last_image_error", "")
                    detail_suffix = f" ({error_detail})" if error_detail else ""
                    state.add_log(f"⚠️ Erreur: impossible de lire l'image {Path(img_path).name}{detail_suffix}")
                    self.db_cache.save_tags(cache_key, img_path, {})
            except Exception as e:
                state.add_log(f"⚠️ Erreur chargement {Path(img_path).name}: {e}")
                self.db_cache.save_tags(cache_key, img_path, {})

            if len(batch_images) >= self.batch_size or (i == len(images_to_process) - 1 and batch_images):
                state.progress = (i + 1) / max(1, len(images_to_process))
                process_batch(batch_images, batch_paths)
                batch_images, batch_paths = [],[]

        # --- ОБРАБОТКА ВИДЕО ---
        batch_images, batch_frame_counts, batch_paths = [], [],[]
        for i, vid_path in enumerate(videos_to_process):
            time.sleep(0.002)
            if not state.is_processing: break
            state.status_text = f"Tags video: {Path(vid_path).name} ({i+1}/{len(videos_to_process)})"
            
            try:
                frames = media_cache.get_video_frames(vid_path, self.target_size, self.video_frames)
                if frames:
                    batch_images.extend(frames)
                    batch_paths.append(vid_path)
                    batch_frame_counts.append(len(frames))
                else:
                    state.add_log(f"⚠️ Erreur: extraction des frames impossible depuis {Path(vid_path).name}")
                    self.db_cache.save_tags(cache_key, vid_path, {})
            except Exception as e:
                state.add_log(f"⚠️ Erreur chargement video {Path(vid_path).name}: {e}")
                self.db_cache.save_tags(cache_key, vid_path, {})

            if len(batch_images) >= self.batch_size or (i == len(videos_to_process) - 1 and batch_images):
                state.progress = (i + 1) / max(1, len(videos_to_process))
                
                try:
                    all_probs =[]
                    for k in range(0, len(batch_images), self.batch_size):
                        chunk = batch_images[k:k+self.batch_size]
                        probs_chunk = _infer_with_auto_fallback(chunk, context_label="video")
                        all_probs.extend(probs_chunk)
                        
                    idx = 0
                    db_data =[]
                    for p, count in zip(batch_paths, batch_frame_counts):
                        vid_probs = np.stack(all_probs[idx : idx + count])
                        idx += count
                        max_probs = vid_probs.max(axis=0)
                        tags_dict = {str(tag): float(prob) for tag, prob in zip(self.tag_names, max_probs) if float(prob) >= self.min_save_threshold}
                        
                        if len(tags_dict) == 0:
                            state.add_log(f"⚠️ Debug: Pour la video {Path(p).name}, aucun tag >= {self.min_save_threshold}. Probabilite max: {float(max_probs.max()):.3f}")
                            
                        db_data.append((cache_key, p, tags_dict))
                        
                    self.db_cache.save_tags_batch(db_data)
                except Exception as e: state.add_log(f"⚠️ Erreur inference tags (video): {e}")
                
                batch_images, batch_frame_counts, batch_paths = [], [], []

# ==========================================
# 4. СОСТОЯНИЕ И UI УТИЛИТЫ
# ==========================================
class AppState:
    def __init__(self):
        # Initialize cache FIRST so it's available for auto-clear
        self.db_cache = DatabaseCache()
        
        self.search_results =[]
        self.aesthetic_results =[]
        self.nsfw_results =[]
        self.nsfw_all_results =[]
        self.face_results =[]
        self.tags_results =[]
        self.prompt_results =[]
        
        self.sel_search = {}
        self.sel_aes = {}
        self.aes_scan_mode = 'score'  # 'score' or 'blur'
        self.sel_nsfw = {}
        self.sel_face = {}
        self.sel_tags = {}
        self.sel_prompt = {}

        # Wizard de génération multi-photos (mode séquentiel: 1 photo à la fois)
        self.prompt_wizard_queue = []   # liste des chemins restant à traiter (incluant le courant)
        self.prompt_wizard_total = 0    # nombre total initial
        self.prompt_wizard_done = 0     # nombre déjà sauvegardés
        
        self.search_page = 1
        self.aes_page = 1
        self.nsfw_page = 1
        self.face_page = 1
        self.tags_page = 1
        self.prompt_page = 1
        
        self.search_base_dir = ""
        self.aes_base_dir = ""
        self.nsfw_base_dir = ""
        self.face_base_dir = ""
        self.tags_base_dir = ""
        self.prompt_base_dir = ""
        
        self.search_res_filter = 'Tout'
        self.aes_res_filter = 'Tout'
        self.nsfw_res_filter = 'Tout'
        self.nsfw_hide_sain = True
        self.nsfw_hide_sensuel = False
        self.nsfw_hide_explicite = False
        self.nsfw_bulk_new_label = 'SAIN'
        self.face_res_filter = 'Tout'
        self.tags_res_filter = 'Tout'
        self.prompt_res_filter = 'Tout'
        self.tags_nsfw_filter = 'Tout'   # Tout | Sain | Sensuel | Explicit | Non validé
        self.prompt_nsfw_filter = 'Tout' # Tout | Sain | Sensuel | Explicit | Non validé
        self.prompt_hide_with_prompt = False
        self.tags_search = ''
        self.prompt_search = ''
        self.tags_sort = 'score'
        self.prompt_sort = 'score'
        self.aes_sort = 'score'
        self.nsfw_sort = 'score'
        self.tags_per_page = 40
        self.prompt_per_page = 40
        self.aes_per_page = 40
        self.nsfw_per_page = 40
        self.tags_compact = False
        self.prompt_compact = False
        self.aes_compact = False
        self.nsfw_compact = False
        self.aes_search = ''
        self.nsfw_search = ''

        # --- Détecteur IA ---
        self.sel_ia = {}
        self.ia_results = []          # (score, path, is_ai, confidence, method, detection)
        self.ia_page = 1
        self.ia_base_dir = ""
        self.ia_res_filter = 'Tout'
        self.ia_status_filter = 'Tout'  # Tout | IA | Photo | Inconnu
        self.ia_search = ''
        self.ia_sort = 'score'
        self.ia_per_page = 40
        self.ia_compact = False
        self.aes_nsfw_filter_res = 'Tout'
        
        self.viewer_open = False
        self.viewer_items =[]
        self.viewer_index = 0
        
        self.is_processing = False
        self.progress = 0.0
        self.status_text = "Pret a l'emploi"
        
        self.logs =[]
        self.full_log_history =[]
        self.current_tab = 'Search'

        # NSFW settings (no presets, just model + threshold)
        self.nsfw_model = DEFAULT_NSFW_MODEL
        self.nsfw_threshold = NSFW_THRESHOLD
        self.flatten_structure = False
        self.grid_columns = 4

    def add_log(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        self.logs.append(line)
        self.full_log_history.append(line)

state = AppState()

# Auto-clear NSFW cache if version changed
cfg = load_config()
last_seen_version = cfg.get('_nsfw_cache_version')
if last_seen_version != NSFW_CACHE_VERSION:
    state.db_cache.clear_nsfw_cache()
    save_config({'_nsfw_cache_version': NSFW_CACHE_VERSION})
    print(f"[STARTUP] Cache NSFW effacé (version: {last_seen_version} → {NSFW_CACHE_VERSION})")

search_engine = SearchEngine(
    log_callback=lambda m: state.add_log(m),
    progress_callback=lambda p, m: setattr(state, 'status_text', m) or setattr(state, 'progress', p),
    db_cache=state.db_cache
)
aesthetic_engine = AestheticEngine(search_engine)
nsfw_engine = NsfwEngine(search_engine)
face_engine = FaceEngine(search_engine)
tag_engine = TagEngine(search_engine)

# --- Helpers d'indexation de masse (IA + Prompt) ---
def bulk_run_ia_detection(paths, ollama_model: str = "", use_ollama: bool = False, force: bool = False):
    """Detection IA en lot. Skippe les fichiers deja en cache sauf si force=True.
    save_ai_detection ecrit automatiquement le sidecar .ia."""
    total = len(paths)
    if not total:
        return
    state.add_log(f"[BULK-IA] {total} image(s) a traiter (ollama={'oui' if use_ollama else 'non'}, modele='{ollama_model or '-'}', force={force})")
    done = 0
    for fp in paths:
        if not state.is_processing or search_engine.cancel_flag:
            state.add_log("[BULK-IA] Annule par utilisateur")
            break
        done += 1
        state.progress = done / total
        state.status_text = f"IA {done}/{total} : {os.path.basename(fp)}"
        try:
            if not force:
                cached = search_engine.db_cache.get_ai_detection(fp)
                if cached and cached.get('is_ai') is not None:
                    continue
            res = run_ai_detection(fp, ollama_model=ollama_model, use_ollama_fallback=use_ollama)
            if not isinstance(res, dict) or res.get('is_ai') is None:
                continue
            search_engine.db_cache.save_ai_detection(
                fp,
                bool(res.get('is_ai')),
                float(res.get('confidence', 0.0)),
                str(res.get('method', '')),
                res,
            )
        except Exception as e:
            state.add_log(f"[BULK-IA] err {fp}: {e}")
    state.add_log(f"[BULK-IA] Termine ({done}/{total})")

def bulk_run_prompt_generation(paths, provider: str = "local", model: str = "", mode: str = "both", force: bool = False):
    """Genere prompts raw et/ou detailed en lot. save_prompt/save_detailed_prompt
    ecrivent automatiquement le sidecar {stem}_prompt.txt."""
    total = len(paths)
    if not total:
        return
    state.add_log(f"[BULK-PROMPT] {total} image(s), provider={provider}, mode={mode}, modele='{model or '-'}', force={force}")
    done = 0
    src_tag = f"bulk_{provider}" + (f":{model}" if provider == 'ollama' and model else '')
    for fp in paths:
        if not state.is_processing or search_engine.cancel_flag:
            state.add_log("[BULK-PROMPT] Annule par utilisateur")
            break
        done += 1
        state.progress = done / total
        state.status_text = f"Prompt {done}/{total} : {os.path.basename(fp)}"
        try:
            payload = _collect_llm_payload_from_cache(fp)
            base_text = str(payload.get('prompt') or '').strip()
            tags_dict = payload.get('tags') or {}
            ai_payload = payload.get('ai') or None

            def _gen(sub_mode: str) -> str:
                if provider == 'ollama' and model:
                    if sub_mode == 'detailed':
                        return _ollama_detailed_prompt(model, base_text, tags_dict, ai_payload, fp)
                    return _ollama_raw_prompt(model, base_text, tags_dict, fp)
                # local fallback
                if sub_mode == 'detailed':
                    return TagEngine._build_detailed_prompt_fallback(base_text, tags_dict, ai_payload, fp)
                # raw local = base_text si dispo, sinon liste de tags
                if base_text:
                    return base_text
                if tags_dict:
                    return ", ".join(sorted(tags_dict.keys(), key=lambda k: tags_dict[k], reverse=True))
                return ''

            if mode in ('raw', 'both'):
                if force or not (search_engine.db_cache.get_prompt(fp) or {}).get('text'):
                    raw_out = _gen('raw')
                    if raw_out:
                        search_engine.db_cache.save_prompt(fp, raw_out, source=src_tag)
            if mode in ('detailed', 'both'):
                if force or not (search_engine.db_cache.get_detailed_prompt(fp) or {}).get('text'):
                    det_out = _gen('detailed')
                    if det_out:
                        search_engine.db_cache.save_detailed_prompt(fp, det_out, source=src_tag)
        except Exception as e:
            state.add_log(f"[BULK-PROMPT] err {fp}: {e}")
    state.add_log(f"[BULK-PROMPT] Termine ({done}/{total})")

def open_file_native(filepath):
    try: os.startfile(filepath) if os.name == 'nt' else subprocess.call(('xdg-open', filepath))
    except Exception as e: ui.notify(f"Erreur d'ouverture : {e}", type='negative')

def reveal_file_native(filepath):
    try:
        if os.name == 'nt': # Windows
            subprocess.run(['explorer', '/select,', os.path.normpath(filepath)])
        elif sys.platform == 'darwin': # macOS
            subprocess.run(['open', '-R', filepath])
        else: # Linux
            desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
            if 'gnome' in desktop or 'unity' in desktop:
                subprocess.Popen(['nautilus', '--select', filepath])
            elif 'kde' in desktop:
                subprocess.Popen(['dolphin', '--select', filepath])
            else: # Fallback для остальных Linux
                subprocess.Popen(['xdg-open', os.path.dirname(filepath)])
    except Exception as e: 
        ui.notify(f"Erreur d'ouverture du dossier : {e}", type='negative')

def pick_folder_native():
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.attributes('-topmost', True)
    root.withdraw()
    folder = filedialog.askdirectory()
    root.destroy()
    return folder

async def select_folder(input_element):
    folder = await run.io_bound(pick_folder_native)
    if folder: input_element.value = folder

def pick_file_native():
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.attributes('-topmost', True)
    root.withdraw()
    file = filedialog.askopenfilename(filetypes=[("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.tiff")])
    root.destroy()
    return file

async def select_file(input_element):
    file = await run.io_bound(pick_file_native)
    if file: input_element.value = file

def clear_folder_cache(folder_path):
    if not folder_path: return
    normalized_target = os.path.normcase(os.path.normpath(folder_path))
    keys_to_delete = [
        k for k in list(search_engine.files_cache._data.keys())
        if os.path.normcase(os.path.normpath(k)) == normalized_target
    ]
    if keys_to_delete:
        for k in keys_to_delete:
            del search_engine.files_cache._data[k]
        search_engine.files_cache.save_cache()
        ui.notify(f'Index du dossier effacé !', type='positive')
    else:
        ui.notify(f'Dossier introuvable dans l\'index (cache vide)', type='info')

def clear_prompt_folder_cache(folder_path):
    if not folder_path:
        return 0, 0

    normalized_target = os.path.normcase(os.path.normpath(folder_path))
    all_files = search_engine._gather_files(folder_path, tuple(SUPPORTED_IMAGES + SUPPORTED_VIDEOS))
    matching_paths = [
        path for path in all_files
        if os.path.normcase(os.path.normpath(os.path.dirname(path))) == normalized_target
        or os.path.normcase(os.path.normpath(path)).startswith(normalized_target + os.sep)
    ]
    if not matching_paths:
        return 0, 0

    c = search_engine.db_cache.conn.cursor()
    prompt_deleted = 0
    detailed_deleted = 0
    chunk_size = 900
    for i in range(0, len(matching_paths), chunk_size):
        chunk = matching_paths[i:i + chunk_size]
        placeholders = ','.join(['?'] * len(chunk))
        c.execute(f"DELETE FROM prompt_cache WHERE path IN ({placeholders})", chunk)
        prompt_deleted += c.rowcount if c.rowcount != -1 else 0
        c.execute(f"DELETE FROM detailed_prompt_cache WHERE path IN ({placeholders})", chunk)
        detailed_deleted += c.rowcount if c.rowcount != -1 else 0
    search_engine.db_cache.conn.commit()
    return prompt_deleted, detailed_deleted

# Liste d'elements ui.log additionnels (panneaux inline par onglet)
_extra_log_elements = []

def register_log_panel(elem):
    if elem not in _extra_log_elements:
        _extra_log_elements.append(elem)

def update_ui_logs():
    if not state.logs:
        return
    has_main = 'ui_log_element' in globals()
    for msg in state.logs:
        if has_main:
            try: ui_log_element.push(msg)
            except Exception: pass
        for elem in _extra_log_elements:
            try: elem.push(msg)
            except Exception: pass
    state.logs.clear()

def clear_logs():
    state.full_log_history.clear()
    ui_log_element.clear()
    state.add_log("Journaux effacés.")

def copy_logs():
    ui.clipboard.write('\n'.join(state.full_log_history))
    ui.notify('Journaux copiés !', type='positive', color='green')

# --- COPIE PRESSE-PAPIERS MULTIPLATEFORME ---
def copy_image_to_clipboard(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in SUPPORTED_VIDEOS:
        ui.notify('Impossible de copier une vidéo dans le presse-papiers', type='warning')
        return
    if ext in SUPPORTED_TEXTS:
        ui.notify('Impossible de copier du texte comme image', type='warning')
        return
        
    try:
        if os.name == 'nt':
            import ctypes
            from PIL import Image
            import io
            
            # Lecture image via PIL (compatible webp)
            img = Image.open(path).convert('RGB')
            output = io.BytesIO()
            img.save(output, 'BMP')
            data = output.getvalue()[14:] # Ignore 14 octets de l'en-tete BMP
            output.close()
            
            # Utilise ctypes pour acces direct a l'API Windows
            CF_DIB = 8
            GMEM_MOVEABLE = 0x0002
            
            # Definit les types pour eviter la troncature 64 bits
            ctypes.windll.kernel32.GlobalAlloc.restype = ctypes.c_void_p
            ctypes.windll.kernel32.GlobalAlloc.argtypes =[ctypes.c_uint, ctypes.c_size_t]
            ctypes.windll.kernel32.GlobalLock.restype = ctypes.c_void_p
            ctypes.windll.kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            ctypes.windll.kernel32.GlobalUnlock.restype = ctypes.c_int
            ctypes.windll.kernel32.GlobalUnlock.argtypes =[ctypes.c_void_p]
            ctypes.windll.user32.SetClipboardData.restype = ctypes.c_void_p
            ctypes.windll.user32.SetClipboardData.argtypes =[ctypes.c_uint, ctypes.c_void_p]
            
            hGlobalMem = ctypes.windll.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not hGlobalMem:
                raise Exception("Impossible d'allouer la mémoire")
                
            lpGlobalMem = ctypes.windll.kernel32.GlobalLock(hGlobalMem)
            if not lpGlobalMem:
                raise Exception("Impossible de verrouiller la mémoire")
                
            ctypes.memmove(lpGlobalMem, data, len(data))
            ctypes.windll.kernel32.GlobalUnlock(hGlobalMem)
            
            if not ctypes.windll.user32.OpenClipboard(0):
                raise Exception("Presse-papiers occupé par un autre processus")
                
            try:
                ctypes.windll.user32.EmptyClipboard()
                ctypes.windll.user32.SetClipboardData(CF_DIB, hGlobalMem)
            finally:
                ctypes.windll.user32.CloseClipboard()
                
        elif sys.platform == 'darwin':
            abs_path = os.path.abspath(path)
            subprocess.run(['osascript', '-e', f'set the clipboard to (read (POSIX file "{abs_path}") as JPEG picture)'])
        else:
            import mimetypes
            mimetype, _ = mimetypes.guess_type(path)
            if not mimetype: mimetype = 'image/png'
            subprocess.run(['xclip', '-selection', 'clipboard', '-t', mimetype, '-i', path])
            
        ui.notify('Image copiée dans le presse-papiers !', type='positive')
    except Exception as e:
        ui.notify(f'Erreur copie presse-papiers : {e}', type='negative')

# ==========================================
# 5. ВЕРСТКА И ИНТЕРФЕЙС NICEGUI
# ==========================================
@ui.page('/')
def index_page():
    cfg = load_config()
    state.nsfw_model = cfg.get('nsfw_model', DEFAULT_NSFW_MODEL)
    state.nsfw_threshold = float(cfg.get('nsfw_threshold', NSFW_THRESHOLD))
    state.flatten_structure = bool(cfg.get('flatten_structure', False))
    state.grid_columns = int(cfg.get('grid_columns', 4))

    def cancel_all_tasks():
        if state.is_processing:
            search_engine.cancel()         # Флаг для Поиска и Индексатора
            state.is_processing = False    # Флаг для Эстетики и NSFW
            state.add_log("🛑 Signal d'interruption envoye...")
            state.status_text = "Arret des traitements (fin du lot courant)..."
            ui.notify('Arret en cours...', type='warning', position='top')

    ui.colors(primary='#2563eb', secondary='#10b981', accent='#f59e0b', dark='#1e1e2f')
    ui.query('body').classes('bg-[#121212] text-white overflow-hidden m-0 p-0')

    with ui.header().classes('bg-gray-900 border-b border-gray-800 flex justify-between items-center px-4 py-0 shrink-0 h-[60px]'):
        ui.label('🤖 AI Media Organizer Pro').classes('text-xl font-bold tracking-wider text-blue-400 shrink-0')
        
        with ui.tabs().bind_value(state, 'current_tab').classes('h-full') as tabs:
            tab_search = ui.tab('Search', label='Recherche IA', icon='search')
            tab_aesthetic = ui.tab('Aesthetic', label='Esthétique', icon='star')
            tab_nsfw = ui.tab('NSFW', label='Détecteur NSFW', icon='visibility_off')
            tab_face = ui.tab('Face', label='Recherche Visage', icon='face')
            tab_tags = ui.tab('Tags', label='Tags Danbooru', icon='label')
            tab_prompt = ui.tab('Prompt', label='Prompts', icon='article')
            tab_ia = ui.tab('IA', label='Détecteur IA', icon='auto_awesome')
            tab_cache = ui.tab('Cache', label='Indexeur', icon='storage')
            
        ui.button(icon='settings', on_click=lambda: global_settings_dialog.open()).props('flat round dense text-color=white').classes('shrink-0').tooltip('Paramètres généraux')
        
    with ui.dialog() as global_settings_dialog:
        with ui.card().classes('w-[500px] max-w-full bg-gray-900 text-white border border-gray-700'):
            ui.label('Paramètres généraux').classes('text-xl font-bold mb-2 text-blue-400')

            with ui.dialog() as local_models_dialog:
                with ui.card().classes('w-[760px] max-w-full bg-gray-900 text-white border border-gray-700'):
                    ui.label('Vérification des modèles locaux').classes('text-xl font-bold mb-2 text-blue-400')
                    local_models_report = ui.label('').classes('w-full whitespace-pre-wrap font-mono text-xs text-gray-200 max-h-[60vh] overflow-auto')
                    ui.button('Fermer', on_click=local_models_dialog.close).classes('w-full mt-4 bg-gray-800 hover:bg-gray-700')

            def open_local_models_dialog():
                local_models_report.text = "\n".join(build_local_models_report_lines())
                local_models_report.update()
                local_models_dialog.open()
            
            ui.number('Seuil de danger NSFW (0.0 - 1.0)', value=state.nsfw_threshold, min=0.0, max=1.0, step=0.01, format='%.2f').bind_value(state, 'nsfw_threshold').classes('w-full')
            ui.number('Colonnes de la grille (plus = plus petit)', value=state.grid_columns, min=1, max=12, format='%d').bind_value(state, 'grid_columns').classes('w-full mt-2')
            ui.checkbox('Copier/Déplacer sans structure de dossiers (répertoire plat)', value=state.flatten_structure).bind_value(state, 'flatten_structure').classes('w-full mt-2')
            
            # --- GESTION DU CACHE ---
            ui.label('Gestion de la base de données et du cache').classes('text-lg font-bold mt-6 mb-2 text-red-400')
            
            with ui.row().classes('w-full gap-2 items-center'):
                model_to_clear = ui.select(['Tous les modeles'], value='Tous les modeles', label='Sélectionner le modèle à effacer').classes('flex-grow')
                
                def clear_selected_model():
                    if model_to_clear.value == 'Tous les modeles':
                        search_engine.db_cache.clear_model_cache(None)
                        ui.notify("Base de données ENTIÈREMENT effacée et compressée !", type="positive")
                    else:
                        search_engine.db_cache.clear_model_cache(model_to_clear.value)
                        ui.notify(f"Cache du modèle {model_to_clear.value} effacé !", type="positive")
                    refresh_models_list()

                ui.button(icon='delete_forever', on_click=clear_selected_model).props('color=red').tooltip('Effacer la BD pour le modele selectionne')

            def refresh_models_list():
                models = search_engine.db_cache.get_all_models()
                model_to_clear.options = ['Tous les modeles'] + models
                model_to_clear.value = 'Tous les modeles'
                model_to_clear.update()

            global_settings_dialog.on('show', refresh_models_list)

            async def cleanup_dead_links():
                ui.notify("Recherche des fichiers supprimés... Cela peut prendre du temps", type="info")
                def task():
                    paths = search_engine.db_cache.get_all_paths()
                    dead =[p for p in paths if not os.path.exists(p)]
                    if dead:
                        search_engine.db_cache.remove_paths(dead)
                        search_engine.db_cache.conn.execute("VACUUM")
                    return len(dead)
                dead_count = await run.io_bound(task)
                if dead_count > 0:
                    ui.notify(f"{dead_count} entrées mortes supprimées de la BD !", type="positive")
                else:
                    ui.notify("Aucune entrée morte trouvée, la BD est propre.", type="positive")

            def cleanup_thumbnails():
                count = 0
                for f in os.listdir(THUMB_CACHE_DIR):
                    try:
                        os.remove(os.path.join(THUMB_CACHE_DIR, f))
                        count += 1
                    except: pass
                ui.notify(f"{count} miniatures supprimées", type="positive")

            def cleanup_file_index():
                search_engine.files_cache._data.clear()
                search_engine.files_cache.save_cache()
                ui.notify("Index des fichiers (cache chemins) réinitialisé", type="positive")

            with ui.column().classes('w-full gap-2 mt-4'):
                ui.button('Supprimer les entrées orphelines (fichiers absents du disque)', on_click=cleanup_dead_links).props('outline color=orange').classes('w-full')
                with ui.row().classes('w-full gap-2'):
                    ui.button('Effacer les miniatures', on_click=cleanup_thumbnails).props('outline color=gray').classes('flex-grow')
                    ui.button('Réinitialiser l\'index des dossiers', on_click=cleanup_file_index).props('outline color=gray').classes('flex-grow')
                ui.button('Vérifier modèles locaux', on_click=open_local_models_dialog).props('outline color=blue').classes('w-full')
                
                def clear_nsfw_cache_action():
                    state.db_cache.clear_nsfw_cache()
                    ui.notify('Cache NSFW effacé. Relancez l\'analyse NSFW pour recalculer les scores.', type='positive')
                
                ui.button('Vider cache NSFW', on_click=clear_nsfw_cache_action).props('outline color=red-700').classes('w-full').tooltip('Force réanalyse des fichiers NSFW')

            def save_global_settings():
                state.grid_columns = int(state.grid_columns)
                save_config({
                    'nsfw_model': state.nsfw_model,
                    'nsfw_threshold': state.nsfw_threshold,
                    'flatten_structure': state.flatten_structure,
                    'grid_columns': state.grid_columns
                })
                ui.notify('Paramètres généraux sauvegardés', type='positive')
                search_gallery_ui.refresh()
                aesthetic_gallery_ui.refresh()
                nsfw_gallery_ui.refresh()
                face_gallery_ui.refresh()
                tags_gallery_ui.refresh()
                global_settings_dialog.close()
                
            ui.button('Sauvegarder et fermer', on_click=save_global_settings).classes('w-full mt-6 bg-blue-600 hover:bg-blue-500 font-bold')

    with ui.right_drawer(value=False).props('width=550').classes('bg-gray-900 border-l border-gray-800 p-4 z-50 flex flex-col') as log_drawer:
        with ui.row().classes('w-full flex justify-between items-center mb-2 shrink-0'):
            ui.label('Journaux système').classes('text-lg font-bold text-white')
            with ui.row().classes('gap-2'):
                ui.button(icon='content_copy', on_click=copy_logs).props('flat round dense text-color=gray').tooltip('Copier tous les journaux')
                ui.button(icon='delete_sweep', on_click=clear_logs).props('flat round dense text-color=red').tooltip('Effacer les journaux')
        
        global ui_log_element
        ui_log_element = ui.log().classes('w-full flex-grow bg-black text-green-400 font-mono text-xs p-2 rounded overflow-y-auto whitespace-pre-wrap break-words')

    # --- DIALOGUE DEBUG NSFW ---
    with ui.dialog() as nsfw_debug_dialog:
        with ui.card().classes('w-[500px] max-w-full bg-gray-900 text-white border border-gray-700'):
            debug_title = ui.label('Détails NSFW').classes('text-lg font-bold mb-2 break-all')
            debug_container = ui.column().classes('w-full gap-1 max-h-[60vh] overflow-y-auto')
            ui.button('Fermer', on_click=nsfw_debug_dialog.close).classes('w-full mt-4 bg-gray-800 hover:bg-gray-700')

    def show_nsfw_debug(path, details):
        debug_title.set_text(os.path.basename(path))
        debug_container.clear()
        safe_set = {'safe', 'sfw', 'normal', 'general', 'neutral', 'drawing', 'safe_content', 'anime picture', 'anime'}
        neutral_set = {'suggestive'}  # no more modes
        
        with debug_container:
            # Show metadata
            if '_raw_top_label' in details:
                ui.label(f"Top Label: {details['_raw_top_label']}").classes('text-blue-400 text-xs font-mono')
            if '_portrait_guard' in details and details['_portrait_guard'] > 0:
                ui.label("⚠️ PORTRAIT DÉTECTÉ → danger capped").classes('text-yellow-400 text-xs font-bold')
            
            ui.separator()
            
            # Show category legend
            with ui.row().classes('w-full gap-4 text-xs mb-2 px-2'):
                ui.label("🟢 SAFE").classes('text-green-400 font-mono')
                ui.label("🟡 NEUTRAL").classes('text-yellow-400 font-mono')
                ui.label("🔴 UNSAFE").classes('text-red-400 font-mono')
            
            ui.separator()
            
            # Show scores sorted by probability
            sorted_details = sorted([(k, v) for k, v in details.items() if not k.startswith('_')], key=lambda x: x[1], reverse=True)
            for lbl, prob in sorted_details:
                lbl_lower = lbl.lower()
                if lbl_lower in safe_set:
                    color = "text-green-400 font-bold"
                    marker = "🟢"
                elif lbl_lower in neutral_set:
                    color = "text-yellow-400 font-bold"
                    marker = "🟡"
                else:
                    color = "text-red-400 font-bold" if prob > 0.15 else "text-orange-400"
                    marker = "🔴"
                
                with ui.row().classes('w-full justify-between border-b border-gray-800 py-1 px-2 items-center'):
                    ui.label(f"{marker} {lbl}").classes(f'font-mono text-sm {color} flex-1 break-all')
                    ui.label(f"{prob*100:.2f}%").classes(f'font-mono text-sm {color} min-w-fit')
        nsfw_debug_dialog.open()

    with ui.dialog() as tags_debug_dialog:
        with ui.card().classes('w-[500px] max-w-full bg-gray-900 text-white border border-gray-700'):
            tags_debug_title = ui.label('Tags (Danbooru)').classes('text-lg font-bold mb-2 break-all')
            tags_debug_text = ui.label('').classes('w-full max-h-[60vh] overflow-y-auto font-mono text-xs whitespace-pre-wrap break-all text-pink-200 bg-gray-950/50 rounded p-2')
            ui.button('Fermer', on_click=tags_debug_dialog.close).classes('w-full mt-4 bg-gray-800 hover:bg-gray-700')

    with ui.dialog() as prompt_debug_dialog:
        with ui.card().classes('w-[760px] max-w-full bg-gray-900 text-white border border-gray-700'):
            prompt_debug_title = ui.label('Prompt').classes('text-lg font-bold mb-2 break-all')
            ui.label('Prompt brut').classes('text-xs uppercase tracking-wide text-gray-400 mb-1')
            prompt_debug_text = ui.textarea().props('readonly autogrow filled').classes('w-full')
            ui.label('Prompt détaillé').classes('text-xs uppercase tracking-wide text-gray-400 mt-3 mb-1')
            prompt_detailed_text = ui.textarea().props('readonly autogrow filled').classes('w-full')
            ui.button('Fermer', on_click=prompt_debug_dialog.close).classes('w-full mt-4 bg-gray-800 hover:bg-gray-700')

    def show_tags_debug(path, tags_dict):
        tags_debug_title.set_text(os.path.basename(path))
        sorted_tags = sorted(tags_dict.items(), key=lambda x: x[1], reverse=True)
        # One-shot render avoids flooding native event channel with many element updates.
        max_rows = 1200
        lines = [f"{lbl}: {prob*100:.2f}%" for lbl, prob in sorted_tags[:max_rows]]
        if len(sorted_tags) > max_rows:
            lines.append(f"... ({len(sorted_tags) - max_rows} tags supplémentaires non affichés)")
        tags_debug_text.set_text('\n'.join(lines) if lines else 'Aucun tag disponible')
        tags_debug_dialog.open()

    def show_prompt_debug(path):
        payload = _collect_llm_payload_from_cache(path)
        prompt_debug_title.set_text(os.path.basename(path))
        prompt_debug_text.value = str(((payload.get('prompt') or {}).get('text') or '')).strip() or 'Aucun prompt.'
        prompt_detailed_text.value = str(((payload.get('detailed_prompt') or {}).get('text') or '')).strip() or 'Aucun prompt détaillé.'
        prompt_debug_text.update()
        prompt_detailed_text.update()
        prompt_debug_dialog.open()

    # --- LECTEUR PLEIN ECRAN ---
    with ui.dialog().on('hide', lambda: setattr(state, 'viewer_open', False)).props('maximized transition-show=fade transition-hide=fade') as media_dialog:
        with ui.element('div') \
            .classes('w-full h-full bg-black/95 p-0 flex flex-col relative items-center justify-center overflow-hidden') \
            .on('wheel.prevent', lambda e: change_media(1 if e.args['deltaY'] > 0 else -1),['deltaY']) \
            .on('click.self', media_dialog.close):
            
            ui.button(icon='close', on_click=media_dialog.close).classes('absolute top-4 right-4 z-50 bg-white/10 hover:bg-white/20 text-white').props('flat round')
            
            ui.button(icon='chevron_left', on_click=lambda: change_media(-1)).classes('absolute left-4 top-1/2 -translate-y-1/2 z-50 bg-white/10 hover:bg-white/20 text-white text-4xl p-2').props('flat round').tooltip('Précédent (←)')
            ui.button(icon='chevron_right', on_click=lambda: change_media(1)).classes('absolute right-4 top-1/2 -translate-y-1/2 z-50 bg-white/10 hover:bg-white/20 text-white text-4xl p-2').props('flat round').tooltip('Suivant (→)')
            
            # Отступ p-8 не даст картинке залезть под боковые кнопки
            media_container = ui.element('div') \
                .classes('absolute inset-0 w-full h-full flex items-center justify-center z-0 p-8') \
                .on('click.self', media_dialog.close)
            
            with ui.row().classes('absolute bottom-6 left-1/2 -translate-x-1/2 bg-black/80 border border-gray-700 px-4 py-2 rounded-full text-white flex-nowrap items-center gap-3 z-50 shadow-lg'):
                btn_viewer_select = ui.button(on_click=lambda: toggle_selection()).props('flat round dense size=sm').tooltip('Sélectionner (Espace)')
                lbl_media_name = ui.label().classes('font-mono text-xs text-center whitespace-nowrap overflow-hidden text-ellipsis min-w-[150px] max-w-[400px] px-2')
                ui.button(icon='content_copy', on_click=lambda: copy_image_to_clipboard(state.viewer_items[state.viewer_index])).props('flat round dense size=sm color=white').tooltip('Copier l\'image dans le presse-papiers (C)')
                ui.button(icon='download', on_click=lambda: download_current_item()).props('flat round dense size=sm color=white').tooltip('Enregistrer dans Téléchargements (D)')

    def update_viewer_selection_ui():
        if not state.viewer_items: return
        path = state.viewer_items[state.viewer_index]
        is_selected = False
        if state.current_tab == 'Search' and path in state.sel_search: is_selected = state.sel_search[path]
        elif state.current_tab == 'Aesthetic' and path in state.sel_aes: is_selected = state.sel_aes[path]
        elif state.current_tab == 'NSFW' and path in state.sel_nsfw: is_selected = state.sel_nsfw[path]
        elif state.current_tab == 'Face' and path in state.sel_face: is_selected = state.sel_face[path]
        elif state.current_tab == 'Tags' and path in state.sel_tags: is_selected = state.sel_tags[path]
        elif state.current_tab == 'Prompt' and path in state.sel_prompt: is_selected = state.sel_prompt[path]
            
        btn_viewer_select._props['icon'] = 'check_box' if is_selected else 'check_box_outline_blank'
        btn_viewer_select._props['color'] = 'green' if is_selected else 'white'
        btn_viewer_select.update()

    def toggle_selection():
        if not state.viewer_items: return
        path = state.viewer_items[state.viewer_index]
        if state.current_tab == 'Search' and path in state.sel_search:
            state.sel_search[path] = not state.sel_search[path]
        elif state.current_tab == 'Aesthetic' and path in state.sel_aes:
            state.sel_aes[path] = not state.sel_aes[path]
        elif state.current_tab == 'NSFW' and path in state.sel_nsfw:
            state.sel_nsfw[path] = not state.sel_nsfw[path]
        elif state.current_tab == 'Face' and path in state.sel_face:
            state.sel_face[path] = not state.sel_face[path]
        elif state.current_tab == 'Tags' and path in state.sel_tags:
            state.sel_tags[path] = not state.sel_tags[path]
        elif state.current_tab == 'Prompt' and path in state.sel_prompt:
            state.sel_prompt[path] = not state.sel_prompt[path]
        update_viewer_selection_ui()

    def download_current_item():
        if not state.viewer_items: return
        path = state.viewer_items[state.viewer_index]
        tab = state.current_tab.lower()
        base_dir = getattr(state, f"{tab}_base_dir", state.search_base_dir)
        try:
            dl_dir = os.path.join(str(Path.home()), 'Downloads')
            try:
                rel_path = os.path.relpath(path, base_dir)
                if rel_path.startswith('..') or os.path.isabs(rel_path): rel_path = os.path.basename(path)
            except: rel_path = os.path.basename(path)
                
            dest = os.path.join(dl_dir, rel_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            
            if os.path.exists(dest):
                base, ext = os.path.splitext(os.path.basename(dest))
                dest = os.path.join(os.path.dirname(dest), f"{base}_{int(time.time())}{ext}")
                
            shutil.copy2(path, dest)
            ui.notify(f'Sauvegarde: {rel_path}', type='positive', position='bottom-right', timeout=1500, group='downloads')
        except Exception as e:
            ui.notify(f'Erreur: {e}', type='negative', position='bottom-right', timeout=2000, group='err_downloads')

    def render_viewer():
        if not state.viewer_items:
            return
        state.viewer_index = max(0, min(len(state.viewer_items) - 1, int(state.viewer_index)))
        path = state.viewer_items[state.viewer_index]
        safe_path = urllib.parse.quote(path)
        ext = os.path.splitext(path)[1].lower()

        media_container.clear()
        with media_container:
            if ext in SUPPORTED_IMAGES:
                ui.image(f"/media/{safe_path}").classes('max-w-full max-h-full object-contain select-none').props('fit=contain')
            elif ext in SUPPORTED_VIDEOS:
                ui.video(f"/media/{safe_path}").classes('max-w-full max-h-full').props('controls autoplay')
            else:
                with ui.column().classes('items-center gap-3 text-gray-300'):
                    ui.icon('description').classes('text-4xl')
                    ui.label('Prévisualisation non disponible pour ce type de fichier.')
                    ui.button('Ouvrir le fichier', on_click=lambda p=path: open_file_native(p)).props('outline color=white')

        lbl_media_name.set_text(f"{state.viewer_index + 1}/{len(state.viewer_items)}  {os.path.basename(path)}")
        lbl_media_name.tooltip(path)
        update_viewer_selection_ui()

    def change_media(delta):
        if not state.viewer_items:
            return
        state.viewer_index = (state.viewer_index + int(delta)) % len(state.viewer_items)
        render_viewer()

    def open_media(index, items):
        if not items:
            return
        state.viewer_items = list(items)
        state.viewer_index = max(0, min(len(state.viewer_items) - 1, int(index)))
        state.viewer_open = True
        render_viewer()
        media_dialog.open()
        
    def _on_demand_worker():
        """Thread de fond: analyse les images soumises via /api/llm/request_analysis."""
        while True:
            try:
                path = _on_demand_queue.get(timeout=5)
            except _queue_mod.Empty:
                continue
            try:
                if not path or not os.path.isfile(path):
                    _on_demand_queue.task_done()
                    continue
                ext = os.path.splitext(path)[1].lower()
                if ext not in SUPPORTED_IMAGES:
                    _on_demand_queue.task_done()
                    continue
                fname = os.path.basename(path)
                # Aesthetic
                try:
                    aesthetic_engine.evaluate_media(None, SUPPORTED_IMAGES, override_files=[path])
                except Exception:
                    pass
                # NSFW — utilise le modèle courant ou le premier disponible
                try:
                    _nsfw_model = getattr(nsfw_engine, "current_model_name", None) or "strangerguardhf/nsfw-image-detection"
                    nsfw_engine.evaluate_media(os.path.dirname(path), _nsfw_model, SUPPORTED_IMAGES, override_files=[path])
                except Exception:
                    pass
                # Tags
                try:
                    _tag_model = getattr(tag_engine, "model_name", None) or "SmilingWolf/wd-vit-tagger-v3"
                    tag_engine.evaluate_media(None, _tag_model, SUPPORTED_IMAGES, override_files=[path])
                except Exception:
                    pass
                remaining = _on_demand_queue.qsize()
                state.add_log(
                    f"✅ Indexation terminée : {fname}"
                    + (f" ({remaining} restant(s) en file)" if remaining else " — file vide")
                )
            except Exception:
                pass
            finally:
                try:
                    _on_demand_queue.task_done()
                except ValueError:
                    pass
            render_viewer()

    def handle_keyboard(e):
        if not e.action.keydown or not state.viewer_open: return
        if e.key.arrow_right: change_media(1)
        elif e.key.arrow_left: change_media(-1)
        elif e.key.space: toggle_selection()
        elif e.key.name and e.key.name.lower() == 'd': download_current_item()
        elif e.key.name and e.key.name.lower() == 'c': copy_image_to_clipboard(state.viewer_items[state.viewer_index])

    ui.keyboard(on_key=handle_keyboard, ignore=['input', 'textarea', 'select'])

    # --- EXPORT HTML ---
    async def export_html_action(tab='search'):
        folder = await run.io_bound(pick_folder_native)
        if not folder: return
        
        html_path = os.path.join(folder, f"gallery_{tab}.html")
        html_content =[
            "<html><body style='background-color:#1e1e1e; color:white; font-family:sans-serif;'>",
            f"<h2>Export des résultats</h2>",
            "<div style='display:flex; flex-wrap:wrap; gap:15px;'>"
        ]
        
        items = getattr(state, f"{tab}_results",[])
        for item in items:
            path = item[1]
            filter_val = getattr(state, f"{tab}_res_filter", 'Tout')
            
            if filter_val == 'Images' and not path.lower().endswith(SUPPORTED_IMAGES): continue
            if filter_val == 'Vidéos' and not path.lower().endswith(SUPPORTED_VIDEOS): continue
            
            if tab == 'search': label_text = f"Score: {item[0]:.3f}"
            elif tab == 'aes': label_text = f"★ {item[0]:.2f} (Pic: {item[2]:.2f})"
            elif tab == 'nsfw': label_text = f"🚨 Danger: {item[0]*100:.1f}% | {item[2].upper()}"
            elif tab == 'face': label_text = f"Match: {item[0]*100:.1f}%"
            elif tab == 'tags': label_text = f"Tags Score: {item[0]:.2f}"
            elif tab == 'prompt': label_text = f"Prompt Score: {item[0]:.2f}"
                
            uri = Path(path).absolute().as_uri()
            ext = os.path.splitext(path)[1].lower()
            
            if ext in SUPPORTED_VIDEOS:
                path_hash = hashlib.md5(path.encode('utf-8')).hexdigest()
                thumb_path = os.path.join(THUMB_CACHE_DIR, f"{path_hash}.jpg")
                if not os.path.exists(thumb_path):
                    try:
                        with av.open(path) as container:
                            for frame in container.decode(video=0):
                                img = frame.to_image()
                                img.thumbnail((300, 300))
                                img.convert('RGB').save(thumb_path, format="JPEG", quality=80)
                                break
                    except: pass
                
                thumb_uri = Path(thumb_path).absolute().as_uri() if os.path.exists(thumb_path) else uri
                
                html_content.append(
                    f"<div style='background:#2d2d2d; padding:10px; border-radius:8px; text-align:center; max-width:320px;'>"
                    f"<a href='{uri}' target='_blank' title='Cliquez pour ouvrir la video'>"
                    f"<div style='position:relative; width:300px; height:200px; background:#111; border-radius:4px; display:flex; align-items:center; justify-content:center; overflow:hidden;'>"
                    f"<img src='{thumb_uri}' style='max-width:100%; max-height:100%; object-fit:contain;'>"
                    f"<div style='position:absolute; top:5px; right:5px; background:rgba(0,0,0,0.7); padding:3px 6px; border-radius:4px; font-size:12px;'>▶ Video</div>"
                    f"</div></a>"
                    f"<h4 style='margin:10px 0 5px 0; color:#4caf50;'>{label_text}</h4>"
                    f"<div style='font-size:11px; color:#aaa; word-wrap:break-word;'>{os.path.basename(path)}</div></div>"
                )
            else:
                html_content.append(
                    f"<div style='background:#2d2d2d; padding:10px; border-radius:8px; text-align:center; max-width:320px;'>"
                    f"<a href='{uri}' target='_blank'>"
                    f"<div style='width:300px; height:200px; background:#111; border-radius:4px; display:flex; align-items:center; justify-content:center; overflow:hidden;'>"
                    f"<img src='{uri}' style='max-width:100%; max-height:100%; object-fit:contain;'></div></a>"
                    f"<h4 style='margin:10px 0 5px 0; color:#4caf50;'>{label_text}</h4>"
                    f"<div style='font-size:11px; color:#aaa; word-wrap:break-word;'>{os.path.basename(path)}</div></div>"
                )
        html_content.append("</div></body></html>")
        
        try:
            with open(html_path, "w", encoding="utf-8") as f: f.write("\n".join(html_content))
            ui.notify(f"Galerie sauvegardée : {html_path}", type='positive')
        except Exception as e: ui.notify(f"Erreur d'export : {e}", type='negative')

    def modify_nsfw_category(photo_path, current_tier):
        """Ouvre un dialog pour modifier manuellement la catégorie NSFW"""
        with ui.dialog() as modify_dialog:
            with ui.card().classes('w-[400px] max-w-full bg-gray-900 border border-gray-700'):
                ui.label('Modifier la catégorie NSFW').classes('text-lg font-bold mb-4')
                
                # Trouver l'élément dans nsfw_results
                photo_item = None
                for item in state.nsfw_results:
                    if item[1] == photo_path:
                        photo_item = item
                        break
                
                if not photo_item:
                    ui.label('Photo non trouvée').classes('text-red-400')
                    return
                
                danger_score, path, tier, details = photo_item
                
                # Afficher l'info actuelle
                current_icon = {'SAIN': '🟢', 'SENSUEL': '🟡', 'EXPLICITE': '🔴'}.get(tier, '?')
                with ui.row().classes('w-full bg-gray-800 p-3 rounded mb-4'):
                    ui.label(f'Catégorie actuelle: {current_icon} {tier}').classes('font-bold')
                    ui.label(f'Danger: {danger_score*100:.1f}%').classes('text-gray-400')
                
                # Sélection de la nouvelle catégorie
                ui.label('Nouvelle catégorie:').classes('text-sm text-gray-400')
                
                new_tier = ui.radio(['🟢 SAIN', '🟡 SENSUEL', '🔴 EXPLICITE'], 
                                     value=f"{current_icon} {tier}").classes('w-full')
                
                def save_new_category():
                    tier_map = {
                        '🟢 SAIN': 'SAIN',
                        '🟡 SENSUEL': 'SENSUEL', 
                        '🔴 EXPLICITE': 'EXPLICITE'
                    }
                    new_tier_value = tier_map.get(new_tier.value, tier)
                    
                    try:
                        updated_details = dict(details) if isinstance(details, dict) else {}
                        updated_details['_manual_tier'] = new_tier_value

                        # Mettre à jour state.nsfw_results
                        for i, item in enumerate(state.nsfw_results):
                            if item[1] == photo_path:
                                danger, path, old_tier, _details = item
                                state.nsfw_results[i] = (danger, path, new_tier_value, updated_details)
                                break

                        # Garder la liste complète synchronisée aussi
                        for i, item in enumerate(state.nsfw_all_results):
                            if item[1] == photo_path:
                                danger, path, old_tier, _details = item
                                state.nsfw_all_results[i] = (danger, path, new_tier_value, updated_details)
                                break
                        
                        # Mettre à jour la base de données
                        cache_key = f"{state.nsfw_model}_{int(getattr(search_engine, 'video_frames', 4))}_{NSFW_CACHE_VERSION}"
                        top_label = str(updated_details.get('_raw_top_label', new_tier_value))
                        state.db_cache.save_nsfw_score(cache_key, photo_path, top_label, danger_score, updated_details)
                        
                        ui.notify(f"✅ Catégorie mise à jour: {tier} → {new_tier_value}", type='positive')
                        modify_dialog.close()
                        nsfw_gallery_ui.refresh()
                    except Exception as e:
                        ui.notify(f"❌ Erreur: {str(e)}", type='negative')
                
                with ui.row().classes('w-full gap-2 mt-4'):
                    ui.button('Valider', on_click=save_new_category).classes('flex-1 bg-green-700 hover:bg-green-600')
                    ui.button('Annuler', on_click=lambda: modify_dialog.close()).classes('flex-1 bg-gray-700 hover:bg-gray-600')
        
        modify_dialog.open()

    async def write_nsfw_contracts_action():
        source_items = state.nsfw_all_results if state.nsfw_all_results else state.nsfw_results
        if not source_items:
            return ui.notify("Aucun résultat NSFW à valider.", type='warning')

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        contracts_written = 0
        errors = 0
        
        ui.notify("Écriture des contrats JSON en cours...", type='info')
        state.add_log(f"[NSFW] Écriture de {len(source_items)} contrats...")

        for idx, (danger, path, label, details) in enumerate(source_items):
            if not path.lower().endswith(SUPPORTED_IMAGES) and not path.lower().endswith(SUPPORTED_VIDEOS):
                continue

            basename_lower = os.path.basename(path).lower()
            if basename_lower.startswith('tmp') and basename_lower.endswith('.png'):
                continue
            
            try:
                photo_path = Path(path)
                contract_path = photo_path.with_name(f"{photo_path.stem}_validation.json")
                
                # Ensure details is a dict
                if not isinstance(details, dict):
                    details = {}
                
                numeric_details = {}
                for k, v in details.items():
                    if k.startswith('_'):
                        continue
                    if v is None:
                        continue
                    try:
                        numeric_details[k] = float(v)
                    except (ValueError, TypeError):
                        try:
                            numeric_details[k] = int(v)
                        except (ValueError, TypeError):
                            numeric_details[k] = str(v) if v is not None else None
                
                payload = {
                    "schema": "organizador.nsfw.validation.v1",
                    "validated_at": now_iso,
                    "source_file": str(photo_path),
                    "file_name": photo_path.name,
                    "result": {
                        "tier": label,
                        "danger": float(danger),
                        "model": state.nsfw_model,
                        "raw_top_label": str(details.get('_raw_top_label', label)),
                        "explicit_threshold": float(state.nsfw_threshold),
                        "sensual_threshold": float(NSFW_SENSUAL_THRESHOLD),
                        "details": numeric_details,
                    },
                }
                with open(contract_path, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
                contracts_written += 1
                
                # Yield every 5 files to prevent server blocking
                if idx % 5 == 0:
                    await asyncio.sleep(0)
            except Exception as e:
                errors += 1
                state.add_log(f"[NSFW] Erreur contrat {Path(path).name}: {e}")
                await asyncio.sleep(0)

        if contracts_written == 0:
            ui.notify("Aucun contrat écrit (vérifie qu'il y a des photos dans les résultats).", type='warning')
        elif errors:
            ui.notify(f"✅ Contrats écrits: {contracts_written} | ⚠️ erreurs: {errors}", type='warning')
        else:
            ui.notify(f"✅ Contrats écrits pour {contracts_written} photos.", type='positive')
        
        state.add_log(f"[NSFW] Écriture contrats terminée: {contracts_written} réussis, {errors} erreurs")

    def _results_attr_name(tab):
        if tab == 'aes':
            return 'aesthetic_results'
        if tab == 'prompt':
            return 'prompt_results'
        return f"{tab}_results"

    def _refresh_gallery(tab):
        if tab == 'aes':
            aesthetic_gallery_ui.refresh()
            return
        if tab == 'prompt':
            prompt_gallery_ui.refresh()
            return
        if tab == 'ia':
            ia_gallery_ui.refresh()
            return
        gallery = globals().get(f"{tab}_gallery_ui")
        if gallery:
            gallery.refresh()

    # --- ACTIONS PAR LOT ---
    async def execute_batch(action='copy', tab='search', prepend_score=False, export_txt=False, txt_threshold=0.1):
        sel_dict = getattr(state, f"sel_{tab}")
        selected_paths =[p for p, checked in sel_dict.items() if checked]
        if not selected_paths:
            return ui.notify('Rien de sélectionné !', type='warning')
            
        folder = await run.io_bound(pick_folder_native)
        if not folder: return
        
        base_dir = getattr(state, f"{tab}_base_dir", state.search_base_dir)
        success = 0
        moved_paths = set()
        
        for path in selected_paths:
            try:
                rel_path = os.path.relpath(path, base_dir)
                if rel_path.startswith('..') or os.path.isabs(rel_path): rel_path = os.path.basename(path)
            except Exception: rel_path = os.path.basename(path)
                
            rel_dir, fname = os.path.split(rel_path)
            if state.flatten_structure:
                rel_dir = ""

            prefix = ""
            if prepend_score:
                if tab == 'search': prefix = f"{next((s for s, p in state.search_results if p == path), 0):.3f}_"
                elif tab == 'aes': prefix = f"{next((a for a, p, m in state.aesthetic_results if p == path), 0):05.2f}_"
                elif tab == 'nsfw': prefix = f"{next((d for d, p, l, dt in state.nsfw_results if p == path), 0)*100:05.1f}_"
                elif tab == 'face': prefix = f"{next((s for s, p in state.face_results if p == path), 0)*100:05.1f}_"
                    
            dest = os.path.join(folder, rel_dir, prefix + fname)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            
            # Skip if src and dest are the same file (prevents parasitic rename)
            if os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(dest)):
                success += 1
                continue

            try:
                if action == 'copy': shutil.copy2(path, dest)
                else: 
                    shutil.move(path, dest)
                    moved_paths.add(path)

                # Déplacer/copier les fichiers compagnons (_validation.json, _aesthetic.json, .txt tags, .json méta, .ia détection IA)
                src_stem = os.path.splitext(path)[0]
                dest_stem = os.path.splitext(dest)[0]
                for companion_suffix in ('_validation.json', '_aesthetic.json', '.txt', '.json', '.ia'):
                    companion_src = src_stem + companion_suffix
                    if os.path.isfile(companion_src):
                        companion_dst = dest_stem + companion_suffix
                        # Skip if companion src == dst
                        if os.path.normcase(os.path.normpath(companion_src)) == os.path.normcase(os.path.normpath(companion_dst)):
                            continue
                        try:
                            if action == 'copy':
                                shutil.copy2(companion_src, companion_dst)
                            else:
                                shutil.move(companion_src, companion_dst)
                        except Exception as ec:
                            state.add_log(f"⚠️ Compagnon non transféré {os.path.basename(companion_src)} : {ec}")
                
                # Экспорт txt тегов (только для вкладки Tags)
                if export_txt and tab == 'tags':
                    txt_dest = os.path.splitext(dest)[0] + '.txt'
                    # Достаем теги прямо из результатов
                    item_data = next((i for i in state.tags_results if i[1] == path), None)
                    if item_data and len(item_data) > 2:
                        tags_dict = item_data[2]
                        valid_tags =[t for t, s in tags_dict.items() if s >= txt_threshold]
                        if valid_tags:
                            with open(txt_dest, 'w', encoding='utf-8') as f:
                                f.write(", ".join(valid_tags))
                success += 1
            except Exception as e: state.add_log(f"Erreur {path} : {e}")
                
        ui.notify(f'{success} fichiers traités avec succès ({action})', type='positive')
        
        if action == 'move' and moved_paths:
            results_attr = _results_attr_name(tab)
            setattr(state, results_attr, [i for i in getattr(state, results_attr) if i[1] not in moved_paths])
            _refresh_gallery(tab)

    def delete_selected_media(tab='aes'):
        sel_dict = getattr(state, f"sel_{tab}")
        selected_paths = [p for p, checked in sel_dict.items() if checked]
        if not selected_paths:
            return ui.notify('Rien de sélectionné !', type='warning')

        def _do_delete():
            deleted = 0
            errors = 0
            deleted_set = set()

            for path in selected_paths:
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        deleted += 1
                        deleted_set.add(path)
                        # Supprimer les fichiers compagnons (.txt tags, .json méta, _validation.json NSFW, _aesthetic.json esthétique, .ia détection IA)
                        stem = os.path.splitext(path)[0]
                        for companion_suffix in ('_validation.json', '_aesthetic.json', '.txt', '.json', '.ia'):
                            companion = stem + companion_suffix
                            if os.path.isfile(companion):
                                try:
                                    os.remove(companion)
                                except Exception as ec:
                                    state.add_log(f"⚠️ Compagnon non supprimé {os.path.basename(companion)}: {ec}")
                    else:
                        errors += 1
                        state.add_log(f"Suppression ignorée (introuvable): {path}")
                except Exception as e:
                    errors += 1
                    state.add_log(f"Erreur suppression {path}: {e}")

            if deleted_set:
                results_attr = _results_attr_name(tab)
                setattr(state, results_attr, [i for i in getattr(state, results_attr) if i[1] not in deleted_set])
                for p in deleted_set:
                    sel_dict.pop(p, None)
                # Nettoyer les caches DB (toutes les tables)
                try:
                    search_engine.db_cache.remove_paths(list(deleted_set))
                except Exception as e:
                    state.add_log(f"⚠️ Cache DB non nettoyé: {e}")
                # Nettoyer dir_cache.json (liste de fichiers)
                try:
                    deleted_norm = {os.path.normcase(p) for p in deleted_set}
                    for dir_key in list(search_engine.files_cache._data.keys()):
                        search_engine.files_cache._data[dir_key] = [
                            f for f in search_engine.files_cache._data[dir_key]
                            if os.path.normcase(f) not in deleted_norm
                        ]
                    search_engine.files_cache.save_cache()
                except Exception as e:
                    state.add_log(f"⚠️ dir_cache non nettoyé: {e}")
                # Nettoyer le cache NSFW en mémoire
                for p in deleted_set:
                    _nsfw_tier_cache.pop(p, None)
                _refresh_gallery(tab)

            if errors:
                ui.notify(f"Supprimés: {deleted} | erreurs: {errors}", type='warning')
            else:
                ui.notify(f"{deleted} fichier(s) + compagnons supprimé(s)", type='positive')

        count = len(selected_paths)
        with ui.dialog() as confirm_dlg, ui.card().classes('bg-gray-900 text-white'):
            ui.label(f'⚠️ Supprimer {count} fichier(s) ?').classes('text-lg font-bold mb-1')
            ui.label('Les fichiers compagnons (.txt, .json, _validation.json, _aesthetic.json, .ia) seront aussi supprimés.').classes('text-sm text-gray-400 mb-3')
            with ui.row().classes('gap-2 mt-1'):
                ui.button('Supprimer', icon='delete', on_click=lambda: (confirm_dlg.close(), _do_delete())).props('color=red')
                ui.button('Annuler', on_click=confirm_dlg.close).props('outline color=white')
        confirm_dlg.open()

    def set_page_items(tab, value, page_paths):
        """Sélectionner/désélectionner uniquement les items de la page courante."""
        sel_dict = getattr(state, f"sel_{tab}")
        for p in page_paths:
            if p in sel_dict:
                sel_dict[p] = value

    async def save_aesthetic_scores_action():
        """Écrit _aesthetic.json à côté de chaque image dans les résultats esthétiques."""
        sel = {p for p, checked in state.sel_aes.items() if checked}
        items = [i for i in state.aesthetic_results if (not sel or i[1] in sel)]
        if not items:
            return ui.notify('Aucun résultat esthétique à sauvegarder.', type='warning')

        ui.notify(f"💾 Sauvegarde de {len(items):,} fichiers...", type='info', timeout=2000)
        state.add_log(f"[AES] Sauvegarde de {len(items):,} scores _aesthetic.json...")

        def _write_all():
            written = 0
            errors = 0
            for avg_score, path, max_score in items:
                try:
                    stem = os.path.splitext(path)[0]
                    out_path = stem + '_aesthetic.json'
                    data = {
                        "schema": "organizador.aesthetic.v1",
                        "result": {
                            "avg_score": round(float(avg_score), 6),
                            "max_score": round(float(max_score), 6),
                            "model": "v2_5_siglip"
                        }
                    }
                    with open(out_path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2)
                    written += 1
                except Exception as e:
                    errors += 1
                    state.add_log(f"[AES] Erreur écriture sidecar {os.path.basename(path)}: {e}")
            return written, errors

        written, errors = await run.io_bound(_write_all)
        if errors:
            ui.notify(f"Sauvegardés: {written} | erreurs: {errors}", type='warning')
        else:
            ui.notify(f"✅ {written} score(s) sauvegardé(s) en _aesthetic.json", type='positive')
        state.add_log(f"[AES] ✅ {written} _aesthetic.json écrits" + (f" ({errors} erreurs)" if errors else ""))

    async def write_tags_sidecar_files(file_format='txt', threshold=0.0):
        """Écrit les tags à côté de chaque média (.txt ou .json)."""
        if file_format not in {'txt', 'json'}:
            return ui.notify('Format non supporté', type='warning')

        selected_paths = [p for p, checked in state.sel_tags.items() if checked]
        selected_set = set(selected_paths)

        # Si rien de sélectionné: exporter tous les résultats visibles de l'onglet Tags.
        source_items = [
            item for item in state.tags_results
            if (
                state.tags_res_filter == 'Tout'
                or (state.tags_res_filter == 'Images' and item[1].lower().endswith(SUPPORTED_IMAGES))
                or (state.tags_res_filter == 'Vidéos' and item[1].lower().endswith(SUPPORTED_VIDEOS))
            )
        ]
        if selected_set:
            source_items = [item for item in source_items if item[1] in selected_set]

        if not source_items:
            return ui.notify('Aucun résultat tags à écrire.', type='warning')

        ui.notify(f"💾 Écriture de {len(source_items):,} sidecars {file_format.upper()}...", type='info', timeout=2000)

        threshold_val = float(threshold)

        def _write_all():
            written = 0
            errors = 0
            for _score, media_path, tags_dict in source_items:
                try:
                    p = Path(media_path)
                    out_path = p.with_suffix('.json' if file_format == 'json' else '.txt')

                    sorted_tags = sorted(tags_dict.items(), key=lambda x: x[1], reverse=True)
                    filtered_tags = [(k, v) for k, v in sorted_tags if float(v) >= threshold_val]

                    if file_format == 'json':
                        payload = {
                            'schema': 'organizador.tags.validation.v1',
                            'source_file': str(p),
                            'file_name': p.name,
                            'written_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            'threshold': threshold_val,
                            'tags': {k: f"{float(v)*100:.2f}%" for k, v in sorted_tags},
                        }
                        with open(out_path, 'w', encoding='utf-8') as f:
                            json.dump(payload, f, indent=2, ensure_ascii=False)
                    else:
                        tag_names = [k for k, _v in filtered_tags]
                        with open(out_path, 'w', encoding='utf-8') as f:
                            f.write(', '.join(tag_names))

                    written += 1
                except Exception as e:
                    errors += 1
                    state.add_log(f"[TAGS] Erreur écriture sidecar {media_path}: {e}")
            return written, errors

        written, errors = await run.io_bound(_write_all)
        if errors:
            ui.notify(f"Tags écrits: {written} | erreurs: {errors}", type='warning')
        else:
            ui.notify(f"✅ Tags écrits pour {written} fichier(s)", type='positive')

    async def write_prompt_sidecar_files(tab='prompt'):
        sel_dict = getattr(state, f"sel_{tab}", {})
        selected_paths = [p for p, checked in sel_dict.items() if checked]
        if not selected_paths:
            return ui.notify('Sélectionnez au moins une image.', type='warning')

        ui.notify(f"💾 Écriture de {len(selected_paths):,} prompts TXT...", type='info', timeout=2000)

        db_cache = state.db_cache

        def _write_all():
            written = 0
            errors = 0
            for path in selected_paths:
                try:
                    detailed = db_cache.get_detailed_prompt(path) or {}
                    basic = db_cache.get_prompt(path) or {}
                    text = str(detailed.get('text') or basic.get('text') or '').strip()
                    if not text:
                        errors += 1
                        continue
                    p = Path(path)
                    p.with_name(f"{p.stem}_prompt.txt").write_text(text, encoding='utf-8')
                    written += 1
                except Exception as e:
                    errors += 1
                    state.add_log(f"[PROMPT] Erreur écriture TXT {path}: {e}")
            return written, errors

        written, errors = await run.io_bound(_write_all)
        if written:
            ui.notify(f"Prompts TXT écrits: {written}" + (f" | erreurs: {errors}" if errors else ''), type='positive' if errors == 0 else 'warning')
        else:
            ui.notify('Aucun fichier prompt TXT écrit.', type='warning')

    async def handle_shift_click(e, idx, path, tab):
        is_shift = isinstance(e.args, dict) and e.args.get('shiftKey', False)
        await asyncio.sleep(0.05) 
        
        sel_dict = getattr(state, f"sel_{tab}")
        results_attr = _results_attr_name(tab)
        all_p = [p for i in getattr(state, results_attr) for p in[i[1]]]

        last_idx = getattr(state, f'last_clicked_{tab}', None)

        if not is_shift:
            setattr(state, f'last_clicked_{tab}', idx)
        else:
            if last_idx is not None:
                start = min(idx, last_idx)
                end = max(idx, last_idx)
                target_val = sel_dict.get(path, True)
                for i in range(start, end + 1):
                    sel_dict[all_p[i]] = target_val

    def set_all(tab, value):
        filter_val = getattr(state, f"{tab}_res_filter")
        sel_dict = getattr(state, f"sel_{tab}")
        results_attr = _results_attr_name(tab)
        for item in getattr(state, results_attr):
            p = item[1]
            if filter_val == 'Images' and not p.lower().endswith(SUPPORTED_IMAGES): continue
            if filter_val == 'Vidéos' and not p.lower().endswith(SUPPORTED_VIDEOS): continue
            if p in sel_dict: sel_dict[p] = value

    # --- COMPOSANTS DE GALERIE ---
    @ui.refreshable
    def search_gallery_ui():
        if not state.search_results:
            return ui.label("Les résultats apparaîtront ici...").classes("text-gray-400 m-4")

        filtered_results = []
        for item in state.search_results:
            p = item[1].lower()
            if state.search_res_filter == 'Images' and not p.endswith(SUPPORTED_IMAGES): continue
            if state.search_res_filter == 'Vidéos' and not p.endswith(SUPPORTED_VIDEOS): continue
            filtered_results.append(item)

        total_pages = max(1, (len(filtered_results) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        if state.search_page > total_pages: state.search_page = 1

        def change_page(d):
            state.search_page = max(1, min(total_pages, state.search_page + d))
            search_gallery_ui.refresh()

        def apply_filter(e):
            state.search_res_filter = e.value
            state.search_page = 1
            search_gallery_ui.refresh()

        _s_start = (state.search_page - 1) * ITEMS_PER_PAGE
        _page_paths_search = [item[1] for item in filtered_results[_s_start:_s_start + ITEMS_PER_PAGE]]

        with ui.column().classes('w-full h-full flex flex-col p-0 m-0 gap-0 relative'):
            with ui.column().classes('w-full shrink-0 bg-gray-900 p-4 pb-2 border-b border-gray-800 z-20 gap-0 shadow-md'):
                with ui.row().classes('w-full flex justify-between items-center p-2 bg-gray-800 rounded-lg mb-2'):
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Tout sélectionner', on_click=lambda: set_all('search', True)).props('outline color=white dense')
                        ui.button('Tout désélectionner', on_click=lambda: set_all('search', False)).props('outline color=white dense')
                        ui.button('Sél. page', on_click=lambda pp=_page_paths_search: set_page_items('search', True, pp)).props('outline color=cyan dense')
                        ui.button('Désél. page', on_click=lambda pp=_page_paths_search: set_page_items('search', False, pp)).props('outline color=cyan dense')
                        ui.toggle(['Tout', 'Images', 'Vidéos'], value=state.search_res_filter, on_change=apply_filter).classes('text-xs ml-2')
                    with ui.row().classes('gap-2 items-center'):
                        ui.button('Export HTML', icon='html', on_click=lambda: export_html_action('search')).props('color=purple dense outline')
                        ui.button('Copier ✔', icon='content_copy', on_click=lambda: execute_batch('copy', 'search', chk_prefix_search.value)).props('color=blue dense')
                        ui.button('Déplacer ✔', icon='drive_file_move', on_click=lambda: execute_batch('move', 'search', chk_prefix_search.value)).props('color=red dense')
                        ui.button('Supprimer ✔', icon='delete', on_click=lambda: delete_selected_media('search')).props('color=red dense outline')
                
                with ui.row().classes('w-full justify-center my-0 items-center gap-4'):
                    ui.button(icon='chevron_left', on_click=lambda: change_page(-1)).props('flat outline color=white')
                    ui.label(f'Page {state.search_page} / {total_pages}').classes('text-gray-300 font-bold')
                    ui.button(icon='chevron_right', on_click=lambda: change_page(1)).props('flat outline color=white')

            scroll_id = 'search_scroll_area'
            with ui.column().classes('w-full flex-1 overflow-y-auto p-4 relative').props(f'id="{scroll_id}"'):
                start_idx = (state.search_page - 1) * ITEMS_PER_PAGE
                page_items = filtered_results[start_idx : start_idx + ITEMS_PER_PAGE]
                all_paths =[p for s, p in filtered_results]

                if not page_items:
                    ui.label("Aucun fichier pour le filtre sélectionné.").classes("text-gray-400 m-4")

                with ui.grid(columns=int(state.grid_columns)).classes('w-full gap-6 pb-10'):
                    for score, path in page_items:
                        safe_path = urllib.parse.quote(path)
                        global_index = all_paths.index(path)
                        
                        with ui.card().classes('bg-gray-800 border border-gray-700 hover:border-blue-500 transition-colors p-0 overflow-hidden relative'):
                            with ui.row().classes('absolute top-2 left-2 bg-black/60 rounded px-1 z-10'):
                                ui.checkbox().bind_value(state.sel_search, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'search'), ['shiftKey'])
                            _emit_ia_badge_overlay(path, position_classes='top-2 right-2')

                            with ui.context_menu():
                                ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))

                            if os.path.splitext(path)[1].lower() in SUPPORTED_TEXTS:
                                ui.icon('article', size='4rem').classes('w-full aspect-square flex items-center justify-center bg-gray-900 cursor-pointer text-gray-500').on('click', lambda p=path: open_file_native(p))
                            else:
                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))
                            
                            with ui.row().classes('w-full justify-between items-center p-2 bg-gray-800'):
                                ui.label(f"Score: {score:.3f}").classes('text-green-400 font-bold text-sm')
                                ui.button(icon='folder', on_click=lambda p=path: reveal_file_native(p)).props('flat round dense color=white')
                            ui.label(os.path.basename(path)).classes('text-xs text-gray-400 px-2 pb-2 truncate w-full').tooltip(path)

            ui.button(icon='keyboard_arrow_up', on_click=lambda: ui.run_javascript(f'document.getElementById("{scroll_id}").scrollTo({{top: 0, behavior: "smooth"}})')).props('round color=blue').classes('absolute bottom-6 right-6 z-50 shadow-lg').tooltip('Haut de page')

    @ui.refreshable
    def aesthetic_gallery_ui():
        if not state.aesthetic_results:
            return ui.label("Les meilleures photos/vidéos apparaîtront ici...").classes("text-gray-400 m-4")

        search_q = str(state.aes_search or '').strip().lower()
        filtered_results = []
        for item in state.aesthetic_results:
            p = item[1].lower()
            if state.aes_res_filter == 'Images' and not p.endswith(SUPPORTED_IMAGES): continue
            if state.aes_res_filter == 'Vidéos' and not p.endswith(SUPPORTED_VIDEOS): continue
            if state.aes_nsfw_filter_res != 'Tout':
                tier = _read_nsfw_tier(item[1])
                if state.aes_nsfw_filter_res == 'Non validé' and tier != '': continue
                elif state.aes_nsfw_filter_res != 'Non validé' and tier.capitalize() != state.aes_nsfw_filter_res: continue
            if search_q:
                name = os.path.basename(item[1]).lower()
                if search_q not in name: continue
            filtered_results.append(item)

        if state.aes_sort == 'name_asc':
            filtered_results.sort(key=lambda x: os.path.basename(x[1]).lower())
        elif state.aes_sort == 'name_desc':
            filtered_results.sort(key=lambda x: os.path.basename(x[1]).lower(), reverse=True)
        elif state.aes_sort == 'score_asc':
            filtered_results.sort(key=lambda x: x[0])

        per_page = int(state.aes_per_page or 40)
        total_pages = max(1, (len(filtered_results) + per_page - 1) // per_page)
        if state.aes_page > total_pages: state.aes_page = 1

        def change_page(d):
            state.aes_page = max(1, min(total_pages, state.aes_page + d))
            aesthetic_gallery_ui.refresh()

        def apply_filter(e):
            state.aes_res_filter = e.value
            state.aes_page = 1
            aesthetic_gallery_ui.refresh()

        def apply_nsfw_filter_aes(e):
            state.aes_nsfw_filter_res = e.value
            state.aes_page = 1
            aesthetic_gallery_ui.refresh()

        def apply_search(e):
            state.aes_search = str(e.value or '')
            state.aes_page = 1
            aesthetic_gallery_ui.refresh()

        def apply_sort(e):
            state.aes_sort = str(e.value or 'score')
            state.aes_page = 1
            aesthetic_gallery_ui.refresh()

        def apply_per_page(e):
            state.aes_per_page = int(e.value or 40)
            state.aes_page = 1
            aesthetic_gallery_ui.refresh()

        compact = bool(state.aes_compact)

        _s_start = (state.aes_page - 1) * per_page
        _page_paths_aes = [item[1] for item in filtered_results[_s_start:_s_start + per_page]]

        with ui.column().classes('w-full h-full flex flex-col p-0 m-0 gap-0 relative'):
            with ui.column().classes('w-full shrink-0 bg-gray-900 p-3 pb-2 border-b border-gray-800 z-20 gap-2 shadow-md'):
                with ui.row().classes('w-full flex justify-between items-center p-2 bg-gray-800 rounded-lg'):
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Tout sélectionner', on_click=lambda: set_all('aes', True)).props('outline color=white dense')
                        ui.button('Tout désélectionner', on_click=lambda: set_all('aes', False)).props('outline color=white dense')
                        ui.button('Sél. page', on_click=lambda pp=_page_paths_aes: set_page_items('aes', True, pp)).props('outline color=cyan dense')
                        ui.button('Désél. page', on_click=lambda pp=_page_paths_aes: set_page_items('aes', False, pp)).props('outline color=cyan dense')
                        ui.toggle(['Tout', 'Images', 'Vidéos'], value=state.aes_res_filter, on_change=apply_filter).classes('text-xs ml-2')
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Export HTML', icon='html', on_click=lambda: export_html_action('aes')).props('color=purple dense outline')
                        ui.button('💾 Sauver ✔', on_click=save_aesthetic_scores_action).props('color=teal dense outline').tooltip('Écrire _aesthetic.json à côté de chaque image sélectionnée (ou tous si rien sélectionné)')
                        ui.button('Copier ✔', icon='content_copy', on_click=lambda: execute_batch('copy', 'aes', chk_prefix_aes.value)).props('color=yellow-800 dense')
                        ui.button('Déplacer ✔', icon='drive_file_move', on_click=lambda: execute_batch('move', 'aes', chk_prefix_aes.value)).props('color=red dense')
                        ui.button('Supprimer ✔', icon='delete', on_click=lambda: delete_selected_media('aes')).props('color=red dense outline')

                with ui.row().classes('w-full items-center gap-2 flex-wrap'):
                    ui.input(placeholder='🔍 Rechercher nom...', value=state.aes_search, on_change=apply_search).props('dense outlined clearable').classes('flex-1 min-w-[160px] bg-gray-800 rounded')
                    ui.select({'score': '↓ Score', 'score_asc': '↑ Score', 'name_asc': 'A→Z', 'name_desc': 'Z→A'}, value=state.aes_sort, label='Tri', on_change=apply_sort).props('dense outlined').classes('w-32')
                    ui.select({20: '20/page', 40: '40/page', 100: '100/page'}, value=per_page, label='Par page', on_change=apply_per_page).props('dense outlined').classes('w-28')
                    ui.select(
                        {'Tout': '🔵 Tout', 'Sain': '✅ Sain', 'Sensuel': '⚡ Sensuel', 'Explicit': '🔞 Explicit', 'Non validé': '? Non validé'},
                        value=state.aes_nsfw_filter_res, label='NSFW', on_change=apply_nsfw_filter_aes,
                    ).props('dense outlined').classes('w-28').tooltip('Filtrer par validation NSFW')
                    ui.button(icon='grid_view' if not compact else 'view_module', on_click=lambda: (setattr(state, 'aes_compact', not state.aes_compact), aesthetic_gallery_ui.refresh())).props('flat round dense color=white').tooltip('Basculer vue compacte')
                    ui.label(f'{len(filtered_results)} résultat(s)').classes('text-xs text-gray-400 ml-1')

                with ui.row().classes('w-full justify-center my-0 items-center gap-4'):
                    ui.button(icon='chevron_left', on_click=lambda: change_page(-1)).props('flat outline color=white')
                    ui.label(f'Page {state.aes_page} / {total_pages} ({len(filtered_results)} items)').classes('text-gray-300 font-bold text-sm')
                    ui.button(icon='chevron_right', on_click=lambda: change_page(1)).props('flat outline color=white')

            scroll_id = 'aes_scroll_area'
            with ui.column().classes('w-full flex-1 overflow-y-auto p-4 relative').props(f'id="{scroll_id}"'):
                start_idx = (state.aes_page - 1) * per_page
                page_items = filtered_results[start_idx : start_idx + per_page]
                all_paths = [p for a, p, m in filtered_results]

                if not page_items:
                    ui.label("Aucun fichier pour le filtre sélectionné.").classes("text-gray-400 m-4")

                cols = int(state.grid_columns) + (3 if compact else 0)
                gap_cls = 'w-full gap-1 pb-10' if compact else 'w-full gap-6 pb-10'
                with ui.grid(columns=cols).classes(gap_cls):
                    for avg_score, path, max_score in page_items:
                        safe_path = urllib.parse.quote(path)
                        global_index = all_paths.index(path)
                        is_video = path.lower().endswith(SUPPORTED_VIDEOS)
                        aes_info = aesthetic_explain(avg_score, max_score, is_video) if state.aes_scan_mode == 'score' else None
                        nsfw_tier = _read_nsfw_tier(path)
                        card_bdr = f'border-2 {_NSFW_CARD_BORDER.get(nsfw_tier, "border-gray-600")}' if nsfw_tier else 'border border-gray-700'

                        if compact:
                            with ui.card().classes(f'bg-gray-800 {card_bdr} hover:border-yellow-500 transition-colors p-0 overflow-hidden relative'):
                                with ui.row().classes('absolute top-1 left-1 bg-black/70 rounded px-0.5 z-10'):
                                    ui.checkbox().bind_value(state.sel_aes, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'aes'), ['shiftKey']).props('dense size=xs')
                                if nsfw_tier:
                                    lbl, cls = _NSFW_BADGE.get(nsfw_tier, _NSFW_BADGE[''])
                                    ui.label(lbl).classes(f'absolute top-1 right-1 text-[8px] font-bold px-1 rounded border {cls} z-10')
                                _emit_ia_badge_overlay(path, position_classes='top-6 right-1', size_classes='text-[8px] px-1')
                                with ui.context_menu():
                                    ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                    ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                    ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))
                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))
                                if state.aes_scan_mode == 'blur':
                                    ui.label(f"🌫 {avg_score:.0f}").classes('text-[9px] text-orange-400 px-1 pb-0.5 truncate w-full')
                                else:
                                    ui.label(f"★ {avg_score:.2f} ({aes_info['avg_pct']:.0f}%)").classes('text-[9px] text-yellow-400 px-1 pb-0.5 truncate w-full')
                        else:
                            with ui.card().classes(f'bg-gray-800 {card_bdr} hover:border-yellow-500 transition-colors p-0 overflow-hidden relative'):
                                with ui.row().classes('absolute top-2 left-2 bg-black/60 rounded px-1 z-10'):
                                    ui.checkbox().bind_value(state.sel_aes, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'aes'), ['shiftKey'])
                                if nsfw_tier:
                                    lbl, cls = _NSFW_BADGE.get(nsfw_tier, _NSFW_BADGE[''])
                                    ui.label(lbl).classes(f'absolute top-2 right-2 text-[9px] font-bold px-1.5 py-0.5 rounded border {cls} z-10')
                                _emit_ia_badge_overlay(path, position_classes='top-8 right-2')
                                with ui.context_menu():
                                    ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                    ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                    ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))
                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))

                                with ui.row().classes('w-full justify-between items-center p-2'):
                                    if state.aes_scan_mode == 'blur':
                                        ui.label(f"🌫 Netteté: {avg_score:.0f}").classes('text-orange-400 font-bold text-lg')
                                    else:
                                        ui.label(f"★ {avg_score:.2f} ({aes_info['avg_pct']:.0f}%)").classes('text-yellow-400 font-bold text-lg')
                                        if avg_score != max_score:
                                            ui.label(f"Pic: {max_score:.2f} ({aes_info['max_pct']:.0f}%)").classes('text-xs text-gray-500')

                                if state.aes_scan_mode == 'score':
                                    with ui.expansion('Pourquoi ce score', icon='help_outline').classes('w-full px-2 pb-1 text-xs text-gray-300'):
                                        ui.label(f"Niveau: {aes_info['level']} ({aes_info['avg_pct']:.0f}%)").classes('text-yellow-300')
                                        ui.label(f"Méthode: {aes_info['method']}").classes('text-gray-300')
                                        if is_video:
                                            ui.label(f"Stabilité visuelle: {aes_info['stability']} (écart pic/moyenne: {aes_info['delta']:.1f}%)").classes('text-gray-400')

                                ui.label(os.path.basename(path)).classes('text-xs text-gray-400 px-2 pb-2 truncate w-full').tooltip(path)

            ui.button(icon='keyboard_arrow_up', on_click=lambda: ui.run_javascript(f'document.getElementById("{scroll_id}").scrollTo({{top: 0, behavior: "smooth"}})')).props('round color=yellow-800').classes('absolute bottom-6 right-6 z-50 shadow-lg').tooltip('Haut de page')

    @ui.refreshable
    def nsfw_gallery_ui():
        if not state.nsfw_results:
            return ui.label("Les résultats de l'analyse NSFW apparaîtront ici...").classes("text-gray-400 m-4")

        search_q = str(state.nsfw_search or '').strip().lower()
        filtered_results = []
        for item in state.nsfw_results:
            p = item[1].lower()
            if state.nsfw_res_filter == 'Images' and not p.endswith(SUPPORTED_IMAGES): continue
            if state.nsfw_res_filter == 'Vidéos' and not p.endswith(SUPPORTED_VIDEOS): continue
            if state.nsfw_hide_sain and item[2] == 'SAIN': continue
            if state.nsfw_hide_sensuel and item[2] == 'SENSUEL': continue
            if state.nsfw_hide_explicite and item[2] == 'EXPLICITE': continue
            if search_q:
                name = os.path.basename(item[1]).lower()
                if search_q not in name: continue
            filtered_results.append(item)

        if state.nsfw_sort == 'name_asc':
            filtered_results.sort(key=lambda x: os.path.basename(x[1]).lower())
        elif state.nsfw_sort == 'name_desc':
            filtered_results.sort(key=lambda x: os.path.basename(x[1]).lower(), reverse=True)

        per_page = int(state.nsfw_per_page or 40)
        total_pages = max(1, (len(filtered_results) + per_page - 1) // per_page)
        if state.nsfw_page > total_pages: state.nsfw_page = 1

        def change_page(d):
            state.nsfw_page = max(1, min(total_pages, state.nsfw_page + d))
            nsfw_gallery_ui.refresh()

        def apply_filter(e):
            state.nsfw_res_filter = e.value
            state.nsfw_page = 1
            nsfw_gallery_ui.refresh()

        def apply_search(e):
            state.nsfw_search = str(e.value or '')
            state.nsfw_page = 1
            nsfw_gallery_ui.refresh()

        def apply_sort(e):
            state.nsfw_sort = str(e.value or 'score')
            state.nsfw_page = 1
            nsfw_gallery_ui.refresh()

        def apply_per_page(e):
            state.nsfw_per_page = int(e.value or 40)
            state.nsfw_page = 1
            nsfw_gallery_ui.refresh()

        compact = bool(state.nsfw_compact)

        _s_start = (state.nsfw_page - 1) * per_page
        _page_paths_nsfw = [item[1] for item in filtered_results[_s_start:_s_start + per_page]]

        with ui.column().classes('w-full h-full flex flex-col p-0 m-0 gap-0 relative'):
            with ui.column().classes('w-full shrink-0 bg-gray-900 p-3 pb-2 border-b border-gray-800 z-20 gap-2 shadow-md'):
                with ui.row().classes('w-full flex justify-between items-center p-2 bg-gray-800 rounded-lg'):
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Tout sélectionner', on_click=lambda: set_all('nsfw', True)).props('outline color=white dense')
                        ui.button('Tout désélectionner', on_click=lambda: set_all('nsfw', False)).props('outline color=white dense')
                        ui.button('Sél. page', on_click=lambda pp=_page_paths_nsfw: set_page_items('nsfw', True, pp)).props('outline color=cyan dense')
                        ui.button('Désél. page', on_click=lambda pp=_page_paths_nsfw: set_page_items('nsfw', False, pp)).props('outline color=cyan dense')
                        ui.toggle(['Tout', 'Images', 'Vidéos'], value=state.nsfw_res_filter, on_change=apply_filter).classes('text-xs ml-2')
                        def toggle_hide_sain(e):
                            state.nsfw_hide_sain = e.value
                            state.nsfw_page = 1
                            nsfw_gallery_ui.refresh()
                        def toggle_hide_sensuel(e):
                            state.nsfw_hide_sensuel = e.value
                            state.nsfw_page = 1
                            nsfw_gallery_ui.refresh()
                        def toggle_hide_explicite(e):
                            state.nsfw_hide_explicite = e.value
                            state.nsfw_page = 1
                            nsfw_gallery_ui.refresh()
                        ui.checkbox('🟢 Masquer SAIN', value=state.nsfw_hide_sain, on_change=toggle_hide_sain).classes('text-xs text-green-300 ml-2')
                        ui.checkbox('🟡 Masquer SENSUEL', value=state.nsfw_hide_sensuel, on_change=toggle_hide_sensuel).classes('text-xs text-yellow-300')
                        ui.checkbox('🔴 Masquer EXPLICITE', value=state.nsfw_hide_explicite, on_change=toggle_hide_explicite).classes('text-xs text-red-300')
                    def open_bulk_nsfw_modify_dialog():
                        selected_paths = [p for p, checked in (state.sel_nsfw or {}).items() if checked]
                        if not selected_paths:
                            ui.notify('Sélectionnez au moins une image.', type='warning')
                            return

                        with ui.dialog() as bulk_dialog, ui.card().classes('bg-gray-900 text-white w-[460px] max-w-[95vw]'):
                            ui.label('Modification groupée NSFW').classes('text-lg font-bold mb-2')
                            ui.label(f'{len(selected_paths)} média sélectionné(s)').classes('text-sm text-gray-400 mb-3')
                            bulk_new_tier = ui.radio(
                                ['🟢 SAIN', '🟡 SENSUEL', '🔴 EXPLICITE'],
                                value={
                                    'SAIN': '🟢 SAIN',
                                    'SENSUEL': '🟡 SENSUEL',
                                    'EXPLICITE': '🔴 EXPLICITE',
                                }.get(state.nsfw_bulk_new_label, '🟢 SAIN')
                            ).classes('w-full')

                            def apply_bulk_new_category():
                                tier_map = {
                                    '🟢 SAIN': 'SAIN',
                                    '🟡 SENSUEL': 'SENSUEL',
                                    '🔴 EXPLICITE': 'EXPLICITE',
                                }
                                new_tier_value = tier_map.get(bulk_new_tier.value, 'SAIN')
                                state.nsfw_bulk_new_label = new_tier_value
                                cache_key = f"{state.nsfw_model}_{int(nsfw_video_frames.value)}_{NSFW_CACHE_VERSION}"
                                updated = 0
                                errors = 0

                                for photo_path in selected_paths:
                                    try:
                                        photo_item = next((item for item in state.nsfw_all_results if item[1] == photo_path), None)
                                        if not photo_item:
                                            raise ValueError('Photo non trouvée dans les résultats')

                                        danger_score, path, _tier, details = photo_item
                                        updated_details = dict(details) if isinstance(details, dict) else {}
                                        updated_details['_manual_tier'] = new_tier_value
                                        top_label = str(updated_details.get('_raw_top_label', new_tier_value))
                                        state.db_cache.save_nsfw_score(cache_key, path, top_label, danger_score, updated_details)

                                        for i, item in enumerate(state.nsfw_results):
                                            if item[1] == path:
                                                state.nsfw_results[i] = (danger_score, path, new_tier_value, updated_details)
                                                break

                                        for i, item in enumerate(state.nsfw_all_results):
                                            if item[1] == path:
                                                state.nsfw_all_results[i] = (danger_score, path, new_tier_value, updated_details)
                                                break

                                        updated += 1
                                    except Exception as e:
                                        errors += 1
                                        state.add_log(f'[NSFW] Erreur modif lot {photo_path}: {e}')

                                if updated:
                                    for path in selected_paths:
                                        if path in state.sel_nsfw:
                                            state.sel_nsfw[path] = False
                                    nsfw_gallery_ui.refresh()
                                    bulk_dialog.close()
                                    ui.notify(
                                        f'Catégorie appliquée à {updated} média' + ('s' if updated > 1 else '') + (f' | erreurs: {errors}' if errors else ''),
                                        type='positive' if errors == 0 else 'warning'
                                    )
                                else:
                                    ui.notify('Aucune image sélectionnée n’a pu être mise à jour.', type='negative')

                            with ui.row().classes('w-full gap-2 mt-4'):
                                ui.button('Valider', on_click=apply_bulk_new_category).classes('flex-1 bg-green-700 hover:bg-green-600')
                                ui.button('Annuler', on_click=lambda: bulk_dialog.close()).classes('flex-1 bg-gray-700 hover:bg-gray-600')

                        bulk_dialog.open()

                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Export HTML', icon='html', on_click=lambda: export_html_action('nsfw')).props('color=purple dense outline')
                        ui.button('Écrire contrats JSON', icon='assignment_turned_in', on_click=lambda: write_nsfw_contracts_action()).props('color=teal dense outline')
                        ui.button('💾 Modifier sélection', icon='edit', on_click=open_bulk_nsfw_modify_dialog).props('color=cyan dense outline').tooltip('Changer la catégorie NSFW de tous les médias sélectionnés')
                        ui.button('Copier ✔', icon='content_copy', on_click=lambda: execute_batch('copy', 'nsfw', chk_prefix_nsfw.value)).props('color=red-800 dense')
                        ui.button('Déplacer ✔', icon='drive_file_move', on_click=lambda: execute_batch('move', 'nsfw', chk_prefix_nsfw.value)).props('color=red dense')
                        ui.button('Supprimer ✔', icon='delete', on_click=lambda: delete_selected_media('nsfw')).props('color=red dense outline')

                with ui.row().classes('w-full items-center gap-2 flex-wrap'):
                    ui.input(placeholder='🔍 Rechercher nom...', value=state.nsfw_search, on_change=apply_search).props('dense outlined clearable').classes('flex-1 min-w-[160px] bg-gray-800 rounded')
                    ui.select({'score': '↓ Score', 'name_asc': 'A→Z', 'name_desc': 'Z→A'}, value=state.nsfw_sort, label='Tri', on_change=apply_sort).props('dense outlined').classes('w-28')
                    ui.select({20: '20/page', 40: '40/page', 100: '100/page'}, value=per_page, label='Par page', on_change=apply_per_page).props('dense outlined').classes('w-28')
                    ui.button(icon='grid_view' if not compact else 'view_module', on_click=lambda: (setattr(state, 'nsfw_compact', not state.nsfw_compact), nsfw_gallery_ui.refresh())).props('flat round dense color=white').tooltip('Basculer vue compacte')
                    ui.label(f'{len(filtered_results)} résultat(s)').classes('text-xs text-gray-400 ml-1')

                with ui.row().classes('w-full justify-center my-0 items-center gap-4'):
                    ui.button(icon='chevron_left', on_click=lambda: change_page(-1)).props('flat outline color=white')
                    ui.label(f'Page {state.nsfw_page} / {total_pages} ({len(filtered_results)} items)').classes('text-gray-300 font-bold text-sm')
                    ui.button(icon='chevron_right', on_click=lambda: change_page(1)).props('flat outline color=white')

            scroll_id = 'nsfw_scroll_area'
            with ui.column().classes('w-full flex-1 overflow-y-auto p-4 relative').props(f'id="{scroll_id}"'):
                start_idx = (state.nsfw_page - 1) * per_page
                page_items = filtered_results[start_idx : start_idx + per_page]
                all_paths = [p for d, p, l, dt in filtered_results]

                if not page_items:
                    ui.label("Aucun fichier pour le filtre sélectionné.").classes("text-gray-400 m-4")

                cols = int(state.grid_columns) + (3 if compact else 0)
                gap_cls = 'w-full gap-1 pb-10' if compact else 'w-full gap-6 pb-10'
                with ui.grid(columns=cols).classes(gap_cls):
                    for danger_score, path, tier, details in page_items:
                        safe_path = urllib.parse.quote(path)
                        global_index = all_paths.index(path)

                        # Tier-based visual styling
                        if tier == 'EXPLICITE':
                            card_border  = 'border-red-600'
                            score_color  = 'text-red-400'
                            badge_cls    = 'bg-red-900 text-red-300'
                            badge_icon   = '🔴'
                        elif tier == 'SENSUEL':
                            card_border  = 'border-yellow-500'
                            score_color  = 'text-yellow-400'
                            badge_cls    = 'bg-yellow-900 text-yellow-300'
                            badge_icon   = '🟡'
                        else:  # SAIN
                            card_border  = 'border-green-800'
                            score_color  = 'text-green-400'
                            badge_cls    = 'bg-green-900 text-green-300'
                            badge_icon   = '🟢'

                        if compact:
                            with ui.card().classes(f'bg-gray-800 border-2 {card_border} transition-colors p-0 overflow-hidden relative'):
                                with ui.row().classes('absolute top-1 left-1 bg-black/70 rounded px-0.5 z-10'):
                                    ui.checkbox().bind_value(state.sel_nsfw, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'nsfw'), ['shiftKey']).props('dense size=xs')
                                _emit_ia_badge_overlay(path, position_classes='top-1 right-1', size_classes='text-[8px] px-1')
                                with ui.context_menu():
                                    ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                    ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                    ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))
                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))
                                with ui.row().classes('w-full items-center justify-between px-1 pb-0.5 gap-1'):
                                    ui.label(f"{badge_icon} {danger_score*100:.0f}%").classes(f'{score_color} text-[9px] font-bold truncate flex-1')
                                    ui.label(os.path.basename(path)).classes('text-[9px] text-gray-400 truncate flex-1').tooltip(path)
                        else:
                            with ui.card().classes(f'bg-gray-800 border-2 {card_border} transition-colors p-0 overflow-hidden relative'):
                                with ui.row().classes('absolute top-2 left-2 bg-black/60 rounded px-1 z-10'):
                                    ui.checkbox().bind_value(state.sel_nsfw, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'nsfw'), ['shiftKey'])
                                _emit_ia_badge_overlay(path, position_classes='top-2 right-2')

                                with ui.context_menu():
                                    ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                    ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                    ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))

                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))

                                with ui.row().classes('w-full items-center justify-between px-2 pt-2 pb-1 gap-1'):
                                    ui.label(f"{danger_score*100:.1f}%").classes(f'{score_color} font-bold text-lg')
                                    ui.label(f"{badge_icon} {tier}").classes(f'{badge_cls} text-xs font-bold px-2 py-0.5 rounded')

                                # Colored label chips — actual raw model labels
                                top_scores = sorted([(lbl, prob) for lbl, prob in details.items() if not lbl.startswith('_')], key=lambda x: x[1], reverse=True)[:4]
                                with ui.row().classes('w-full flex-wrap gap-1 px-2 pb-1'):
                                    for lbl, prob in top_scores:
                                        if lbl.lower() in NSFW_SENSUAL_LABELS:
                                            chip = 'bg-yellow-900 text-yellow-300 border border-yellow-700'
                                        elif lbl.lower() in NSFW_SAFE_LABELS:
                                            chip = 'bg-green-900 text-green-300 border border-green-800'
                                        else:
                                            chip = 'bg-red-900 text-red-300 border border-red-800'
                                        ui.label(f"{lbl}: {prob*100:.0f}%").classes(f'{chip} text-xs px-1.5 py-0 rounded font-mono leading-5')

                                ui.label(os.path.basename(path)).classes('text-xs text-gray-400 px-2 pb-1 truncate w-full').tooltip(path)

                                # Bouton pour modifier la catégorie manuellement
                                with ui.row().classes('w-full px-2 pb-2 gap-1'):
                                    ui.button('✏️ Modifier', on_click=lambda p=path, t=tier: modify_nsfw_category(p, t)).classes('flex-1 bg-blue-700 hover:bg-blue-600 text-xs py-1').tooltip('Corriger la catégorie si l\'IA s\'est trompée')

            ui.button(icon='keyboard_arrow_up', on_click=lambda: ui.run_javascript(f'document.getElementById("{scroll_id}").scrollTo({{top: 0, behavior: "smooth"}})')).props('round color=red-800').classes('absolute bottom-6 right-6 z-50 shadow-lg').tooltip('Haut de page')

    @ui.refreshable
    def face_gallery_ui():
        if not state.face_results:
            return ui.label("Les photos contenant le visage recherché apparaîtront ici...").classes("text-gray-400 m-4")

        filtered_results = []
        for item in state.face_results:
            p = item[1].lower()
            if state.face_res_filter == 'Images' and not p.endswith(SUPPORTED_IMAGES): continue
            if state.face_res_filter == 'Vidéos' and not p.endswith(SUPPORTED_VIDEOS): continue
            filtered_results.append(item)

        total_pages = max(1, (len(filtered_results) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        if state.face_page > total_pages: state.face_page = 1

        def change_page(d):
            state.face_page = max(1, min(total_pages, state.face_page + d))
            face_gallery_ui.refresh()

        def apply_filter(e):
            state.face_res_filter = e.value
            state.face_page = 1
            face_gallery_ui.refresh()

        _s_start = (state.face_page - 1) * ITEMS_PER_PAGE
        _page_paths_face = [item[1] for item in filtered_results[_s_start:_s_start + ITEMS_PER_PAGE]]

        with ui.column().classes('w-full h-full flex flex-col p-0 m-0 gap-0 relative'):
            with ui.column().classes('w-full shrink-0 bg-gray-900 p-4 pb-2 border-b border-gray-800 z-20 gap-0 shadow-md'):
                with ui.row().classes('w-full flex justify-between items-center p-2 bg-gray-800 rounded-lg mb-2'):
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Tout sélectionner', on_click=lambda: set_all('face', True)).props('outline color=white dense')
                        ui.button('Tout désélectionner', on_click=lambda: set_all('face', False)).props('outline color=white dense')
                        ui.button('Sél. page', on_click=lambda pp=_page_paths_face: set_page_items('face', True, pp)).props('outline color=cyan dense')
                        ui.button('Désél. page', on_click=lambda pp=_page_paths_face: set_page_items('face', False, pp)).props('outline color=cyan dense')
                        ui.toggle(['Tout', 'Images', 'Vidéos'], value=state.face_res_filter, on_change=apply_filter).classes('text-xs ml-2')
                    with ui.row().classes('gap-2 items-center'):
                        ui.button('Export HTML', icon='html', on_click=lambda: export_html_action('face')).props('color=purple dense outline')
                        ui.button('Copier ✔', icon='content_copy', on_click=lambda: execute_batch('copy', 'face', chk_prefix_face.value)).props('color=teal-800 dense')
                        ui.button('Déplacer ✔', icon='drive_file_move', on_click=lambda: execute_batch('move', 'face', chk_prefix_face.value)).props('color=red dense')
                        ui.button('Supprimer ✔', icon='delete', on_click=lambda: delete_selected_media('face')).props('color=red dense outline')

                with ui.row().classes('w-full justify-center my-0 items-center gap-4'):
                    ui.button(icon='chevron_left', on_click=lambda: change_page(-1)).props('flat outline color=white')
                    ui.label(f'Page {state.face_page} / {total_pages}').classes('text-gray-300 font-bold')
                    ui.button(icon='chevron_right', on_click=lambda: change_page(1)).props('flat outline color=white')

            scroll_id = 'face_scroll_area'
            with ui.column().classes('w-full flex-1 overflow-y-auto p-4 relative').props(f'id="{scroll_id}"'):
                start_idx = (state.face_page - 1) * ITEMS_PER_PAGE
                page_items = filtered_results[start_idx : start_idx + ITEMS_PER_PAGE]
                all_paths =[p for s, p in filtered_results]

                if not page_items:
                    ui.label("Aucun fichier pour le filtre sélectionné.").classes("text-gray-400 m-4")

                with ui.grid(columns=int(state.grid_columns)).classes('w-full gap-6 pb-10'):
                    for sim_score, path in page_items:
                        safe_path = urllib.parse.quote(path)
                        global_index = all_paths.index(path)
                        
                        with ui.card().classes('bg-gray-800 border border-gray-700 hover:border-teal-500 transition-colors p-0 overflow-hidden relative'):
                            with ui.row().classes('absolute top-2 left-2 bg-black/60 rounded px-1 z-10'):
                                ui.checkbox().bind_value(state.sel_face, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'face'), ['shiftKey'])
                            _emit_ia_badge_overlay(path, position_classes='top-2 right-2')

                            with ui.context_menu():
                                ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))

                            ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))
                            
                            with ui.row().classes('w-full justify-between items-center p-2 bg-gray-800'):
                                ui.label(f"Similarité : {sim_score*100:.1f}%").classes('text-teal-400 font-bold text-sm')
                                ui.button(icon='folder', on_click=lambda p=path: reveal_file_native(p)).props('flat round dense color=white')
                            ui.label(os.path.basename(path)).classes('text-xs text-gray-400 px-2 pb-2 truncate w-full').tooltip(path)

            ui.button(icon='keyboard_arrow_up', on_click=lambda: ui.run_javascript(f'document.getElementById("{scroll_id}").scrollTo({{top: 0, behavior: "smooth"}})')).props('round color=teal-800').classes('absolute bottom-6 right-6 z-50 shadow-lg').tooltip('Haut de page')

    # --- HELPER : lecture du tier NSFW depuis _validation.json ---
    _nsfw_tier_cache: dict = {}

    def _read_nsfw_tier(path: str) -> str:
        """Retourne '' (inconnu), 'SAIN', 'SENSUEL' ou 'EXPLICIT'."""
        if path in _nsfw_tier_cache:
            return _nsfw_tier_cache[path]
        stem = os.path.splitext(path)[0]
        json_path = stem + '_validation.json'
        tier = ''
        if os.path.isfile(json_path):
            try:
                import json as _j
                with open(json_path, 'r', encoding='utf-8') as _f:
                    data = _j.load(_f)
                raw = data.get('result', {}).get('tier', '')
                if raw:
                    tier = raw.upper()
            except Exception:
                pass
        _nsfw_tier_cache[path] = tier
        return tier

    _NSFW_BADGE = {
        'SAIN':      ('✅ SAIN',     'text-green-400  bg-green-900/40  border-green-700'),
        'SENSUEL':   ('⚡ SENSUEL',  'text-yellow-300 bg-yellow-900/40 border-yellow-700'),
        'EXPLICIT':  ('🔞 EXPLICIT', 'text-red-400    bg-red-900/40    border-red-700'),
        'EXPLICITE': ('🔞 EXPLICIT', 'text-red-400    bg-red-900/40    border-red-700'),
        '':          ('? N/V',       'text-gray-500   bg-gray-800/60   border-gray-600'),
    }

    # Couleur de bordure de carte selon le tier NSFW
    _NSFW_CARD_BORDER = {
        'SAIN':      'border-green-700',
        'SENSUEL':   'border-yellow-500',
        'EXPLICIT':  'border-red-600',
        'EXPLICITE': 'border-red-600',
    }

    def _nsfw_badge_ui(tier: str):
        label, cls = _NSFW_BADGE.get(tier, _NSFW_BADGE[''])
        ui.label(label).classes(f'text-[9px] font-bold px-1 py-0.5 rounded border {cls}')

    # --- HELPER : verdict IA (.ia sidecar + DB cache) ---
    _ia_verdict_cache: dict = {}
    _IA_OVERLAY_BADGES = {
        True:  ('🤖 IA',     'bg-pink-900/60 text-pink-200 border-pink-700'),
        False: ('📷 Photo',  'bg-emerald-900/60 text-emerald-200 border-emerald-700'),
    }

    def _read_ia_verdict(path: str):
        """Retourne True (IA), False (Photo) ou None (inconnu).

        Source de vérité : DB cache (rapide). Fallback : sidecar .ia.
        """
        if path in _ia_verdict_cache:
            return _ia_verdict_cache[path]
        verdict = None
        try:
            det = search_engine.db_cache.get_ai_detection(path)
            if det and det.get('is_ai') is not None:
                verdict = bool(det['is_ai'])
        except Exception:
            pass
        if verdict is None:
            stem = os.path.splitext(path)[0]
            ia_path = stem + '.ia'
            if os.path.isfile(ia_path):
                try:
                    import json as _j
                    with open(ia_path, 'r', encoding='utf-8') as _f:
                        data = _j.load(_f)
                    if data.get('is_ai') is not None:
                        verdict = bool(data['is_ai'])
                except Exception:
                    pass
        _ia_verdict_cache[path] = verdict
        return verdict

    def _emit_ia_badge_overlay(path: str, position_classes: str = 'top-8 right-2', size_classes: str = 'text-[9px] px-1.5 py-0.5'):
        """Emet un badge IA/Photo flottant si un verdict est disponible."""
        v = _read_ia_verdict(path)
        if v is None:
            return
        text, cls = _IA_OVERLAY_BADGES[v]
        ui.label(text).classes(f'absolute {position_classes} {size_classes} font-bold rounded border {cls} z-10')

    # --- COMPOSANT GALERIE TAGS ---
    @ui.refreshable
    def tags_gallery_ui():
        if not state.tags_results:
            return ui.label("Les images correspondant aux tags sélectionnés apparaîtront ici...").classes("text-gray-400 m-4")

        search_q = str(state.tags_search or '').strip().lower()
        filtered_results = []
        for item in state.tags_results:
            p = item[1].lower()
            if state.tags_res_filter == 'Images' and not p.endswith(SUPPORTED_IMAGES): continue
            if state.tags_res_filter == 'Vidéos' and not p.endswith(SUPPORTED_VIDEOS): continue
            # Filtre NSFW
            if state.tags_nsfw_filter != 'Tout':
                tier = _read_nsfw_tier(item[1])
                if state.tags_nsfw_filter == 'Non validé' and tier != '': continue
                elif state.tags_nsfw_filter != 'Non validé' and tier.capitalize() != state.tags_nsfw_filter: continue
            if search_q:
                name = os.path.basename(item[1]).lower()
                top_tag_text = ' '.join(list((item[2] or {}).keys())[:15]).lower()
                if search_q not in name and search_q not in top_tag_text: continue
            filtered_results.append(item)

        if state.tags_sort == 'name_asc':
            filtered_results.sort(key=lambda x: os.path.basename(x[1]).lower())
        elif state.tags_sort == 'name_desc':
            filtered_results.sort(key=lambda x: os.path.basename(x[1]).lower(), reverse=True)

        per_page = int(state.tags_per_page or 40)
        total_pages = max(1, (len(filtered_results) + per_page - 1) // per_page)
        if state.tags_page > total_pages: state.tags_page = 1

        def change_page(d):
            state.tags_page = max(1, min(total_pages, state.tags_page + d))
            tags_gallery_ui.refresh()

        def apply_filter(e):
            state.tags_res_filter = e.value
            state.tags_page = 1
            tags_gallery_ui.refresh()

        def apply_nsfw_filter_tags(e):
            state.tags_nsfw_filter = e.value
            state.tags_page = 1
            tags_gallery_ui.refresh()

        def apply_search(e):
            state.tags_search = str(e.value or '')
            state.tags_page = 1
            tags_gallery_ui.refresh()

        def apply_sort(e):
            state.tags_sort = str(e.value or 'score')
            state.tags_page = 1
            tags_gallery_ui.refresh()

        def apply_per_page(e):
            state.tags_per_page = int(e.value or 40)
            state.tags_page = 1
            tags_gallery_ui.refresh()

        compact = bool(state.tags_compact)

        _s_start = (state.tags_page - 1) * per_page
        _page_paths_tags = [item[1] for item in filtered_results[_s_start:_s_start + per_page]]

        with ui.column().classes('w-full h-full flex flex-col p-0 m-0 gap-0 relative'):
            with ui.column().classes('w-full shrink-0 bg-gray-900 p-3 pb-2 border-b border-gray-800 z-20 gap-2 shadow-md'):
                with ui.row().classes('w-full flex justify-between items-center p-2 bg-gray-800 rounded-lg'):
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Tout sélectionner', on_click=lambda: set_all('tags', True)).props('outline color=white dense')
                        ui.button('Tout désélectionner', on_click=lambda: set_all('tags', False)).props('outline color=white dense')
                        ui.button('Sél. page', on_click=lambda pp=_page_paths_tags: set_page_items('tags', True, pp)).props('outline color=cyan dense')
                        ui.button('Désél. page', on_click=lambda pp=_page_paths_tags: set_page_items('tags', False, pp)).props('outline color=cyan dense')
                        ui.toggle(['Tout', 'Images', 'Vidéos'], value=state.tags_res_filter, on_change=apply_filter).classes('text-xs ml-2')
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Export HTML', icon='html', on_click=lambda: export_html_action('tags')).props('color=purple dense outline')
                        ui.button('Écrire TXT', icon='description', on_click=lambda: write_tags_sidecar_files('txt', tags_threshold.value)).props('color=cyan dense outline')
                        ui.button('Écrire JSON', icon='data_object', on_click=lambda: write_tags_sidecar_files('json', tags_threshold.value)).props('color=indigo dense outline')
                        ui.button('Copier ✔', icon='content_copy', on_click=lambda: execute_batch('copy', 'tags', False, chk_txt_tags.value, tags_threshold.value)).props('color=pink-800 dense')
                        ui.button('Déplacer ✔', icon='drive_file_move', on_click=lambda: execute_batch('move', 'tags', False, chk_txt_tags.value, tags_threshold.value)).props('color=red dense')
                        ui.button('Supprimer ✔', icon='delete', on_click=lambda: delete_selected_media('tags')).props('color=red dense outline')

                with ui.row().classes('w-full items-center gap-2 flex-wrap'):
                    ui.input(placeholder='🔍 Rechercher nom / tag...', value=state.tags_search, on_change=apply_search).props('dense outlined clearable').classes('flex-1 min-w-[160px] bg-gray-800 rounded')
                    ui.select({'score': '↓ Score', 'name_asc': 'A→Z', 'name_desc': 'Z→A'}, value=state.tags_sort, label='Tri', on_change=apply_sort).props('dense outlined').classes('w-28')
                    ui.select({20: '20/page', 40: '40/page', 100: '100/page'}, value=per_page, label='Par page', on_change=apply_per_page).props('dense outlined').classes('w-28')
                    ui.select(
                        {'Tout': '🔵 Tout', 'Sain': '✅ Sain', 'Sensuel': '⚡ Sensuel', 'Explicit': '🔞 Explicit', 'Non validé': '? Non validé'},
                        value=state.tags_nsfw_filter, label='NSFW', on_change=apply_nsfw_filter_tags,
                    ).props('dense outlined').classes('w-28').tooltip('Filtrer par validation NSFW')
                    ui.button(icon='grid_view' if not compact else 'view_module', on_click=lambda: (setattr(state, 'tags_compact', not state.tags_compact), tags_gallery_ui.refresh())).props('flat round dense color=white').tooltip('Basculer vue compacte')
                    ui.label(f'{len(filtered_results)} résultat(s)').classes('text-xs text-gray-400 ml-1')

                with ui.row().classes('w-full justify-center my-0 items-center gap-4'):
                    ui.button(icon='chevron_left', on_click=lambda: change_page(-1)).props('flat outline color=white')
                    ui.label(f'Page {state.tags_page} / {total_pages} ({len(filtered_results)} items)').classes('text-gray-300 font-bold text-sm')
                    ui.button(icon='chevron_right', on_click=lambda: change_page(1)).props('flat outline color=white')

            scroll_id = 'tags_scroll_area'
            with ui.column().classes('w-full flex-1 overflow-y-auto p-4 relative').props(f'id="{scroll_id}"'):
                start_idx = (state.tags_page - 1) * per_page
                page_items = filtered_results[start_idx : start_idx + per_page]
                all_paths = [p for s, p, t in filtered_results]

                if not page_items:
                    ui.label("Aucun fichier pour le filtre sélectionné.").classes("text-gray-400 m-4")

                cols = int(state.grid_columns) + (3 if compact else 0)
                gap_cls = 'w-full gap-1 pb-10' if compact else 'w-full gap-6 pb-10'
                with ui.grid(columns=cols).classes(gap_cls):
                    for score, path, tags_dict in page_items:
                        safe_path = urllib.parse.quote(path)
                        global_index = all_paths.index(path)
                        top_tags = sorted(tags_dict.items(), key=lambda x: x[1], reverse=True)[:3]
                        top_tags_str = ", ".join([f"{k}" for k, v in top_tags])
                        nsfw_tier = _read_nsfw_tier(path)
                        card_bdr = f'border-2 {_NSFW_CARD_BORDER.get(nsfw_tier, "border-gray-600")}' if nsfw_tier else 'border border-gray-700'

                        if compact:
                            with ui.card().classes(f'bg-gray-800 {card_bdr} hover:border-pink-500 transition-colors p-0 overflow-hidden relative'):
                                with ui.row().classes('absolute top-1 left-1 bg-black/70 rounded px-0.5 z-10'):
                                    ui.checkbox().bind_value(state.sel_tags, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'tags'), ['shiftKey']).props('dense size=xs')
                                if nsfw_tier:
                                    lbl, cls = _NSFW_BADGE.get(nsfw_tier, _NSFW_BADGE[''])
                                    ui.label(lbl).classes(f'absolute top-1 right-1 text-[8px] font-bold px-1 rounded border {cls} z-10')
                                _emit_ia_badge_overlay(path, position_classes='top-6 right-1', size_classes='text-[8px] px-1')
                                with ui.context_menu():
                                    ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                    ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                    ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))
                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))
                                ui.label(os.path.basename(path)).classes('text-[9px] text-gray-400 px-1 pb-0.5 truncate w-full').tooltip(path)
                        else:
                            with ui.card().classes(f'bg-gray-800 {card_bdr} hover:border-pink-500 transition-colors p-0 overflow-hidden relative'):
                                with ui.row().classes('absolute top-2 left-2 bg-black/60 rounded px-1 z-10'):
                                    ui.checkbox().bind_value(state.sel_tags, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'tags'), ['shiftKey'])
                                if nsfw_tier:
                                    lbl, cls = _NSFW_BADGE.get(nsfw_tier, _NSFW_BADGE[''])
                                    ui.label(lbl).classes(f'absolute top-2 right-2 text-[9px] font-bold px-1.5 py-0.5 rounded border {cls} z-10')
                                _emit_ia_badge_overlay(path, position_classes='top-8 right-2')
                                with ui.context_menu():
                                    ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                    ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                    ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))
                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))
                                with ui.row().classes('w-full justify-between items-center p-2 bg-gray-800'):
                                    ui.label(top_tags_str).classes('text-pink-400 font-bold text-xs truncate max-w-[80%]').tooltip(", ".join([f"{k} ({v:.2f})" for k, v in top_tags]))
                                    with ui.row().classes('gap-1 items-center'):
                                        ui.button(icon='article', on_click=lambda p=path: show_prompt_debug(p)).props('flat round dense color=cyan').tooltip('Prompt / prompt détaillé')
                                        ui.button(icon='sell', on_click=lambda p=path, d=tags_dict: show_tags_debug(p, d)).props('flat round dense color=white').tooltip('Tous les tags')
                                ui.label(os.path.basename(path)).classes('text-xs text-gray-400 px-2 pb-2 truncate w-full').tooltip(path)

            ui.button(icon='keyboard_arrow_up', on_click=lambda: ui.run_javascript(f'document.getElementById("{scroll_id}").scrollTo({{top: 0, behavior: "smooth"}})')).props('round color=pink-800').classes('absolute bottom-6 right-6 z-50 shadow-lg').tooltip('Haut de page')

    @ui.refreshable
    def prompt_gallery_ui():
        if not state.prompt_results:
            return ui.label("Les prompts détectés apparaîtront ici...").classes("text-gray-400 m-4")

        search_q = str(state.prompt_search or '').strip().lower()
        filtered_results = []
        for item in state.prompt_results:
            p = item[1].lower()
            prompt_text = str(item[2] or '').strip()
            detailed_text = str(item[3] or '').strip()
            # Validation runtime : si garbage en mémoire, on l'ignore pour l'affichage
            if prompt_text and not TagEngine._is_valid_prompt_text(prompt_text):
                prompt_text = ''
            if detailed_text and not TagEngine._is_valid_prompt_text(detailed_text):
                detailed_text = ''
            # Réécrire l'item avec les valeurs nettoyées pour que les cartes utilisent les bonnes
            item = (item[0], item[1], prompt_text, detailed_text, item[4], item[5])
            if state.prompt_res_filter == 'Images' and not p.endswith(SUPPORTED_IMAGES): continue
            if state.prompt_res_filter == 'Vidéos' and not p.endswith(SUPPORTED_VIDEOS): continue
            if state.prompt_hide_with_prompt and (prompt_text or detailed_text): continue
            # Filtre NSFW
            if state.prompt_nsfw_filter != 'Tout':
                tier = _read_nsfw_tier(item[1])
                if state.prompt_nsfw_filter == 'Non validé' and tier != '': continue
                elif state.prompt_nsfw_filter != 'Non validé' and tier.capitalize() != state.prompt_nsfw_filter: continue
            if search_q:
                name = os.path.basename(item[1]).lower()
                combined_text = (prompt_text + ' ' + detailed_text).lower()
                if search_q not in name and search_q not in combined_text: continue
            filtered_results.append(item)

        if state.prompt_sort == 'name_asc':
            filtered_results.sort(key=lambda x: os.path.basename(x[1]).lower())
        elif state.prompt_sort == 'name_desc':
            filtered_results.sort(key=lambda x: os.path.basename(x[1]).lower(), reverse=True)
        elif state.prompt_sort == 'score_asc':
            filtered_results.sort(key=lambda x: x[0])
        else:  # 'score' — score descendant (défaut)
            filtered_results.sort(key=lambda x: x[0], reverse=True)

        per_page = int(state.prompt_per_page or 40)
        total_pages = max(1, (len(filtered_results) + per_page - 1) // per_page)
        if state.prompt_page > total_pages: state.prompt_page = 1

        def change_page(d):
            state.prompt_page = max(1, min(total_pages, state.prompt_page + d))
            prompt_gallery_ui.refresh()

        def apply_filter(e):
            state.prompt_res_filter = e.value
            state.prompt_page = 1
            prompt_gallery_ui.refresh()

        def apply_nsfw_filter_prompt(e):
            state.prompt_nsfw_filter = e.value
            state.prompt_page = 1
            prompt_gallery_ui.refresh()

        def apply_search(e):
            state.prompt_search = str(e.value or '')
            state.prompt_page = 1
            prompt_gallery_ui.refresh()

        def apply_sort(e):
            state.prompt_sort = str(e.value or 'score')
            state.prompt_page = 1
            prompt_gallery_ui.refresh()

        def apply_per_page(e):
            state.prompt_per_page = int(e.value or 40)
            state.prompt_page = 1
            prompt_gallery_ui.refresh()

        compact = bool(state.prompt_compact)

        _s_start = (state.prompt_page - 1) * per_page
        _page_paths_prompt = [item[1] for item in filtered_results[_s_start:_s_start + per_page]]

        with ui.column().classes('w-full h-full flex flex-col p-0 m-0 gap-0 relative'):
            with ui.column().classes('w-full shrink-0 bg-gray-900 p-3 pb-2 border-b border-gray-800 z-20 gap-2 shadow-md'):
                with ui.row().classes('w-full flex justify-between items-center p-2 bg-gray-800 rounded-lg'):
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Tout sélectionner', on_click=lambda: set_all('prompt', True)).props('outline color=white dense')
                        ui.button('Tout désélectionner', on_click=lambda: set_all('prompt', False)).props('outline color=white dense')
                        ui.button('Sél. page', on_click=lambda pp=_page_paths_prompt: set_page_items('prompt', True, pp)).props('outline color=cyan dense')
                        ui.button('Désél. page', on_click=lambda pp=_page_paths_prompt: set_page_items('prompt', False, pp)).props('outline color=cyan dense')
                        ui.toggle(['Tout', 'Images', 'Vidéos'], value=state.prompt_res_filter, on_change=apply_filter).classes('text-xs ml-2')
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Export HTML', icon='html', on_click=lambda: export_html_action('prompt')).props('color=purple dense outline')
                        ui.button('📄 Prompts TXT', on_click=lambda: write_prompt_sidecar_files('prompt')).props('color=indigo dense outline')
                        ui.button('Copier ✔', icon='content_copy', on_click=lambda: execute_batch('copy', 'prompt', False, False)).props('color=teal-800 dense')
                        ui.button('Déplacer ✔', icon='drive_file_move', on_click=lambda: execute_batch('move', 'prompt', False, False)).props('color=red dense')
                        ui.button('Supprimer ✔', icon='delete', on_click=lambda: delete_selected_media('prompt')).props('color=red dense outline')

                with ui.row().classes('w-full items-center gap-2 flex-wrap'):
                    ui.input(placeholder='🔍 Rechercher nom / prompt...', value=state.prompt_search, on_change=apply_search).props('dense outlined clearable').classes('flex-1 min-w-[160px] bg-gray-800 rounded')
                    ui.select({'score': '↓ Score', 'score_asc': '↑ Score', 'name_asc': 'A→Z', 'name_desc': 'Z→A'}, value=state.prompt_sort, label='Tri', on_change=apply_sort).props('dense outlined').classes('w-28')
                    ui.select({20: '20/page', 40: '40/page', 100: '100/page'}, value=per_page, label='Par page', on_change=apply_per_page).props('dense outlined').classes('w-28')
                    ui.select(
                        {'Tout': '🔵 Tout', 'Sain': '✅ Sain', 'Sensuel': '⚡ Sensuel', 'Explicit': '🔞 Explicit', 'Non validé': '? Non validé'},
                        value=state.prompt_nsfw_filter, label='NSFW', on_change=apply_nsfw_filter_prompt,
                    ).props('dense outlined').classes('w-28').tooltip('Filtrer par validation NSFW')
                    ui.button(icon='grid_view' if not compact else 'view_module', on_click=lambda: (setattr(state, 'prompt_compact', not state.prompt_compact), prompt_gallery_ui.refresh())).props('flat round dense color=white').tooltip('Basculer vue compacte')
                    ui.label(f'{len(filtered_results)} résultat(s)').classes('text-xs text-gray-400 ml-1')

                with ui.row().classes('w-full justify-center my-0 items-center gap-4'):
                    ui.button(icon='chevron_left', on_click=lambda: change_page(-1)).props('flat outline color=white')
                    ui.label(f'Page {state.prompt_page} / {total_pages} ({len(filtered_results)} items)').classes('text-gray-300 font-bold text-sm')
                    ui.button(icon='chevron_right', on_click=lambda: change_page(1)).props('flat outline color=white')

            scroll_id = 'prompt_scroll_area'
            with ui.column().classes('w-full flex-1 overflow-y-auto p-4 relative').props(f'id="{scroll_id}"'):
                start_idx = (state.prompt_page - 1) * per_page
                page_items = filtered_results[start_idx : start_idx + per_page]
                all_paths = [p for _score, p, _prompt, _detailed, _psrc, _dsrc in filtered_results]

                if not page_items:
                    ui.label("Aucun fichier pour le filtre sélectionné.").classes("text-gray-400 m-4")

                cols = int(state.grid_columns) + (3 if compact else 0)
                gap_cls = 'w-full gap-1 pb-10' if compact else 'w-full gap-6 pb-10'
                with ui.grid(columns=cols).classes(gap_cls):
                    for score, path, prompt_text, detailed_prompt_text, prompt_source, detailed_source in page_items:
                        safe_path = urllib.parse.quote(path)
                        global_index = all_paths.index(path)
                        preview = (prompt_text or detailed_prompt_text or '').strip()
                        nsfw_tier = _read_nsfw_tier(path)
                        card_bdr = f'border-2 {_NSFW_CARD_BORDER.get(nsfw_tier, "border-gray-600")}' if nsfw_tier else 'border border-gray-700'

                        if compact:
                            with ui.card().classes(f'bg-gray-800 {card_bdr} hover:border-cyan-500 transition-colors p-0 overflow-hidden relative'):
                                with ui.row().classes('absolute top-1 left-1 bg-black/70 rounded px-0.5 z-10'):
                                    ui.checkbox().bind_value(state.sel_prompt, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'prompt'), ['shiftKey']).props('dense size=xs')
                                if nsfw_tier:
                                    lbl, cls = _NSFW_BADGE.get(nsfw_tier, _NSFW_BADGE[''])
                                    ui.label(lbl).classes(f'absolute top-1 right-1 text-[8px] font-bold px-1 rounded border {cls} z-10')
                                _emit_ia_badge_overlay(path, position_classes='top-6 right-1', size_classes='text-[8px] px-1')
                                with ui.context_menu():
                                    ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                    ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                    ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))
                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))
                                with ui.row().classes('w-full items-center justify-between px-1 pb-0.5 gap-1'):
                                    ui.label(os.path.basename(path)).classes('text-[9px] text-gray-400 truncate flex-1').tooltip(preview or path)
                                    if prompt_source:
                                        ui.label('●').classes('text-[8px] text-emerald-400').tooltip(f'Source: {prompt_source}')
                                    if detailed_source:
                                        ui.label('●').classes('text-[8px] text-cyan-400').tooltip(f'Détaillé: {detailed_source}')
                        else:
                            source_badges = []
                            if prompt_source:
                                source_badges.append((f"Source: {prompt_source}", 'text-emerald-300 bg-emerald-900/30 border-emerald-700'))
                            if detailed_source:
                                source_badges.append((f"Détaillé: {detailed_source}", 'text-cyan-300 bg-cyan-900/30 border-cyan-700'))
                            preview_short = preview[:240] + ('...' if len(preview) > 240 else '')

                            with ui.card().classes(f'bg-gray-800 {card_bdr} hover:border-cyan-500 transition-colors p-0 overflow-hidden relative'):
                                with ui.row().classes('absolute top-2 left-2 bg-black/60 rounded px-1 z-10'):
                                    ui.checkbox().bind_value(state.sel_prompt, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'prompt'), ['shiftKey'])
                                if nsfw_tier:
                                    lbl, cls = _NSFW_BADGE.get(nsfw_tier, _NSFW_BADGE[''])
                                    ui.label(lbl).classes(f'absolute top-2 right-2 text-[9px] font-bold px-1.5 py-0.5 rounded border {cls} z-10')
                                _emit_ia_badge_overlay(path, position_classes='top-8 right-2')
                                with ui.context_menu():
                                    ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                    ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                    ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))
                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))
                                with ui.column().classes('w-full p-3 gap-2 bg-gray-800'):
                                    with ui.row().classes('w-full justify-between items-start gap-2'):
                                        ui.label(os.path.basename(path)).classes('text-sm text-cyan-300 font-bold truncate max-w-[75%]').tooltip(path)
                                        ui.button(icon='article', on_click=lambda p=path: show_prompt_debug(p)).props('flat round dense color=cyan').tooltip('Voir prompts')
                                    if source_badges:
                                        with ui.row().classes('w-full gap-2 flex-wrap items-center'):
                                            for badge_text, badge_classes in source_badges:
                                                ui.label(badge_text).classes(f'text-[10px] px-2 py-1 rounded-full border {badge_classes}')
                                    ui.label(preview_short or 'Aucun prompt disponible').classes('text-sm text-gray-200 whitespace-pre-wrap leading-5')
                                    ui.label(f'Score: {score:.2f}').classes('text-xs text-gray-500')

            ui.button(icon='keyboard_arrow_up', on_click=lambda: ui.run_javascript(f'document.getElementById("{scroll_id}").scrollTo({{top: 0, behavior: "smooth"}})')).props('round color=cyan').classes('absolute bottom-6 right-6 z-50 shadow-lg').tooltip('Haut de page')

    # ------------------------------------------------------------------
    # Galerie Détecteur IA — clone allégé de prompt_gallery_ui()
    # ------------------------------------------------------------------
    _IA_BADGES = {
        True:  ('🤖 IA',     'bg-pink-900/40 text-pink-200 border-pink-700'),
        False: ('📷 Photo',  'bg-emerald-900/40 text-emerald-200 border-emerald-700'),
        None:  ('❓ Inconnu', 'bg-gray-800 text-gray-300 border-gray-600'),
    }

    @ui.refreshable
    def ia_gallery_ui():
        if not state.ia_results:
            return ui.label("Lance une analyse pour peupler la galerie de détection IA…").classes("text-gray-400 m-4")

        search_q = str(state.ia_search or '').strip().lower()
        status_filter = str(getattr(state, 'ia_status_filter', 'Tout') or 'Tout')
        filtered = []
        for item in state.ia_results:
            # item = (score, path, is_ai, confidence, method, detection)
            p_l = item[1].lower()
            if state.ia_res_filter == 'Images' and not p_l.endswith(SUPPORTED_IMAGES): continue
            if state.ia_res_filter == 'Vidéos' and not p_l.endswith(SUPPORTED_VIDEOS): continue
            is_ai_v = item[2]
            if status_filter == 'IA' and is_ai_v is not True: continue
            if status_filter == 'Photo' and is_ai_v is not False: continue
            if status_filter == 'Inconnu' and is_ai_v is not None: continue
            if search_q:
                name = os.path.basename(item[1]).lower()
                method = str(item[4] or '').lower()
                if search_q not in name and search_q not in method: continue
            filtered.append(item)

        if state.ia_sort == 'name_asc':
            filtered.sort(key=lambda x: os.path.basename(x[1]).lower())
        elif state.ia_sort == 'name_desc':
            filtered.sort(key=lambda x: os.path.basename(x[1]).lower(), reverse=True)
        elif state.ia_sort == 'score_asc':
            filtered.sort(key=lambda x: x[0])
        else:
            filtered.sort(key=lambda x: x[0], reverse=True)

        per_page = int(state.ia_per_page or 40)
        total_pages = max(1, (len(filtered) + per_page - 1) // per_page)
        if state.ia_page > total_pages: state.ia_page = 1

        def change_page(d):
            state.ia_page = max(1, min(total_pages, state.ia_page + d))
            ia_gallery_ui.refresh()
        def apply_filter(e):
            state.ia_res_filter = e.value; state.ia_page = 1; ia_gallery_ui.refresh()
        def apply_status(e):
            state.ia_status_filter = e.value; state.ia_page = 1; ia_gallery_ui.refresh()
        def apply_search(e):
            state.ia_search = str(e.value or ''); state.ia_page = 1; ia_gallery_ui.refresh()
        def apply_sort(e):
            state.ia_sort = str(e.value or 'score'); state.ia_page = 1; ia_gallery_ui.refresh()
        def apply_per_page(e):
            state.ia_per_page = int(e.value or 40); state.ia_page = 1; ia_gallery_ui.refresh()

        compact = bool(state.ia_compact)
        _s_start = (state.ia_page - 1) * per_page
        _page_paths_ia = [item[1] for item in filtered[_s_start:_s_start + per_page]]

        with ui.column().classes('w-full h-full flex flex-col p-0 m-0 gap-0 relative'):
            with ui.column().classes('w-full shrink-0 bg-gray-900 p-3 pb-2 border-b border-gray-800 z-20 gap-2 shadow-md'):
                with ui.row().classes('w-full flex justify-between items-center p-2 bg-gray-800 rounded-lg'):
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Tout sélectionner', on_click=lambda: set_all('ia', True)).props('outline color=white dense')
                        ui.button('Tout désélectionner', on_click=lambda: set_all('ia', False)).props('outline color=white dense')
                        ui.button('Sél. page', on_click=lambda pp=_page_paths_ia: set_page_items('ia', True, pp)).props('outline color=cyan dense')
                        ui.button('Désél. page', on_click=lambda pp=_page_paths_ia: set_page_items('ia', False, pp)).props('outline color=cyan dense')
                        ui.toggle(['Tout', 'Images', 'Vidéos'], value=state.ia_res_filter, on_change=apply_filter).classes('text-xs ml-2')
                        ui.toggle(['Tout', 'IA', 'Photo', 'Inconnu'], value=status_filter, on_change=apply_status).classes('text-xs ml-2')
                    with ui.row().classes('gap-2 items-center flex-wrap'):
                        ui.button('Export HTML', icon='html', on_click=lambda: export_html_action('ia')).props('color=purple dense outline')
                        ui.button('Copier ✔', icon='content_copy', on_click=lambda: execute_batch('copy', 'ia', False, False)).props('color=teal-800 dense')
                        ui.button('Déplacer ✔', icon='drive_file_move', on_click=lambda: execute_batch('move', 'ia', False, False)).props('color=red dense')
                        ui.button('Supprimer ✔', icon='delete', on_click=lambda: delete_selected_media('ia')).props('color=red dense outline')

                with ui.row().classes('w-full items-center gap-2 flex-wrap'):
                    ui.input(placeholder='🔍 Rechercher nom / méthode…', value=state.ia_search, on_change=apply_search).props('dense outlined clearable').classes('flex-1 min-w-[160px] bg-gray-800 rounded')
                    ui.select({'score': '↓ Score', 'score_asc': '↑ Score', 'name_asc': 'A→Z', 'name_desc': 'Z→A'}, value=state.ia_sort, label='Tri', on_change=apply_sort).props('dense outlined').classes('w-28')
                    ui.select({20: '20/page', 40: '40/page', 100: '100/page'}, value=per_page, label='Par page', on_change=apply_per_page).props('dense outlined').classes('w-28')
                    ui.button(icon='grid_view' if not compact else 'view_module', on_click=lambda: (setattr(state, 'ia_compact', not state.ia_compact), ia_gallery_ui.refresh())).props('flat round dense color=white').tooltip('Basculer vue compacte')
                    ui.label(f'{len(filtered)} résultat(s)').classes('text-xs text-gray-400 ml-1')

                with ui.row().classes('w-full justify-center my-0 items-center gap-4'):
                    ui.button(icon='chevron_left', on_click=lambda: change_page(-1)).props('flat outline color=white')
                    ui.label(f'Page {state.ia_page} / {total_pages} ({len(filtered)} items)').classes('text-gray-300 font-bold text-sm')
                    ui.button(icon='chevron_right', on_click=lambda: change_page(1)).props('flat outline color=white')

            scroll_id = 'ia_scroll_area'
            with ui.column().classes('w-full flex-1 overflow-y-auto p-4 relative').props(f'id="{scroll_id}"'):
                start_idx = (state.ia_page - 1) * per_page
                page_items = filtered[start_idx : start_idx + per_page]
                all_paths = [it[1] for it in filtered]

                if not page_items:
                    ui.label("Aucun fichier pour le filtre sélectionné.").classes("text-gray-400 m-4")

                cols = int(state.grid_columns) + (3 if compact else 0)
                gap_cls = 'w-full gap-1 pb-10' if compact else 'w-full gap-6 pb-10'
                with ui.grid(columns=cols).classes(gap_cls):
                    for score, path, is_ai_v, conf, method, _det in page_items:
                        safe_path = urllib.parse.quote(path)
                        global_index = all_paths.index(path)
                        badge_text, badge_cls = _IA_BADGES.get(is_ai_v, _IA_BADGES[None])
                        nsfw_tier = _read_nsfw_tier(path)
                        card_bdr = f'border-2 {_NSFW_CARD_BORDER.get(nsfw_tier, "border-gray-600")}' if nsfw_tier else 'border border-gray-700'

                        if compact:
                            with ui.card().classes(f'bg-gray-800 {card_bdr} hover:border-cyan-500 transition-colors p-0 overflow-hidden relative'):
                                with ui.row().classes('absolute top-1 left-1 bg-black/70 rounded px-0.5 z-10'):
                                    ui.checkbox().bind_value(state.sel_ia, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'ia'), ['shiftKey']).props('dense size=xs')
                                ui.label(badge_text).classes(f'absolute top-1 right-1 text-[9px] font-bold px-1 rounded border {badge_cls} z-10')
                                with ui.context_menu():
                                    ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                    ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                    ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))
                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))
                                ui.label(os.path.basename(path)).classes('text-[9px] text-gray-400 truncate px-1 pb-0.5').tooltip(f'{method} · conf={conf:.2f}')
                        else:
                            with ui.card().classes(f'bg-gray-800 {card_bdr} hover:border-cyan-500 transition-colors p-0 overflow-hidden relative'):
                                with ui.row().classes('absolute top-2 left-2 bg-black/60 rounded px-1 z-10'):
                                    ui.checkbox().bind_value(state.sel_ia, path).on('click', lambda e, i=global_index, p=path: handle_shift_click(e, i, p, 'ia'), ['shiftKey'])
                                ui.label(badge_text).classes(f'absolute top-2 right-2 text-[10px] font-bold px-2 py-0.5 rounded border {badge_cls} z-10')
                                with ui.context_menu():
                                    ui.menu_item('Copier le chemin', on_click=lambda p=path: ui.clipboard.write(p))
                                    ui.menu_item('Copier l\'image', on_click=lambda p=path: copy_image_to_clipboard(p))
                                    ui.menu_item('Ouvrir le dossier', on_click=lambda p=path: reveal_file_native(p))
                                ui.image(f"/thumb/{safe_path}").classes('w-full aspect-square object-contain cursor-pointer bg-black').props('fit=contain').on('click', lambda e, idx=global_index: open_media(idx, all_paths))
                                with ui.column().classes('w-full p-3 gap-2 bg-gray-800'):
                                    ui.label(os.path.basename(path)).classes('text-sm text-cyan-300 font-bold truncate').tooltip(path)
                                    ui.label(f'Méthode: {method or "—"}').classes('text-[10px] text-gray-400 truncate').tooltip(method or '')
                                    ui.label(f'Confiance: {conf:.2f}  ·  Score: {score:.2f}').classes('text-xs text-gray-500')

            ui.button(icon='keyboard_arrow_up', on_click=lambda: ui.run_javascript(f'document.getElementById("{scroll_id}").scrollTo({{top: 0, behavior: "smooth"}})')).props('round color=pink').classes('absolute bottom-6 right-6 z-50 shadow-lg').tooltip('Haut de page')

    with ui.tab_panels(tabs).bind_value(state, 'current_tab').classes('w-full bg-[#121212] p-0'):
        
        # ВКЛАДКА 1: ПОИСК
        with ui.tab_panel(tab_search).classes('w-full h-[calc(100vh-115px)] p-4 flex flex-row flex-nowrap items-stretch gap-4'):
            with ui.column().classes('w-[350px] shrink-0 bg-gray-900 rounded-xl border border-gray-800 shadow-lg flex flex-col overflow-hidden p-0 gap-0'):
                with ui.row().classes('w-full p-4 pb-2 shrink-0 border-b border-gray-800 bg-gray-900 z-10'):
                    ui.label('Paramètres de recherche').classes('text-lg font-bold')
                
                with ui.column().classes('w-full flex-1 overflow-y-auto p-4 gap-2 min-h-0'):
                    with ui.row().classes('w-full items-center gap-1 flex-nowrap'):
                        inp_dir = ui.input('Dossier', value=cfg.get('inp_dir', '')).classes('flex-grow')
                        ui.button(icon='folder', on_click=lambda: select_folder(inp_dir)).props('flat round dense')
                        ui.button(icon='delete_sweep', on_click=lambda: clear_folder_cache(inp_dir.value)).props('flat round dense text-color=red').tooltip('Effacer l\'index du dossier')
                    
                    inp_query = ui.input('Requête ou chemin', value=cfg.get('inp_query', '')).classes('w-full')
                    
                    with ui.row().classes('w-full gap-2'):
                        chk_img = ui.checkbox('Images', value=cfg.get('chk_img', True))
                        chk_vid = ui.checkbox('Vidéos', value=cfg.get('chk_vid', True))
                        chk_txt = ui.checkbox('Texte', value=cfg.get('chk_txt', False))
                    
                    emb_model = ui.select(['Qwen/Qwen3-VL-Embedding-2B', 'Qwen/Qwen3-VL-Embedding-8B'], value=cfg.get('emb_model', 'Qwen/Qwen3-VL-Embedding-2B'), label='Modèle').classes('w-full')
                    top_k = ui.number('Top K', value=cfg.get('top_k', 50), format='%.0f').classes('w-full')
                    use_reranker = ui.switch('Analyse approfondie (Reranker)', value=cfg.get('use_reranker', False))
                    rerank_model = ui.select(['Qwen/Qwen3-VL-Reranker-2B', 'Qwen/Qwen3-VL-Reranker-8B'], value=cfg.get('rerank_model', 'Qwen/Qwen3-VL-Reranker-2B')).classes('w-full').bind_visibility_from(use_reranker, 'value')
                    
                    chk_prefix_search = ui.checkbox('Préfixer le score au nom (copie)', value=cfg.get('chk_prefix_search', False)).classes('text-sm text-gray-300 w-full mt-2')

                    with ui.expansion('Paramètres avancés de recherche', icon='tune').classes('w-full bg-gray-800/50 rounded-lg border border-gray-700 mt-2'):
                        with ui.row().classes('w-full gap-2 px-2 pt-2'):
                            batch_size = ui.number('Lot', value=cfg.get('batch_size', 16), format='%.0f').classes('w-[45%]')
                            video_frames = ui.number('Images vid.', value=cfg.get('video_frames', 4), format='%.0f').classes('w-[45%]')
                        with ui.row().classes('w-full gap-2 px-2'):
                            emb_size = ui.number('Rés. (P1)', value=cfg.get('emb_size', 512), format='%.0f').classes('w-[45%]')
                            rerank_size = ui.number('Rés. (P2)', value=cfg.get('rerank_size', 800), format='%.0f').classes('w-[45%]')
                        with ui.row().classes('w-full gap-2 px-2 pb-2'):
                            search_quant_mode = ui.select(['None', '8-bit', '4-bit'], value=cfg.get('search_quant_mode', 'None'), label='Quant.').classes('w-[45%]')
                            min_score = ui.number('Score min.', value=cfg.get('min_score', 0.25), format='%.2f', step=0.05).classes('w-[45%]')
                        
                        with ui.column().classes('w-full gap-0 px-2 pb-2 pt-2 border-t border-gray-700'):
                            search_nsfw_filter = ui.toggle(['Tout', 'SFW seulement', 'NSFW seulement'], value=cfg.get('search_nsfw_filter', 'Tout')).classes('w-full text-xs mb-1')
                            search_strict_nsfw = ui.checkbox('Mode strict (masquer fichiers absents de la BD NSFW)', value=cfg.get('search_strict_nsfw', False)) \
                                .classes('text-xs text-red-400') \
                                .bind_visibility_from(search_nsfw_filter, 'value', value=lambda v: v != 'Tout')

                async def run_search_action():
                    save_config({
                        'inp_dir': inp_dir.value, 'inp_query': inp_query.value,
                        'chk_img': chk_img.value, 'chk_vid': chk_vid.value, 'chk_txt': chk_txt.value,
                        'emb_model': emb_model.value, 'top_k': top_k.value,
                        'use_reranker': use_reranker.value, 'rerank_model': rerank_model.value,
                        'batch_size': batch_size.value, 'video_frames': video_frames.value,
                        'emb_size': emb_size.value, 'rerank_size': rerank_size.value,
                        'search_quant_mode': search_quant_mode.value, 'min_score': min_score.value,
                        'chk_prefix_search': chk_prefix_search.value,
                        'search_nsfw_filter': search_nsfw_filter.value, 'search_strict_nsfw': search_strict_nsfw.value
                    })
                    if not inp_dir.value or not inp_query.value: return ui.notify("Indiquez un dossier et une requête !", type='warning')
                    
                    state.is_processing = True
                    search_engine.cancel_flag = False
                    state.search_results.clear()
                    state.sel_search.clear()
                    state.search_page = 1
                    search_gallery_ui.refresh()
                    btn_search.disable()
                    aesthetic_engine.unload()
                    nsfw_engine.unload()
                    face_engine.unload()
                    tag_engine.unload()
                    
                    exts =[]
                    if chk_img.value: exts.extend(SUPPORTED_IMAGES)
                    if chk_vid.value: exts.extend(SUPPORTED_VIDEOS)
                    if chk_txt.value: exts.extend(SUPPORTED_TEXTS)
                    
                    def task():
                        try:
                            state.add_log(f"Démarrage de la recherche IA pour : '{inp_query.value}'")
                            search_engine.video_frames = int(video_frames.value)
                            search_engine.emb_size = int(emb_size.value)
                            search_engine.rerank_size = int(rerank_size.value)
                            search_engine.quant_mode = search_quant_mode.value
                            
                            state.search_base_dir = inp_dir.value
                            q_emb, q_rank = search_engine.prepare_query(inp_query.value)
                            cands = search_engine.phase1_recall(inp_dir.value, inp_query.value, q_emb, int(top_k.value), emb_model.value, int(batch_size.value), tuple(exts))
                            
                            if use_reranker.value: 
                                cands = search_engine.phase2_rerank(inp_query.value, q_rank, cands, float(min_score.value), rerank_model.value)

                            if search_nsfw_filter.value != 'Tout':
                                filtered_cands =[]
                                for score, path in cands:
                                    danger = search_engine.db_cache.get_max_danger_score(path)
                                    if danger == -1.0:
                                        if search_strict_nsfw.value: continue
                                        else:
                                            if search_nsfw_filter.value == 'NSFW seulement': continue 
                                    else:
                                        is_nsfw = danger >= state.nsfw_threshold
                                        if search_nsfw_filter.value == 'SFW seulement' and is_nsfw: continue
                                        if search_nsfw_filter.value == 'NSFW seulement' and not is_nsfw: continue
                                    filtered_cands.append((score, path))
                                cands = filtered_cands
                                
                            state.search_results = cands
                            state.sel_search = {path: False for _, path in cands}
                            state.add_log("✅ Recherche terminée avec succès !")
                        except Exception as e: state.add_log(f"❌ Erreur de recherche : {e}")
                        finally:
                            state.status_text = "Prêt !"
                            state.progress = 1.0
                            state.is_processing = False

                    await run.io_bound(task)
                    search_gallery_ui.refresh()
                    btn_search.enable()

                with ui.row().classes('w-full p-4 pt-2 shrink-0 border-t border-gray-800 bg-gray-900 z-10'):
                    btn_search = ui.button('🚀 Rechercher', on_click=run_search_action).classes('w-full bg-blue-600 hover:bg-blue-500 font-bold')

            with ui.column().classes('flex-1 w-0 bg-gray-900 rounded-xl border border-gray-800 overflow-hidden h-full relative p-0'):
                search_gallery_ui()

        # ВКЛАДКА 2: ЭСТЕТИКА
        with ui.tab_panel(tab_aesthetic).classes('w-full h-[calc(100vh-115px)] p-4 flex flex-row flex-nowrap items-stretch gap-4'):
            with ui.column().classes('w-[350px] shrink-0 bg-gray-900 rounded-xl border border-gray-800 shadow-lg flex flex-col overflow-hidden p-0 gap-0'):
                with ui.row().classes('w-full p-4 pb-2 shrink-0 border-b border-gray-800 bg-gray-900 z-10'):
                    ui.label('Évaluation Esthétique').classes('text-lg font-bold')
                
                with ui.column().classes('w-full flex-1 overflow-y-auto p-4 gap-2 min-h-0'):
                    with ui.row().classes('w-full items-center gap-1 flex-nowrap'):
                        rate_dir = ui.input('Dossier', value=cfg.get('rate_dir', '')).classes('flex-grow')
                        ui.button(icon='folder', on_click=lambda: select_folder(rate_dir)).props('flat round dense')
                        ui.button(icon='delete_sweep', on_click=lambda: clear_folder_cache(rate_dir.value)).props('flat round dense text-color=red').tooltip('Effacer l\'index du dossier')
                        
                    with ui.row().classes('w-full gap-2'):
                        chk_img_aes = ui.checkbox('Images', value=cfg.get('chk_img_aes', True))
                        chk_vid_aes = ui.checkbox('Vidéos', value=cfg.get('chk_vid_aes', False))

                    top_n_rate = ui.number('Conserver le TOP (nb)', value=cfg.get('top_n_rate', 100), format='%.0f').classes('w-full')
                    chk_prefix_aes = ui.checkbox('Préfixer le score au nom (copie)', value=cfg.get('chk_prefix_aes', False)).classes('text-sm text-gray-300 w-full mt-2')

                    with ui.expansion('Paramètres avancés', icon='tune').classes('w-full bg-gray-800/50 rounded-lg border border-gray-700 mt-2'):
                        with ui.row().classes('w-full gap-2 px-2 pt-2'):
                            aes_batch_size = ui.number('Lot', value=cfg.get('aes_batch_size', 16), format='%.0f').classes('w-[45%]')
                            aes_video_frames = ui.number('Images vid.', value=cfg.get('aes_video_frames', 4), format='%.0f').classes('w-[45%]')
                        with ui.row().classes('w-full gap-2 px-2 pb-2'):
                            aes_max_dim = ui.number('Limite résol.', value=cfg.get('aes_max_dim', 512), format='%.0f').classes('w-[45%]')
                            aes_quant_mode = ui.select(['None', '8-bit', '4-bit'], value=cfg.get('aes_quant_mode', 'None'), label='Quant.').classes('w-[45%]')
                            
                        with ui.column().classes('w-full gap-0 px-2 pb-2 pt-2 border-t border-gray-700'):
                            aes_nsfw_filter = ui.toggle(['Tout', 'SFW seulement', 'NSFW seulement'], value=cfg.get('aes_nsfw_filter', 'Tout')).classes('w-full text-xs mb-1')
                            aes_strict_nsfw = ui.checkbox('Mode strict (masquer fichiers absents de la BD NSFW)', value=cfg.get('aes_strict_nsfw', False)) \
                                .classes('text-xs text-red-400') \
                                .bind_visibility_from(aes_nsfw_filter, 'value', value=lambda v: v != 'Tout')
                        with ui.column().classes('w-full gap-1 px-2 pb-2 pt-2 border-t border-gray-700'):
                            ui.label('Filtre qualité').classes('text-xs text-gray-400 font-semibold')
                            aes_sharpness_min = ui.number('Netteté min. (0 = désactivé)', value=cfg.get('aes_sharpness_min', 0), format='%.0f', min=0).classes('w-full') \
                                .tooltip('Variance Laplacien: 0=off, ~50=filtre flou fort, ~200=normal, ~500=strict. Calcul rapide sans IA.')
                
                async def run_aesthetic_action():
                    save_config({
                        'rate_dir': rate_dir.value, 'chk_img_aes': chk_img_aes.value, 'chk_vid_aes': chk_vid_aes.value,
                        'top_n_rate': top_n_rate.value, 'aes_batch_size': aes_batch_size.value, 
                        'aes_video_frames': aes_video_frames.value, 'aes_max_dim': aes_max_dim.value, 
                        'aes_quant_mode': aes_quant_mode.value, 'chk_prefix_aes': chk_prefix_aes.value,
                        'aes_nsfw_filter': aes_nsfw_filter.value, 'aes_strict_nsfw': aes_strict_nsfw.value,
                        'aes_sharpness_min': aes_sharpness_min.value
                    })
                    if not rate_dir.value: return ui.notify("Indiquez un dossier !", type='warning')
                    
                    state.is_processing = True
                    search_engine.cancel_flag = False
                    state.aesthetic_results.clear()
                    state.sel_aes.clear()
                    state.aes_page = 1
                    aesthetic_gallery_ui.refresh()
                    btn_rate.disable()
                    search_engine._unload_embedding_model()
                    nsfw_engine.unload()
                    face_engine.unload()
                    tag_engine.unload()
                    
                    exts =[]
                    if chk_img_aes.value: exts.extend(SUPPORTED_IMAGES)
                    if chk_vid_aes.value: exts.extend(SUPPORTED_VIDEOS)

                    def bg_task():
                        try:
                            state.add_log(f"Démarrage de l'évaluation esthétique : '{rate_dir.value}'")
                            aesthetic_engine.batch_size = int(aes_batch_size.value or 16)
                            aesthetic_engine.video_frames = int(aes_video_frames.value or 4)
                            aesthetic_engine.max_dim = int(aes_max_dim.value or 512)
                            aesthetic_engine.quant_mode = aes_quant_mode.value
                            
                            state.aes_base_dir = rate_dir.value
                            n = int(top_n_rate.value)
                            
                            res = aesthetic_engine.evaluate_media(rate_dir.value, tuple(exts))

                            if aes_nsfw_filter.value != 'Tout':
                                filtered_res =[]
                                for item in res:
                                    path = item[1] 
                                    danger = aesthetic_engine.db_cache.get_max_danger_score(path)
                                    if danger == -1.0: 
                                        if aes_strict_nsfw.value: continue
                                        else:
                                            if aes_nsfw_filter.value == 'NSFW seulement': continue
                                    else:
                                        is_nsfw = danger >= state.nsfw_threshold
                                        if aes_nsfw_filter.value == 'SFW seulement' and is_nsfw: continue
                                        if aes_nsfw_filter.value == 'NSFW seulement' and not is_nsfw: continue
                                    filtered_res.append(item)
                                res = filtered_res

                            sharpness_min = float(aes_sharpness_min.value or 0)
                            if sharpness_min > 0:
                                state.add_log(f"[AES] Calcul de netteté (seuil={sharpness_min:.0f})...")
                                sharp_dim = int(aes_max_dim.value or 512)
                                sharp_res = []
                                removed = 0
                                for idx2, item in enumerate(res):
                                    path = item[1]
                                    if not path.lower().endswith(SUPPORTED_IMAGES):
                                        sharp_res.append(item)  # keep videos
                                        continue
                                    sharpness = _compute_sharpness(path, sharp_dim)
                                    if sharpness >= sharpness_min:
                                        sharp_res.append(item)
                                    else:
                                        removed += 1
                                res = sharp_res
                                state.add_log(f"[AES] Filtre netteté : {removed} image(s) flou(s) retirée(s), {len(res)} restante(s).")
                                
                            state.aes_scan_mode = 'score'
                            state.aesthetic_results = res[:n]
                            state.sel_aes = {path: False for _, path, _ in state.aesthetic_results}
                            state.add_log("✅ Évaluation esthétique terminée !")
                        except Exception as e:
                            state.add_log(f"❌ Erreur : {e}")
                        finally:
                            state.status_text = "Prêt !"
                            state.progress = 1.0
                            state.is_processing = False

                    await run.io_bound(bg_task)
                    aesthetic_gallery_ui.refresh()
                    btn_rate.enable()

                async def run_load_aesthetic_action():
                    if not rate_dir.value:
                        return ui.notify("Indiquez un dossier !", type='warning')
                    if state.is_processing:
                        return ui.notify('Traitement en cours...', type='warning')
                    btn_load_aes.disable()
                    state.is_processing = True
                    state.aesthetic_results.clear()
                    state.sel_aes.clear()
                    state.aes_page = 1
                    aesthetic_gallery_ui.refresh()

                    def bg_load_aes():
                        try:
                            loaded = []
                            state.add_log(f"[AES] Chargement des scores depuis _aesthetic.json : '{rate_dir.value}'")
                            for root, dirs, files in os.walk(rate_dir.value):
                                dirs.sort()
                                for fname in sorted(files):
                                    if not fname.lower().endswith(SUPPORTED_IMAGES):
                                        continue
                                    img_path = os.path.join(root, fname)
                                    stem = os.path.splitext(img_path)[0]
                                    aes_path = stem + '_aesthetic.json'
                                    if not os.path.isfile(aes_path):
                                        continue
                                    try:
                                        with open(aes_path, 'r', encoding='utf-8') as f:
                                            data = json.load(f)
                                        result = data.get('result', {})
                                        avg_score = float(result.get('avg_score', 0.0))
                                        max_score = float(result.get('max_score', avg_score))
                                        loaded.append((avg_score, img_path, max_score))
                                    except Exception as e:
                                        state.add_log(f"[AES] Impossible de lire {fname}: {e}")
                            loaded.sort(key=lambda x: x[0], reverse=True)
                            state.aes_scan_mode = 'score'
                            state.aes_base_dir = rate_dir.value
                            n = int(top_n_rate.value or 0)
                            state.aesthetic_results = loaded[:n] if 0 < n < len(loaded) else loaded
                            state.sel_aes = {p: False for _, p, _ in state.aesthetic_results}
                            state.add_log(f"✅ {len(state.aesthetic_results)} score(s) esthétique(s) chargé(s).")
                        except Exception as e:
                            state.add_log(f"❌ Erreur chargement scores : {e}")
                        finally:
                            state.is_processing = False

                    await run.io_bound(bg_load_aes)
                    aesthetic_gallery_ui.refresh()
                    btn_load_aes.enable()

                async def run_blur_scan_action():
                    threshold = float(aes_sharpness_min.value or 0)
                    if threshold <= 0:
                        ui.notify('Définissez "Netteté min." > 0 pour scanner les images floues.', type='warning')
                        return
                    if state.is_processing:
                        ui.notify('Traitement en cours...', type='warning')
                        return
                    btn_blur_scan.disable()
                    state.is_processing = True
                    state.status_text = "Scan flou en cours..."
                    state.progress = 0.0
                    state.aesthetic_results.clear()
                    state.sel_aes.clear()
                    state.aes_page = 1
                    aesthetic_gallery_ui.refresh()

                    def bg_blur():
                        try:
                            rate_d = rate_dir.value
                            if not rate_d or not os.path.isdir(rate_d):
                                state.add_log("❌ Dossier invalide pour scan flou.")
                                return
                            exts = list(SUPPORTED_IMAGES) if chk_img_aes.value else []
                            all_files = []
                            for root, _, files in os.walk(rate_d):
                                for f in files:
                                    if f.lower().endswith(tuple(exts)):
                                        all_files.append(os.path.join(root, f))
                            total = len(all_files)
                            state.add_log(f"[FLOU] Scan de {total} image(s) (seuil netteté={threshold:.0f})...")
                            sharp_dim = int(aes_max_dim.value or 512)
                            blurry = []
                            for i, path in enumerate(all_files):
                                state.progress = i / max(total, 1)
                                sharpness = _compute_sharpness(path, sharp_dim)
                                if sharpness < threshold:
                                    blurry.append((sharpness, path, sharpness))
                            blurry.sort(key=lambda x: x[0])  # blurriest first
                            state.aes_scan_mode = 'blur'
                            state.aesthetic_results = blurry
                            state.sel_aes = {p: False for _, p, _ in blurry}
                            state.add_log(f"[FLOU] {len(blurry)} image(s) floue(s) trouvée(s) sur {total}.")
                        except Exception as e:
                            state.add_log(f"❌ Erreur scan flou : {e}")
                        finally:
                            state.status_text = "Prêt !"
                            state.progress = 1.0
                            state.is_processing = False

                    await run.io_bound(bg_blur)
                    aesthetic_gallery_ui.refresh()
                    btn_blur_scan.enable()

                with ui.row().classes('w-full p-4 pt-2 shrink-0 border-t border-gray-800 bg-gray-900 z-10 gap-2'):
                    btn_load_aes = ui.button('📂 Charger', on_click=run_load_aesthetic_action).classes('w-[27%] bg-gray-700 hover:bg-gray-600 font-bold text-lg').tooltip('Charger les scores depuis les fichiers _aesthetic.json existants')
                    btn_blur_scan = ui.button('🔍 Flou', on_click=run_blur_scan_action).classes('w-[27%] bg-orange-700 hover:bg-orange-600 font-bold text-lg').tooltip('Scanner les images floues (sous le seuil Netteté min.)')
                    btn_rate = ui.button('✨ Évaluer', on_click=run_aesthetic_action).classes('flex-1 bg-yellow-600 hover:bg-yellow-500 font-bold text-lg')

            with ui.column().classes('flex-1 w-0 bg-gray-900 rounded-xl border border-gray-800 overflow-hidden h-full relative p-0'):
                aesthetic_gallery_ui()

        # ВКЛАДКА 3: NSFW
        with ui.tab_panel(tab_nsfw).classes('w-full h-[calc(100vh-115px)] p-4 flex flex-row flex-nowrap items-stretch gap-4'):
            with ui.column().classes('w-[350px] shrink-0 bg-gray-900 rounded-xl border border-gray-800 shadow-lg flex flex-col overflow-hidden p-0 gap-0'):
                with ui.row().classes('w-full p-4 pb-2 shrink-0 border-b border-gray-800 bg-gray-900 z-10'):
                    ui.label('Détecteur NSFW').classes('text-lg font-bold')
                
                with ui.column().classes('w-full flex-1 overflow-y-auto p-4 gap-2 min-h-0'):
                    with ui.row().classes('w-full items-center gap-1 flex-nowrap'):
                        default_nsfw_dir = cfg.get('nsfw_dir', str(Path.home() / 'Pictures'))
                        nsfw_dir = ui.input('Dossier', value=default_nsfw_dir).classes('flex-grow')
                        ui.button(icon='folder', on_click=lambda: select_folder(nsfw_dir)).props('flat round dense')
                        ui.button(icon='delete_sweep', on_click=lambda: clear_folder_cache(nsfw_dir.value)).props('flat round dense text-color=red').tooltip('Effacer l\'index du dossier')

                    with ui.row().classes('w-full gap-2 items-center'):
                        nsfw_model_sel = ui.select(AVAILABLE_NSFW_MODELS, value=cfg.get('nsfw_model', DEFAULT_NSFW_MODEL), label='Modèle').classes('w-full')
                        
                    nsfw_threshold_slider = ui.slider(min=0.0, max=1.0, step=0.01, value=cfg.get('nsfw_threshold', NSFW_THRESHOLD)).classes('w-full')
                    nsfw_threshold_label = ui.label(f"Seuil: {nsfw_threshold_slider.value:.2f}").classes('text-xs text-gray-400 px-2')
                    def on_threshold_change(e):
                        v = float(e.value)
                        state.nsfw_threshold = v
                        nsfw_threshold_label.set_text(f"Seuil: {v:.2f}")
                        # Re-tier all cached results with new threshold
                        if state.nsfw_all_results:
                            state.nsfw_all_results = [
                                (d, p, nsfw_engine.present_label(dt, bool(dt.get('_portrait_guard', 0))), dt)
                                for d, p, _, dt in state.nsfw_all_results
                            ]
                        if state.nsfw_results:
                            state.nsfw_results = [
                                (d, p, nsfw_engine.present_label(dt, bool(dt.get('_portrait_guard', 0))), dt)
                                for d, p, _, dt in state.nsfw_results
                            ]
                            nsfw_gallery_ui.refresh()
                    nsfw_threshold_slider.on_value_change(on_threshold_change)

                    with ui.row().classes('w-full gap-2'):
                        chk_img_nsfw = ui.checkbox('Images', value=cfg.get('chk_img_nsfw', True))
                        chk_vid_nsfw = ui.checkbox('Vidéos', value=cfg.get('chk_vid_nsfw', False))
                    
                    top_n_default = int(cfg.get('top_n_nsfw', 0) or 0)
                    if top_n_default == 100:
                        top_n_default = 0
                    top_n_nsfw = ui.number('Conserver le TOP (nb, 0 = tout)', value=top_n_default, format='%.0f').classes('w-full')
                    chk_prefix_nsfw = ui.checkbox('Préfixer le taux de danger au nom', value=cfg.get('chk_prefix_nsfw', False)).classes('text-sm text-gray-300 w-full mt-2')

                    def bulk_update_nsfw_labels():
                        selected_paths = [p for p, checked in (state.sel_nsfw or {}).items() if checked]
                        if not selected_paths:
                            return ui.notify('Sélectionnez au moins une image.', type='warning')
                        
                        new_label = state.nsfw_bulk_new_label or 'SAIN'
                        cache_key = f"{state.nsfw_model}_{int(nsfw_video_frames.value)}_{NSFW_CACHE_VERSION}"
                        updated = 0
                        errors = 0
                        
                        for path in selected_paths:
                            try:
                                photo_item = next((item for item in state.nsfw_all_results if item[1] == path), None)
                                if not photo_item:
                                    raise ValueError('Photo non trouvée dans les résultats')

                                danger_score, _path, _tier, details = photo_item
                                updated_details = dict(details) if isinstance(details, dict) else {}
                                updated_details['_manual_tier'] = new_label
                                top_label = str(updated_details.get('_raw_top_label', new_label))
                                state.db_cache.save_nsfw_score(cache_key, path, top_label, danger_score, updated_details)

                                for i, item in enumerate(state.nsfw_results):
                                    if item[1] == path:
                                        state.nsfw_results[i] = (danger_score, path, new_label, updated_details)
                                        break

                                for i, item in enumerate(state.nsfw_all_results):
                                    if item[1] == path:
                                        state.nsfw_all_results[i] = (danger_score, path, new_label, updated_details)
                                        break

                                updated += 1
                            except Exception as e:
                                errors += 1
                                state.add_log(f'[NSFW] Erreur modif {path}: {e}')
                        
                        if updated > 0:
                            for path in selected_paths:
                                if path in state.sel_nsfw:
                                    state.sel_nsfw[path] = False
                            nsfw_gallery_ui.refresh()
                            ui.notify(
                                f'Labels modifiés: {updated}' + (f' | erreurs: {errors}' if errors else ''),
                                type='positive' if errors == 0 else 'warning'
                            )
                        else:
                            ui.notify('Aucune image sélectionnée n’a pu être mise à jour.', type='negative')

                    with ui.expansion('Modifier sélection', icon='edit').classes('w-full bg-gray-800/50 rounded-lg border border-cyan-700 mt-3'):
                        ui.label('Changer le label NSFW des images sélectionnées').classes('text-xs font-bold text-cyan-300')
                        nsfw_bulk_label_sel = ui.select(
                            {'SAIN': '🟢 SAIN', 'SENSUEL': '🟡 SENSUEL', 'EXPLICITE': '🔴 EXPLICITE'},
                            value='SAIN',
                            label='Nouveau label'
                        ).classes('w-full text-sm').bind_value(state, 'nsfw_bulk_new_label')
                        ui.button('💾 Appliquer à la sélection', on_click=bulk_update_nsfw_labels).props('outline color=cyan').classes('w-full')

                    with ui.expansion('Paramètres avancés', icon='tune').classes('w-full bg-gray-800/50 rounded-lg border border-gray-700 mt-2'):
                        with ui.row().classes('w-full gap-2 px-2 pt-2'):
                            nsfw_batch_size = ui.number('Lot', value=cfg.get('nsfw_batch_size', 16), format='%.0f').classes('w-[45%]')
                            nsfw_video_frames = ui.number('Images vid.', value=cfg.get('nsfw_video_frames', 4), format='%.0f').classes('w-[45%]')
                        with ui.row().classes('w-full gap-2 px-2 pb-2'):
                            nsfw_max_dim = ui.number('Limite résol.', value=cfg.get('nsfw_max_dim', 512), format='%.0f').classes('w-[45%]')
                            nsfw_quant_mode = ui.select(['None', '8-bit', '4-bit'], value=cfg.get('nsfw_quant_mode', 'None'), label='Quant.').classes('w-[45%]')
                
                async def run_nsfw_action():
                    state.nsfw_model = nsfw_model_sel.value
                    state.nsfw_threshold = float(nsfw_threshold_slider.value)
                    
                    save_config({
                        'nsfw_dir': nsfw_dir.value, 'chk_img_nsfw': chk_img_nsfw.value, 'chk_vid_nsfw': chk_vid_nsfw.value,
                        'nsfw_model': state.nsfw_model, 'nsfw_threshold': state.nsfw_threshold, 
                        'top_n_nsfw': top_n_nsfw.value, 
                        'nsfw_batch_size': nsfw_batch_size.value, 'nsfw_video_frames': nsfw_video_frames.value, 
                        'nsfw_max_dim': nsfw_max_dim.value, 'nsfw_quant_mode': nsfw_quant_mode.value,
                        'chk_prefix_nsfw': chk_prefix_nsfw.value
                    })
                    if not nsfw_dir.value: return ui.notify("Indiquez un dossier !", type='warning')
                    
                    state.is_processing = True
                    search_engine.cancel_flag = False
                    state.nsfw_results.clear()
                    state.nsfw_all_results.clear()
                    state.sel_nsfw.clear()
                    state.nsfw_page = 1
                    nsfw_gallery_ui.refresh()
                    btn_nsfw.disable()
                    search_engine._unload_embedding_model()
                    aesthetic_engine.unload()
                    face_engine.unload()
                    tag_engine.unload()
                    
                    exts =[]
                    if chk_img_nsfw.value: exts.extend(SUPPORTED_IMAGES)
                    if chk_vid_nsfw.value: exts.extend(SUPPORTED_VIDEOS)

                    checkpoint_path = nsfw_engine._nsfw_checkpoint_path(nsfw_dir.value, state.nsfw_model)

                    def bg_task():
                        try:
                            state.add_log(f"Démarrage de l'analyse NSFW : '{nsfw_dir.value}'")
                            state.add_log(f"[NSFW] Reprise automatique via checkpoint: {checkpoint_path}")
                            nsfw_engine.batch_size = int(nsfw_batch_size.value)
                            nsfw_engine.video_frames = int(nsfw_video_frames.value)
                            nsfw_engine.max_dim = int(nsfw_max_dim.value)
                            nsfw_engine.quant_mode = nsfw_quant_mode.value
                            
                            state.nsfw_base_dir = nsfw_dir.value
                            n = int(top_n_nsfw.value)
                            
                            res = nsfw_engine.evaluate_media(
                                nsfw_dir.value,
                                state.nsfw_model,
                                tuple(exts),
                            )
                            state.nsfw_all_results = res

                            if n <= 0 or n >= len(res):
                                state.nsfw_results = res
                            else:
                                state.nsfw_results = res[:n]
                                state.add_log(f"[NSFW] TOP limité à {n}/{len(res)} résultats (augmente 'TOP' ou mets 0 pour tout).")
                            state.sel_nsfw = {path: False for _, path, _, _ in state.nsfw_results}
                            state.add_log("✅ Analyse NSFW terminée !")
                        except Exception as e:
                            state.add_log(f"❌ Erreur : {e}")
                            checkpoint_data = nsfw_engine._load_nsfw_checkpoint(nsfw_dir.value, state.nsfw_model)
                            if checkpoint_data and checkpoint_data.get('results'):
                                state.nsfw_all_results = list(checkpoint_data['results'])
                                if n <= 0 or n >= len(state.nsfw_all_results):
                                    state.nsfw_results = list(state.nsfw_all_results)
                                else:
                                    state.nsfw_results = state.nsfw_all_results[:n]
                                state.sel_nsfw = {path: False for _, path, _, _ in state.nsfw_results}
                                nsfw_gallery_ui.refresh()
                                state.add_log(f"[NSFW] Résultats partiels restaurés depuis checkpoint: {len(state.nsfw_all_results)} fichier(s)")
                        finally:
                            state.status_text = "Prêt !"
                            state.progress = 1.0
                            state.is_processing = False

                    await run.io_bound(bg_task)
                    nsfw_gallery_ui.refresh()
                    btn_nsfw.enable()
                    
                async def load_validated_action():
                    if not nsfw_dir.value:
                        return ui.notify("Indiquez un dossier !", type='warning')
                    state.is_processing = True
                    btn_load_validated.disable()
                    btn_nsfw.disable()
                    state.nsfw_results.clear()
                    state.nsfw_all_results.clear()
                    state.sel_nsfw.clear()
                    state.nsfw_page = 1
                    nsfw_gallery_ui.refresh()

                    def bg_load():
                        loaded = []
                        state.add_log(f"[NSFW] Chargement des images déjà validées dans : '{nsfw_dir.value}'")
                        for root, dirs, files in os.walk(nsfw_dir.value):
                            dirs.sort()
                            for fname in sorted(files):
                                if not fname.lower().endswith(SUPPORTED_IMAGES):
                                    continue
                                img_path = os.path.join(root, fname)
                                stem = os.path.splitext(img_path)[0]
                                val_path = stem + '_validation.json'
                                if not os.path.isfile(val_path):
                                    continue
                                try:
                                    with open(val_path, 'r', encoding='utf-8') as f:
                                        data = json.load(f)
                                    result = data.get('result', {})
                                    danger = float(result.get('danger', 0.0))
                                    label = str(result.get('tier', result.get('raw_top_label', 'unknown')))
                                    details = result.get('details', {})
                                    if not isinstance(details, dict):
                                        details = {}
                                    loaded.append((danger, img_path, label, details))
                                except Exception as e:
                                    state.add_log(f"[NSFW] Impossible de lire {fname}: {e}")

                        loaded.sort(key=lambda x: x[0], reverse=True)
                        state.nsfw_base_dir = nsfw_dir.value
                        state.nsfw_all_results = loaded
                        n = int(top_n_nsfw.value)
                        if n <= 0 or n >= len(loaded):
                            state.nsfw_results = loaded
                        else:
                            state.nsfw_results = loaded[:n]
                            state.add_log(f"[NSFW] TOP limité à {n}/{len(loaded)} résultats.")
                        state.sel_nsfw = {path: False for _, path, _, _ in state.nsfw_results}
                        state.add_log(f"✅ {len(loaded)} images validées chargées.")

                    await run.io_bound(bg_load)
                    nsfw_gallery_ui.refresh()
                    state.is_processing = False
                    btn_load_validated.enable()
                    btn_nsfw.enable()

                with ui.row().classes('w-full p-4 pt-2 shrink-0 border-t border-gray-800 bg-gray-900 z-10'):
                    btn_load_validated = ui.button('📂 Charger validés', on_click=load_validated_action).classes('w-[45%] bg-gray-700 hover:bg-gray-600 font-bold text-lg').tooltip('Charge les images qui ont déjà un _validation.json sans relancer l\'analyse')
                    btn_nsfw = ui.button('🚨 Analyser', on_click=run_nsfw_action).classes('w-[55%] bg-red-800 hover:bg-red-700 font-bold text-lg')
            with ui.column().classes('flex-1 w-0 bg-gray-900 rounded-xl border border-gray-800 overflow-hidden h-full relative p-0'):
                nsfw_gallery_ui()

        # ВКЛАДКА 4: ПОИСК ПО ЛИЦУ (FACE SEARCH)
        with ui.tab_panel(tab_face).classes('w-full h-[calc(100vh-115px)] p-4 flex flex-row flex-nowrap items-stretch gap-4'):
            with ui.column().classes('w-[350px] shrink-0 bg-gray-900 rounded-xl border border-gray-800 shadow-lg flex flex-col overflow-hidden p-0 gap-0'):
                with ui.row().classes('w-full p-4 pb-2 shrink-0 border-b border-gray-800 bg-gray-900 z-10'):
                    ui.label('Recherche par Visage').classes('text-lg font-bold')
                
                with ui.column().classes('w-full flex-1 overflow-y-auto p-4 gap-2 min-h-0'):
                    with ui.row().classes('w-full items-center gap-1 flex-nowrap'):
                        face_dir = ui.input('Dossier de recherche', value=cfg.get('face_dir', '')).classes('flex-grow')
                        ui.button(icon='folder', on_click=lambda: select_folder(face_dir)).props('flat round dense')
                        ui.button(icon='delete_sweep', on_click=lambda: clear_folder_cache(face_dir.value)).props('flat round dense text-color=red').tooltip('Effacer l\'index du dossier')
                        
                    with ui.row().classes('w-full items-center gap-1 flex-nowrap mt-2'):
                        ref_img = ui.input('Photo de référence (visage)', value=cfg.get('ref_img', '')).classes('flex-grow')
                        ui.button(icon='image', on_click=lambda: select_file(ref_img)).props('flat round dense')
                        
                    with ui.row().classes('w-full gap-2 mt-2'):
                        chk_img_face = ui.checkbox('Images', value=cfg.get('chk_img_face', True))
                        chk_vid_face = ui.checkbox('Vidéos (1er fotogramme)', value=cfg.get('chk_vid_face', False))
                    
                    face_threshold = ui.number('Similarité min. (0.0 - 1.0)', value=cfg.get('face_threshold', 0.40), format='%.2f', step=0.05).classes('w-full mt-2')
                    chk_prefix_face = ui.checkbox('Préfixer la similarité au nom (copie)', value=cfg.get('chk_prefix_face', False)).classes('text-sm text-gray-300 w-full mt-2')

                    with ui.expansion('Paramètres avancés', icon='tune').classes('w-full bg-gray-800/50 rounded-lg border border-gray-700 mt-2'):
                        with ui.row().classes('w-full gap-2 px-2 pt-2 pb-2'):
                            face_batch_size = ui.number('Lot', value=cfg.get('face_batch_size', 16), format='%.0f').classes('w-[45%]')

                async def run_face_action():
                    save_config({
                        'face_dir': face_dir.value, 'ref_img': ref_img.value,
                        'chk_img_face': chk_img_face.value, 'chk_vid_face': chk_vid_face.value,
                        'face_threshold': face_threshold.value, 'chk_prefix_face': chk_prefix_face.value,
                        'face_batch_size': face_batch_size.value
                    })
                    if not face_dir.value or not ref_img.value: return ui.notify("Indiquez un dossier et une photo de référence !", type='warning')
                    
                    state.is_processing = True
                    search_engine.cancel_flag = False
                    state.face_results.clear()
                    state.sel_face.clear()
                    state.face_page = 1
                    face_gallery_ui.refresh()
                    btn_face.disable()
                    
                    search_engine._unload_embedding_model()
                    aesthetic_engine.unload()
                    nsfw_engine.unload()
                    tag_engine.unload()
                    
                    exts =[]
                    if chk_img_face.value: exts.extend(SUPPORTED_IMAGES)
                    if chk_vid_face.value: exts.extend(SUPPORTED_VIDEOS)

                    def bg_task():
                        try:
                            state.add_log(f"Démarrage de la recherche par visage : '{face_dir.value}'")
                            face_engine.batch_size = int(face_batch_size.value)
                            state.face_base_dir = face_dir.value
                            
                            res = face_engine.search_faces(ref_img.value, face_dir.value, tuple(exts), float(face_threshold.value))
                                
                            state.face_results = res
                            state.sel_face = {path: False for _, path in state.face_results}
                            state.add_log("✅ Recherche par visage terminée !")
                        except Exception as e: state.add_log(f"❌ Erreur de recherche par visage : {e}")
                        finally:
                            state.status_text = "Prêt !"
                            state.progress = 1.0
                            state.is_processing = False

                    await run.io_bound(bg_task)
                    try:
                        face_gallery_ui.refresh()
                        state.add_log(f"[FACE] Galerie rafraîchie : {len(state.face_results)} résultat(s).")
                    except Exception as _e:
                        state.add_log(f"❌ Erreur affichage Visage : {_e}")
                    finally:
                        btn_face.enable()
                        state.is_processing = False
                        state.status_text = "Prêt !"
                        state.progress = 1.0
                    
                with ui.row().classes('w-full p-4 pt-2 shrink-0 border-t border-gray-800 bg-gray-900 z-10'):
                    btn_face = ui.button('🕵️ Rechercher Visage', on_click=run_face_action).classes('w-full bg-teal-600 hover:bg-teal-500 font-bold text-lg')

            with ui.column().classes('flex-1 w-0 bg-gray-900 rounded-xl border border-gray-800 overflow-hidden h-full relative p-0'):
                face_gallery_ui()

        # ВКЛАДКА 5: ТЕГИ (DANBOORU)
        with ui.tab_panel(tab_tags).classes('w-full h-[calc(100vh-115px)] p-4 flex flex-row flex-nowrap items-stretch gap-4'):
            with ui.column().classes('w-[350px] shrink-0 bg-gray-900 rounded-xl border border-gray-800 shadow-lg flex flex-col overflow-hidden p-0 gap-0'):
                with ui.row().classes('w-full p-4 pb-2 shrink-0 border-b border-gray-800 bg-gray-900 z-10'):
                    ui.label('Recherche par Tags').classes('text-lg font-bold')
                
                with ui.column().classes('w-full flex-1 overflow-y-auto p-4 gap-2 min-h-0'):
                    with ui.row().classes('w-full items-center gap-1 flex-nowrap'):
                        tags_dir = ui.input('Dossier', value=cfg.get('tags_dir', '')).classes('flex-grow')
                        ui.button(icon='folder', on_click=lambda: select_folder(tags_dir)).props('flat round dense')
                        ui.button(icon='delete_sweep', on_click=lambda: clear_folder_cache(tags_dir.value)).props('flat round dense text-color=red').tooltip('Effacer l\'index du dossier')
                    
                    with ui.row().classes('w-full gap-2'):
                        chk_img_tags = ui.checkbox('Images', value=cfg.get('chk_img_tags', True))
                        chk_vid_tags = ui.checkbox('Vidéos', value=cfg.get('chk_vid_tags', False))
                    
                    tags_model_sel = ui.select([
                        'SmilingWolf/wd-swinv2-tagger-v3',
                        'SmilingWolf/wd-convnext-tagger-v3',
                        'SmilingWolf/wd-eva02-large-tagger-v3',
                        'SmilingWolf/wd-vit-tagger-v3',
                        'Camais03/camie-tagger-v2',
                        'fancyfeast/joytag'
                    ], value=cfg.get('tags_model', 'SmilingWolf/wd-swinv2-tagger-v3'), label='Modèle').classes('w-full text-xs')
                    
                    # Кнопка подгрузки тегов из БД
                    async def load_available_tags():
                        if not tags_dir.value: return ui.notify("Sélectionnez d'abord un dossier", type='warning')
                        
                        dir_val = tags_dir.value
                        exts =[]
                        if chk_img_tags.value: exts.extend(SUPPORTED_IMAGES)
                        if chk_vid_tags.value: exts.extend(SUPPORTED_VIDEOS)
                        
                        # Берем значение кадров из конфига, так как UI элемент tags_video_frames создается ниже
                        frames_val = int(cfg.get('tags_video_frames', 4))
                        cache_key = f"{tags_model_sel.value}_{frames_val}"
                        
                        ui.notify("🔄 Collecte des tags depuis la BD, veuillez patienter...", type='info', timeout=2000)
                        
                        def process_tags(directory, extensions, key):
                            all_files = search_engine._gather_files(directory, tuple(extensions))
                            unique_tags = {}
                            
                            # Ускорение: запрашиваем все теги для модели ОДНИМ запросом (Bulk fetch)
                            c = search_engine.db_cache.conn.cursor()
                            c.execute("SELECT path, tags FROM tags_cache WHERE model=?", (key,))
                            db_data = c.fetchall()
                            
                            valid_paths = set(all_files)
                            for row in db_data:
                                path, tags_json = row[0], row[1]
                                if path in valid_paths and tags_json:
                                    tags = json.loads(tags_json)
                                    for t in tags.keys(): 
                                        unique_tags[t] = unique_tags.get(t, 0) + 1
                            return unique_tags

                        # Выполняем в фоне, чтобы не заблокировать веб-сервер и не потерять соединение
                        unique_tags = await run.io_bound(process_tags, dir_val, exts, cache_key)
                                
                        if not unique_tags:
                            return ui.notify("Aucun tag en BD pour ce dossier. Cliquez 'Indexer'.", type='warning')
                            
                        sorted_tags = sorted(unique_tags.keys(), key=lambda x: unique_tags[x], reverse=True)
                        
                        # Ограничиваем до 8000 тегов (этого хватит для 99% базы), а редкие можно вводить вручную
                        top_tags = sorted_tags[:8000]
                        pos_tags_sel.options = top_tags
                        neg_tags_sel.options = top_tags
                        pos_tags_sel.update()
                        neg_tags_sel.update()
                        ui.notify(f"{len(sorted_tags)} tags uniques chargés (TOP-8000 affiché).", type='positive')

                    ui.button('🔄 Charger les tags disponibles (BD)', on_click=load_available_tags).props('outline color=pink size=sm').classes('w-full')

                    # --- БЛОК ПОЗИТИВНЫХ ТЕГОВ ---
                    with ui.row().classes('w-full items-center justify-between mt-2 mb-[-12px]'):
                        ui.label('Inclure (Positive - ET)').classes('text-sm font-bold text-blue-400')
                        ui.button('Tout effacer', on_click=lambda: pos_tags_sel.set_value([])).props('flat dense size=sm color=red')
                    
                    # Свойство hide-selected скроет теги внутри поля
                    pos_tags_sel = ui.select([], multiple=True, with_input=True, value=cfg.get('pos_tags',[])).classes('w-full').props('hide-selected new-value-mode=add-unique')
                    pos_tags_container = ui.row().classes('w-full gap-1 mt-1')
                    
                    def remove_pos_tag(tag):
                        pos_tags_sel.set_value([t for t in pos_tags_sel.value if t != tag])

                    def update_pos_tags(e=None):
                        pos_tags_container.clear()
                        with pos_tags_container:
                            for tag in pos_tags_sel.value:
                                ui.button(f"{tag} ✖", on_click=lambda _, t=tag: remove_pos_tag(t)) \
                                    .props('dense size=sm outline color=blue-300 no-caps') \
                                    .classes('rounded-full px-2 py-0 min-h-0 text-xs bg-blue-900/30')
                    
                    pos_tags_sel.on_value_change(update_pos_tags)
                    update_pos_tags()

                    # --- БЛОК НЕГАТИВНЫХ ТЕГОВ ---
                    with ui.row().classes('w-full items-center justify-between mt-4 mb-[-12px]'):
                        ui.label('Exclure (Negative - PAS)').classes('text-sm font-bold text-pink-400')
                        ui.button('Tout effacer', on_click=lambda: neg_tags_sel.set_value([])).props('flat dense size=sm color=red')
                        
                    neg_tags_sel = ui.select([], multiple=True, with_input=True, value=cfg.get('neg_tags',[])).classes('w-full').props('hide-selected new-value-mode=add-unique')
                    neg_tags_container = ui.row().classes('w-full gap-1 mt-1')

                    def remove_neg_tag(tag):
                        neg_tags_sel.set_value([t for t in neg_tags_sel.value if t != tag])

                    def update_neg_tags(e=None):
                        neg_tags_container.clear()
                        with neg_tags_container:
                            for tag in neg_tags_sel.value:
                                ui.button(f"{tag} ✖", on_click=lambda _, t=tag: remove_neg_tag(t)) \
                                    .props('dense size=sm outline color=pink-300 no-caps') \
                                    .classes('rounded-full px-2 py-0 min-h-0 text-xs bg-pink-900/30')
                                
                    neg_tags_sel.on_value_change(update_neg_tags)
                    update_neg_tags()

                    prompt_req_state = {
                        'seq': 0,
                        'active_id': None,
                        'cancelled': set(),
                    }

                    if True:

                        def _safe_ui_call(fn, context: str) -> bool:
                            try:
                                fn()
                                return True
                            except RuntimeError as e:
                                if 'has been deleted' in str(e):
                                    _ollama_trace('ui.slot_deleted', context=context, error=repr(e))
                                    return False
                                raise

                        def _safe_notify(message: str, type_: str = 'info'):
                            _safe_ui_call(lambda: ui.notify(message, type=type_), f'notify:{type_}')

                        def _start_prompt_request(kind: str) -> str:
                            prompt_req_state['seq'] += 1
                            req_id = f"{kind}-{prompt_req_state['seq']}"
                            prompt_req_state['active_id'] = req_id
                            return req_id

                        def _finish_prompt_request(req_id: str):
                            prompt_req_state['cancelled'].discard(req_id)
                            if prompt_req_state.get('active_id') == req_id:
                                prompt_req_state['active_id'] = None

                        def _is_prompt_request_stale(req_id: str) -> bool:
                            return req_id in prompt_req_state['cancelled'] or prompt_req_state.get('active_id') != req_id

                        def _cancel_prompt_request_action():
                            req_id = prompt_req_state.get('active_id')
                            if not req_id:
                                return _safe_notify('Aucune requête prompt en cours.', 'warning')
                            prompt_req_state['cancelled'].add(req_id)
                            _set_prompt_status('annulation demandée...', busy=False)
                            _set_prompt_batch_progress('')
                            state.add_log('[PROMPT] Annulation demandée.')
                            _ollama_trace('ui.prompt.cancel_requested', req_id=req_id)
                            _safe_notify('Annulation demandée. Le résultat sera ignoré à son retour.', 'warning')

                        def _set_prompt_status(message: str, busy: bool = False):
                            prompt_status_label.text = f'Statut: {message}'
                            _safe_ui_call(prompt_status_label.update, 'prompt_status_label.update')
                            prompt_busy_bar.visible = bool(busy)
                            _safe_ui_call(prompt_busy_bar.update, 'prompt_busy_bar.update')
                            state.status_text = f'Prompt: {message}'
                            if busy:
                                state.progress = min(max(float(state.progress or 0.0), 0.02), 0.95)
                            elif 'termin' in message.lower() or 'sauvegard' in message.lower() or 'prêt' in message.lower():
                                state.progress = 1.0

                        def _set_prompt_batch_progress(message: str = ''):
                            prompt_batch_label.text = str(message or '')
                            _safe_ui_call(prompt_batch_label.update, 'prompt_batch_label.update')
                            if message:
                                state.status_text = f'Prompt: {message}'

                        async def refresh_ollama_models_action():
                            _ollama_trace('ui.refresh_models.clicked', provider=str(prompt_provider_sel.value or ''), selected_model=str(ollama_tags_model_sel.value or ''))
                            try:
                                models = await run.io_bound(_ollama_list_models)
                                ollama_tags_model_sel.options = models
                                if ollama_tags_model_sel.value not in models:
                                    ollama_tags_model_sel.value = models[0] if models else None
                                elif models and not ollama_tags_model_sel.value:
                                    ollama_tags_model_sel.value = models[0]
                                ollama_tags_model_sel.update()
                                save_config({'ollama_models_cached': models, 'tags_ollama_model': ollama_tags_model_sel.value or ''})
                                ui.notify(f'{len(models)} modèle(s) Ollama détecté(s).', type='positive')
                            except Exception as e:
                                _ollama_trace('ui.refresh_models.error', error=repr(e))
                                ui.notify(f'Ollama indisponible: {e}', type='warning')

                        def _merge_prompt_tags(new_tags: dict, replace: bool = False):
                            if not isinstance(new_tags, dict) or not new_tags:
                                return 0
                            current = [] if replace else list(pos_tags_sel.value or [])
                            for tag in new_tags.keys():
                                if tag not in current:
                                    current.append(tag)
                            pos_tags_sel.set_value(current)
                            return len(new_tags)

                        def _get_selected_prompt_paths() -> list[str]:
                            prompt_paths = [p for p, checked in (state.sel_prompt or {}).items() if checked]
                            if prompt_paths:
                                return prompt_paths
                            return [p for p, checked in (state.sel_tags or {}).items() if checked]

                        def _build_selected_media_context(max_items: int = 12) -> dict:
                            selected_paths = _get_selected_prompt_paths()
                            if not selected_paths:
                                return {
                                    'count': 0,
                                    'paths': [],
                                    'base_prompt': '',
                                    'merged_tags': {},
                                    'ai_payload': {},
                                    'source_path': '',
                                }

                            merged_tags = {}

                            base_prompt = ''
                            representative_payload = {}
                            representative_path = selected_paths[0]
                            for p in selected_paths[:max_items]:
                                item_ctx = _build_single_media_context(p)
                                payload = item_ctx.get('ai_payload') if isinstance(item_ctx.get('ai_payload'), dict) else {}
                                cached_tags = item_ctx.get('tags') or {}
                                if isinstance(cached_tags, dict):
                                    for tag, prob in cached_tags.items():
                                        try:
                                            cp = float(prob)
                                        except Exception:
                                            cp = 1.0
                                        merged_tags[tag] = max(float(merged_tags.get(tag, 0.0)), cp)
                                prompt_text = str(item_ctx.get('base_prompt') or '').strip()
                                if not base_prompt and prompt_text:
                                    base_prompt = prompt_text
                                    representative_payload = payload
                                    representative_path = p

                            return {
                                'count': len(selected_paths),
                                'paths': selected_paths,
                                'base_prompt': base_prompt,
                                'merged_tags': merged_tags,
                                'ai_payload': representative_payload,
                                'source_path': representative_path,
                            }

                        def _build_single_media_context(path: str) -> dict:
                            payload = _collect_llm_payload_from_cache(path)
                            prompt_payload = payload.get('prompt') or {}
                            prompt_source = str((prompt_payload.get('source') or '')).strip().lower()
                            base_prompt = str((prompt_payload.get('text') or '')).strip()

                            # Always prefer the embedded image prompt over AI-generated raw prompts.
                            if path.lower().endswith(SUPPORTED_IMAGES) and (not base_prompt or not prompt_source.startswith('image_metadata')):
                                try:
                                    embedded = TagEngine._extract_prompt_from_image_metadata(path)
                                    if embedded:
                                        if base_prompt and not prompt_source.startswith('image_metadata'):
                                            search_engine.db_cache.save_detailed_prompt(path, base_prompt, source=f"raw:{prompt_source or 'cache'}")
                                        base_prompt = embedded
                                        search_engine.db_cache.save_prompt(path, embedded, source="image_metadata_positive_prompt")
                                        prompt_payload = {'source': 'image_metadata_positive_prompt', 'text': embedded}
                                        payload = dict(payload or {})
                                        payload['prompt'] = prompt_payload
                                except Exception:
                                    pass

                            tags_values = ((payload.get('tags') or {}).get('values') or {})

                            if (not isinstance(tags_values, dict) or not tags_values) and state.tags_results:
                                item = next((item for item in state.tags_results if item[1] == path), None)
                                if item and isinstance(item[2], dict):
                                    tags_values = item[2]

                            return {
                                'path': path,
                                'base_prompt': base_prompt,
                                'tags': tags_values if isinstance(tags_values, dict) else {},
                                'ai_payload': payload if isinstance(payload, dict) else {},
                            }

                        def _generate_prompt_sync(provider, model, mode, prompt_text, tags_dict, ai_payload, source_path):
                            if mode == 'detailed':
                                if provider == 'ollama':
                                    return _ollama_detailed_prompt(model, prompt_text, tags_dict, ai_payload, source_path), f'Ollama: {model}'
                                return TagEngine._build_detailed_prompt_fallback(prompt_text, tags_dict, ai_payload, source_path), 'Local'

                            if provider == 'ollama':
                                # Toujours appeler ollama — tags passés en contexte si présents, sinon "none"
                                return _ollama_raw_prompt(model, prompt_text, tags_dict, image_path=source_path), f'Ollama: {model}'

                            # Moteur local : prompt_text en priorité, tags en contexte secondaire
                            generated = prompt_text or (', '.join(list(tags_dict.keys())[:25]) if tags_dict else '')
                            if not generated and source_path:
                                generated = f"Prompt based on {os.path.basename(source_path)}"
                            return generated or '', 'Local'

                        async def prompt_to_tags_action(run_search_after: bool = False):
                            req_id = _start_prompt_request('p2t')
                            prompt_text = str(prompt_query_input.value or '').strip()
                            selected_ctx = _build_selected_media_context()
                            if not prompt_text and selected_ctx['base_prompt']:
                                prompt_text = selected_ctx['base_prompt']
                            if not prompt_text:
                                _finish_prompt_request(req_id)
                                return _safe_notify('Entrez un prompt ou sélectionnez une image avec prompt en cache.', 'warning')
                            provider = prompt_provider_sel.value or 'local'
                            model = str(ollama_tags_model_sel.value or '').strip()
                            mode = str(prompt_mode_sel.value or 'raw').strip().lower()
                            save_config({
                                'tags_prompt_query': prompt_text,
                                'tags_prompt_provider': provider,
                                'tags_ollama_model': model,
                                'tags_prompt_mode': mode,
                            })
                            _set_prompt_status('traitement prompt -> tags en cours...', busy=True)
                            state.add_log(f"[PROMPT] Conversion prompt -> tags démarrée ({int(selected_ctx['count'])} sélection(s)).")
                            start_ts = time.monotonic()
                            _ollama_trace(
                                'ui.prompt_to_tags.start',
                                req_id=req_id,
                                provider=provider,
                                model=model,
                                prompt_len=len(prompt_text),
                                selected_count=int(selected_ctx['count']),
                                run_search_after=bool(run_search_after),
                            )
                            try:
                                if provider == 'ollama':
                                    if not model:
                                        _set_prompt_status('échec: modèle Ollama manquant', busy=False)
                                        _finish_prompt_request(req_id)
                                        return _safe_notify('Choisissez un modèle Ollama.', 'warning')
                                    tags = await run.io_bound(_ollama_prompt_to_tags, model, prompt_text)
                                    engine = f'Ollama: {model}'
                                else:
                                    tags = await run.io_bound(TagEngine._prompt_text_to_tags, prompt_text)
                                    engine = 'Local'

                                if _is_prompt_request_stale(req_id):
                                    _ollama_trace('ui.prompt_to_tags.discarded', req_id=req_id, reason='cancelled_or_stale')
                                    _set_prompt_status('requête annulée (résultat ignoré)', busy=False)
                                    _set_prompt_batch_progress('')
                                    return

                                count = _merge_prompt_tags(tags, replace=False)
                                update_pos_tags()
                                elapsed = time.monotonic() - start_ts
                                _set_prompt_status(f'terminée en {elapsed:.1f}s ({engine})', busy=False)
                                _set_prompt_batch_progress('')
                                state.add_log(f"[PROMPT] Conversion prompt -> tags terminée en {elapsed:.1f}s via {engine}: {count} tag(s).")
                                _ollama_trace('ui.prompt_to_tags.done', req_id=req_id, elapsed_s=f"{elapsed:.2f}", count=count, engine=engine)
                                _safe_notify(f'{count} tag(s) injecté(s) depuis le prompt. Source: {engine}', 'positive')
                                if run_search_after:
                                    await search_tags_action()
                            except Exception as e:
                                _set_prompt_status('erreur prompt -> tags', busy=False)
                                _set_prompt_batch_progress('')
                                state.add_log(f"[PROMPT] Erreur prompt -> tags: {e}")
                                _ollama_trace('ui.prompt_to_tags.error', req_id=req_id, error=repr(e))
                                _safe_notify(f'Erreur prompt -> tags: {e}', 'negative')
                            finally:
                                _finish_prompt_request(req_id)

                        # ===== Wizard modal multi-photos =====
                        # État partagé via closure (le widget UI est créé paresseusement la 1re fois)
                        _wizard = {
                            'queue': [],            # list[str] paths restants (tête = courant)
                            'total': 0,
                            'done': 0,
                            'mode': 'both',         # snapshot du mode au démarrage du wizard
                            'busy': False,
                            'auto': False,          # True = auto-validate après chaque génération
                            'dialog': None,         # ui.dialog instance
                            'thumb': None,          # ui.image
                            'title_label': None,    # ui.label
                            'progress_label': None, # ui.label
                            'raw_area': None,       # ui.textarea (mode raw / both)
                            'detailed_area': None,  # ui.textarea (mode detailed / both)
                            'single_area': None,    # ui.textarea (mode raw OR detailed seul)
                            'raw_row': None,        # row container
                            'detailed_row': None,
                            'single_row': None,
                            'status_label': None,
                            'model_sel': None,      # ui.select override modèle
                            'busy_bar': None,
                        }

                        def _wizard_build_dialog():
                            if _wizard['dialog'] is not None:
                                return
                            with ui.dialog().props('persistent') as dlg, ui.card().classes('bg-gray-900 text-white w-[860px] max-w-[95vw] max-h-[92vh] overflow-auto'):
                                _wizard['title_label'] = ui.label('Wizard prompt').classes('text-lg font-bold')
                                _wizard['progress_label'] = ui.label('').classes('text-xs text-cyan-300 mb-1')
                                with ui.row().classes('w-full gap-3 items-start'):
                                    _wizard['thumb'] = ui.image('').classes('w-[280px] max-h-[280px] rounded border border-gray-700')
                                    with ui.column().classes('flex-1 gap-2'):
                                        # Override modèle (mêmes options que le select principal)
                                        try:
                                            _opts = list(getattr(ollama_tags_model_sel, 'options', None) or [])
                                        except Exception:
                                            _opts = []
                                        _wizard['model_sel'] = ui.select(
                                            options=_opts or [''],
                                            value=(ollama_tags_model_sel.value if _opts else None),
                                            label='Modèle Ollama (override)'
                                        ).classes('w-full')
                                        _wizard['status_label'] = ui.label('Statut: prêt').classes('text-xs text-gray-400')
                                        _wizard['busy_bar'] = ui.linear_progress(value=None).props('indeterminate color=cyan').classes('w-full')
                                        _wizard['busy_bar'].visible = False
                                # Zones de texte (visibilité ajustée selon mode)
                                _wizard['raw_row'] = ui.row().classes('w-full gap-2')
                                with _wizard['raw_row']:
                                    _wizard['raw_area'] = ui.textarea('Prompt brut', value='').props('autogrow filled').classes('w-full text-xs')
                                _wizard['detailed_row'] = ui.row().classes('w-full gap-2')
                                with _wizard['detailed_row']:
                                    _wizard['detailed_area'] = ui.textarea('Prompt détaillé', value='').props('autogrow filled').classes('w-full text-xs')
                                _wizard['single_row'] = ui.row().classes('w-full gap-2')
                                with _wizard['single_row']:
                                    _wizard['single_area'] = ui.textarea('Résultat', value='').props('autogrow filled').classes('w-full text-sm')
                                # Boutons
                                with ui.row().classes('w-full gap-2 mt-3 justify-end'):
                                    ui.button('✖ Annuler', on_click=_wizard_cancel).props('flat color=red')
                                    ui.button('⏭️ Passer', on_click=_wizard_skip).props('outline color=orange')
                                    ui.button('🔄 Régénérer', on_click=_wizard_regenerate).props('outline color=cyan')
                                    ui.button('✅ Valider et suivant', on_click=_wizard_validate).props('color=green')
                            _wizard['dialog'] = dlg

                        def _wizard_apply_mode_visibility():
                            mode = str(_wizard['mode'] or 'both').strip().lower()
                            try:
                                if mode == 'both':
                                    _wizard['raw_row'].visible = True
                                    _wizard['detailed_row'].visible = True
                                    _wizard['single_row'].visible = False
                                elif mode == 'detailed':
                                    _wizard['raw_row'].visible = False
                                    _wizard['detailed_row'].visible = False
                                    _wizard['single_row'].visible = True
                                    _wizard['single_area'].props(remove='readonly')
                                    _wizard['single_area'].label = 'Prompt détaillé'
                                else:
                                    _wizard['raw_row'].visible = False
                                    _wizard['detailed_row'].visible = False
                                    _wizard['single_row'].visible = True
                                    _wizard['single_area'].label = 'Prompt brut'
                                for r in (_wizard['raw_row'], _wizard['detailed_row'], _wizard['single_row']):
                                    _safe_ui_call(r.update, 'wizard_row.update')
                            except Exception as e:
                                state.add_log(f"[WIZARD] visibility err: {e}")

                        def _wizard_set_busy(busy: bool, status: str = ''):
                            _wizard['busy'] = busy
                            try:
                                if _wizard['busy_bar'] is not None:
                                    _wizard['busy_bar'].visible = busy
                                    _safe_ui_call(_wizard['busy_bar'].update, 'wizard.busy_bar')
                                if status and _wizard['status_label'] is not None:
                                    _wizard['status_label'].set_text(f'Statut: {status}')
                            except Exception:
                                pass

                        def _wizard_update_header():
                            if not _wizard['queue']:
                                return
                            cur = _wizard['queue'][0]
                            idx = _wizard['done'] + 1
                            tot = _wizard['total']
                            try:
                                _wizard['title_label'].set_text(f"Photo {idx}/{tot} — {os.path.basename(cur)}")
                                _wizard['progress_label'].set_text(cur)
                                if cur.lower().endswith(SUPPORTED_IMAGES):
                                    try:
                                        _safe_url = urllib.parse.quote(cur)
                                    except Exception:
                                        _safe_url = cur
                                    _wizard['thumb'].set_source(f'/media/{_safe_url}')
                                    _wizard['thumb'].visible = True
                                else:
                                    _wizard['thumb'].visible = False
                                _safe_ui_call(_wizard['thumb'].update, 'wizard.thumb')
                            except Exception as e:
                                state.add_log(f"[WIZARD] header err: {e}")

                        async def _wizard_generate_current():
                            if not _wizard['queue']:
                                return
                            cur = _wizard['queue'][0]
                            _wizard_set_busy(True, f'génération pour {os.path.basename(cur)}...')
                            try:
                                single = _build_single_media_context(cur)
                                base_prompt = str(single.get('base_prompt') or '')
                                tags_dict = dict(single.get('tags') or {})
                                ai_payload = dict(single.get('ai_payload') or {})
                                shared_hint = str(prompt_query_input.value or '').strip()
                                force_overwrite = bool(force_overwrite_metadata_switch.value)
                                gen_prompt = shared_hint if force_overwrite else (shared_hint or base_prompt)
                                provider = str(prompt_provider_sel.value or 'local').strip().lower()
                                # Préfère le modèle override du wizard si présent
                                model = ''
                                if _wizard['model_sel'] is not None:
                                    model = str(_wizard['model_sel'].value or '').strip()
                                if not model:
                                    model = str(ollama_tags_model_sel.value or '').strip()
                                mode = _wizard['mode']

                                # Mode 'both': générer brut + détaillé séparément
                                if mode == 'both':
                                    # Auto-skip si déjà existant en BD (sauf force_overwrite)
                                    existing_raw = ''
                                    existing_det = ''
                                    if not force_overwrite:
                                        try:
                                            existing_raw = str((search_engine.db_cache.get_prompt(cur) or {}).get('text') or '').strip()
                                            existing_det = str((search_engine.db_cache.get_detailed_prompt(cur) or {}).get('text') or '').strip()
                                        except Exception:
                                            pass
                                    if existing_raw and not existing_det:
                                        raw_text = existing_raw
                                    else:
                                        raw_out, _eng = await run.io_bound(_generate_prompt_sync, provider, model, 'raw', gen_prompt, tags_dict, ai_payload, cur)
                                        raw_text = str(raw_out or '').strip()
                                    if existing_det and not existing_raw:
                                        det_text = existing_det
                                    else:
                                        det_out, _eng = await run.io_bound(_generate_prompt_sync, provider, model, 'detailed', gen_prompt, tags_dict, ai_payload, cur)
                                        det_text = str(det_out or '').strip()
                                    _wizard['raw_area'].value = raw_text
                                    _wizard['detailed_area'].value = det_text
                                    _safe_ui_call(_wizard['raw_area'].update, 'wizard.raw_area')
                                    _safe_ui_call(_wizard['detailed_area'].update, 'wizard.detailed_area')
                                else:
                                    out, _eng = await run.io_bound(_generate_prompt_sync, provider, model, mode, gen_prompt, tags_dict, ai_payload, cur)
                                    _wizard['single_area'].value = str(out or '').strip()
                                    _safe_ui_call(_wizard['single_area'].update, 'wizard.single_area')
                                _wizard_set_busy(False, 'prêt (vérifiez / éditez puis Validez)')
                            except Exception as e:
                                state.add_log(f"[WIZARD] gen err: {e}")
                                _wizard_set_busy(False, f'erreur: {e}')
                                _safe_notify(f'Erreur génération: {e}', 'negative')
                                return
                            # Mode auto : valider et passer automatiquement
                            if _wizard.get('auto'):
                                try:
                                    await _wizard_validate()
                                except Exception as ex:
                                    state.add_log(f"[WIZARD] auto-validate err: {ex}")

                        async def _open_prompt_wizard(paths):
                            if not paths:
                                return
                            _wizard_build_dialog()
                            _wizard['queue'] = list(paths)
                            _wizard['total'] = len(paths)
                            _wizard['done'] = 0
                            _wizard['mode'] = str(prompt_mode_sel.value or 'both').strip().lower()
                            _wizard['auto'] = str(prompt_wizard_mode_sel.value or 'manual').strip().lower() == 'auto'
                            # Sync override-modèle avec le modèle principal
                            try:
                                _opts = list(getattr(ollama_tags_model_sel, 'options', None) or [])
                                if _wizard['model_sel'] is not None:
                                    _wizard['model_sel'].options = _opts or ['']
                                    _wizard['model_sel'].value = ollama_tags_model_sel.value if _opts else None
                                    _safe_ui_call(_wizard['model_sel'].update, 'wizard.model_sel')
                            except Exception:
                                pass
                            _wizard_apply_mode_visibility()
                            _wizard_update_header()
                            state.add_log(f"[WIZARD] Démarré ({_wizard['total']} photos, mode={_wizard['mode']}).")
                            _wizard['dialog'].open()
                            await _wizard_generate_current()

                        async def _wizard_regenerate():
                            if _wizard['busy']:
                                return
                            await _wizard_generate_current()

                        def _wizard_persist_current() -> bool:
                            if not _wizard['queue']:
                                return False
                            cur = _wizard['queue'][0]
                            provider = str(prompt_provider_sel.value or 'local').strip().lower()
                            model = ''
                            if _wizard['model_sel'] is not None:
                                model = str(_wizard['model_sel'].value or '').strip()
                            if not model:
                                model = str(ollama_tags_model_sel.value or '').strip()
                            source = f"ui_{provider}" + (f":{model}" if provider == 'ollama' and model else '')
                            mode = _wizard['mode']
                            try:
                                if mode == 'both':
                                    raw_txt = str(_wizard['raw_area'].value or '').strip()
                                    det_txt = str(_wizard['detailed_area'].value or '').strip()
                                    if raw_txt:
                                        _persist_prompt_to_paths([cur], raw_txt, 'raw', source, allow_metadata_redirect=False)
                                    if det_txt:
                                        _persist_prompt_to_paths([cur], det_txt, 'detailed', source)
                                    return bool(raw_txt or det_txt)
                                else:
                                    txt = str(_wizard['single_area'].value or '').strip()
                                    if txt:
                                        _persist_prompt_to_paths([cur], txt, mode, source)
                                        return True
                                    return False
                            except Exception as e:
                                state.add_log(f"[WIZARD] persist err: {e}")
                                _safe_notify(f'Erreur sauvegarde: {e}', 'negative')
                                return False

                        def _wizard_refresh_gallery_for(path: str):
                            # Met à jour le tuple correspondant dans state.prompt_results
                            if not state.prompt_results:
                                return
                            try:
                                for i, (score, p, ptext, dtext, psrc, dsrc) in enumerate(state.prompt_results):
                                    if p == path:
                                        new_p = search_engine.db_cache.get_prompt(p) or {}
                                        new_d = search_engine.db_cache.get_detailed_prompt(p) or {}
                                        ptext_new = str(new_p.get('text') or '').strip()
                                        dtext_new = str(new_d.get('text') or '').strip()
                                        psrc_new = str(new_p.get('source') or '').strip().lower()
                                        dsrc_new = str(new_d.get('source') or '').strip().lower()
                                        new_score = 0.0
                                        if ptext_new:
                                            new_score += min(len(ptext_new) / 240.0, 1.0)
                                        if dtext_new:
                                            new_score += 1.0
                                        state.prompt_results[i] = (new_score, p, ptext_new, dtext_new, psrc_new, dsrc_new)
                                        break
                                state.prompt_results.sort(key=lambda x: x[0], reverse=True)
                                prompt_gallery_ui.refresh()
                            except Exception:
                                pass

                        async def _wizard_validate():
                            if _wizard['busy'] or not _wizard['queue']:
                                return
                            cur = _wizard['queue'][0]
                            ok = _wizard_persist_current()
                            if ok:
                                _wizard_refresh_gallery_for(cur)
                                state.add_log(f"[WIZARD] Sauvegardé: {os.path.basename(cur)}")
                            else:
                                _safe_notify('Aucun texte à sauvegarder pour cette photo.', 'warning')
                                return
                            await _wizard_advance()

                        async def _wizard_skip():
                            if _wizard['busy'] or not _wizard['queue']:
                                return
                            state.add_log(f"[WIZARD] Passé: {os.path.basename(_wizard['queue'][0])}")
                            await _wizard_advance()

                        async def _wizard_advance():
                            _wizard['queue'].pop(0)
                            _wizard['done'] += 1
                            if not _wizard['queue']:
                                total = _wizard['total']
                                _wizard['total'] = 0
                                _wizard['done'] = 0
                                try:
                                    _wizard['dialog'].close()
                                except Exception:
                                    pass
                                state.add_log(f"[WIZARD] Terminé: {total} photo(s) traitées.")
                                _safe_notify(f'Wizard terminé: {total} photo(s) traitées.', 'positive')
                                return
                            # Vide les zones pour la photo suivante puis génère
                            try:
                                if _wizard['raw_area'] is not None: _wizard['raw_area'].value = ''
                                if _wizard['detailed_area'] is not None: _wizard['detailed_area'].value = ''
                                if _wizard['single_area'] is not None: _wizard['single_area'].value = ''
                            except Exception:
                                pass
                            _wizard_update_header()
                            await _wizard_generate_current()

                        def _wizard_cancel():
                            _wizard['queue'] = []
                            _wizard['total'] = 0
                            _wizard['done'] = 0
                            try:
                                _wizard['dialog'].close()
                            except Exception:
                                pass
                            state.add_log("[WIZARD] Annulé par l'utilisateur.")
                            _safe_notify('Wizard annulé.', 'info')

                        async def _run_silent_prompt_batch(paths):
                            """Génère et persiste les prompts pour `paths` sans aucun popup."""
                            if not paths:
                                return
                            mode = str(prompt_mode_sel.value or 'both').strip().lower()
                            provider = str(prompt_provider_sel.value or 'local').strip().lower()
                            model = str(ollama_tags_model_sel.value or '').strip()
                            force_overwrite = bool(force_overwrite_metadata_switch.value)
                            shared_hint = str(prompt_query_input.value or '').strip()
                            source = f"ui_{provider}" + (f":{model}" if provider == 'ollama' and model else '')
                            total = len(paths)
                            done = 0
                            saved = 0
                            errors = 0
                            _set_prompt_status(f'batch silencieux : 0/{total}', busy=True)
                            state.add_log(f"[PROMPT-BATCH] Démarrage silencieux: {total} photo(s), mode={mode}.")
                            def _bg_one(cur: str):
                                """Génère et persiste pour une photo. Retourne (saved_bool, err_str)."""
                                try:
                                    single = _build_single_media_context(cur)
                                    base_prompt = str(single.get('base_prompt') or '')
                                    tags_dict_l = dict(single.get('tags') or {})
                                    ai_payload_l = dict(single.get('ai_payload') or {})
                                    gen_prompt = shared_hint if force_overwrite else (shared_hint or base_prompt)
                                    if mode == 'both':
                                        existing_raw = ''
                                        existing_det = ''
                                        if not force_overwrite:
                                            try:
                                                existing_raw = str((search_engine.db_cache.get_prompt(cur) or {}).get('text') or '').strip()
                                                existing_det = str((search_engine.db_cache.get_detailed_prompt(cur) or {}).get('text') or '').strip()
                                            except Exception:
                                                pass
                                        if existing_raw and not existing_det:
                                            raw_text = existing_raw
                                        else:
                                            raw_out, _e = _generate_prompt_sync(provider, model, 'raw', gen_prompt, tags_dict_l, ai_payload_l, cur)
                                            raw_text = str(raw_out or '').strip()
                                        if existing_det and not existing_raw:
                                            det_text = existing_det
                                        else:
                                            det_out, _e = _generate_prompt_sync(provider, model, 'detailed', gen_prompt, tags_dict_l, ai_payload_l, cur)
                                            det_text = str(det_out or '').strip()
                                        did = False
                                        if raw_text:
                                            _persist_prompt_to_paths([cur], raw_text, 'raw', source, allow_metadata_redirect=False)
                                            did = True
                                        if det_text:
                                            _persist_prompt_to_paths([cur], det_text, 'detailed', source)
                                            did = True
                                        return (did, '')
                                    else:
                                        out, _e = _generate_prompt_sync(provider, model, mode, gen_prompt, tags_dict_l, ai_payload_l, cur)
                                        txt = str(out or '').strip()
                                        if txt:
                                            _persist_prompt_to_paths([cur], txt, mode, source)
                                            return (True, '')
                                        return (False, '')
                                except Exception as ex:
                                    return (False, repr(ex))
                            for cur in list(paths):
                                ok, err = await run.io_bound(_bg_one, cur)
                                done += 1
                                if ok:
                                    saved += 1
                                if err:
                                    errors += 1
                                    state.add_log(f"[PROMPT-BATCH] {os.path.basename(cur)}: {err}")
                                try:
                                    _set_prompt_batch_progress(f'{done}/{total} — {os.path.basename(cur)}')
                                except Exception:
                                    pass
                                _set_prompt_status(f'batch silencieux : {done}/{total}', busy=True)
                                # Met à jour la galerie au fil de l'eau
                                try:
                                    if state.prompt_results:
                                        for i, (score, p, ptext, dtext, psrc, dsrc) in enumerate(state.prompt_results):
                                            if p == cur:
                                                new_p = search_engine.db_cache.get_prompt(p) or {}
                                                new_d = search_engine.db_cache.get_detailed_prompt(p) or {}
                                                ptext_new = str(new_p.get('text') or '').strip()
                                                dtext_new = str(new_d.get('text') or '').strip()
                                                psrc_new = str(new_p.get('source') or '').strip().lower()
                                                dsrc_new = str(new_d.get('source') or '').strip().lower()
                                                new_score = 0.0
                                                if ptext_new: new_score += min(len(ptext_new) / 240.0, 1.0)
                                                if dtext_new: new_score += 1.0
                                                state.prompt_results[i] = (new_score, p, ptext_new, dtext_new, psrc_new, dsrc_new)
                                                break
                                except Exception:
                                    pass
                            try:
                                state.prompt_results.sort(key=lambda x: x[0], reverse=True)
                                prompt_gallery_ui.refresh()
                            except Exception:
                                pass
                            _set_prompt_batch_progress('')
                            _set_prompt_status(f'batch silencieux terminé : {saved}/{total} sauvegardé(s)' + (f', {errors} erreur(s)' if errors else ''), busy=False)
                            state.add_log(f"[PROMPT-BATCH] Terminé: {saved}/{total} sauvegardé(s), {errors} erreur(s).")
                            _safe_notify(
                                f'Batch silencieux terminé : {saved}/{total} sauvegardé(s)' + (f' | {errors} erreur(s)' if errors else ''),
                                'positive' if errors == 0 else 'warning'
                            )

                        async def generate_prompt_action():
                            req_id = _start_prompt_request('gen')

                            # Mode wizard multi-photos: si plus d'une photo sélectionnée
                            _sel_now = _get_selected_prompt_paths()
                            if len(_sel_now) > 1 and not _wizard['queue']:
                                _finish_prompt_request(req_id)
                                _wizard_mode = str(prompt_wizard_mode_sel.value or 'manual').strip().lower()
                                if _wizard_mode == 'silent':
                                    await _run_silent_prompt_batch(list(_sel_now))
                                else:
                                    await _open_prompt_wizard(list(_sel_now))
                                return

                            shared_prompt_hint = str(prompt_query_input.value or '').strip()
                            user_tags_dict = {tag: 1.0 for tag in (pos_tags_sel.value or [])}
                            selected_ctx = _build_selected_media_context()
                            force_overwrite = bool(force_overwrite_metadata_switch.value)
                            # Always use full context for the "insufficient context" check
                            prompt_text = shared_prompt_hint or selected_ctx['base_prompt']
                            # But for generation, ignore metadata text if force_overwrite is ON
                            generation_prompt_text = shared_prompt_hint if force_overwrite else prompt_text
                            tags_dict = dict(user_tags_dict or {})
                            if not tags_dict and selected_ctx['merged_tags']:
                                tags_dict = dict(selected_ctx['merged_tags'])
                            ai_payload = selected_ctx['ai_payload'] if isinstance(selected_ctx.get('ai_payload'), dict) else {}
                            source_path = str(selected_ctx.get('source_path') or '')
                            provider = prompt_provider_sel.value or 'local'
                            model = str(ollama_tags_model_sel.value or '').strip()
                            mode = str(prompt_mode_sel.value or 'raw').strip().lower()
                            save_config({
                                'tags_prompt_query': shared_prompt_hint,
                                'tags_prompt_provider': provider,
                                'tags_ollama_model': model,
                                'tags_prompt_mode': mode,
                            })
                            _set_prompt_status('génération du prompt en cours...', busy=True)
                            state.add_log(f"[PROMPT] Génération {('détaillée' if mode == 'detailed' else 'brute')} démarrée ({int(selected_ctx['count'])} sélection(s)).")
                            start_ts = time.monotonic()
                            _ollama_trace(
                                'ui.generate_prompt.start',
                                req_id=req_id,
                                provider=provider,
                                model=model,
                                mode=mode,
                                prompt_len=len(shared_prompt_hint or prompt_text),
                                tags_count=len(tags_dict),
                                selected_count=int(selected_ctx['count']),
                                source_path=source_path,
                            )
                            try:
                                if provider == 'ollama' and not model:
                                    _set_prompt_status('échec: modèle Ollama manquant', busy=False)
                                    _set_prompt_batch_progress('')
                                    _finish_prompt_request(req_id)
                                    return _safe_notify('Choisissez un modèle Ollama.', 'warning')

                                if not prompt_text and not tags_dict and not source_path:
                                    _set_prompt_status('échec: contexte insuffisant', busy=False)
                                    _set_prompt_batch_progress('')
                                    _finish_prompt_request(req_id)
                                    return _safe_notify('Ajoutez un prompt, des tags, ou sélectionnez des photos analysées.', 'warning')

                                # When force_overwrite is ON and there's truly nothing (no hint, no tags, no image), block.
                                if force_overwrite and not tags_dict and not shared_prompt_hint and not source_path:
                                    _set_prompt_status('échec: contexte insuffisant pour régénération', busy=False)
                                    _set_prompt_batch_progress('')
                                    _finish_prompt_request(req_id)
                                    return _safe_notify(
                                        'Écraser les métadonnées nécessite au moins une description ou une image sélectionnée.',
                                        'warning'
                                    )

                                if selected_ctx['count']:
                                    _set_prompt_status(f'génération basée sur {selected_ctx["count"]} photo(s) sélectionnée(s)...', busy=True)

                                # Auto-détection en mode 'both': ne génère que ce qui manque pour l'image active
                                skip_raw = False
                                skip_detailed = False
                                existing_raw_text = ''
                                existing_detailed_text = ''
                                if mode == 'both' and source_path and not force_overwrite:
                                    try:
                                        _existing_raw = search_engine.db_cache.get_prompt(source_path) or {}
                                        existing_raw_text = str(_existing_raw.get('text') or '').strip()
                                        _existing_det = search_engine.db_cache.get_detailed_prompt(source_path) or {}
                                        existing_detailed_text = str(_existing_det.get('text') or '').strip()
                                        # Si UN seul des deux existe, ne régénérer que le manquant
                                        if existing_raw_text and not existing_detailed_text:
                                            skip_raw = True
                                        elif existing_detailed_text and not existing_raw_text:
                                            skip_detailed = True
                                        # Si les deux existent: régénérer les deux (l'utilisateur a explicitement demandé)
                                    except Exception:
                                        pass

                                # Gestion du mode "both": générer brut ET détaillé
                                if mode == 'both':
                                    if skip_raw:
                                        generated_raw, engine_raw = existing_raw_text, 'existant (BD)'
                                        _ollama_trace('ui.generate_prompt.both.skip_raw', path=source_path)
                                    else:
                                        generated_raw, engine_raw = await run.io_bound(
                                            _generate_prompt_sync,
                                            provider,
                                            model,
                                            'raw',
                                            generation_prompt_text,
                                            tags_dict,
                                            ai_payload,
                                            source_path,
                                        )
                                    generated_raw = str(generated_raw or '').strip()

                                    if skip_detailed:
                                        generated_detailed, engine_detailed = existing_detailed_text, 'existant (BD)'
                                        _ollama_trace('ui.generate_prompt.both.skip_detailed', path=source_path)
                                    else:
                                        generated_detailed, engine_detailed = await run.io_bound(
                                            _generate_prompt_sync,
                                            provider,
                                            model,
                                            'detailed',
                                            generation_prompt_text,
                                            tags_dict,
                                            ai_payload,
                                            source_path,
                                        )
                                    generated_detailed = str(generated_detailed or '').strip()
                                    
                                    if not generated_raw and not generated_detailed:
                                        _set_prompt_status('aucun texte généré', busy=False)
                                        _set_prompt_batch_progress('')
                                        _finish_prompt_request(req_id)
                                        return _safe_notify('Aucun prompt généré. Vérifiez le prompt de base ou les tags.', 'warning')
                                    
                                    if _is_prompt_request_stale(req_id):
                                        _ollama_trace('ui.generate_prompt.discarded', req_id=req_id, reason='cancelled_or_stale')
                                        _set_prompt_status('requête annulée (résultat ignoré)', busy=False)
                                        _set_prompt_batch_progress('')
                                        return
                                    
                                    # Afficher les deux prompts dans les textbox respectifs
                                    generated_prompt_output_raw.value = generated_raw or ''
                                    generated_prompt_output_detailed.value = generated_detailed or ''
                                    _safe_ui_call(generated_prompt_output_raw.update, 'generated_prompt_output_raw.update')
                                    _safe_ui_call(generated_prompt_output_detailed.update, 'generated_prompt_output_detailed.update')
                                    elapsed = time.monotonic() - start_ts
                                    _set_prompt_status(f'génération terminée en {elapsed:.1f}s (Brut: {engine_raw}, Détaillé: {engine_detailed})', busy=False)
                                    _set_prompt_batch_progress('')
                                    state.add_log(f"[PROMPT] Génération (brut + détaillé) terminée en {elapsed:.1f}s.")
                                    _ollama_trace('ui.generate_prompt.both.done', req_id=req_id, elapsed_s=f"{elapsed:.2f}", engine_raw=engine_raw, engine_detailed=engine_detailed, raw_len=len(generated_raw), detailed_len=len(generated_detailed))
                                    _safe_notify(f'Prompts brut et détaillé générés.', 'positive')
                                    _finish_prompt_request(req_id)
                                    return

                                generated, engine = await run.io_bound(
                                    _generate_prompt_sync,
                                    provider,
                                    model,
                                    mode,
                                    generation_prompt_text,
                                    tags_dict,
                                    ai_payload,
                                    source_path,
                                )

                                generated = str(generated or '').strip()
                                if not generated:
                                    _set_prompt_status('aucun texte généré', busy=False)
                                    _set_prompt_batch_progress('')
                                    _finish_prompt_request(req_id)
                                    return _safe_notify('Aucun prompt généré. Vérifiez le prompt de base ou les tags.', 'warning')

                                if _is_prompt_request_stale(req_id):
                                    _ollama_trace('ui.generate_prompt.discarded', req_id=req_id, reason='cancelled_or_stale')
                                    _set_prompt_status('requête annulée (résultat ignoré)', busy=False)
                                    _set_prompt_batch_progress('')
                                    return

                                generated_prompt_output.value = generated or ''
                                _safe_ui_call(generated_prompt_output.update, 'generated_prompt_output.update')
                                elapsed = time.monotonic() - start_ts
                                _set_prompt_status(f'génération terminée en {elapsed:.1f}s ({engine})', busy=False)
                                _set_prompt_batch_progress('')
                                state.add_log(f"[PROMPT] Génération terminée en {elapsed:.1f}s via {engine}.")
                                _ollama_trace('ui.generate_prompt.done', req_id=req_id, elapsed_s=f"{elapsed:.2f}", engine=engine, out_len=len(generated))
                                _safe_notify(f'Prompt {"détaillé" if mode == "detailed" else "brut"} généré.', 'positive')

                            except Exception as e:
                                _set_prompt_status('erreur génération prompt', busy=False)
                                _set_prompt_batch_progress('')
                                state.add_log(f"[PROMPT] Erreur génération prompt: {e}")
                                _ollama_trace('ui.generate_prompt.error', req_id=req_id, error=repr(e))
                                _safe_notify(f'Erreur génération prompt: {e}', 'negative')
                            finally:
                                _finish_prompt_request(req_id)

                        def _persist_prompt_to_paths(paths, prompt_text, mode, source, allow_metadata_redirect=True):
                            saved = 0
                            errors = 0
                            for path in (paths or []):
                                try:
                                    if mode == 'detailed':
                                        search_engine.db_cache.save_detailed_prompt(path, prompt_text, source=source)
                                    else:
                                        existing_prompt = search_engine.db_cache.get_prompt(path) or {}
                                        existing_source = str(existing_prompt.get('source') or '').strip().lower()
                                        # Check if we should force overwrite metadata prompts
                                        force_overwrite = bool(force_overwrite_metadata_switch.value)
                                        if allow_metadata_redirect and existing_source.startswith('image_metadata') and not force_overwrite:
                                            search_engine.db_cache.save_detailed_prompt(path, prompt_text, source=f"raw:{source}")
                                        else:
                                            search_engine.db_cache.save_prompt(path, prompt_text, source=source)
                                    # Écriture systématique du sidecar TXT (détaillé prioritaire sur brut)
                                    try:
                                        det_after = (search_engine.db_cache.get_detailed_prompt(path) or {}).get('text') or ''
                                        raw_after = (search_engine.db_cache.get_prompt(path) or {}).get('text') or ''
                                        best_txt = str(det_after).strip() or str(raw_after).strip()
                                        if best_txt:
                                            p_obj = Path(path)
                                            p_obj.with_name(f"{p_obj.stem}_prompt.txt").write_text(best_txt, encoding='utf-8')
                                    except Exception as _txt_e:
                                        _ollama_trace('ui.save_prompt_txt.error', path=path, mode=mode, error=repr(_txt_e))
                                    saved += 1
                                except Exception as e:
                                    errors += 1
                                    _ollama_trace('ui.save_prompt.error', path=path, mode=mode, error=repr(e))
                            return saved, errors

                        async def save_generated_prompt_action():
                            mode = str(prompt_mode_sel.value or 'raw').strip().lower()
                            
                            # Récupérer le/les texte(s) généré(s)
                            if mode == 'both':
                                generated_text_raw = str(generated_prompt_output_raw.value or '').strip()
                                generated_text_detailed = str(generated_prompt_output_detailed.value or '').strip()
                                if not generated_text_raw and not generated_text_detailed:
                                    return _safe_notify('Aucun prompt généré à sauvegarder.', 'warning')
                            else:
                                generated_text = str(generated_prompt_output.value or '').strip()
                                if not generated_text:
                                    return _safe_notify('Aucun prompt généré à sauvegarder.', 'warning')

                            provider = str(prompt_provider_sel.value or 'local').strip().lower()
                            model = str(ollama_tags_model_sel.value or '').strip()
                            source = f"ui_{provider}" + (f":{model}" if provider == 'ollama' and model else '')

                            selected_paths = _get_selected_prompt_paths()
                            if not selected_paths:
                                return _safe_notify('Sélectionnez au moins une photo dans la galerie Prompt.', 'warning')

                            if mode == 'both':
                                # Sauvegarder les deux prompts (sans redirection raw->detailed)
                                saved_raw, errors_raw = _persist_prompt_to_paths(selected_paths, generated_text_raw, 'raw', source, allow_metadata_redirect=False) if generated_text_raw else (0, 0)
                                saved_detailed, errors_detailed = _persist_prompt_to_paths(selected_paths, generated_text_detailed, 'detailed', source) if generated_text_detailed else (0, 0)
                                saved = saved_raw + saved_detailed
                                errors = errors_raw + errors_detailed
                                if saved:
                                    _set_prompt_status(f'prompts sauvegardés pour {saved_raw or saved_detailed} photo(s)', busy=False)
                                    _ollama_trace('ui.save_prompt.both.done', saved_raw=saved_raw, saved_detailed=saved_detailed, errors=errors, source=source)
                                    msg = f'Prompts brut et détaillé sauvegardés sur {saved_raw or saved_detailed} photo(s).'
                                    if errors:
                                        msg += f' ({errors} erreur(s))'
                                    _safe_notify(msg, 'positive' if errors == 0 else 'warning')
                            else:
                                saved, errors = _persist_prompt_to_paths(selected_paths, generated_text, mode, source)
                                if saved:
                                    _set_prompt_status(f'prompt sauvegardé pour {saved} photo(s)', busy=False)
                                    _ollama_trace('ui.save_prompt.done', mode=mode, saved=saved, errors=errors, source=source)
                                    msg = f'Prompt {"détaillé" if mode == "detailed" else "brut"} sauvegardé sur {saved} photo(s).'
                                    if errors:
                                        msg += f' ({errors} erreur(s))'
                                    _safe_notify(msg, 'positive' if errors == 0 else 'warning')
                            
                            if saved or (mode == 'both' and (saved_raw or saved_detailed)):
                                state.add_log(f"[PROMPT] Sauvegarde terminée: {saved if mode != 'both' else (saved_raw or saved_detailed)} photo(s) mise(s) à jour.")
                                # Mettre à jour l'affichage des prompts sauvegardés dans la galerie
                                updated_count = 0
                                if state.prompt_results:
                                    for i, (score, path, prompt_text, detailed_text, prompt_source, detailed_source) in enumerate(state.prompt_results):
                                        if path in selected_paths:
                                            if mode == 'both':
                                                if generated_text_raw: prompt_text = generated_text_raw
                                                if generated_text_detailed: detailed_text = generated_text_detailed
                                            elif mode == 'detailed':
                                                item = search_engine.db_cache.get_detailed_prompt(path) or {}
                                                detailed_text = str(item.get('text') or '').strip()
                                            else:
                                                item = search_engine.db_cache.get_prompt(path) or {}
                                                prompt_text = str(item.get('text') or '').strip()
                                            
                                            # Recalculer le score
                                            new_score = 0.0
                                            if prompt_text:
                                                new_score += min(len(prompt_text) / 240.0, 1.0)
                                            if detailed_text:
                                                new_score += 1.0
                                            
                                            # Récupérer les sources mises à jour depuis la BD
                                            item = search_engine.db_cache.get_prompt(path) or {}
                                            prompt_source = str(item.get('source') or '').strip().lower()
                                            detailed_item = search_engine.db_cache.get_detailed_prompt(path) or {}
                                            detailed_source = str(detailed_item.get('source') or '').strip().lower()
                                            
                                            # Mettre à jour le tuple
                                            state.prompt_results[i] = (new_score, path, prompt_text, detailed_text, prompt_source, detailed_source)
                                            updated_count += 1
                                    # Retrier les résultats par score
                                    state.prompt_results.sort(key=lambda x: x[0], reverse=True)
                                state.prompt_page = 1
                                prompt_gallery_ui.refresh()
                            else:
                                _safe_notify('Échec de sauvegarde du prompt.', 'negative')

                        def save_prompts_txt_action():
                            selected_paths = _get_selected_prompt_paths()
                            if not selected_paths:
                                return _safe_notify('Sélectionnez au moins une photo dans la galerie Prompt.', 'warning')

                            mode = str(prompt_mode_sel.value or 'raw').strip().lower()
                            written = 0
                            errors = 0

                            for path in selected_paths:
                                try:
                                    if mode == 'both':
                                        # Pour le mode "both", écrire le prompt brut dans le fichier TXT
                                        text_to_write = str(generated_prompt_output_raw.value or '').strip()
                                    elif mode == 'detailed':
                                        text_to_write = str(generated_prompt_output.value or '').strip()
                                        if not text_to_write:
                                            cached = search_engine.db_cache.get_detailed_prompt(path) or {}
                                            text_to_write = str(cached.get('text') or '').strip()
                                    else:
                                        text_to_write = str(generated_prompt_output.value or '').strip()
                                        if not text_to_write:
                                            cached = search_engine.db_cache.get_prompt(path) or {}
                                            text_to_write = str(cached.get('text') or '').strip()
                                    
                                    if not text_to_write:
                                        errors += 1
                                        continue

                                    p = Path(path)
                                    out_path = p.with_name(f"{p.stem}_prompt.txt")
                                    out_path.write_text(text_to_write, encoding='utf-8')
                                    search_engine.db_cache.save_prompt(path, text_to_write, source='file_sidecar')
                                    written += 1
                                except Exception as e:
                                    errors += 1
                                    _ollama_trace('ui.save_prompt_txt.error', path=path, mode=mode, error=repr(e))

                            if written:
                                _safe_notify(
                                    f'Fichiers prompts TXT écrits: {written}' + (f' ({errors} erreur(s))' if errors else ''),
                                    'positive' if errors == 0 else 'warning'
                                )
                                state.add_log(f"[PROMPT] Fichiers TXT écrits et sauvegardés en cache: {written} fichier(s).")
                                # Mettre à jour l'affichage des prompts dans la galerie
                                updated_count = 0
                                if state.prompt_results:
                                    for i, (score, path, prompt_text, detailed_text, prompt_source, detailed_source) in enumerate(state.prompt_results):
                                        if path in selected_paths:
                                            if mode == 'detailed':
                                                item = search_engine.db_cache.get_detailed_prompt(path) or {}
                                            else:
                                                item = search_engine.db_cache.get_prompt(path) or {}
                                            new_prompt_text = str(item.get('text') or '').strip()
                                            new_source = str(item.get('source') or '').strip().lower()
                                            if new_prompt_text:
                                                # Recalculer le score
                                                new_score = 0.0
                                                if mode == 'detailed':
                                                    new_score = 1.0
                                                else:
                                                    new_score = min(len(new_prompt_text) / 240.0, 1.0)
                                                if detailed_text:
                                                    new_score += 1.0
                                                # Mettre à jour le tuple
                                                if mode == 'detailed':
                                                    state.prompt_results[i] = (new_score, path, prompt_text, new_prompt_text, prompt_source, new_source)
                                                else:
                                                    state.prompt_results[i] = (new_score, path, new_prompt_text, detailed_text, new_source, detailed_source)
                                                updated_count += 1
                                    # Retrier les résultats par score
                                    state.prompt_results.sort(key=lambda x: x[0], reverse=True)
                                state.prompt_page = 1
                                prompt_gallery_ui.refresh()
                            else:
                                _safe_notify('Aucun fichier TXT écrit.', 'warning')

                        async def search_from_prompt_action():
                            await prompt_to_tags_action(run_search_after=True)
                    
                    tags_threshold = ui.number('Seuil de confiance (0.1 - 1.0)', value=cfg.get('tags_threshold', 0.4), format='%.2f', step=0.05).classes('w-full mt-4')
                    chk_txt_tags = ui.checkbox('Sauvegarder un .txt avec les tags lors de la copie', value=cfg.get('chk_txt_tags', True)).classes('text-sm text-gray-300 w-full')

                    with ui.expansion('Paramètres avancés', icon='tune').classes('w-full bg-gray-800/50 rounded-lg border border-gray-700 mt-2'):
                        with ui.row().classes('w-full gap-2 px-2 pt-2'):
                            tags_batch_size = ui.number('Lot', value=cfg.get('tags_batch_size', 8), format='%.0f').classes('w-[45%]')
                            tags_video_frames = ui.number('Images vid.', value=cfg.get('tags_video_frames', 4), format='%.0f').classes('w-[45%]')

                def _load_all_tags_results(directory, extensions, key):
                    all_files = search_engine._gather_files(directory, tuple(extensions))
                    valid_paths = set(all_files)

                    c = search_engine.db_cache.conn.cursor()
                    c.execute("SELECT path, tags FROM tags_cache WHERE model=?", (key,))
                    db_data = c.fetchall()

                    res = []
                    for path, tags_json in db_data:
                        if path not in valid_paths or not tags_json:
                            continue
                        tags = json.loads(tags_json)
                        if not tags:
                            continue
                        score = max(tags.values()) if tags else 0.0
                        res.append((float(score), path, tags))

                    res.sort(key=lambda x: x[0], reverse=True)
                    return res

                def _load_all_prompt_results(directory, extensions):
                    all_files = search_engine._gather_files(directory, tuple(extensions))
                    unique_files = []
                    seen_paths = set()
                    for path in all_files:
                        norm_key = os.path.normcase(os.path.normpath(os.path.realpath(path)))
                        if norm_key in seen_paths:
                            continue
                        seen_paths.add(norm_key)
                        unique_files.append(path)

                    if len(unique_files) < len(all_files):
                        state.add_log(f"[PROMPT] Doublons ignorés dans la galerie: {len(all_files) - len(unique_files)}")

                    res = []
                    for path in unique_files:
                        prompt_item = search_engine.db_cache.get_prompt(path) or {}
                        detailed_item = search_engine.db_cache.get_detailed_prompt(path) or {}
                        prompt_source = str(prompt_item.get('source') or '').strip().lower()
                        prompt_text = str(prompt_item.get('text') or '').strip()
                        detailed_source = str(detailed_item.get('source') or '').strip().lower()
                        detailed_text = str(detailed_item.get('text') or '').strip()

                        # Nettoyer les entrées polluées par des métadonnées XMP/XML Adobe
                        _xmp_markers = ('<?xpacket', '<x:xmpmeta', '<?xml', '<rdf:', '<dc:')
                        for _field, _src_attr in ((prompt_text, 'prompt_source'), (detailed_text, 'detailed_source')):
                            _t = _field.lstrip()
                            if _t.startswith(_xmp_markers) or (_t.startswith('<') and 'xmlns:' in _t[:500]):
                                if _field is prompt_text:
                                    search_engine.db_cache.save_prompt(path, '', source=prompt_source or 'image_metadata_positive_prompt')
                                    prompt_text = ''
                                    prompt_source = ''
                                else:
                                    search_engine.db_cache.save_detailed_prompt(path, '', source=detailed_source or 'heuristic')
                                    detailed_text = ''
                                    detailed_source = ''

                        # Rejeter les faux positifs numériques du cache (ex : '68', '127', "['68', 0]")
                        if prompt_text and not TagEngine._is_valid_prompt_text(prompt_text):
                            search_engine.db_cache.save_prompt(path, '', source=prompt_source or '')
                            prompt_text = ''
                            prompt_source = ''
                        if detailed_text and not TagEngine._is_valid_prompt_text(detailed_text):
                            search_engine.db_cache.save_detailed_prompt(path, '', source=detailed_source or '')
                            detailed_text = ''
                            detailed_source = ''

                        # Load prompts from .txt sidecar files if present and not already cached
                        # Conventions: `{stem}_prompt.txt` ou `{stem}.txt` (SD WebUI / kohya)
                        if not prompt_text:
                            txt_content = TagEngine._read_sidecar_prompt_txt(path)
                            if txt_content:
                                prompt_text = txt_content
                                prompt_source = 'file_sidecar'
                                search_engine.db_cache.save_prompt(path, txt_content, source='file_sidecar')

                        # Restore the embedded source prompt if a generated raw prompt replaced it earlier.
                        if path.lower().endswith(SUPPORTED_IMAGES):
                            try:
                                embedded = TagEngine._extract_prompt_from_image_metadata(path)
                                if embedded:
                                    if prompt_text and not prompt_source.startswith('image_metadata') and not detailed_text:
                                        detailed_text = prompt_text
                                        detailed_source = f"raw:{prompt_source or 'cache'}"
                                        search_engine.db_cache.save_detailed_prompt(path, prompt_text, source=detailed_source)
                                    if not prompt_text or not prompt_source.startswith('image_metadata'):
                                        prompt_text = embedded
                                        prompt_source = "image_metadata_positive_prompt"
                                        search_engine.db_cache.save_prompt(path, prompt_text, source="image_metadata_positive_prompt")
                            except Exception:
                                pass

                        score = 0.0
                        if prompt_text:
                            score += min(len(prompt_text) / 240.0, 1.0)
                        if detailed_text:
                            score += 1.0
                        res.append((float(score), path, prompt_text, detailed_text, prompt_source, detailed_source))
                    res.sort(key=lambda x: x[0], reverse=True)
                    return res

                def _tags_results_loaded() -> bool:
                    return bool(state.tags_results)

                def _update_tags_primary_button():
                    btn_search_tags.text = '🎯 Rechercher les tags' if _tags_results_loaded() else '🖼️ Lire les images'
                    btn_search_tags.tooltip('Filtre les médias déjà chargés avec les tags sélectionnés' if _tags_results_loaded() else 'Lit les images du dossier, indexe les tags puis affiche la galerie')
                    btn_search_tags.update()

                async def index_tags_action():
                    save_config({
                        'tags_dir': tags_dir.value, 'chk_img_tags': chk_img_tags.value, 'chk_vid_tags': chk_vid_tags.value,
                        'tags_model': tags_model_sel.value, 'tags_batch_size': tags_batch_size.value, 
                        'tags_video_frames': tags_video_frames.value
                    })
                    if not tags_dir.value: return ui.notify("Indiquez un dossier !", type='warning')
                    
                    state.is_processing = True
                    search_engine.cancel_flag = False
                    btn_index_tags.disable()
                    btn_search_tags.disable()
                    
                    search_engine._unload_embedding_model()
                    aesthetic_engine.unload()
                    nsfw_engine.unload()
                    face_engine.unload()
                    
                    exts =[]
                    if chk_img_tags.value: exts.extend(SUPPORTED_IMAGES)
                    if chk_vid_tags.value: exts.extend(SUPPORTED_VIDEOS)

                    def bg_task():
                        try:
                            state.add_log(f"Indexation des tags : '{tags_dir.value}'")
                            tag_engine.batch_size = int(tags_batch_size.value)
                            tag_engine.video_frames = int(tags_video_frames.value)
                            tag_engine.evaluate_media(tags_dir.value, tags_model_sel.value, tuple(exts))
                            state.add_log("✅ Indexation des tags terminee !")
                        except Exception as e: state.add_log(f"❌ Erreur : {e}")
                        finally:
                            state.status_text = "Prêt !"
                            state.progress = 1.0
                            state.is_processing = False

                    await run.io_bound(bg_task)
                    btn_index_tags.enable()
                    btn_search_tags.enable()

                async def force_reindex_tags_action():
                    save_config({
                        'tags_dir': tags_dir.value, 'chk_img_tags': chk_img_tags.value, 'chk_vid_tags': chk_vid_tags.value,
                        'tags_model': tags_model_sel.value, 'tags_batch_size': tags_batch_size.value,
                        'tags_video_frames': tags_video_frames.value
                    })
                    if not tags_dir.value:
                        return ui.notify("Indiquez un dossier !", type='warning')

                    state.is_processing = True
                    search_engine.cancel_flag = False
                    btn_index_tags.disable()
                    btn_force_index_tags.disable()
                    btn_search_tags.disable()

                    search_engine._unload_embedding_model()
                    aesthetic_engine.unload()
                    nsfw_engine.unload()
                    face_engine.unload()

                    exts =[]
                    if chk_img_tags.value: exts.extend(SUPPORTED_IMAGES)
                    if chk_vid_tags.value: exts.extend(SUPPORTED_VIDEOS)

                    def bg_task():
                        try:
                            state.add_log(f"♻️ Régénération forcée des tags : '{tags_dir.value}'")
                            tag_engine.batch_size = int(tags_batch_size.value)
                            tag_engine.video_frames = int(tags_video_frames.value)

                            cache_key = f"{tags_model_sel.value}_{int(tags_video_frames.value)}"
                            files_in_scope = search_engine._gather_files(tags_dir.value, tuple(exts))
                            if files_in_scope:
                                c = search_engine.db_cache.conn.cursor()
                                chunk_size = 900
                                for i in range(0, len(files_in_scope), chunk_size):
                                    chunk = files_in_scope[i:i + chunk_size]
                                    placeholders = ','.join(['?'] * len(chunk))
                                    c.execute(f"DELETE FROM tags_cache WHERE model=? AND path IN ({placeholders})", [cache_key, *chunk])
                                search_engine.db_cache.conn.commit()
                                state.add_log(f"[TAGS] Cache supprimé pour {len(files_in_scope)} fichier(s), modèle: {cache_key}")
                            else:
                                state.add_log("[TAGS] Aucun fichier trouvé pour la régénération forcée.")

                            tag_engine.evaluate_media(tags_dir.value, tags_model_sel.value, tuple(exts))
                            state.add_log("✅ Régénération forcée des tags terminée !")
                        except Exception as e:
                            state.add_log(f"❌ Erreur (régénération forcée tags) : {e}")
                        finally:
                            state.status_text = "Prêt !"
                            state.progress = 1.0
                            state.is_processing = False

                    await run.io_bound(bg_task)

                    # Après régénération forcée: réafficher automatiquement les photos du dossier.
                    cache_key = f"{tags_model_sel.value}_{int(tags_video_frames.value)}"
                    refreshed = await run.io_bound(_load_all_tags_results, tags_dir.value, exts, cache_key)
                    state.tags_base_dir = tags_dir.value
                    state.tags_results = refreshed
                    state.sel_tags = {p: False for _s, p, _t in refreshed}
                    state.tags_page = 1
                    tags_gallery_ui.refresh()
                    _update_tags_primary_button()
                    state.add_log(f"[TAGS] Galerie rafraîchie: {len(refreshed)} média(s) affiché(s).")

                    btn_index_tags.enable()
                    btn_force_index_tags.enable()
                    btn_search_tags.enable()

                async def search_tags_action():
                    save_config({
                        'tags_dir': tags_dir.value, 'pos_tags': pos_tags_sel.value, 'neg_tags': neg_tags_sel.value,
                        'tags_threshold': tags_threshold.value, 'chk_txt_tags': chk_txt_tags.value
                    })
                    if not tags_dir.value: return ui.notify("Indiquez un dossier !", type='warning')
                    
                    state.is_processing = True
                    btn_search_tags.disable()
                    state.tags_base_dir = tags_dir.value
                    state.tags_results.clear()
                    state.sel_tags.clear()
                    state.tags_page = 1
                    tags_gallery_ui.refresh()
                    
                    dir_val = tags_dir.value
                    thres_val = float(tags_threshold.value)
                    pos_val = set(pos_tags_sel.value)
                    neg_val = set(neg_tags_sel.value)
                    cache_key = f"{tags_model_sel.value}_{int(tags_video_frames.value)}"
                    
                    exts =[]
                    if chk_img_tags.value: exts.extend(SUPPORTED_IMAGES)
                    if chk_vid_tags.value: exts.extend(SUPPORTED_VIDEOS)

                    def process_search(directory, extensions, key, thres, pos, neg):
                        all_files = search_engine._gather_files(directory, tuple(extensions))
                        
                        c = search_engine.db_cache.conn.cursor()
                        c.execute("SELECT path, tags FROM tags_cache WHERE model=?", (key,))
                        db_data = c.fetchall()
                        
                        valid_paths = set(all_files)
                        res =[]
                        
                        for row in db_data:
                            path, tags_json = row[0], row[1]
                            if path not in valid_paths or not tags_json: continue
                            
                            tags = json.loads(tags_json)
                            valid = True
                            for pt in pos:
                                if pt not in tags or tags[pt] < thres:
                                    valid = False; break
                            if not valid: continue
                            
                            for nt in neg:
                                if nt in tags and tags[nt] >= thres:
                                    valid = False; break
                            if not valid: continue
                            
                            score = sum([tags[pt] for pt in pos]) if pos else max(tags.values()) if tags else 0
                            res.append((score, path, tags))
                            
                        res.sort(key=lambda x: x[0], reverse=True)
                        return res

                    res = None
                    try:
                        res = await run.io_bound(process_search, dir_val, exts, cache_key, thres_val, pos_val, neg_val)
                    except Exception as _e:
                        state.add_log(f"❌ Erreur recherche Tags : {_e}")

                    try:
                        state.tags_results = res if res is not None else []
                        state.sel_tags = {p: False for s, p, t in state.tags_results}
                        tags_gallery_ui.refresh()
                        _update_tags_primary_button()
                        state.add_log(f"[TAGS] Recherche terminée : {len(state.tags_results)} résultat(s).")
                    except Exception as _e:
                        state.add_log(f"❌ Erreur affichage résultats Tags : {_e}")
                    finally:
                        btn_search_tags.enable()
                        state.is_processing = False
                        state.status_text = "Prêt !"
                        state.progress = 1.0

                async def load_txt_tags_action():
                    if not tags_dir.value:
                        return ui.notify("Indiquez un dossier !", type='warning')
                    state.is_processing = True
                    btn_load_txt_tags.disable()
                    btn_index_tags.disable()
                    btn_search_tags.disable()
                    state.tags_results.clear()
                    state.sel_tags.clear()
                    state.tags_page = 1
                    tags_gallery_ui.refresh()

                    def bg_load():
                        exts = []
                        if chk_img_tags.value: exts.extend(SUPPORTED_IMAGES)
                        if chk_vid_tags.value: exts.extend(SUPPORTED_VIDEOS)
                        exts_set = set(e.lower() for e in exts)

                        loaded = []
                        state.add_log(f"[TAGS] Chargement depuis .txt : '{tags_dir.value}'")
                        for root, dirs, files in os.walk(tags_dir.value):
                            dirs.sort()
                            for fname in sorted(files):
                                ext = os.path.splitext(fname)[1].lower()
                                if ext not in exts_set:
                                    continue
                                img_path = os.path.join(root, fname)
                                stem = os.path.splitext(img_path)[0]
                                txt_path = stem + '.txt'
                                if not os.path.isfile(txt_path):
                                    continue
                                try:
                                    content = open(txt_path, 'r', encoding='utf-8').read().strip()
                                    if not content:
                                        continue
                                    tags = {t.strip(): 1.0 for t in content.split(',') if t.strip()}
                                    if not tags:
                                        continue
                                    loaded.append((1.0, img_path, tags))
                                except Exception as e:
                                    state.add_log(f"[TAGS] Impossible de lire {fname}: {e}")

                        state.tags_base_dir = tags_dir.value
                        state.tags_results = loaded
                        state.sel_tags = {p: False for _s, p, _t in loaded}
                        state.add_log(f"✅ {len(loaded)} images chargées depuis .txt (sans scores IA).")

                    await run.io_bound(bg_load)
                    tags_gallery_ui.refresh()
                    _update_tags_primary_button()
                    state.is_processing = False
                    btn_load_txt_tags.enable()
                    btn_index_tags.enable()
                    btn_search_tags.enable()

                async def read_or_search_tags_action():
                    if not _tags_results_loaded():
                        await index_tags_action()
                    await search_tags_action()
                    
                with ui.row().classes('w-full p-4 pt-2 shrink-0 border-t border-gray-800 bg-gray-900 z-10 gap-2'):
                    btn_load_txt_tags = ui.button('📂 Charger .txt', on_click=load_txt_tags_action).classes('w-full bg-gray-700 hover:bg-gray-600 font-bold').tooltip('Charge les images qui ont déjà un .txt de tags, sans relancer le modèle IA')
                    btn_index_tags = ui.button('🏷️ Indexer les tags', on_click=index_tags_action).classes('w-full bg-gray-700 hover:bg-gray-600 font-bold').tooltip('Indexe ou regénère les tags IA dans la base')
                    btn_force_index_tags = ui.button('♻️ Réindexer les tags', on_click=force_reindex_tags_action).classes('w-full bg-orange-700 hover:bg-orange-600 font-bold').tooltip('Supprime le cache tags du dossier puis regénère tout')
                    btn_search_tags = ui.button('🖼️ Lire les images', on_click=read_or_search_tags_action).classes('w-full bg-pink-700 hover:bg-pink-600 font-bold')

                _update_tags_primary_button()

            with ui.column().classes('flex-1 w-0 bg-gray-900 rounded-xl border border-gray-800 overflow-hidden h-full relative p-0'):
                tags_gallery_ui()

        with ui.tab_panel(tab_prompt).classes('w-full h-[calc(100vh-115px)] p-4 flex flex-row flex-nowrap items-stretch gap-4'):
            with ui.column().classes('w-[350px] shrink-0 bg-gray-900 rounded-xl border border-gray-800 shadow-lg flex flex-col overflow-hidden p-0 gap-0'):
                with ui.row().classes('w-full p-4 pb-2 shrink-0 border-b border-gray-800 bg-gray-900 z-10'):
                    ui.label('Recherche par Prompt').classes('text-lg font-bold')

                with ui.column().classes('w-full flex-1 overflow-y-auto p-4 gap-2 min-h-0'):
                    with ui.row().classes('w-full items-center gap-1 flex-nowrap'):
                        prompt_dir = ui.input('Dossier', value=cfg.get('prompt_dir', cfg.get('tags_dir', ''))).classes('flex-grow')
                        ui.button(icon='folder', on_click=lambda: select_folder(prompt_dir)).props('flat round dense')
                        ui.button(icon='delete_sweep', on_click=lambda: clear_folder_cache(prompt_dir.value)).props('flat round dense text-color=red').tooltip('Effacer l\'index du dossier')

                    with ui.row().classes('w-full gap-2'):
                        chk_img_prompt = ui.checkbox('Images', value=cfg.get('chk_img_prompt', True))
                        chk_vid_prompt = ui.checkbox('Vidéos', value=cfg.get('chk_vid_prompt', False))

                    ui.checkbox('Masquer ceux avec prompt', value=bool(cfg.get('prompt_hide_with_prompt', False))) \
                        .bind_value(state, 'prompt_hide_with_prompt') \
                        .on_value_change(lambda _e: (save_config({'prompt_hide_with_prompt': bool(state.prompt_hide_with_prompt)}), setattr(state, 'prompt_page', 1), prompt_gallery_ui.refresh())) \
                        .classes('w-full text-sm text-amber-300')

                    prompt_text_filter = ui.textarea(
                        'Recherche texte dans le prompt',
                        value=cfg.get('prompt_text_filter', ''),
                        placeholder='Tapez un extrait de prompt, un mot-clé, un style, ou laissez vide pour tout afficher.'
                    ).props('autogrow filled').classes('w-full')

                    ui.label('Cet onglet affiche les prompts en grand et permet de relire/rechercher les images sans passer par les cartes Tags.').classes('text-xs text-gray-400')

                    with ui.expansion('Outils prompt / Ollama', icon='psychology').classes('w-full bg-gray-800/50 rounded-lg border border-cyan-800 mt-3'):
                        ollama_model_options = list(cfg.get('ollama_models_cached', []) or [])
                        ollama_model_default = str(cfg.get('tags_ollama_model', '') or '').strip()
                        if ollama_model_default not in ollama_model_options:
                            ollama_model_default = ollama_model_options[0] if ollama_model_options else None
                        provider_default = str(cfg.get('tags_prompt_provider', 'local') or 'local').strip().lower()
                        if provider_default not in ('local', 'ollama'):
                            provider_default = 'local'
                        mode_default = str(cfg.get('tags_prompt_mode', 'both') or 'both').strip().lower()
                        if mode_default not in ('raw', 'detailed', 'both'):
                            mode_default = 'both'
                        wizard_mode_default = str(cfg.get('tags_prompt_wizard_mode', 'manual') or 'manual').strip().lower()
                        if wizard_mode_default not in ('manual', 'auto', 'silent'):
                            wizard_mode_default = 'manual'

                        prompt_provider_sel = ui.select(
                            {'local': 'Moteur local', 'ollama': 'Ollama'},
                            value=provider_default,
                            label='Moteur de génération'
                        ).classes('w-full')
                        ollama_tags_model_sel = ui.select(
                            ollama_model_options,
                            value=ollama_model_default,
                            label='Modèle Ollama'
                        ).classes('w-full').bind_visibility_from(prompt_provider_sel, 'value', backward=lambda v: v == 'ollama')
                        prompt_query_input = ui.textarea(
                            'Prompt / description de base',
                            value=cfg.get('tags_prompt_query', ''),
                            placeholder='Entrez un prompt positif ou une description. Vous pouvez ensuite convertir en tags ou générer un prompt.'
                        ).props('autogrow filled').classes('w-full')
                        prompt_mode_sel = ui.select(
                            {'raw': 'Prompt brut', 'detailed': 'Prompt détaillé', 'both': 'Les deux (auto: ne génère que ce qui manque)'},
                            value=mode_default,
                            label='Type de prompt'
                        ).classes('w-full')
                        force_overwrite_metadata_switch = ui.switch(
                            '🔄 Écraser les métadonnées',
                            value=False
                        ).classes('w-full text-sm text-gray-300').tooltip('Force l\'écrasement du prompt image_metadata existant')

                        prompt_wizard_mode_sel = ui.select(
                            {
                                'manual': '👁️ Manuel — popup + validation à chaque photo',
                                'auto':   '⏩ Auto — popup visible, validation automatique',
                                'silent': '🤖 Silencieux — batch en arrière-plan, sans popup',
                            },
                            value=wizard_mode_default,
                            label='Mode wizard multi-photos',
                        ).classes('w-full').tooltip(
                            'Lorsque plusieurs photos sont sélectionnées :\n'
                            '• Manuel : tu valides chaque photo dans le popup\n'
                            '• Auto : le popup défile, validation auto à chaque génération\n'
                            '• Silencieux : aucun popup, traitement direct'
                        )
                        prompt_wizard_mode_sel.on_value_change(
                            lambda e: save_config({'tags_prompt_wizard_mode': str(e.value or 'manual')})
                        )
                        
                        # Affichage conditionnel: une ou deux textbox selon le mode
                        with ui.column().classes('w-full gap-2'):
                            # Conteneur pour les deux textbox côte à côte (mode "both")
                            output_container_both = ui.row().classes('w-full gap-2')
                            with output_container_both:
                                generated_prompt_output_raw = ui.textarea(
                                    'Brut',
                                    value='',
                                ).props('readonly autogrow filled').classes('w-1/2 text-xs')
                                generated_prompt_output_detailed = ui.textarea(
                                    'Détaillé',
                                    value='',
                                ).props('readonly autogrow filled').classes('w-1/2 text-xs')
                            
                            # Textbox unique pour les modes "raw" et "detailed"
                            generated_prompt_output = ui.textarea(
                                'Résultat généré',
                                value='',
                            ).props('readonly autogrow filled').classes('w-full text-sm')
                        
                        def _update_prompt_output_visibility():
                            mode_val = str(prompt_mode_sel.value or 'raw').strip().lower()
                            if mode_val == 'both':
                                output_container_both.visible = True
                                generated_prompt_output.visible = False
                            else:
                                output_container_both.visible = False
                                generated_prompt_output.visible = True
                            if output_container_both.visible: output_container_both.update()
                            if generated_prompt_output.visible: generated_prompt_output.update()
                        
                        prompt_mode_sel.on_value_change(lambda: _update_prompt_output_visibility())
                        _update_prompt_output_visibility()
                        ui.label('Garde-fou: les métadonnées déjà présentes dans l’image ne sont jamais modifiées. Un prompt régénéré est stocké seulement dans le cache et/ou dans le fichier TXT.').classes('text-xs text-amber-300')
                        prompt_status_label = ui.label('Statut: prêt').classes('text-xs text-gray-400')
                        prompt_batch_label = ui.label('').classes('text-xs text-cyan-300')
                        prompt_busy_bar = ui.linear_progress(value=None).props('indeterminate color=cyan').classes('w-full')
                        prompt_busy_bar.visible = False

                        with ui.row().classes('w-full gap-2 items-center'):
                            ui.button('🔄 Actualiser modèles Ollama', on_click=refresh_ollama_models_action).props('outline color=cyan size=sm').bind_visibility_from(prompt_provider_sel, 'value', backward=lambda v: v == 'ollama')
                            ui.button('🛑 Annuler', on_click=_cancel_prompt_request_action).props('outline color=red size=sm')
                            ui.button('📝 Convertir en tags', on_click=prompt_to_tags_action).props('outline color=amber size=sm').classes('flex-grow')
                        with ui.row().classes('w-full gap-2 items-center'):
                            ui.button('✨ Générer un prompt', on_click=generate_prompt_action).props('outline color=blue size=sm').classes('flex-grow')
                            ui.button('💾 Sauvegarder le prompt', on_click=save_generated_prompt_action).props('outline color=teal size=sm').classes('flex-grow')
                            ui.button('📄 Exporter en TXT', on_click=save_prompts_txt_action).props('outline color=indigo size=sm').classes('flex-grow')
                            ui.button('🎯 Rechercher les tags', on_click=search_from_prompt_action).props('outline color=green size=sm').classes('flex-grow')

                def _prompt_results_loaded() -> bool:
                    return bool(state.prompt_results)

                def _update_prompt_primary_button():
                    btn_prompt_primary.text = '🔎 Rechercher les prompts' if _prompt_results_loaded() else '🖼️ Lire les images'
                    btn_prompt_primary.tooltip('Recherche dans les prompts déjà chargés' if _prompt_results_loaded() else 'Lit les images du dossier et charge la galerie Prompt')
                    btn_prompt_primary.update()

                async def load_prompt_images_action(force_reindex: bool = False):
                    save_config({
                        'prompt_dir': prompt_dir.value,
                        'chk_img_prompt': chk_img_prompt.value,
                        'chk_vid_prompt': chk_vid_prompt.value,
                        'prompt_text_filter': prompt_text_filter.value,
                        'prompt_hide_with_prompt': bool(state.prompt_hide_with_prompt),
                    })
                    if not prompt_dir.value:
                        return ui.notify('Indiquez un dossier !', type='warning')

                    exts = []
                    if chk_img_prompt.value: exts.extend(SUPPORTED_IMAGES)
                    if chk_vid_prompt.value: exts.extend(SUPPORTED_VIDEOS)
                    if not exts:
                        return ui.notify('Choisissez au moins un type de média.', type='warning')

                    state.is_processing = True
                    btn_prompt_primary.disable()
                    btn_prompt_reload.disable()
                    _set_prompt_status('lecture des images en cours...', busy=True)
                    _set_prompt_batch_progress('Analyse des médias et extraction des prompts...')
                    state.progress = 0.02

                    def bg_task():
                        try:
                            state.add_log(f"[PROMPT] Lecture des images : '{prompt_dir.value}'")
                            if force_reindex:
                                state.add_log('[PROMPT] Relecture forcée demandée.')
                                deleted_prompt, deleted_detailed = clear_prompt_folder_cache(prompt_dir.value)
                                state.add_log(
                                    f"[PROMPT] Cache réinitialisé: {deleted_prompt} prompt(s), {deleted_detailed} prompt(s) détaillé(s) supprimé(s)."
                                )
                        except Exception as e:
                            state.add_log(f"[PROMPT] Erreur lecture images: {e}")
                        finally:
                            state.is_processing = False

                    await run.io_bound(bg_task)
                    _set_prompt_status('chargement de la galerie Prompt...', busy=True)
                    _set_prompt_batch_progress('Lecture du cache prompt et construction de la galerie...')
                    state.progress = max(float(state.progress or 0.0), 0.75)
                    loaded = await run.io_bound(_load_all_prompt_results, prompt_dir.value, exts)
                    state.prompt_base_dir = prompt_dir.value
                    state.prompt_results = loaded
                    state.sel_prompt = {p: False for _s, p, _prompt, _detailed, _psrc, _dsrc in loaded}
                    state.prompt_page = 1
                    prompt_gallery_ui.refresh()
                    btn_prompt_primary.enable()
                    btn_prompt_reload.enable()
                    _set_prompt_status(f'{len(loaded)} média(s) chargé(s)', busy=False)
                    _set_prompt_batch_progress('')
                    state.status_text = 'Prêt !'
                    state.progress = 1.0
                    state.add_log(f"[PROMPT] Galerie chargée: {len(loaded)} média(s).")
                    _update_prompt_primary_button()

                async def search_prompt_action():
                    save_config({
                        'prompt_dir': prompt_dir.value,
                        'chk_img_prompt': chk_img_prompt.value,
                        'chk_vid_prompt': chk_vid_prompt.value,
                        'prompt_text_filter': prompt_text_filter.value,
                        'prompt_hide_with_prompt': bool(state.prompt_hide_with_prompt),
                    })
                    if not prompt_dir.value:
                        return ui.notify('Indiquez un dossier !', type='warning')

                    exts = []
                    if chk_img_prompt.value: exts.extend(SUPPORTED_IMAGES)
                    if chk_vid_prompt.value: exts.extend(SUPPORTED_VIDEOS)
                    if not exts:
                        return ui.notify('Choisissez au moins un type de média.', type='warning')

                    btn_prompt_primary.disable()
                    query = str(prompt_text_filter.value or '').strip().lower()
                    loaded = await run.io_bound(_load_all_prompt_results, prompt_dir.value, exts)

                    if query:
                        loaded = [
                            item for item in loaded
                            if query in str(item[2] or '').lower() or query in str(item[3] or '').lower()
                        ]

                    state.prompt_base_dir = prompt_dir.value
                    state.prompt_results = loaded
                    state.sel_prompt = {p: False for _s, p, _prompt, _detailed, _psrc, _dsrc in loaded}
                    state.prompt_page = 1
                    prompt_gallery_ui.refresh()
                    btn_prompt_primary.enable()
                    _update_prompt_primary_button()

                async def read_or_search_prompt_action():
                    if not _prompt_results_loaded():
                        await load_prompt_images_action(force_reindex=False)
                        return
                    await search_prompt_action()

                with ui.row().classes('w-full p-4 pt-2 shrink-0 border-t border-gray-800 bg-gray-900 z-10 gap-2'):
                    btn_prompt_reload = ui.button('♻️ Relire les images', on_click=lambda: load_prompt_images_action(True)).classes('w-full bg-orange-700 hover:bg-orange-600 font-bold').tooltip('Recharge les images et les prompts du dossier depuis les médias et le cache')
                    btn_prompt_primary = ui.button('🖼️ Lire les images', on_click=read_or_search_prompt_action).classes('w-full bg-cyan-700 hover:bg-cyan-600 font-bold')

                _update_prompt_primary_button()

            with ui.column().classes('flex-1 w-0 bg-gray-900 rounded-xl border border-gray-800 overflow-hidden h-full relative p-0'):
                prompt_gallery_ui()

        # ВКЛАДКА: ДЕТЕКТОР IA
        with ui.tab_panel(tab_ia).classes('w-full h-[calc(100vh-115px)] p-4 flex flex-row flex-nowrap items-stretch gap-4'):
            with ui.column().classes('w-[350px] shrink-0 bg-gray-900 rounded-xl border border-gray-800 shadow-lg flex flex-col overflow-hidden p-0 gap-0'):
                with ui.row().classes('w-full p-4 pb-2 shrink-0 border-b border-gray-800 bg-gray-900 z-10'):
                    ui.label('Détecteur IA').classes('text-lg font-bold')

                with ui.column().classes('w-full flex-1 overflow-y-auto p-4 gap-2 min-h-0'):
                    with ui.row().classes('w-full items-center gap-1 flex-nowrap'):
                        ia_dir = ui.input('Dossier', value=cfg.get('ia_dir', cfg.get('prompt_dir', ''))).classes('flex-grow')
                        ui.button(icon='folder', on_click=lambda: select_folder(ia_dir)).props('flat round dense')
                        ui.button(icon='delete_sweep', on_click=lambda: clear_folder_cache(ia_dir.value)).props('flat round dense text-color=red').tooltip('Effacer l\'index du dossier')

                    ui.label('Détecte si une image est générée par IA (3 passes : signatures de générateur, EXIF caméra, vision Ollama).').classes('text-xs text-gray-400')

                    with ui.expansion('Options Ollama (fallback vision)', icon='psychology', value=True).classes('w-full bg-gray-800/50 rounded-lg border border-pink-800 mt-3'):
                        ia_ollama_options = list(cfg.get('ollama_models_cached', []) or [])
                        ia_ollama_default = str(cfg.get('ia_ollama_model', cfg.get('tags_ollama_model', '')) or '').strip()
                        if ia_ollama_default not in ia_ollama_options:
                            ia_ollama_default = ia_ollama_options[0] if ia_ollama_options else None
                        ia_ollama_model_sel = ui.select(
                            ia_ollama_options,
                            value=ia_ollama_default,
                            label='Modèle Ollama (vision)'
                        ).classes('w-full')
                        ia_use_ollama = ui.switch('Utiliser Ollama en fallback', value=bool(cfg.get('ia_use_ollama', False))).classes('w-full text-sm text-pink-300')
                        ia_write_sidecar_auto = ui.switch('Écrire .ia sidecar à la détection', value=bool(cfg.get('ia_write_sidecar_auto', True))).classes('w-full text-sm text-gray-300')
                        ia_force_recompute = ui.switch('🔄 Recalculer même si déjà en cache', value=False).classes('w-full text-sm text-amber-300')

                        async def ia_refresh_ollama_models_action():
                            try:
                                models = await run.io_bound(_ollama_list_models)
                                ia_ollama_model_sel.options = models
                                if ia_ollama_model_sel.value not in models:
                                    ia_ollama_model_sel.value = models[0] if models else None
                                ia_ollama_model_sel.update()
                                ui.notify(f'{len(models)} modèle(s) Ollama trouvé(s)', type='positive')
                            except Exception as e:
                                ui.notify(f'Erreur Ollama: {e}', type='negative')

                        ui.button('🔄 Actualiser modèles Ollama', on_click=ia_refresh_ollama_models_action).props('outline color=pink size=sm').classes('w-full')

                    ia_status_label = ui.label('Statut: prêt').classes('text-xs text-gray-400 mt-2')
                    ia_progress_label = ui.label('').classes('text-xs text-pink-300')
                    ia_busy_bar = ui.linear_progress(value=None).props('indeterminate color=pink').classes('w-full')
                    ia_busy_bar.visible = False

                    def _ia_set_status(text, busy=False):
                        try:
                            ia_status_label.text = f'Statut: {text}'
                            ia_status_label.update()
                            ia_busy_bar.visible = busy
                            ia_busy_bar.update()
                        except Exception:
                            pass

                    def _ia_set_progress(text):
                        try:
                            ia_progress_label.text = text
                            ia_progress_label.update()
                        except Exception:
                            pass

                def _load_all_ia_results(directory):
                    """Charge tous les résultats de détection IA du dossier (cache + sidecar).

                    Force toujours un re-scan disque : l'utilisateur a explicitement
                    cliqué pour lire le dossier, on ne se fie pas au dir_cache.json
                    qui peut être obsolète après un déplacement/copie d'organizator.
                    """
                    all_files = search_engine._gather_files(directory, SUPPORTED_IMAGES, force_rescan=True)
                    seen, unique = set(), []
                    for path in all_files:
                        k = os.path.normcase(os.path.normpath(os.path.realpath(path)))
                        if k in seen: continue
                        seen.add(k); unique.append(path)
                    out = []
                    for path in unique:
                        det = search_engine.db_cache.get_ai_detection(path)
                        if det is None:
                            # Tente sidecar .ia
                            side = TagEngine._read_sidecar_ia(path)
                            if side and side.get('is_ai') is not None:
                                search_engine.db_cache.save_ai_detection(
                                    path,
                                    bool(side.get('is_ai')),
                                    float(side.get('confidence', 0.0) or 0.0),
                                    str(side.get('method', 'sidecar')),
                                    side,
                                )
                                det = search_engine.db_cache.get_ai_detection(path)
                        if det is None:
                            # Pas encore analysé : entrée "Inconnu"
                            out.append((0.0, path, None, 0.0, '', {}))
                        else:
                            is_ai_v = det.get('is_ai')
                            conf = float(det.get('confidence', 0.0) or 0.0)
                            # Score d'affichage : confiance, inversée pour photo (les + sûres en haut quand "↓ Score")
                            score = conf if is_ai_v else (conf * 0.5)
                            out.append((score, path, is_ai_v, conf, str(det.get('method', '')), det.get('detection', {})))
                    return out

                async def load_ia_action(force_reindex: bool = False):
                    save_config({
                        'ia_dir': ia_dir.value,
                        'ia_ollama_model': ia_ollama_model_sel.value,
                        'ia_use_ollama': bool(ia_use_ollama.value),
                        'ia_write_sidecar_auto': bool(ia_write_sidecar_auto.value),
                    })
                    if not ia_dir.value:
                        return ui.notify('Indiquez un dossier !', type='warning')
                    btn_ia_primary.disable(); btn_ia_reload.disable()
                    _ia_set_status('lecture du cache et sidecars .ia…', busy=True)
                    _ia_set_progress('')
                    if force_reindex:
                        # Re-scan disque + vider le cache IA pour ces fichiers
                        def _purge():
                            files = search_engine._gather_files(ia_dir.value, SUPPORTED_IMAGES, force_rescan=True)
                            for fp in files:
                                try: search_engine.db_cache.delete_ai_detection(fp)
                                except Exception: pass
                            return len(files)
                        n = await run.io_bound(_purge)
                        state.add_log(f"[IA] Cache disque + détection IA purgés pour {n} fichier(s).")
                    loaded = await run.io_bound(_load_all_ia_results, ia_dir.value)
                    state.ia_base_dir = ia_dir.value
                    state.ia_results = loaded
                    state.sel_ia = {p: False for _s, p, *_ in loaded}
                    state.ia_page = 1
                    ia_gallery_ui.refresh()
                    btn_ia_primary.enable(); btn_ia_reload.enable()
                    _ia_set_status(f'{len(loaded)} média(s) listé(s)', busy=False)
                    state.add_log(f"[IA] Galerie chargée : {len(loaded)} média(s).")

                async def detect_ia_action(only_unknown: bool = False):
                    """Lance la détection IA sur les fichiers sélectionnés (ou tous les inconnus)."""
                    if not state.ia_results:
                        return ui.notify("Charge d'abord le dossier.", type='warning')
                    if only_unknown:
                        targets = [p for _s, p, is_ai_v, *_ in state.ia_results if is_ai_v is None]
                    else:
                        targets = [p for p, v in state.sel_ia.items() if v]
                        if not targets:
                            return ui.notify('Rien de sélectionné. Astuce: bouton « Détecter tous les inconnus » à droite.', type='warning')
                    if not targets:
                        return ui.notify('Aucun fichier à traiter.', type='info')
                    ollama_model = str(ia_ollama_model_sel.value or '').strip()
                    use_ollama = bool(ia_use_ollama.value)
                    if use_ollama and not ollama_model:
                        ui.notify('Aucun modèle Ollama choisi : fallback désactivé pour ce run.', type='warning')
                        use_ollama = False
                    write_sidecar = bool(ia_write_sidecar_auto.value)
                    force = bool(ia_force_recompute.value)
                    btn_ia_detect_sel.disable(); btn_ia_detect_unknown.disable()
                    _ia_set_status(f'détection IA en cours sur {len(targets)} fichier(s)…', busy=True)

                    def _bg():
                        done = 0
                        for fp in targets:
                            try:
                                if not force:
                                    cached = search_engine.db_cache.get_ai_detection(fp)
                                    if cached is not None and cached.get('is_ai') is not None:
                                        done += 1
                                        msg = f'{done}/{len(targets)} (cache hit) — {os.path.basename(fp)}'
                                        _safe_ui_call(lambda m=msg: _ia_set_progress(m), 'ia_progress.cache')
                                        continue
                                res = run_ai_detection(fp, ollama_model=ollama_model, use_ollama_fallback=use_ollama)
                                if res.get('is_ai') is not None:
                                    search_engine.db_cache.save_ai_detection(
                                        fp,
                                        bool(res['is_ai']),
                                        float(res.get('confidence', 0.0) or 0.0),
                                        str(res.get('method', '')),
                                        res,
                                    )
                                    if write_sidecar:
                                        TagEngine._write_sidecar_ia(fp, res)
                                done += 1
                                msg = f'{done}/{len(targets)} — {os.path.basename(fp)} → {res.get("method","")}'
                                _safe_ui_call(lambda m=msg: _ia_set_progress(m), 'ia_progress.update')
                            except Exception as e:
                                state.add_log(f"[IA] Erreur sur {fp}: {e}")
                        return done

                    n_done = await run.io_bound(_bg)
                    _ia_verdict_cache.clear()
                    # Recharger les résultats
                    loaded = await run.io_bound(_load_all_ia_results, state.ia_base_dir or ia_dir.value)
                    state.ia_results = loaded
                    state.sel_ia = {p: state.sel_ia.get(p, False) for _s, p, *_ in loaded}
                    ia_gallery_ui.refresh()
                    btn_ia_detect_sel.enable(); btn_ia_detect_unknown.enable()
                    _ia_set_status(f'terminé : {n_done} fichier(s) traité(s)', busy=False)
                    _ia_set_progress('')
                    state.add_log(f"[IA] Détection terminée : {n_done} fichier(s).")

                async def write_ia_sidecars_action():
                    """Écrit les sidecars .ia pour la sélection (ou tous ceux qui ont un verdict)."""
                    targets = [p for p, v in state.sel_ia.items() if v]
                    if not targets:
                        # Tous ceux avec verdict
                        targets = [p for _s, p, is_ai_v, *_ in state.ia_results if is_ai_v is not None]
                    if not targets:
                        return ui.notify('Aucun verdict à écrire.', type='warning')
                    def _bg():
                        n = 0
                        for fp in targets:
                            det = search_engine.db_cache.get_ai_detection(fp)
                            if det is None or det.get('is_ai') is None: continue
                            payload = det.get('detection') or {
                                'is_ai': det.get('is_ai'),
                                'confidence': det.get('confidence'),
                                'method': det.get('method'),
                                'detected_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                            }
                            if TagEngine._write_sidecar_ia(fp, payload):
                                n += 1
                        return n
                    n_done = await run.io_bound(_bg)
                    ui.notify(f'{n_done} sidecar(s) .ia écrit(s)', type='positive')
                    state.add_log(f"[IA] {n_done} sidecar(s) .ia écrit(s).")

                async def manual_mark_action(verdict):
                    """Force un verdict (True=IA, False=Photo, None=Inconnu) sur la sélection.
                    Met à jour le cache, écrit le sidecar .ia si activé, puis rafraîchit la galerie."""
                    targets = [p for p, v in state.sel_ia.items() if v]
                    if not targets:
                        return ui.notify('Aucune sélection.', type='warning')
                    write_sidecar = bool(ia_write_sidecar_auto.value)
                    label = '🤖 IA' if verdict is True else ('📷 Photo' if verdict is False else '❓ Inconnu')
                    def _bg():
                        n = 0
                        for fp in targets:
                            try:
                                if verdict is None:
                                    # Effacer le verdict : suppression du cache + sidecar
                                    search_engine.db_cache.delete_ai_detection(fp)
                                    try:
                                        sp = os.path.splitext(fp)[0] + '.ia'
                                        if os.path.exists(sp): os.remove(sp)
                                    except Exception: pass
                                else:
                                    payload = {
                                        'is_ai': bool(verdict),
                                        'confidence': 1.0,
                                        'method': 'manual_override',
                                        'detected_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                                    }
                                    search_engine.db_cache.save_ai_detection(fp, bool(verdict), 1.0, 'manual_override', payload)
                                    if write_sidecar:
                                        TagEngine._write_sidecar_ia(fp, payload)
                                n += 1
                            except Exception as ex:
                                state.add_log(f"[IA] manual_mark {fp}: {ex}")
                        return n
                    _ia_set_status(f'marquage {label} en cours...', busy=True)
                    n_done = await run.io_bound(_bg)
                    _ia_verdict_cache.clear()
                    # Recharge la liste pour refléter
                    loaded = await run.io_bound(_load_all_ia_results, state.ia_base_dir or ia_dir.value)
                    state.ia_results = loaded
                    state.sel_ia = {p: state.sel_ia.get(p, False) for _s, p, *_ in loaded}
                    state.ia_page = 1
                    _ia_set_status(f'{n_done} fichier(s) marqué(s) {label}', busy=False)
                    ui.notify(f'{n_done} fichier(s) → {label}', type='positive')
                    state.add_log(f"[IA] {n_done} fichier(s) marqué(s) manuellement {label}.")
                    _refresh_gallery('ia')

                with ui.row().classes('w-full gap-2 mt-2'):
                    btn_ia_detect_sel = ui.button('🔍 Détecter (sélection)', on_click=lambda: detect_ia_action(False)).props('outline color=pink size=sm').classes('flex-grow')
                    btn_ia_detect_unknown = ui.button('❓ Détecter tous les inconnus', on_click=lambda: detect_ia_action(True)).props('outline color=amber size=sm').classes('flex-grow')
                ui.separator().classes('my-2 bg-gray-700')
                ui.label('Marquage manuel (sélection)').classes('text-xs text-gray-400 uppercase')
                with ui.row().classes('w-full gap-2'):
                    ui.button('🤖 Marquer IA', on_click=lambda: manual_mark_action(True)).props('color=pink size=sm').classes('flex-grow').tooltip('Force le verdict IA sur la sélection')
                    ui.button('📷 Marquer Photo', on_click=lambda: manual_mark_action(False)).props('color=emerald size=sm').classes('flex-grow').tooltip('Force le verdict Photo réelle sur la sélection')
                with ui.row().classes('w-full gap-2'):
                    ui.button('❓ Effacer verdict', on_click=lambda: manual_mark_action(None)).props('outline color=gray size=sm').classes('flex-grow').tooltip('Supprime le verdict (cache + sidecar) — repassera en Inconnu')
                    ui.button('💾 Écrire .ia (sélection)', on_click=write_ia_sidecars_action).props('outline color=teal size=sm').classes('flex-grow')

                with ui.row().classes('w-full p-4 pt-2 shrink-0 border-t border-gray-800 bg-gray-900 z-10 gap-2'):
                    btn_ia_reload = ui.button('♻️ Vider et recharger', on_click=lambda: load_ia_action(True)).classes('w-full bg-orange-700 hover:bg-orange-600 font-bold').tooltip('Vide le cache IA et relit le dossier')
                    btn_ia_primary = ui.button('🖼️ Lire les images', on_click=lambda: load_ia_action(False)).classes('w-full bg-pink-700 hover:bg-pink-600 font-bold')

            with ui.column().classes('flex-1 w-0 bg-gray-900 rounded-xl border border-gray-800 overflow-hidden h-full relative p-0'):
                ia_gallery_ui()

        # ВКЛАДКА 6: ИНДЕКСАТОР (Кэш)
        with ui.tab_panel(tab_cache).classes('w-full h-[calc(100vh-115px)] p-8 flex flex-col items-center overflow-y-auto pb-24'):
            with ui.card().classes('w-full max-w-[600px] p-6 flex flex-col gap-4 bg-gray-900 border border-gray-800 shrink-0 mb-12 mt-4'):
                ui.label('Indexation de masse (pre-cache)').classes('text-xl font-bold text-blue-400')
                ui.label('Utilisez ceci pour pre-analyser tout le dossier sans lancer de recherche. Tous les signaux IA seront sauvegardes en base.').classes('text-gray-400 text-sm')
                ui.label("✅ Détection auto des nouveaux fichiers : relancez après organizator, seuls les nouveaux médias seront traités (les médias déjà en cache sont sautés).").classes('text-green-400 text-xs italic')
                
                with ui.row().classes('w-full items-center gap-2 mt-2'):
                    cache_dir = ui.input('Dossier a indexer', value=cfg.get('cache_dir', '')).classes('flex-grow')
                    ui.button(icon='folder', on_click=lambda: select_folder(cache_dir)).props('flat round dense')
                    ui.button(icon='delete_sweep', on_click=lambda: clear_folder_cache(cache_dir.value)).props('flat round dense text-color=red').tooltip('Effacer l\'index du dossier')
                
                with ui.row().classes('w-full gap-2 border-b border-gray-800 pb-4'):
                    chk_cache_img = ui.checkbox('Images', value=cfg.get('chk_cache_img', True))
                    chk_cache_vid = ui.checkbox('Videos', value=cfg.get('chk_cache_vid', True))
                    chk_cache_txt = ui.checkbox('Texte (recherche uniquement)', value=cfg.get('chk_cache_txt', False))

                ui.label('Choisissez quoi mettre en cache :').classes('font-bold mt-2')
                
                chk_cache_search = ui.checkbox('Recherche intelligente (Qwen Embeddings)', value=cfg.get('chk_cache_search', True)).classes('text-md font-bold text-blue-300')
                with ui.row().classes('w-full pl-6 pr-6 items-center gap-2').bind_visibility_from(chk_cache_search, 'value'):
                    emb_model_cache = ui.select(['Qwen/Qwen3-VL-Embedding-2B', 'Qwen/Qwen3-VL-Embedding-8B'], value=cfg.get('emb_model', 'Qwen/Qwen3-VL-Embedding-2B')).classes('flex-1')
                    cache_search_quant = ui.select(['None', '8-bit', '4-bit'], value=cfg.get('search_quant_mode', 'None'), label='Quant.').classes('w-24')
                
                chk_cache_aes = ui.checkbox('Evaluation esthetique', value=cfg.get('chk_cache_aes', True)).classes('text-md font-bold text-yellow-300')
                with ui.row().classes('w-full pl-6 pr-6 items-center gap-2').bind_visibility_from(chk_cache_aes, 'value'):
                    cache_aes_quant = ui.select(['None', '8-bit', '4-bit'], value=cfg.get('aes_quant_mode', 'None'), label='Quant.').classes('w-24')
                
                chk_cache_nsfw = ui.checkbox('Detecteur NSFW', value=cfg.get('chk_cache_nsfw', True)).classes('text-md font-bold text-red-300')
                with ui.row().classes('w-full pl-6 pr-6 items-center gap-2').bind_visibility_from(chk_cache_nsfw, 'value'):
                    nsfw_model_cache = ui.select(['prithivMLmods/siglip2-x256-explicit-content', 'strangerguardhf/nsfw-image-detection'], value=cfg.get('nsfw_model', DEFAULT_NSFW_MODEL)).classes('flex-1')
                    cache_nsfw_quant = ui.select(['None', '8-bit', '4-bit'], value=cfg.get('nsfw_quant_mode', 'None'), label='Quant.').classes('w-24')

                chk_cache_tags = ui.checkbox('Tagging (Danbooru)', value=cfg.get('chk_cache_tags', True)).classes('text-md font-bold text-pink-300')
                with ui.row().classes('w-full pl-6 pr-6 items-center gap-2').bind_visibility_from(chk_cache_tags, 'value'):
                    tags_model_cache = ui.select([
                        'SmilingWolf/wd-swinv2-tagger-v3', 'SmilingWolf/wd-convnext-tagger-v3', 
                        'SmilingWolf/wd-eva02-large-tagger-v3', 'SmilingWolf/wd-vit-tagger-v3', 
                        'Camais03/camie-tagger-v2', 'fancyfeast/joytag'
                    ], value=cfg.get('tags_model', 'SmilingWolf/wd-swinv2-tagger-v3')).classes('flex-1')

                chk_cache_face = ui.checkbox('Recherche par visage (InsightFace)', value=cfg.get('chk_cache_face', False)).classes('text-md font-bold text-teal-300')

                chk_cache_ia = ui.checkbox('Détecteur IA (sidecar .ia)', value=cfg.get('chk_cache_ia', False)).classes('text-md font-bold text-purple-300')
                with ui.column().classes('w-full pl-6 pr-6 gap-1').bind_visibility_from(chk_cache_ia, 'value'):
                    chk_cache_ia_use_ollama = ui.checkbox('Utiliser Ollama en secours (sinon EXIF/PNG metadata seul)', value=cfg.get('chk_cache_ia_use_ollama', False)).classes('text-xs')
                    with ui.row().classes('w-full items-center gap-2'):
                        _ollama_cached = cfg.get('_ollama_models_cached') or []
                        _ia_model_val = cfg.get('ia_ollama_model', '') or ''
                        _ia_options = _ollama_cached if _ollama_cached else ([_ia_model_val] if _ia_model_val else [])
                        cache_ia_ollama_model = ui.select(_ia_options, value=_ia_model_val if _ia_model_val in _ia_options else None, label='Modele Ollama (IA)', with_input=True).classes('flex-grow')
                        def _refresh_ollama_cache_ia():
                            models = _ollama_list_models()
                            save_config({'_ollama_models_cached': models})
                            cache_ia_ollama_model.set_options(models, value=cache_ia_ollama_model.value if cache_ia_ollama_model.value in models else None)
                            cache_prompt_ollama_model.set_options(models, value=cache_prompt_ollama_model.value if cache_prompt_ollama_model.value in models else None)
                            ui.notify(f'{len(models)} modele(s) Ollama detecte(s)', type='positive')
                        ui.button(icon='refresh', on_click=_refresh_ollama_cache_ia).props('flat round dense').tooltip('Recharger la liste Ollama')

                chk_cache_prompt = ui.checkbox('Génération prompt (sidecar _prompt.txt)', value=cfg.get('chk_cache_prompt', False)).classes('text-md font-bold text-indigo-300')
                with ui.column().classes('w-full pl-6 pr-6 gap-1').bind_visibility_from(chk_cache_prompt, 'value'):
                    with ui.row().classes('w-full items-center gap-2'):
                        cache_prompt_provider = ui.select(['local', 'ollama'], value=cfg.get('cache_prompt_provider', 'local'), label='Provider').classes('w-32')
                        cache_prompt_mode = ui.select(['raw', 'detailed', 'both'], value=cfg.get('cache_prompt_mode', 'both'), label='Mode').classes('w-32')
                        _pp_model_val = cfg.get('tags_ollama_model', '') or ''
                        _pp_options = _ollama_cached if _ollama_cached else ([_pp_model_val] if _pp_model_val else [])
                        cache_prompt_ollama_model = ui.select(_pp_options, value=_pp_model_val if _pp_model_val in _pp_options else None, label='Modele Ollama', with_input=True).classes('flex-grow')
                    cache_prompt_force = ui.checkbox('Forcer regeneration (ignorer cache)', value=cfg.get('cache_prompt_force', False)).classes('text-xs')

                with ui.expansion('Parametres avances unifies', icon='tune').classes('w-full bg-gray-800/50 rounded-lg border border-gray-700 mt-4'):
                    with ui.row().classes('w-full gap-4 px-4 pt-4'):
                        cache_batch_size = ui.number('Taille lot', value=cfg.get('batch_size', 16), format='%.0f').classes('flex-1')
                        cache_video_frames = ui.number('Frames video', value=cfg.get('video_frames', 4), format='%.0f').classes('flex-1')
                    with ui.row().classes('w-full gap-4 px-4 pb-4'):
                        cache_max_dim = ui.number('Limite resolution', value=cfg.get('emb_size', 512), format='%.0f').classes('flex-1')
                
                with ui.expansion('Optimisation memoire (RAM)', icon='memory').classes('w-full bg-gray-800/50 rounded-lg border border-gray-700 mt-2'):
                    with ui.column().classes('w-full gap-2 px-4 py-4'):
                        ui.label('Ces parametres evitent les plantages OOM sur les gros dossiers.').classes('text-gray-400 text-xs')
                        use_ram_compression = ui.checkbox('Compression du cache en RAM', value=cfg.get('use_ram_compression', False)).classes('text-sm text-green-400 font-bold')
                        ui.label('Architecture par blocs (reduire fortement la RAM)').classes('font-bold text-sm mt-2')
                        cache_chunk_size = ui.number('Taille bloc fichiers (0 = desactive, recommande = 2000)', value=cfg.get('cache_chunk_size', 2000), format='%.0f').classes('w-full')

                async def run_cache_action():
                    save_config({
                        'cache_dir': cache_dir.value, 'chk_cache_img': chk_cache_img.value, 
                        'chk_cache_vid': chk_cache_vid.value, 'chk_cache_txt': chk_cache_txt.value,
                        'chk_cache_search': chk_cache_search.value, 'chk_cache_aes': chk_cache_aes.value, 
                        'chk_cache_nsfw': chk_cache_nsfw.value, 'chk_cache_face': chk_cache_face.value,
                        'chk_cache_tags': chk_cache_tags.value,
                        'chk_cache_ia': chk_cache_ia.value,
                        'chk_cache_ia_use_ollama': chk_cache_ia_use_ollama.value,
                        'ia_ollama_model': cache_ia_ollama_model.value,
                        'chk_cache_prompt': chk_cache_prompt.value,
                        'cache_prompt_provider': cache_prompt_provider.value,
                        'cache_prompt_mode': cache_prompt_mode.value,
                        'tags_ollama_model': cache_prompt_ollama_model.value,
                        'cache_prompt_force': cache_prompt_force.value,
                        'search_quant_mode': cache_search_quant.value, 'aes_quant_mode': cache_aes_quant.value,
                        'nsfw_quant_mode': cache_nsfw_quant.value,
                        'use_ram_compression': use_ram_compression.value, 'cache_chunk_size': cache_chunk_size.value
                    })
                    if not cache_dir.value: return ui.notify("Indiquez un dossier !", type='warning')
                    
                    state.is_processing = True
                    search_engine.cancel_flag = False
                    btn_cache.disable()
                    
                    def bg_task():
                        try:
                            state.add_log(f"Debut du cycle complet de cache pour le dossier : '{cache_dir.value}'")
                            
                            # Configuration cache RAM (option 1)
                            media_cache.enabled = True
                            media_cache.compress = use_ram_compression.value
                            chunk_size = int(cache_chunk_size.value)
                            
                            exts_search =[]
                            if chk_cache_img.value: exts_search.extend(SUPPORTED_IMAGES)
                            if chk_cache_vid.value: exts_search.extend(SUPPORTED_VIDEOS)
                            if chk_cache_txt.value: exts_search.extend(SUPPORTED_TEXTS)
                            
                            exts_media =[]
                            if chk_cache_img.value: exts_media.extend(SUPPORTED_IMAGES)
                            if chk_cache_vid.value: exts_media.extend(SUPPORTED_VIDEOS)

                            search_engine.emb_size = int(cache_max_dim.value)
                            search_engine.video_frames = int(cache_video_frames.value)
                            search_engine.quant_mode = cache_search_quant.value
                            
                            aesthetic_engine.batch_size = int(cache_batch_size.value)
                            aesthetic_engine.max_dim = int(cache_max_dim.value)
                            aesthetic_engine.video_frames = int(cache_video_frames.value)
                            aesthetic_engine.quant_mode = cache_aes_quant.value
                            
                            nsfw_engine.batch_size = int(cache_batch_size.value)
                            nsfw_engine.max_dim = int(cache_max_dim.value)
                            nsfw_engine.video_frames = int(cache_video_frames.value)
                            nsfw_engine.quant_mode = cache_nsfw_quant.value
                            
                            tag_engine.batch_size = max(1, int(cache_batch_size.value) // 2) # Les taggers consomment plus de VRAM, on reduit un peu
                            tag_engine.video_frames = int(cache_video_frames.value)

                            face_engine.batch_size = int(cache_batch_size.value)

                            all_allowed_exts = tuple(set(exts_search + exts_media))
                            all_files_for_index = search_engine._gather_files(cache_dir.value, all_allowed_exts)
                            
                            if not all_files_for_index:
                                state.add_log("⚠️ Aucun fichier compatible trouve pour la mise en cache.")
                                return

                            if chunk_size > 0:
                                chunks =[all_files_for_index[i:i + chunk_size] for i in range(0, len(all_files_for_index), chunk_size)]
                            else:
                                chunks =[all_files_for_index]

                            state.add_log(f"Total fichiers : {len(all_files_for_index)}. Repartition en {len(chunks)} bloc(s).")

                            for idx, chunk in enumerate(chunks):
                                if search_engine.cancel_flag: break
                                if len(chunks) > 1:
                                    state.add_log(f"🔄 === TRAITEMENT DU BLOC {idx+1}/{len(chunks)} ({len(chunk)} fichiers) === 🔄")
                                
                                # Nettoyage cache RAM avant chaque bloc (option 3)
                                media_cache.clear()

                                if chk_cache_search.value and not search_engine.cancel_flag:
                                    if len(chunks) > 1: state.add_log(f"-> Bloc {idx+1}: Mise en cache recherche intelligente")
                                    else: state.add_log(f"-> Etape 1: Mise en cache recherche intelligente")
                                    nsfw_engine.unload()
                                    aesthetic_engine.unload()
                                    face_engine.unload()
                                    tag_engine.unload()
                                    search_engine.build_cache(cache_dir.value, emb_model_cache.value, int(cache_batch_size.value), tuple(exts_search), override_files=chunk)
                                    
                                if chk_cache_aes.value and not search_engine.cancel_flag:
                                    if len(chunks) > 1: state.add_log(f"-> Bloc {idx+1}: Evaluation esthetique")
                                    else: state.add_log(f"-> Etape 2: Evaluation esthetique")
                                    search_engine._unload_embedding_model()
                                    nsfw_engine.unload()
                                    face_engine.unload()
                                    tag_engine.unload()
                                    aesthetic_engine.evaluate_media(cache_dir.value, tuple(exts_media), override_files=chunk)
                                    
                                if chk_cache_nsfw.value and not search_engine.cancel_flag:
                                    if len(chunks) > 1: state.add_log(f"-> Bloc {idx+1}: Detecteur NSFW")
                                    else: state.add_log(f"-> Etape 3: Detecteur NSFW")
                                    search_engine._unload_embedding_model()
                                    aesthetic_engine.unload()
                                    face_engine.unload()
                                    tag_engine.unload()
                                    nsfw_engine.evaluate_media(cache_dir.value, nsfw_model_cache.value, tuple(exts_media), override_files=chunk)

                                if chk_cache_tags.value and not search_engine.cancel_flag:
                                    if len(chunks) > 1: state.add_log(f"-> Bloc {idx+1}: Tagging (Danbooru)")
                                    else: state.add_log(f"-> Etape 4: Tagging (Danbooru)")
                                    search_engine._unload_embedding_model()
                                    aesthetic_engine.unload()
                                    face_engine.unload()
                                    nsfw_engine.unload()
                                    tag_engine.evaluate_media(cache_dir.value, tags_model_cache.value, tuple(exts_media), override_files=chunk)

                                if chk_cache_face.value and not search_engine.cancel_flag:
                                    if len(chunks) > 1: state.add_log(f"-> Bloc {idx+1}: Cache visages (InsightFace)")
                                    else: state.add_log(f"-> Etape 5: Cache visages (InsightFace)")
                                    search_engine._unload_embedding_model()
                                    aesthetic_engine.unload()
                                    nsfw_engine.unload()
                                    tag_engine.unload()
                                    face_engine.build_cache(cache_dir.value, tuple(exts_media), override_files=chunk)

                                # Etape 6 : Detection IA (images uniquement, sidecar .ia auto)
                                if chk_cache_ia.value and not search_engine.cancel_flag:
                                    if len(chunks) > 1: state.add_log(f"-> Bloc {idx+1}: Detection IA")
                                    else: state.add_log(f"-> Etape 6: Detection IA")
                                    search_engine._unload_embedding_model()
                                    aesthetic_engine.unload()
                                    nsfw_engine.unload()
                                    tag_engine.unload()
                                    face_engine.unload()
                                    images_only = [p for p in chunk if p.lower().endswith(SUPPORTED_IMAGES)]
                                    bulk_run_ia_detection(
                                        images_only,
                                        ollama_model=str(cache_ia_ollama_model.value or '').strip(),
                                        use_ollama=bool(chk_cache_ia_use_ollama.value),
                                        force=False,
                                    )

                                # Etape 7 : Generation prompt (sidecar _prompt.txt auto)
                                if chk_cache_prompt.value and not search_engine.cancel_flag:
                                    if len(chunks) > 1: state.add_log(f"-> Bloc {idx+1}: Generation prompt")
                                    else: state.add_log(f"-> Etape 7: Generation prompt")
                                    search_engine._unload_embedding_model()
                                    aesthetic_engine.unload()
                                    nsfw_engine.unload()
                                    tag_engine.unload()
                                    face_engine.unload()
                                    images_only = [p for p in chunk if p.lower().endswith(SUPPORTED_IMAGES)]
                                    bulk_run_prompt_generation(
                                        images_only,
                                        provider=str(cache_prompt_provider.value or 'local').strip().lower(),
                                        model=str(cache_prompt_ollama_model.value or '').strip(),
                                        mode=str(cache_prompt_mode.value or 'both').strip().lower(),
                                        force=bool(cache_prompt_force.value),
                                    )

                            state.add_log("🎉 Indexation complete terminee avec succes !")
                        except Exception as e: state.add_log(f"❌ Erreur d'indexation : {e}")
                        finally:
                            media_cache.enabled = False
                            media_cache.compress = False
                            media_cache.clear()
                            state.is_processing = False
                            state.progress = 1.0
                            state.status_text = "Prêt !"

                    await run.io_bound(bg_task)
                    btn_cache.enable()

                btn_cache = ui.button('🚀 Lancer indexation complete', on_click=run_cache_action).classes('w-full bg-blue-600 hover:bg-blue-500 font-bold text-lg mt-4')

                # --- Panneau de journaux precis inline ---
                with ui.card().classes('w-full mt-4 bg-gray-900 border border-gray-700 p-3'):
                    with ui.row().classes('w-full items-center justify-between mb-2'):
                        ui.label('📜 Journaux precis (en direct)').classes('text-sm font-bold text-blue-300')
                        with ui.row().classes('gap-1 items-center'):
                            cache_log_filter = ui.input(placeholder='Filtre (substring)').props('dense outlined dark').classes('w-48')
                            def _copy_cache_log():
                                ui.clipboard.write('\n'.join(state.full_log_history))
                                ui.notify('Journaux copies !', type='positive')
                            def _clear_cache_log():
                                cache_log_element.clear()
                                ui.notify('Panneau efface (historique global conserve)', type='info')
                            ui.button(icon='content_copy', on_click=_copy_cache_log).props('flat round dense text-color=gray').tooltip('Copier tout l\'historique')
                            ui.button(icon='delete_sweep', on_click=_clear_cache_log).props('flat round dense text-color=red').tooltip('Vider le panneau')
                    cache_log_element = ui.log(max_lines=2000).classes('w-full h-72 bg-black text-green-400 font-mono text-xs p-2 rounded overflow-y-auto whitespace-pre-wrap break-words')

                    class _FilteredLog:
                        def __init__(self, inner, filter_input):
                            self.inner = inner
                            self.filter_input = filter_input
                        def push(self, msg):
                            f = (self.filter_input.value or '').strip().lower()
                            if f and f not in msg.lower():
                                return
                            try: self.inner.push(msg)
                            except Exception: pass
                        def clear(self):
                            try: self.inner.clear()
                            except Exception: pass

                    register_log_panel(_FilteredLog(cache_log_element, cache_log_filter))
                    for _msg in state.full_log_history[-500:]:
                        try: cache_log_element.push(_msg)
                        except Exception: pass

    with ui.footer().classes('bg-gray-900 border-t border-gray-800 px-4 py-0 flex flex-row flex-nowrap items-center justify-between z-40 h-8 shadow-lg'):
        ui.label().bind_text_from(state, 'status_text').classes('text-blue-400 font-mono text-xs truncate max-w-[30%] shrink-0')
        ui.linear_progress(value=0, show_value=False).bind_value_from(state, 'progress').classes('flex-grow mx-4 h-1.5 rounded text-blue-600')
        
        ui.button('ARRÊTER', icon='cancel', on_click=cancel_all_tasks) \
            .props('color=red size=sm dense outline') \
            .classes('shrink-0 py-0 min-h-0 text-xs font-bold mr-2 bg-red-900/20') \
            .bind_visibility_from(state, 'is_processing')
            
        ui.button('JOURNAUX', icon='terminal', on_click=log_drawer.toggle).props('flat text-color=white size=sm dense').classes('shrink-0 py-0 min-h-0 text-xs')

    ui.timer(0.5, update_ui_logs)

if __name__ in {"__main__", "__mp_main__"}:
    parser = argparse.ArgumentParser(description="AI Media Organizer Pro")
    parser.add_argument('--server-only', action='store_true', help='Lancer en mode serveur (sans fenetre locale)')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Adresse IP du serveur')
    parser.add_argument('--port', type=int, default=8190, help='Port du serveur')
    
    args, unknown = parser.parse_known_args()

    if args.server_only:
        print(f"🌐 Mode serveur active. Ouvrez dans le navigateur : http://{args.host}:{args.port}")
        # native=False отключает десктопное окно, show=False предотвращает автоматическое открытие вкладки
        ui.run(title="AI Media Organizer Pro", host=args.host, port=args.port, native=False, show=False, dark=True, reload=False)
    else:
        # Стандартный оконный (Native) режим
        ui.run(title="AI Media Organizer Pro", port=args.port, native=True, dark=True, window_size=(1400, 900), reload=False)


