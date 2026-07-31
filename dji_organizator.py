"""DJI Organizator — Classement automatique des médias de drones DJI.

Détecte le drone (Mini2 MEO, Neo2 CLEO, Avata2 GINO, Mini4 Pro PEDRO), la catégorie
(VIDEO / PHOTO / PANORAMA / HYPERLAPSE) et copie vers une arborescence datée.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# PATCH pywebview / multiprocessing — MUST run BEFORE nicegui import
# ─────────────────────────────────────────────────────────────────────────────
# Fix "concurrent send_bytes() calls are not supported" causé par pywebview
# qui émet plusieurs events (moved/resized/loaded) depuis des threads différents
# sur la même multiprocessing.Connection. On sérialise avec un lock global.
# Doit être exécuté au top-level du module pour être actif aussi dans le
# processus enfant NiceGUI (__mp_main__).
def _patch_mp_connection_thread_safety() -> None:
    try:
        import multiprocessing.connection as _mpc
        if getattr(_mpc, "_dji_send_patched", False):
            return
        _orig_send = _mpc.Connection.send
        _lock = threading.Lock()

        def _safe_send(self, obj):  # type: ignore
            with _lock:
                try:
                    return _orig_send(self, obj)
                except (ValueError, BrokenPipeError, OSError):
                    return None  # canal fermé / concurrent — ignorer

        _mpc.Connection.send = _safe_send  # type: ignore
        _mpc._dji_send_patched = True  # type: ignore
    except Exception:
        pass


_patch_mp_connection_thread_safety()

# ─────────────────────────────────────────────────────────────────────────────
# Dépendances tierces (installées via run_dji_organizator.bat)
# ─────────────────────────────────────────────────────────────────────────────
try:
    import exiftool  # type: ignore
except ImportError:
    exiftool = None  # gestion douce plus bas

try:
    from send2trash import send2trash  # type: ignore
except ImportError:
    send2trash = None

try:
    from PIL import Image  # type: ignore
except ImportError:
    Image = None

try:
    import cv2  # type: ignore
except ImportError:
    cv2 = None

from nicegui import app, ui, run, background_tasks

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────────────────────────────────────
APP_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = APP_DIR / "dji_config.json"
EXIFTOOL_PATH = APP_DIR / ".tools" / "exiftool" / "exiftool.exe"
THUMB_CACHE_DIR = APP_DIR / ".cache_dji_thumbs"
THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SOURCE = str(APP_DIR / "00-DJI-A-TRIER")
DEFAULT_DEST = str(APP_DIR)

# Extensions média principales
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v"}
PHOTO_EXTS = {".jpg", ".jpeg", ".dng", ".raw", ".png", ".tiff", ".tif", ".heic", ".heif"}
COMPANION_EXTS = {".srt", ".lrf", ".lut", ".cube", ".xmp", ".thm", ".wav"}
ALL_MEDIA_EXTS = VIDEO_EXTS | PHOTO_EXTS

# Mapping drone : (nom identifiant, dossier destination)
DRONE_MAPPING = [
    # (regex sur EXIF Model, id, dossier)
    (r"mini\s*2(?!\s*pro)", "MINI2-MEO", "00-DJI-MINI2-MEO"),
    (r"neo", "NEO2-CLEO", "00-DJI-NEO2-CLEO"),
    (r"avata\s*2", "AVATA2-GINO", "00-DJI-AVATA2-GINO"),
    (r"mini\s*4\s*pro|fc8482", "MINI4PRO-PEDRO", "00-DJI-MINI4PRO-PEDRO"),
]
UNKNOWN_DRONE_DIR = "00-DJI-UNKNOWN"

CATEGORIES = ["VIDEO", "PHOTO", "PANORAMA", "HYPERLAPSE"]

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG: dict[str, Any] = {
    "source_dir": DEFAULT_SOURCE,
    "destination_dir": DEFAULT_DEST,
    "drone_mapping": [
        {"pattern": r"mini\s*2(?!\s*pro)", "id": "MINI2-MEO", "folder": "00-DJI-MINI2-MEO"},
        {"pattern": r"neo", "id": "NEO2-CLEO", "folder": "00-DJI-NEO2-CLEO"},
        {"pattern": r"avata\s*2", "id": "AVATA2-GINO", "folder": "00-DJI-AVATA2-GINO"},
        {"pattern": r"mini\s*4\s*pro|fc8482", "id": "MINI4PRO-PEDRO", "folder": "00-DJI-MINI4PRO-PEDRO"},
    ],
    "send_to_trash_after_copy": True,
    "window_size": [1500, 950],
}


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge avec defaults (nouvelles clés)
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception as e:
            print(f"⚠️ Erreur lecture config: {e}, utilisation des defaults")
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict[str, Any]) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Erreur sauvegarde config: {e}")


CONFIG = load_config()


# ─────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class MediaUnit:
    """Un média principal + ses fichiers compagnons (SRT, LRF, LUT, XMP…)."""
    main_path: str
    companions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)  # dump exiftool complet
    drone_id: str = "UNKNOWN"           # ex "MINI4PRO-PEDRO"
    drone_folder: str = UNKNOWN_DRONE_DIR
    category: str = "VIDEO"             # VIDEO / PHOTO / PANORAMA / HYPERLAPSE
    capture_date: str = ""              # YYYY-MM-DD
    group_subdir: str = ""              # sous-dossier de groupe (PANO_001, HYPER_002…)
    action: str = "move"                # move | delete | skip
    detection_reason: str = ""
    error: str = ""

    @property
    def key(self) -> str:
        return self.main_path

    @property
    def all_files(self) -> list[str]:
        return [self.main_path, *self.companions]

    @property
    def total_size(self) -> int:
        total = 0
        for p in self.all_files:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTEUR DE MÉTADONNÉES (exiftool)
# ─────────────────────────────────────────────────────────────────────────────
class DJIMetadataExtractor:
    """Wrapper autour d'ExifTool pour extraction batch de métadonnées complètes."""

    def __init__(self, exiftool_bin: Optional[Path] = None) -> None:
        self.bin = Path(exiftool_bin) if exiftool_bin else EXIFTOOL_PATH
        if not self.bin.exists():
            raise FileNotFoundError(
                f"ExifTool introuvable : {self.bin}. "
                "Relancez run_dji_organizator.bat pour le télécharger automatiquement."
            )
        if exiftool is None:
            raise ImportError(
                "Le module Python PyExifTool n'est pas installé. "
                "Relancez run_dji_organizator.bat."
            )

    def extract_batch(
        self,
        files: list[str],
        progress_cb: Optional[callable] = None,
    ) -> dict[str, dict[str, Any]]:
        """Retourne {chemin_absolu: dict_metadata_complet} pour tous les fichiers.

        Utilise exiftool en batch pour la performance (une invocation, tous fichiers).
        """
        if not files:
            return {}
        results: dict[str, dict[str, Any]] = {}
        try:
            with exiftool.ExifToolHelper(executable=str(self.bin)) as et:
                # -G : grouper par famille (EXIF/XMP/File/Composite/QuickTime…)
                # -a : tags dupliqués OK
                # -j : json
                # -n : valeurs numériques brutes (utile pour GPS)
                metadata_list = et.get_metadata(files, params=["-G", "-a", "-n"])
                for i, meta in enumerate(metadata_list):
                    source = meta.get("SourceFile", files[i] if i < len(files) else "")
                    source_norm = os.path.normpath(source)
                    results[source_norm] = meta
                    if progress_cb:
                        try:
                            progress_cb(i + 1, len(files))
                        except Exception:
                            pass
        except Exception as e:
            print(f"⚠️ ExifTool batch a échoué: {e}\n{traceback.format_exc()}")
        return results

    def extract_single(self, path: str) -> dict[str, Any]:
        return self.extract_batch([path]).get(os.path.normpath(path), {})


# ─────────────────────────────────────────────────────────────────────────────
# CLASSIFIER (drone + catégorie + groupement compagnons)
# ─────────────────────────────────────────────────────────────────────────────
class DJIClassifier:
    """Détecte drone, catégorie, date, et groupe les fichiers compagnons."""

    def __init__(self, drone_mapping: Optional[list[dict]] = None) -> None:
        raw = drone_mapping or CONFIG.get("drone_mapping", [])
        self.mapping = []
        for entry in raw:
            try:
                self.mapping.append((
                    re.compile(entry["pattern"], re.IGNORECASE),
                    entry["id"],
                    entry["folder"],
                ))
            except re.error:
                pass

    # ── drone ──────────────────────────────────────────────────────────────
    def detect_drone(self, metadata: dict, path: str = "") -> tuple[str, str, str]:
        """Retourne (drone_id, drone_folder, reason).

        Ordre : (1) EXIF/QT Model, (2) champs XMP DJI, (3) nom de fichier (FC7203…),
        (4) hints dans le chemin (nom du drone dans un dossier parent).
        """
        # ── 1. Metadata Model ──
        candidates = [
            metadata.get("EXIF:Model"),
            metadata.get("QuickTime:Model"),
            metadata.get("XMP:Model"),
            metadata.get("MakerNotes:Model"),
            metadata.get("File:Model"),
            metadata.get("EXIF:CameraModelName"),
            metadata.get("QuickTime:HandlerDescription"),
        ]
        candidates = [c for c in candidates if c]
        blob = " ".join(str(c) for c in candidates).lower()
        for regex, drone_id, folder in self.mapping:
            if regex.search(blob):
                return drone_id, folder, f"Model={blob.strip()} → {drone_id}"

        # ── 2. XMP DJI CreatorTool ou autres champs drone-dji ──
        for key in ("XMP:CreatorTool", "XMP-drone-dji:AbsoluteAltitude", "XMP:About"):
            val = metadata.get(key)
            if val:
                blob2 = str(val).lower()
                for regex, drone_id, folder in self.mapping:
                    if regex.search(blob2):
                        return drone_id, folder, f"{key}={blob2} → {drone_id}"

        # ── 3. Nom fichier — modèles caméra DJI ──
        # FC7203=Mini 2, FC7503=Mini SE, FC8482=Mini 4 Pro, FC3411=Air 2S,
        # FC3170=Air 2, FC8283=Neo, FC220=Mavic Pro, XT2=Avata2, etc.
        name = Path(path).name.upper() if path else ""
        # Certaines caméras encodent le modèle dans le nom fichier ou dans SourceFile
        file_hints = {
            r"FC7203": "MINI2-MEO",
            r"FC8482": "MINI4PRO-PEDRO",
            r"FC8283": "NEO2-CLEO",
        }
        for pat, drone_id in file_hints.items():
            if re.search(pat, name):
                folder = self._folder_for(drone_id)
                return drone_id, folder, f"Nom fichier: {pat} → {drone_id}"

        # ── 4. Hints dans le chemin (nom du drone dans un dossier parent) ──
        if path:
            path_blob = str(path).lower().replace("\\", "/")
            path_hints = [
                (r"\bmeo\b|mini\s*2", "MINI2-MEO"),
                (r"\bcleo\b|\bneo\b", "NEO2-CLEO"),
                (r"\bgino\b|avata", "AVATA2-GINO"),
                (r"\bpedro\b|mini\s*4", "MINI4PRO-PEDRO"),
            ]
            for pat, drone_id in path_hints:
                if re.search(pat, path_blob):
                    folder = self._folder_for(drone_id)
                    return drone_id, folder, f"Dossier parent: /{pat}/ → {drone_id}"

        return "UNKNOWN", UNKNOWN_DRONE_DIR, f"Aucun match (Model={blob or 'vide'})"

    def _folder_for(self, drone_id: str) -> str:
        for entry in CONFIG.get("drone_mapping", []):
            if entry.get("id") == drone_id:
                return entry.get("folder", UNKNOWN_DRONE_DIR)
        return UNKNOWN_DRONE_DIR

    # ── catégorie ──────────────────────────────────────────────────────────
    def detect_category(self, path: str, metadata: dict) -> tuple[str, str]:
        """Retourne (categorie, raison)."""
        p = Path(path)
        parent_names = [x.lower() for x in p.parts]
        parent_blob = " / ".join(parent_names)

        # 1. Dossier parent contient PANORAMA
        if any("panorama" in n or n == "pano" or n.startswith("pano_") for n in parent_names):
            return "PANORAMA", f"dossier parent panorama: {parent_blob}"
        # 2. Dossier parent contient HYPERLAPSE
        if any("hyperlapse" in n or "hyper" == n or n.startswith("hyper_") for n in parent_names):
            return "HYPERLAPSE", f"dossier parent hyperlapse: {parent_blob}"

        # 3. XMP:ShootingMode / drone-dji specifics
        shooting_mode = str(
            metadata.get("XMP-drone-dji:ShootingMode")
            or metadata.get("XMP:ShootingMode")
            or metadata.get("MakerNotes:ShootingMode")
            or ""
        ).lower()
        if "hyperlapse" in shooting_mode or "timelapse" in shooting_mode:
            return "HYPERLAPSE", f"ShootingMode={shooting_mode}"
        if "panorama" in shooting_mode or "pano" in shooting_mode:
            return "PANORAMA", f"ShootingMode={shooting_mode}"

        # 4. Nom fichier
        name = p.stem.upper()
        if "_PANO_" in name or name.endswith("_PANO") or "PANORAMA" in name:
            return "PANORAMA", f"nom fichier: {p.name}"
        if "_HYPER" in name or "HYPERLAPSE" in name:
            return "HYPERLAPSE", f"nom fichier: {p.name}"

        # 5. Extension → VIDEO ou PHOTO
        ext = p.suffix.lower()
        if ext in VIDEO_EXTS:
            return "VIDEO", f"extension {ext}"
        if ext in PHOTO_EXTS:
            return "PHOTO", f"extension {ext}"
        return "VIDEO", f"fallback ext {ext}"

    # ── date ───────────────────────────────────────────────────────────────
    def detect_capture_date(self, path: str, metadata: dict) -> str:
        """Retourne YYYY-MM-DD depuis EXIF/QuickTime, fallback mtime."""
        keys = [
            "EXIF:DateTimeOriginal",
            "EXIF:CreateDate",
            "QuickTime:CreateDate",
            "QuickTime:MediaCreateDate",
            "QuickTime:TrackCreateDate",
            "XMP:DateTimeOriginal",
            "XMP:CreateDate",
            "File:FileModifyDate",
        ]
        for k in keys:
            v = metadata.get(k)
            if not v:
                continue
            # Formats communs exiftool: "2026:07:25 18:30:45" ou "2026:07:25 18:30:45-04:00"
            s = str(v).strip()
            m = re.match(r"(\d{4})[:\-/](\d{2})[:\-/](\d{2})", s)
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        # Fallback: mtime
        try:
            ts = os.path.getmtime(path)
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d")

    # ── compagnons ─────────────────────────────────────────────────────────
    @staticmethod
    def find_companions(main_path: str, all_files_in_dir: list[str]) -> list[str]:
        """Trouve tous les fichiers avec le même stem (ou variantes DJI)."""
        main_p = Path(main_path)
        stem = main_p.stem
        parent = main_p.parent
        companions: list[str] = []
        # Variantes DJI: DJI_XXX_D.MP4 (D-Log), DJI_XXX.SRT, DJI_XXX.LRF, DJI_XXX_color.mp4, etc.
        stem_lower = stem.lower()
        # Base stem = enlever suffixes _D, _color, _lut
        base_stem = re.sub(r"_(d|color|lut|proxy|preview)$", "", stem_lower)

        for f in all_files_in_dir:
            fp = Path(f)
            if str(fp) == str(main_p):
                continue
            if fp.parent != parent:
                continue
            fstem_lower = fp.stem.lower()
            fbase = re.sub(r"_(d|color|lut|proxy|preview)$", "", fstem_lower)
            if fbase == base_stem or fstem_lower == stem_lower:
                companions.append(str(fp))
        return companions


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER
# ─────────────────────────────────────────────────────────────────────────────
class DJIScanner:
    """Parcourt le dossier source, extrait metadata, classe, groupe compagnons."""

    def __init__(self, source_dir: str) -> None:
        self.source_dir = source_dir
        self.extractor = DJIMetadataExtractor()
        self.classifier = DJIClassifier()

    def _list_all_files(self) -> tuple[list[str], list[str]]:
        """Retourne (media_files, all_files_in_tree)."""
        media: list[str] = []
        all_files: list[str] = []
        for root, _dirs, files in os.walk(self.source_dir):
            for fn in files:
                fp = os.path.join(root, fn)
                all_files.append(fp)
                ext = Path(fn).suffix.lower()
                if ext in ALL_MEDIA_EXTS:
                    media.append(fp)
        return media, all_files

    def scan(self, progress_cb: Optional[callable] = None) -> list[MediaUnit]:
        media_files, all_files = self._list_all_files()
        if not media_files:
            return []

        if progress_cb:
            progress_cb("Extraction métadonnées via ExifTool…", 0, len(media_files))
        metadata_map = self.extractor.extract_batch(
            media_files,
            progress_cb=lambda i, n: (
                progress_cb("Extraction métadonnées via ExifTool…", i, n) if progress_cb else None
            ),
        )

        units: list[MediaUnit] = []
        used_companions: set[str] = set()

        for idx, media_path in enumerate(media_files):
            if progress_cb:
                progress_cb("Classification et groupement…", idx + 1, len(media_files))
            meta = metadata_map.get(os.path.normpath(media_path), {})
            drone_id, drone_folder, drone_reason = self.classifier.detect_drone(meta, media_path)
            category, cat_reason = self.classifier.detect_category(media_path, meta)
            date_str = self.classifier.detect_capture_date(media_path, meta)
            companions_all = self.classifier.find_companions(media_path, all_files)
            # Exclut fichiers qui sont eux-mêmes des médias principaux (double comptage)
            companions = [
                c for c in companions_all
                if c not in media_files and c not in used_companions
            ]
            used_companions.update(companions)

            unit = MediaUnit(
                main_path=media_path,
                companions=companions,
                metadata=meta,
                drone_id=drone_id,
                drone_folder=drone_folder,
                category=category,
                capture_date=date_str,
                action="move",
                detection_reason=f"Drone: {drone_reason} | Cat: {cat_reason}",
            )
            units.append(unit)

        # ── Assignation des sous-dossiers de groupe pour PANORAMA / HYPERLAPSE ──
        self._assign_group_subdirs(units)
        return units

    def _assign_group_subdirs(self, units: list[MediaUnit]) -> None:
        """Attribue un sous-dossier de groupe aux médias PANORAMA et HYPERLAPSE.

        Règle : on prend le nom du dossier parent immédiat de la source si celui-ci
        n'est PAS le dossier racine `00-DJI-A-TRIER` (ni un dossier générique
        PANORAMA/HYPERLAPSE seul). Si aucun sous-dossier utile, on groupe par
        proximité temporelle (photos prises à moins de 90 s d'écart sur le même
        drone/jour = même panorama) et on assigne PANO_1, PANO_2…
        """
        src_root = os.path.normpath(self.source_dir).lower()
        generic_names = {"panorama", "pano", "hyperlapse", "hyper", "photo", "photos",
                         "video", "videos", "dcim", "media"}

        # 1re passe : essayer d'utiliser le dossier parent source
        for u in units:
            if u.category not in ("PANORAMA", "HYPERLAPSE"):
                continue
            parent = Path(u.main_path).parent
            candidate = None
            probe = parent
            while probe and os.path.normpath(str(probe)).lower() != src_root:
                pname = probe.name.strip()
                if pname and pname.lower() not in generic_names:
                    candidate = pname
                    break
                probe = probe.parent
            if candidate:
                safe = re.sub(r'[<>:"/\\|?*]', "_", candidate).strip(" .")
                if safe:
                    u.group_subdir = safe

        # 2e passe : grouper par proximité temporelle pour les "loose"
        # Récupère timestamp complet depuis metadata
        def _ts(u: MediaUnit) -> Optional[datetime]:
            for k in ("EXIF:DateTimeOriginal", "EXIF:CreateDate",
                      "QuickTime:CreateDate", "QuickTime:MediaCreateDate",
                      "XMP:DateTimeOriginal", "XMP:CreateDate",
                      "File:FileModifyDate"):
                v = u.metadata.get(k)
                if not v:
                    continue
                s = str(v).strip()
                # format exiftool: "2026:07:25 18:30:45[+/-HH:MM]"
                m = re.match(r"(\d{4})[:\-/](\d{2})[:\-/](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", s)
                if m:
                    try:
                        return datetime(*(int(g) for g in m.groups()))
                    except ValueError:
                        continue
            try:
                return datetime.fromtimestamp(os.path.getmtime(u.main_path))
            except OSError:
                return None

        # Buckets : (drone_folder, date, catégorie) → liste triée par timestamp
        buckets: dict[tuple[str, str, str], list[tuple[Optional[datetime], MediaUnit]]] = {}
        for u in units:
            if u.category not in ("PANORAMA", "HYPERLAPSE") or u.group_subdir:
                continue
            key = (u.drone_folder, u.capture_date, u.category)
            buckets.setdefault(key, []).append((_ts(u), u))

        for (drone_folder, date, cat), lst in buckets.items():
            prefix = "PANO" if cat == "PANORAMA" else "HYPER"
            # Tri par timestamp (None en dernier), puis par nom pour stabilité
            lst.sort(key=lambda t: (t[0] or datetime.max, Path(t[1].main_path).name.lower()))
            # Regroupe : nouveau groupe si écart > 90 s avec le précédent
            group_idx = 0
            last_ts: Optional[datetime] = None
            for ts, u in lst:
                if last_ts is None or ts is None or (ts - last_ts).total_seconds() > 90:
                    group_idx += 1
                u.group_subdir = f"{prefix}_{group_idx:03d}"
                last_ts = ts


# ─────────────────────────────────────────────────────────────────────────────
# THUMBNAILS
# ─────────────────────────────────────────────────────────────────────────────
def _thumb_cache_path(source: str) -> Path:
    import hashlib
    h = hashlib.md5(source.encode("utf-8")).hexdigest()
    return THUMB_CACHE_DIR / f"{h}.jpg"


def generate_thumbnail(path: str, size: int = 256) -> Optional[str]:
    """Génère (ou retourne cache) un thumbnail JPG. Retourne chemin absolu ou None."""
    cache = _thumb_cache_path(path)
    if cache.exists():
        try:
            src_mtime = os.path.getmtime(path)
            if os.path.getmtime(cache) >= src_mtime:
                return str(cache)
        except OSError:
            return str(cache)
    ext = Path(path).suffix.lower()
    try:
        if ext in PHOTO_EXTS and Image is not None:
            with Image.open(path) as im:
                im.thumbnail((size, size))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.save(cache, "JPEG", quality=80)
            return str(cache)
        elif ext in VIDEO_EXTS and cv2 is not None:
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                return None
            # Frame à 1s (ou 10% de la durée)
            fps = cap.get(cv2.CAP_PROP_FPS) or 30
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            target_frame = min(int(fps * 1.0), max(frame_count // 10, 0))
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return None
            # Redimensionner
            h, w = frame.shape[:2]
            ratio = size / max(h, w)
            new_size = (int(w * ratio), int(h * ratio))
            frame = cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(cache), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            return str(cache)
    except Exception as e:
        print(f"⚠️ Thumbnail échec pour {path}: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ORGANIZER (copie + rapports)
# ─────────────────────────────────────────────────────────────────────────────
class DJIOrganizer:
    """Effectue les copies, la suppression, et génère les rapports."""

    def __init__(
        self,
        units: list[MediaUnit],
        destination_dir: str,
        overwrite_policy: str = "ask",  # ask | skip | rename | overwrite
        send_to_trash: bool = True,
    ) -> None:
        self.units = units
        self.destination_dir = destination_dir
        self.overwrite_policy = overwrite_policy
        self.send_to_trash = send_to_trash
        self.results: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.on_conflict: Optional[callable] = None  # callback pour policy=ask
        self.on_progress: Optional[callable] = None

    def _destination_for(self, unit: MediaUnit, file_path: str) -> Path:
        dest_root = Path(self.destination_dir) / unit.drone_folder / unit.capture_date / unit.category
        # Sous-dossier de groupe pour PANORAMA/HYPERLAPSE
        if unit.group_subdir and unit.category in ("PANORAMA", "HYPERLAPSE"):
            dest_root = dest_root / unit.group_subdir
        return dest_root / Path(file_path).name

    def _resolve_conflict(self, target: Path) -> Optional[Path]:
        """Retourne le chemin final à utiliser, ou None si skip."""
        if not target.exists():
            return target
        policy = self.overwrite_policy
        if policy == "ask" and self.on_conflict:
            policy = self.on_conflict(target) or "skip"
        if policy == "skip":
            return None
        if policy == "overwrite":
            return target
        if policy == "rename":
            stem, suf = target.stem, target.suffix
            n = 1
            while True:
                cand = target.with_name(f"{stem}_{n}{suf}")
                if not cand.exists():
                    return cand
                n += 1
        return None

    def execute(self) -> dict[str, Any]:
        total_units = len(self.units)
        for i, unit in enumerate(self.units):
            if self.on_progress:
                try:
                    self.on_progress(i + 1, total_units, unit)
                except Exception:
                    pass
            try:
                if unit.action == "skip":
                    self.results.append({
                        "unit": unit.main_path,
                        "action": "skip",
                        "drone": unit.drone_id,
                        "category": unit.category,
                        "date": unit.capture_date,
                        "files": unit.all_files,
                        "destinations": [],
                    })
                    continue

                if unit.action == "delete":
                    deleted: list[str] = []
                    for fp in unit.all_files:
                        if send2trash and os.path.exists(fp):
                            send2trash(fp)
                            deleted.append(fp)
                    self.results.append({
                        "unit": unit.main_path,
                        "action": "delete",
                        "drone": unit.drone_id,
                        "category": unit.category,
                        "date": unit.capture_date,
                        "files": unit.all_files,
                        "deleted": deleted,
                    })
                    continue

                # action == "move" (copie puis corbeille)
                copied: list[dict[str, str]] = []
                for fp in unit.all_files:
                    if not os.path.exists(fp):
                        continue
                    target = self._destination_for(unit, fp)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    final_target = self._resolve_conflict(target)
                    if final_target is None:
                        copied.append({"source": fp, "target": str(target), "status": "skipped_conflict"})
                        continue
                    shutil.copy2(fp, final_target)
                    # Vérification taille
                    if os.path.getsize(final_target) != os.path.getsize(fp):
                        raise IOError(f"Taille différente après copie: {fp} → {final_target}")
                    copied.append({"source": fp, "target": str(final_target), "status": "ok"})

                # Envoi corbeille des originaux si send_to_trash
                trashed: list[str] = []
                if self.send_to_trash and send2trash:
                    for c in copied:
                        if c.get("status") == "ok":
                            try:
                                send2trash(c["source"])
                                trashed.append(c["source"])
                            except Exception as e:
                                self.errors.append({"file": c["source"], "error": f"trash failed: {e}"})

                self.results.append({
                    "unit": unit.main_path,
                    "action": "move",
                    "drone": unit.drone_id,
                    "category": unit.category,
                    "date": unit.capture_date,
                    "files": unit.all_files,
                    "copied": copied,
                    "trashed": trashed,
                })
            except Exception as e:
                self.errors.append({
                    "unit": unit.main_path,
                    "error": str(e),
                    "trace": traceback.format_exc(),
                })
        return self._build_summary()

    def _build_summary(self) -> dict[str, Any]:
        by_drone: dict[str, int] = {}
        by_cat: dict[str, int] = {}
        moved = deleted = skipped = 0
        for r in self.results:
            drone = r.get("drone", "?")
            cat = r.get("category", "?")
            by_drone[drone] = by_drone.get(drone, 0) + 1
            by_cat[cat] = by_cat.get(cat, 0) + 1
            if r["action"] == "move":
                moved += 1
            elif r["action"] == "delete":
                deleted += 1
            else:
                skipped += 1
        return {
            "total_units": len(self.units),
            "moved": moved,
            "deleted": deleted,
            "skipped": skipped,
            "errors": len(self.errors),
            "by_drone": by_drone,
            "by_category": by_cat,
        }

    def write_reports(self) -> dict[str, str]:
        """Écrit rapport.txt + rapport_complet.json + metadata_complet.json."""
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        logs_dir = Path(self.destination_dir) / "Logs_DJI_ORGZ" / ts
        logs_dir.mkdir(parents=True, exist_ok=True)

        summary = self._build_summary()
        context = {
            "app": "DJI Organizator",
            "timestamp": ts,
            "started_at": datetime.now().isoformat(),
            "destination_dir": self.destination_dir,
            "overwrite_policy": self.overwrite_policy,
            "send_to_trash": self.send_to_trash,
            "summary": summary,
        }

        # ── rapport_complet.json ──
        full_json = {
            "context": context,
            "results": self.results,
            "errors": self.errors,
        }
        full_path = logs_dir / "rapport_complet.json"
        with open(full_path, "w", encoding="utf-8") as f:
            json.dump(full_json, f, indent=2, ensure_ascii=False, default=str)

        # ── metadata_complet.json — TOUTES les metadata brutes ExifTool ──
        meta_dump = {
            "context": context,
            "media": [
                {
                    "main_path": u.main_path,
                    "companions": u.companions,
                    "drone_id": u.drone_id,
                    "drone_folder": u.drone_folder,
                    "category": u.category,
                    "capture_date": u.capture_date,
                    "group_subdir": u.group_subdir,
                    "action": u.action,
                    "detection_reason": u.detection_reason,
                    "metadata_exiftool": u.metadata,
                }
                for u in self.units
            ],
        }
        meta_path = logs_dir / "metadata_complet.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta_dump, f, indent=2, ensure_ascii=False, default=str)

        # ── rapport.txt ──
        txt_lines = [
            "=" * 70,
            "DJI ORGANIZATOR — Rapport d'exécution",
            "=" * 70,
            f"Date       : {context['started_at']}",
            f"Destination: {self.destination_dir}",
            f"Corbeille  : {'oui' if self.send_to_trash else 'non'}",
            "",
            "── Résumé ────────────────────────────────────────────────────────────",
            f"Total unités : {summary['total_units']}",
            f"Déplacées    : {summary['moved']}",
            f"Effacées     : {summary['deleted']}",
            f"Ignorées     : {summary['skipped']}",
            f"Erreurs      : {summary['errors']}",
            "",
            "Par drone :",
        ]
        for d, c in summary["by_drone"].items():
            txt_lines.append(f"  {d:20s} : {c}")
        txt_lines += ["", "Par catégorie :"]
        for cat, c in summary["by_category"].items():
            txt_lines.append(f"  {cat:15s} : {c}")
        txt_lines += ["", "── Détail par média ──────────────────────────────────────────────────"]
        for r in self.results:
            txt_lines.append(f"\n[{r['action'].upper()}] {r['unit']}")
            txt_lines.append(f"  Drone: {r['drone']} | Cat: {r['category']} | Date: {r['date']}")
            if r["action"] == "move":
                for c in r.get("copied", []):
                    txt_lines.append(f"    → {c['status']:20s} {c['source']} -> {c['target']}")
                for t in r.get("trashed", []):
                    txt_lines.append(f"    🗑️ corbeille: {t}")
            elif r["action"] == "delete":
                for d in r.get("deleted", []):
                    txt_lines.append(f"    🗑️ {d}")
        if self.errors:
            txt_lines += ["", "── ERREURS ───────────────────────────────────────────────────────────"]
            for e in self.errors:
                txt_lines.append(f"  {e.get('unit', '?')} : {e.get('error', '')}")

        txt_path = logs_dir / "rapport.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(txt_lines))

        return {
            "rapport_txt": str(txt_path),
            "rapport_json": str(full_path),
            "metadata_json": str(meta_path),
            "logs_dir": str(logs_dir),
        }


# ─────────────────────────────────────────────────────────────────────────────
# UTILITAIRES
# ─────────────────────────────────────────────────────────────────────────────
def _is_port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False


def _find_available_port(host: str, start: int, attempts: int = 30) -> int:
    for i in range(attempts):
        if _is_port_available(host, start + i):
            return start + i
    raise RuntimeError(f"Aucun port libre entre {start} et {start + attempts}")


def human_size(n: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} Po"


def image_to_data_uri(path: str) -> str:
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        ext = Path(path).suffix.lower().lstrip(".") or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        return f"data:image/{ext};base64,{b64}"
    except Exception:
        return ""


# ── Sélecteur de dossier natif (sous-processus tkinter, 100% async) ──
_TK_PICKER_SCRIPT = (
    "import sys, tkinter as tk\n"
    "from tkinter import filedialog\n"
    "root = tk.Tk()\n"
    "root.attributes('-topmost', True)\n"
    "root.withdraw()\n"
    "try:\n"
    "    folder = filedialog.askdirectory()\n"
    "finally:\n"
    "    try: root.destroy()\n"
    "    except Exception: pass\n"
    "sys.stdout.write(folder or '')\n"
    "sys.stdout.flush()\n"
)


async def select_folder_async(initial_dir: str = "") -> str:
    """Ouvre un dialogue tkinter dans un sous-processus séparé, 100% non-bloquant.

    Créer tkinter sur un thread NiceGUI/uvicorn fige Windows. On délègue à un
    Python séparé piloté par asyncio.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _TK_PICKER_SCRIPT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return ""
        result = (stdout or b"").decode("utf-8", errors="ignore").strip()
        # tkinter renvoie avec des slash /, on normalise pour Windows
        if result:
            result = os.path.normpath(result)
        return result
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# APPLICATION UI
# ─────────────────────────────────────────────────────────────────────────────
class DJIOrganizatorApp:
    def __init__(self) -> None:
        self.source_dir: str = CONFIG.get("source_dir", DEFAULT_SOURCE)
        self.destination_dir: str = CONFIG.get("destination_dir", DEFAULT_DEST)
        self.send_to_trash: bool = CONFIG.get("send_to_trash_after_copy", True)
        self.units: list[MediaUnit] = []
        self.log_lines: list[str] = []
        self.report_paths: dict[str, str] = {}
        self.summary: dict[str, Any] = {}
        self._stepper = None
        self._scan_progress_label = None
        self._scan_progress_bar = None
        self._review_container = None
        self._confirm_container = None
        self._exec_container = None
        self._exec_log = None
        self._filter_drone = "TOUS"
        self._filter_category = "TOUTES"
        self._selected_units: set[str] = set()
        # Pagination
        self._page_size = 24
        self._current_page = 0
        self._pagination_container = None
        self._page_info_label = None

    # ── logging ────────────────────────────────────────────────────────────
    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        self.log_lines.append(line)
        print(line)
        if self._exec_log is not None:
            try:
                self._exec_log.push(line)
            except Exception:
                pass

    # ── construction UI principale ─────────────────────────────────────────
    def build(self) -> None:
        ui.colors(primary="#1976d2", secondary="#26a69a")
        with ui.header().classes("bg-primary text-white items-center"):
            ui.icon("flight").classes("text-2xl")
            ui.label("DJI Organizator").classes("text-h5")
            ui.space()
            ui.label(f"v1.0 · port {app.config.port if hasattr(app.config, 'port') else 8192}").classes("text-caption")

        with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
            self._stepper = ui.stepper().props("vertical").classes("w-full")
            with self._stepper:
                self._step_config()
                self._step_scan()
                self._step_review()
                self._step_confirm()
                self._step_execute()

    # ── ÉTAPE 1 : Configuration ────────────────────────────────────────────
    def _step_config(self) -> None:
        with ui.step("Configuration").props("icon=folder_open"):
            with ui.card().classes("w-full"):
                ui.label("Dossiers").classes("text-h6")

                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    src = ui.input("Dossier source (à trier)", value=self.source_dir).classes("flex-grow")

                    async def _pick_source() -> None:
                        picked = await select_folder_async(src.value)
                        if picked:
                            src.value = picked
                            src.update()

                    ui.button(icon="folder", on_click=_pick_source).props("flat round").tooltip("Choisir le dossier source")

                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    dst = ui.input("Dossier destination racine", value=self.destination_dir).classes("flex-grow")

                    async def _pick_dest() -> None:
                        picked = await select_folder_async(dst.value)
                        if picked:
                            dst.value = picked
                            dst.update()

                    ui.button(icon="folder", on_click=_pick_dest).props("flat round").tooltip("Choisir le dossier destination")

                trash_sw = ui.switch("Envoyer les originaux à la corbeille après copie réussie",
                                     value=self.send_to_trash)

                def save_and_next() -> None:
                    self.source_dir = src.value.strip()
                    self.destination_dir = dst.value.strip()
                    self.send_to_trash = trash_sw.value
                    if not os.path.isdir(self.source_dir):
                        ui.notify(f"Dossier source introuvable : {self.source_dir}", type="negative")
                        return
                    if not os.path.isdir(self.destination_dir):
                        ui.notify(f"Dossier destination introuvable : {self.destination_dir}", type="negative")
                        return
                    CONFIG["source_dir"] = self.source_dir
                    CONFIG["destination_dir"] = self.destination_dir
                    CONFIG["send_to_trash_after_copy"] = self.send_to_trash
                    save_config(CONFIG)
                    self._stepper.next()

            with ui.stepper_navigation():
                ui.button("Suivant", on_click=save_and_next).props("color=primary")

    # ── ÉTAPE 2 : Scan ─────────────────────────────────────────────────────
    def _step_scan(self) -> None:
        with ui.step("Scan & analyse").props("icon=search"):
            with ui.card().classes("w-full"):
                ui.label("Extraction des métadonnées via ExifTool").classes("text-h6")
                self._scan_progress_label = ui.label("En attente…").classes("text-body2")
                self._scan_progress_bar = ui.linear_progress(0.0, show_value=False).classes("w-full")

            async def run_scan() -> None:
                self._scan_progress_label.text = "Initialisation…"
                self._scan_progress_bar.value = 0.0
                try:
                    scanner = DJIScanner(self.source_dir)
                except Exception as e:
                    ui.notify(f"Erreur init scanner: {e}", type="negative", timeout=10000)
                    return

                def cb(msg: str, cur: int, total: int) -> None:
                    self._scan_progress_label.text = f"{msg}  {cur}/{total}"
                    if total:
                        self._scan_progress_bar.value = cur / total

                try:
                    self.units = await run.io_bound(scanner.scan, cb)
                except Exception as e:
                    ui.notify(f"Erreur scan: {e}", type="negative", timeout=10000)
                    return

                self._scan_progress_label.text = f"✅ {len(self.units)} média(s) trouvés"
                self._scan_progress_bar.value = 1.0
                ui.notify(f"Scan terminé : {len(self.units)} média(s)", type="positive")
                self._stepper.next()
                self._refresh_review()

            with ui.stepper_navigation():
                ui.button("← Retour", on_click=self._stepper.previous).props("flat")
                ui.button("Scanner", on_click=run_scan).props("color=primary icon=play_arrow")

    # ── ÉTAPE 3 : Revue avec thumbnails ────────────────────────────────────
    def _step_review(self) -> None:
        with ui.step("Revue & sélection").props("icon=grid_view"):
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label("Filtres :").classes("text-body2")
                    self._drone_filter_sel = ui.select(
                        options=["TOUS"],
                        value="TOUS",
                        on_change=lambda e: self._apply_filters(),
                        label="Drone",
                    ).classes("min-w-32")
                    self._cat_filter_sel = ui.select(
                        options=["TOUTES"] + CATEGORIES,
                        value="TOUTES",
                        on_change=lambda e: self._apply_filters(),
                        label="Catégorie",
                    ).classes("min-w-32")
                    ui.space()
                    ui.button("Tout → Déplacer", on_click=lambda: self._bulk_action("move")).props("flat")
                    ui.button("Tout → Effacer", on_click=lambda: self._bulk_action("delete")).props("flat color=negative")
                    ui.button("Tout → Ignorer", on_click=lambda: self._bulk_action("skip")).props("flat")

                # Ligne pour réassignement drone en masse (utile pour Mini 2 dont les métadonnées sont vides)
                with ui.row().classes("w-full items-center gap-2 mt-2"):
                    ui.label("Réassigner drone des filtrés :").classes("text-body2")
                    drone_ids = [d["id"] for d in CONFIG.get("drone_mapping", [])]
                    self._bulk_drone_target = ui.select(
                        options=drone_ids,
                        value=drone_ids[0] if drone_ids else "MINI2-MEO",
                        label="→ Drone cible",
                    ).classes("min-w-40")
                    ui.button(
                        "Réassigner filtrés",
                        icon="swap_horiz",
                        on_click=self._bulk_reassign_drone,
                    ).props("color=primary")
                    ui.label("(applique le filtre courant Drone/Catégorie)").classes("text-caption text-grey-6")

                self._review_container = ui.column().classes("w-full gap-2")

            with ui.stepper_navigation():
                ui.button("← Retour", on_click=self._stepper.previous).props("flat")
                ui.button("Suivant : Confirmation", on_click=self._goto_confirm).props("color=primary")

    def _refresh_review(self) -> None:
        # Alimente les filtres
        drones = sorted({u.drone_id for u in self.units})
        try:
            self._drone_filter_sel.options = ["TOUS"] + drones
            self._drone_filter_sel.update()
        except Exception:
            pass
        self._apply_filters()

    def _apply_filters(self, reset_page: bool = True) -> None:
        if self._review_container is None:
            return
        drone_f = getattr(self._drone_filter_sel, "value", "TOUS")
        cat_f = getattr(self._cat_filter_sel, "value", "TOUTES")

        visible = [
            u for u in self.units
            if (drone_f == "TOUS" or u.drone_id == drone_f)
            and (cat_f == "TOUTES" or u.category == cat_f)
        ]

        # Construction des items d'affichage : les PANO/HYPER partageant le même
        # (drone_folder, capture_date, category, group_subdir) sont regroupés en un seul.
        display_items: list[dict[str, Any]] = []
        seen_groups: set[tuple[str, str, str, str]] = set()
        for u in visible:
            if u.category in ("PANORAMA", "HYPERLAPSE") and u.group_subdir:
                key = (u.drone_folder, u.capture_date, u.category, u.group_subdir)
                if key in seen_groups:
                    continue
                seen_groups.add(key)
                members = [
                    v for v in visible
                    if v.category == u.category
                    and v.drone_folder == u.drone_folder
                    and v.capture_date == u.capture_date
                    and v.group_subdir == u.group_subdir
                ]
                display_items.append({"kind": "group", "units": members, "key": key})
            else:
                display_items.append({"kind": "single", "unit": u})

        # Pagination
        total = len(display_items)
        page_size = self._page_size
        pages = max(1, (total + page_size - 1) // page_size)
        if reset_page:
            self._current_page = 0
        if self._current_page >= pages:
            self._current_page = pages - 1
        start = self._current_page * page_size
        end = min(start + page_size, total)
        page_items = display_items[start:end]

        self._review_container.clear()
        with self._review_container:
            # Barre pagination haut
            self._render_pagination_bar(total, pages, start, end, top=True)

            if not page_items:
                ui.label("Aucun média correspondant aux filtres.").classes("text-caption")
                return
            with ui.grid(columns=3).classes("w-full gap-3"):
                for item in page_items:
                    if item["kind"] == "single":
                        self._render_unit_card(item["unit"])
                    else:
                        self._render_group_card(item["units"])

            # Barre pagination bas
            self._render_pagination_bar(total, pages, start, end, top=False)

    def _render_pagination_bar(self, total: int, pages: int, start: int, end: int, top: bool) -> None:
        with ui.row().classes("w-full items-center gap-2 py-1"):
            ui.button(icon="first_page", on_click=lambda: self._goto_page(0)).props("flat dense").tooltip("Première page")
            ui.button(icon="chevron_left", on_click=lambda: self._goto_page(self._current_page - 1)).props("flat dense").tooltip("Précédente")
            ui.label(f"{start + 1}-{end} / {total}  ·  page {self._current_page + 1}/{pages}").classes("text-body2")
            ui.button(icon="chevron_right", on_click=lambda: self._goto_page(self._current_page + 1)).props("flat dense").tooltip("Suivante")
            ui.button(icon="last_page", on_click=lambda: self._goto_page(pages - 1)).props("flat dense").tooltip("Dernière page")
            if top:
                ui.space()
                ui.label("Par page :").classes("text-caption")
                ui.select(
                    options=[12, 24, 48, 96],
                    value=self._page_size,
                    on_change=lambda e: self._change_page_size(e.value),
                ).props("dense options-dense").classes("w-24")

    def _goto_page(self, n: int) -> None:
        self._current_page = max(0, n)
        self._apply_filters(reset_page=False)

    def _change_page_size(self, n: int) -> None:
        try:
            self._page_size = int(n)
        except (TypeError, ValueError):
            return
        self._current_page = 0
        self._apply_filters(reset_page=False)

    def _render_unit_card(self, unit: MediaUnit) -> None:
        with ui.card().classes("w-full"):
            thumb = generate_thumbnail(unit.main_path, size=256)
            if thumb:
                ui.image(image_to_data_uri(thumb)).classes("w-full h-40 object-cover rounded")
            else:
                with ui.element("div").classes(
                    "w-full h-40 flex items-center justify-center bg-grey-3 rounded"
                ):
                    ui.icon("movie" if Path(unit.main_path).suffix.lower() in VIDEO_EXTS else "image").classes("text-4xl text-grey-6")

            ui.label(Path(unit.main_path).name).classes("text-body2 truncate").tooltip(unit.main_path)
            with ui.row().classes("items-center gap-1"):
                ui.badge(unit.drone_id, color="primary")
                ui.badge(unit.category, color="secondary")
                ui.badge(unit.capture_date, color="grey")
                if unit.group_subdir and unit.category in ("PANORAMA", "HYPERLAPSE"):
                    ui.badge(unit.group_subdir, color="orange").tooltip("Sous-dossier de groupe")
            if unit.companions:
                ui.label(f"+ {len(unit.companions)} compagnon(s)").classes("text-caption text-grey-7").tooltip(
                    "\n".join(unit.companions)
                )
            ui.label(f"Taille: {human_size(unit.total_size)}").classes("text-caption")
            ui.label(unit.detection_reason).classes("text-caption text-grey-6 truncate").tooltip(unit.detection_reason)

            # Éditables : drone, catégorie, action
            with ui.row().classes("w-full gap-1 items-center"):
                drone_opts = {d["id"]: d["id"] for d in CONFIG.get("drone_mapping", [])}
                drone_opts["UNKNOWN"] = "UNKNOWN"
                if unit.drone_id not in drone_opts:
                    drone_opts[unit.drone_id] = unit.drone_id

                def _on_drone_change(e, u=unit) -> None:
                    new_val = e.value
                    if new_val == u.drone_id:
                        return
                    u.drone_id = new_val
                    _sync_folder(u)

                ui.select(
                    options=drone_opts,
                    value=unit.drone_id,
                    label="Drone",
                    on_change=_on_drone_change,
                ).props("dense options-dense").classes("min-w-28")

                def _on_cat_change(e, u=unit) -> None:
                    if e.value != u.category:
                        u.category = e.value

                ui.select(
                    options={c: c for c in CATEGORIES},
                    value=unit.category,
                    label="Cat",
                    on_change=_on_cat_change,
                ).props("dense options-dense").classes("min-w-28")

                def _on_action_change(e, u=unit) -> None:
                    if e.value != u.action:
                        u.action = e.value

                ui.select(
                    options={"move": "📥 Déplacer", "delete": "🗑️ Effacer", "skip": "⏭️ Ignorer"},
                    value=unit.action,
                    label="Action",
                    on_change=_on_action_change,
                ).props("dense options-dense").classes("min-w-36")

    def _render_group_card(self, members: list[MediaUnit]) -> None:
        """Carte représentant un groupe PANORAMA/HYPERLAPSE (plusieurs photos en 1)."""
        first = members[0]
        total_size = sum(u.total_size for u in members)
        total_companions = sum(len(u.companions) for u in members)

        with ui.card().classes("w-full border-2 border-orange-4"):
            thumb = generate_thumbnail(first.main_path, size=256)
            if thumb:
                # Overlay avec le compte de photos
                with ui.element("div").classes("relative w-full"):
                    ui.image(image_to_data_uri(thumb)).classes("w-full h-40 object-cover rounded")
                    with ui.element("div").classes(
                        "absolute top-1 right-1 bg-orange-8 text-white text-caption px-2 py-1 rounded-lg"
                    ):
                        ui.label(f"×{len(members)}").classes("text-body2 font-bold")
                    with ui.element("div").classes(
                        "absolute bottom-1 left-1 bg-black bg-opacity-70 text-white text-caption px-2 py-1 rounded"
                    ):
                        ui.icon("photo_library").classes("text-white")
            else:
                with ui.element("div").classes(
                    "w-full h-40 flex items-center justify-center bg-orange-2 rounded"
                ):
                    ui.icon("photo_library").classes("text-4xl text-orange-8")

            ui.label(f"📂 {first.group_subdir}").classes("text-body1 font-bold truncate").tooltip(
                "\n".join(Path(u.main_path).name for u in members)
            )
            ui.label(f"{len(members)} fichier(s) — {first.category}").classes("text-caption")

            with ui.row().classes("items-center gap-1"):
                ui.badge(first.drone_id, color="primary")
                ui.badge(first.category, color="secondary")
                ui.badge(first.capture_date, color="grey")
                ui.badge(first.group_subdir, color="orange")

            if total_companions:
                ui.label(f"+ {total_companions} compagnon(s) au total").classes("text-caption text-grey-7")
            ui.label(f"Taille totale: {human_size(total_size)}").classes("text-caption")

            # Éditables : drone, catégorie, action — appliqués à TOUS les membres
            with ui.row().classes("w-full gap-1 items-center"):
                drone_opts = {d["id"]: d["id"] for d in CONFIG.get("drone_mapping", [])}
                drone_opts["UNKNOWN"] = "UNKNOWN"
                if first.drone_id not in drone_opts:
                    drone_opts[first.drone_id] = first.drone_id

                def _on_drone_change(e, ms=members) -> None:
                    new_val = e.value
                    for m in ms:
                        m.drone_id = new_val
                        _sync_folder(m)

                ui.select(
                    options=drone_opts,
                    value=first.drone_id,
                    label="Drone",
                    on_change=_on_drone_change,
                ).props("dense options-dense").classes("min-w-28")

                def _on_cat_change(e, ms=members) -> None:
                    for m in ms:
                        m.category = e.value

                ui.select(
                    options={c: c for c in CATEGORIES},
                    value=first.category,
                    label="Cat",
                    on_change=_on_cat_change,
                ).props("dense options-dense").classes("min-w-28")

                def _on_action_change(e, ms=members) -> None:
                    for m in ms:
                        m.action = e.value

                ui.select(
                    options={"move": "📥 Déplacer (groupe)", "delete": "🗑️ Effacer (groupe)", "skip": "⏭️ Ignorer (groupe)"},
                    value=first.action,
                    label="Action",
                    on_change=_on_action_change,
                ).props("dense options-dense").classes("min-w-40")


    # ── actions groupées ───────────────────────────────────────────────────
    def _bulk_action(self, action: str) -> None:
        drone_f = getattr(self._drone_filter_sel, "value", "TOUS")
        cat_f = getattr(self._cat_filter_sel, "value", "TOUTES")
        count = 0
        for u in self.units:
            if (drone_f == "TOUS" or u.drone_id == drone_f) and (cat_f == "TOUTES" or u.category == cat_f):
                u.action = action
                count += 1
        ui.notify(f"{count} média(s) → {action}", type="info")
        self._apply_filters()

    def _bulk_reassign_drone(self) -> None:
        """Réassigne le drone à tous les médias correspondant aux filtres courants."""
        target = getattr(self._bulk_drone_target, "value", None)
        if not target:
            ui.notify("Choisir un drone cible", type="warning")
            return
        # Trouver le dossier correspondant au drone cible
        folder = UNKNOWN_DRONE_DIR
        for d in CONFIG.get("drone_mapping", []):
            if d.get("id") == target:
                folder = d.get("folder", UNKNOWN_DRONE_DIR)
                break

        drone_f = getattr(self._drone_filter_sel, "value", "TOUS")
        cat_f = getattr(self._cat_filter_sel, "value", "TOUTES")
        count = 0
        for u in self.units:
            if (drone_f == "TOUS" or u.drone_id == drone_f) and (cat_f == "TOUTES" or u.category == cat_f):
                u.drone_id = target
                u.drone_folder = folder
                count += 1
        ui.notify(f"{count} média(s) réassignés → {target}", type="positive")
        # Rafraîchir la liste des drones dans le filtre + réappliquer
        try:
            drones = sorted({u.drone_id for u in self.units})
            self._drone_filter_sel.options = ["TOUS"] + drones
            if self._drone_filter_sel.value not in self._drone_filter_sel.options:
                self._drone_filter_sel.value = "TOUS"
            self._drone_filter_sel.update()
        except Exception:
            pass
        self._apply_filters()

    # ── ÉTAPE 4 : Confirmation ─────────────────────────────────────────────
    def _step_confirm(self) -> None:
        with ui.step("Confirmation").props("icon=fact_check"):
            with ui.card().classes("w-full"):
                self._confirm_container = ui.column().classes("w-full gap-1")

            with ui.stepper_navigation():
                ui.button("← Retour", on_click=self._stepper.previous).props("flat")
                ui.button("⚠️ CONFIRMER ET EXÉCUTER", on_click=self._start_execute).props("color=negative icon=play_circle")

    def _goto_confirm(self) -> None:
        self._stepper.next()
        self._render_confirm()

    def _render_confirm(self) -> None:
        if self._confirm_container is None:
            return
        self._confirm_container.clear()
        moved = [u for u in self.units if u.action == "move"]
        deleted = [u for u in self.units if u.action == "delete"]
        skipped = [u for u in self.units if u.action == "skip"]
        total_size_move = sum(u.total_size for u in moved)
        total_size_del = sum(u.total_size for u in deleted)

        with self._confirm_container:
            ui.label("Résumé de l'exécution").classes("text-h6")
            with ui.row().classes("gap-4"):
                with ui.card().classes("bg-primary text-white"):
                    ui.label(f"📥 À déplacer : {len(moved)}").classes("text-body1")
                    ui.label(f"Volume : {human_size(total_size_move)}").classes("text-caption")
                with ui.card().classes("bg-negative text-white"):
                    ui.label(f"🗑️ À effacer : {len(deleted)}").classes("text-body1")
                    ui.label(f"Volume : {human_size(total_size_del)}").classes("text-caption")
                with ui.card():
                    ui.label(f"⏭️ Ignorés : {len(skipped)}").classes("text-body1")

            ui.separator()
            ui.label(f"Corbeille après copie : {'✅ activée' if self.send_to_trash else '❌ désactivée'}").classes("text-body2")
            ui.label(f"Destination racine : {self.destination_dir}").classes("text-caption")

            # Aperçu arborescence
            ui.label("Aperçu de la destination :").classes("text-body2 mt-2")
            tree = self._build_dest_tree(moved)
            with ui.scroll_area().classes("w-full h-64 border rounded"):
                for line in tree:
                    ui.label(line).classes("text-caption font-mono")

    def _build_dest_tree(self, units: list[MediaUnit]) -> list[str]:
        # tree[drone][date][category][group] = [filenames]
        tree: dict[str, dict[str, dict[str, dict[str, list[str]]]]] = {}
        for u in units:
            group = u.group_subdir if u.category in ("PANORAMA", "HYPERLAPSE") and u.group_subdir else ""
            (tree.setdefault(u.drone_folder, {})
                 .setdefault(u.capture_date, {})
                 .setdefault(u.category, {})
                 .setdefault(group, [])
                 .append(Path(u.main_path).name))
        lines: list[str] = []
        for drone, dates in sorted(tree.items()):
            lines.append(f"📂 {drone}/")
            for date, cats in sorted(dates.items()):
                lines.append(f"   📅 {date}/")
                for cat, groups in sorted(cats.items()):
                    total = sum(len(v) for v in groups.values())
                    lines.append(f"      📁 {cat}/  ({total} fichier(s))")
                    for group, files in sorted(groups.items()):
                        if group:
                            lines.append(f"         📁 {group}/  ({len(files)})")
                            for fn in files[:5]:
                                lines.append(f"            · {fn}")
                            if len(files) > 5:
                                lines.append(f"            · … +{len(files) - 5} autres")
                        else:
                            for fn in files[:5]:
                                lines.append(f"         · {fn}")
                            if len(files) > 5:
                                lines.append(f"         · … +{len(files) - 5} autres")
        if not lines:
            lines.append("(rien à déplacer)")
        return lines

    # ── ÉTAPE 5 : Exécution ────────────────────────────────────────────────
    def _step_execute(self) -> None:
        with ui.step("Exécution & rapport").props("icon=done_all"):
            with ui.card().classes("w-full"):
                self._exec_container = ui.column().classes("w-full")
                with self._exec_container:
                    ui.label("En attente…").classes("text-body2")
            with ui.stepper_navigation():
                ui.button("Terminer", on_click=lambda: ui.notify("Merci !", type="positive")).props("color=primary")

    async def _start_execute(self) -> None:
        # Confirmation par dialog
        with ui.dialog() as dialog, ui.card():
            ui.label("Confirmer l'exécution ?").classes("text-h6")
            ui.label("Les originaux copiés seront envoyés à la corbeille."
                     if self.send_to_trash else "Les originaux seront conservés.").classes("text-body2")
            with ui.row():
                ui.button("Annuler", on_click=lambda: dialog.submit("cancel")).props("flat")
                ui.button("CONFIRMER", on_click=lambda: dialog.submit("ok")).props("color=negative")
        result = await dialog
        if result != "ok":
            return
        self._stepper.next()
        await self._run_execution()

    async def _run_execution(self) -> None:
        if self._exec_container is None:
            return
        self._exec_container.clear()
        with self._exec_container:
            ui.label("Exécution en cours…").classes("text-h6")
            prog_label = ui.label("Initialisation…").classes("text-body2")
            prog_bar = ui.linear_progress(0.0, show_value=False).classes("w-full")
            self._exec_log = ui.log(max_lines=500).classes("w-full h-64 border rounded")

        organizer = DJIOrganizer(
            units=self.units,
            destination_dir=self.destination_dir,
            overwrite_policy="ask",
            send_to_trash=self.send_to_trash,
        )

        # Conflict resolver via dialog synchrone-like (via event)
        conflict_choice: dict[str, str] = {}

        def on_conflict(target: Path) -> str:
            # NOTE: NiceGUI ne permet pas facilement un dialog synchrone dans un thread io_bound.
            # Pour rester simple, politique par défaut = rename automatique. L'utilisateur pourra
            # ajuster à l'avenir. Log l'événement.
            self.log(f"⚠️ Conflit détecté : {target} — auto-rename")
            return "rename"

        organizer.on_conflict = on_conflict

        def on_progress(cur: int, total: int, unit: MediaUnit) -> None:
            prog_bar.value = cur / total if total else 0
            prog_label.text = f"{cur}/{total} — {Path(unit.main_path).name}"
            self.log(f"[{cur}/{total}] {unit.action.upper()} {unit.drone_id}/{unit.category} — {Path(unit.main_path).name}")

        organizer.on_progress = on_progress

        try:
            summary = await run.io_bound(organizer.execute)
            self.summary = summary
            self.report_paths = await run.io_bound(organizer.write_reports)
        except Exception as e:
            self.log(f"❌ Exception: {e}\n{traceback.format_exc()}")
            ui.notify(f"Erreur exécution: {e}", type="negative")
            return

        prog_bar.value = 1.0
        prog_label.text = "✅ Terminé"
        self.log(f"✅ Terminé — {summary}")

        with self._exec_container:
            ui.separator()
            ui.label("Résultat").classes("text-h6")
            ui.label(f"Total: {summary['total_units']}  |  📥 Déplacés: {summary['moved']}  |  "
                     f"🗑️ Effacés: {summary['deleted']}  |  ⏭️ Ignorés: {summary['skipped']}  |  "
                     f"❌ Erreurs: {summary['errors']}").classes("text-body1")
            if summary.get("by_drone"):
                ui.label("Par drone : " + ", ".join(f"{k}={v}" for k, v in summary["by_drone"].items())).classes("text-caption")
            if summary.get("by_category"):
                ui.label("Par catégorie : " + ", ".join(f"{k}={v}" for k, v in summary["by_category"].items())).classes("text-caption")
            ui.separator()
            ui.label("Rapports générés :").classes("text-body2")
            for key, path in self.report_paths.items():
                with ui.row().classes("items-center gap-2"):
                    ui.icon("description")
                    ui.label(f"{key}: {path}").classes("text-caption font-mono")
        ui.notify("✅ Exécution terminée !", type="positive", timeout=10000)


# ─────────────────────────────────────────────────────────────────────────────
# Helper (correctif closure drone_folder)
# ─────────────────────────────────────────────────────────────────────────────
def _sync_folder(unit: MediaUnit) -> None:
    for d in CONFIG.get("drone_mapping", []):
        if d["id"] == unit.drone_id:
            unit.drone_folder = d["folder"]
            return
    unit.drone_folder = UNKNOWN_DRONE_DIR


# ─────────────────────────────────────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────────────────────
_app_instance = DJIOrganizatorApp()


@ui.page("/")
def index_page():
    _app_instance.build()


if __name__ in {"__main__", "__mp_main__"}:
    parser = argparse.ArgumentParser(description="DJI Organizator — Classement média drones")
    parser.add_argument("--server-only", action="store_true", help="Mode serveur web (sans fenêtre native)")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8192)
    args, _ = parser.parse_known_args()

    if args.server_only:
        try:
            selected_port = _find_available_port(args.host, args.port)
        except RuntimeError as e:
            print(f"❌ {e}")
            sys.exit(1)
        if selected_port != args.port:
            print(f"⚠️ Port {args.port} occupé, bascule automatique sur {selected_port}.")
        print(f"🌐 Serveur : http://{args.host}:{selected_port}")
        ui.run(
            title="DJI Organizator",
            host=args.host,
            port=selected_port,
            native=False,
            show=False,
            dark=True,
            reload=False,
            reconnect_timeout=30.0,
        )
    else:
        ui.run(
            title="DJI Organizator",
            port=args.port,
            native=True,
            dark=True,
            window_size=tuple(CONFIG.get("window_size", [1500, 950])),
            reload=False,
            reconnect_timeout=30.0,
        )
