"""DJI Organizator — Classement automatique des médias de drones DJI.

Détecte le drone (Mini2 MEO, Neo2 CLEO, Avata2 GINO, Mini4 Pro PEDRO), la catégorie
(VIDEO / PHOTO / PANORAMA / HYPERLAPSE) et copie vers une arborescence datée.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import calendar
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
from typing import Any, Callable, Optional

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
# .pan = fichier de projet panorama DJI (à déplacer avec le média principal)
COMPANION_EXTS = {".srt", ".lrf", ".lut", ".cube", ".xmp", ".thm", ".wav", ".pan"}
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
# ID de l'entrée Goggles (utilisé pour l'assignation par défaut des .mov)
GOGGLES_DRONE_ID = "GOGGLES-N3"

CATEGORIES = ["VIDEO", "PHOTO", "PANORAMA", "HYPERLAPSE", "GOGGLES", "REALITY_SCAN", "WAYPOINTS"]

# Marqueurs de projets RealityScan (reconstruction 3D)
# .rsproj = fichier projet, .rsinfo = infos LOD (accompagne .ply)
RS_PROJECT_MARKER_EXTS = {".rsproj"}
RS_COMPANION_EXTS = {".rsproj", ".rsinfo", ".ply"}

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG: dict[str, Any] = {
    "source_dir": DEFAULT_SOURCE,
    "destination_dir": DEFAULT_DEST,
    "drone_mapping": [
        {"pattern": r"mini\s*2(?!\s*pro)", "id": "MINI2-MEO", "label": "DJI Mini 2 — MEO",
         "folder": "00-DJI-MINI2-MEO", "image": "assets/mini2.jpg"},
        {"pattern": r"neo", "id": "NEO2-CLEO", "label": "DJI Neo 2 — CLEO",
         "folder": "00-DJI-NEO2-CLEO", "image": "assets/neo.jpg"},
        {"pattern": r"avata\s*2", "id": "AVATA2-GINO", "label": "DJI Avata 2 — GINO",
         "folder": "00-DJI-AVATA2-GINO", "image": "assets/avata2.jpg"},
        {"pattern": r"mini\s*4\s*pro|fc8482", "id": "MINI4PRO-PEDRO", "label": "DJI Mini 4 Pro — PEDRO",
         "folder": "00-DJI-MINI4PRO-PEDRO", "image": "assets/mini4pro.jpg"},
        {"pattern": r"goggles|dji\s*goggles|integra", "id": "GOGGLES-N3", "label": "DJI Goggles N3",
         "folder": "00-DJI-GOGGLES-N3", "image": "assets/goggles N3.jpg"},
    ],
    "send_to_trash_after_copy": True,
    "window_size": [1500, 950],
    "map_tile_provider": "osm",
    # Tags : liste de {name, color (hex), icon (emoji), hidden (bool)}
    # `hidden=True` : le média porteur est masqué par défaut dans le visualiseur.
    "tags": [
        {"name": "vol test",         "color": "#42A5F5", "icon": "🧪", "hidden": False},
        {"name": "coucher de soleil","color": "#FF7043", "icon": "🌇", "hidden": False},
        {"name": "3d",               "color": "#7C4DFF", "icon": "🧱", "hidden": False},
        {"name": "favori",           "color": "#FFC107", "icon": "⭐", "hidden": False},
        {"name": "NSFW",             "color": "#E53935", "icon": "🔞", "hidden": True},
    ],
    # Le visualiseur affiche-t-il aussi les médias porteurs d'un tag `hidden` ?
    "viewer_show_hidden_tags": False,
}

# Fournisseurs de tuiles pour la mini-carte GPS
MAP_TILE_PROVIDERS: dict[str, tuple[str, str, str]] = {
    # id: (label, url_template, attribution)
    "osm":       ("Plan (OSM)",         "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                  "&copy; OpenStreetMap"),
    "satellite": ("Satellite (Esri)",   "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                  "Tiles &copy; Esri"),
    "hybrid":    ("Hybride (Esri+labels)",
                  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                  "Tiles &copy; Esri"),
    "topo":      ("Topographique",      "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
                  "&copy; OpenTopoMap (CC-BY-SA)"),
    "dark":      ("Sombre (Carto)",     "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
                  "&copy; CARTO"),
    "light":     ("Clair (Carto)",      "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
                  "&copy; CARTO"),
}
# Calque de labels pour la vue "hybrid" (par-dessus le satellite)
_HYBRID_LABELS_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"


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
    project_root: str = ""              # racine d'un projet RealityScan (préserve arborescence interne)
    action: str = "move"                # move | delete | skip
    detection_reason: str = ""
    error: str = ""
    tags: list[str] = field(default_factory=list)  # noms de tags (voir CONFIG["tags"])
    custom_name: str = ""               # nom personnalisé donné par l'utilisateur

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
                common_args = ["-G", "-a", "-n"]
                # Compatibilité ancienne vs nouvelle version de pyexiftool :
                # les versions récentes (≥0.7?) ont supprimé params= au profit de common_args=
                import inspect as _inspect
                try:
                    _sig = _inspect.signature(et.get_metadata)
                    _params_ok = "params" in _sig.parameters
                except Exception:
                    _params_ok = True  # fallback : on tente params par défaut
                try:
                    if _params_ok:
                        metadata_list = et.get_metadata(files, params=common_args)
                    else:
                        metadata_list = et.get_metadata(files, common_args=common_args)
                except TypeError:
                    # dernière chance : appels directs sans argument nommé
                    metadata_list = et.get_metadata(files, common_args)
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

        Ordre : (1) EXIF/QT Model, (2) champs XMP DJI, (3) balayage global
        (n'importe quel champ contenant FC9470, dvtm_NEO2, model_name:…),
        (4) nom de fichier (FC7203…), (5) hints dans le chemin.
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

        # ── 3. Balayage global — cherche codes caméra dans TOUS les champs ──
        # DJI encode parfois le modèle dans des tags obscurs, ex:
        #   QuickTime:Comment / UserData:… → "pb_file:dvtm_NEO2.proto; model_name:FC9470; …"
        # On agrège tous les champs texte et on recherche des marqueurs connus.
        big_blob = ""
        for k, v in metadata.items():
            if v is None:
                continue
            try:
                big_blob += " " + str(v).lower()
            except Exception:
                continue
        # Marqueurs prioritaires (code caméra ou nom protobuf DJI)
        marker_hints: list[tuple[str, str, str]] = [
            (r"fc7203|dvtm_mini2\b|dvtm_mavicmini2", "MINI2-MEO", "FC7203/dvtm_MINI2"),
            (r"fc7303",                              "MINI2-MEO", "FC7303"),
            (r"fc8482|dvtm_mini4pro|mini\s*4\s*pro", "MINI4PRO-PEDRO", "FC8482/dvtm_MINI4PRO"),
            (r"fc9470|dvtm_neo2|model_name\s*:\s*fc9470", "NEO2-CLEO", "FC9470/dvtm_NEO2"),
            (r"fc8283|dvtm_neo\b",                   "NEO2-CLEO", "FC8283/dvtm_NEO"),
            (r"xt2|dvtm_avata2|avata\s*2",           "AVATA2-GINO", "XT2/dvtm_AVATA2"),
        ]
        for pat, drone_id, tag in marker_hints:
            if re.search(pat, big_blob):
                folder = self._folder_for(drone_id)
                return drone_id, folder, f"Meta scan: {tag} → {drone_id}"

        # ── 4. Nom fichier — modèles caméra DJI ──
        # FC7203=Mini 2, FC7303=Mini 2 SE, FC7503=Mini SE, FC8482=Mini 4 Pro,
        # FC3411=Air 2S, FC3170=Air 2, FC8283=Neo, FC9470=Neo2, XT2=Avata2, etc.
        name = Path(path).name.upper() if path else ""
        # Certaines caméras encodent le modèle dans le nom fichier ou dans SourceFile
        file_hints = {
            r"FC7203": "MINI2-MEO",
            r"FC7303": "MINI2-MEO",
            r"FC8482": "MINI4PRO-PEDRO",
            r"FC8283": "NEO2-CLEO",
            r"FC9470": "NEO2-CLEO",
        }
        for pat, drone_id in file_hints.items():
            if re.search(pat, name):
                folder = self._folder_for(drone_id)
                return drone_id, folder, f"Nom fichier: {pat} → {drone_id}"

        # ── 5. Hints dans le chemin (nom du drone dans un dossier parent) ──
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

        # 3b. Champs DJI panorama spécifiques (PanoramaFrameNum, IsPanorama…)
        pano_keys_hit = []
        for k, v in metadata.items():
            kl = str(k).lower()
            if "pano" not in kl:
                continue
            # ignore ShootingMode déjà couvert et champs génériques vides
            if v is None or str(v).strip() == "":
                continue
            pano_keys_hit.append(f"{k}={v}")
            if len(pano_keys_hit) >= 2:
                break
        if pano_keys_hit:
            return "PANORAMA", f"metadata panorama: {'; '.join(pano_keys_hit)}"

        # 4. Nom fichier
        name = p.stem.upper()
        if "_PANO_" in name or name.endswith("_PANO") or "PANORAMA" in name:
            return "PANORAMA", f"nom fichier: {p.name}"
        if "_HYPER" in name or "HYPERLAPSE" in name:
            return "HYPERLAPSE", f"nom fichier: {p.name}"

        # 5. Extension → VIDEO ou PHOTO
        ext = p.suffix.lower()
        # Les .mov proviennent presque toujours des DJI Goggles (ou d'un écran
        # d'enregistrement de manette). On les regroupe pour tri manuel ultérieur.
        if ext == ".mov":
            return "GOGGLES", "extension .mov (Goggles/screen record)"
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
    def _strip_variant(stem_lower: str) -> str:
        """Enlève les suffixes de variante DJI (_D, _color, _lut, _proxy, _preview)."""
        return re.sub(r"_(d|color|lut|proxy|preview)$", "", stem_lower)

    @staticmethod
    def find_companions_split(main_path: str, all_files_in_dir: list[str]) -> tuple[list[str], list[str]]:
        """Retourne (exact_matches, base_stem_matches).

        - `exact_matches` : même stem exact (mêmes suffixes de variante).
          Priorité maximale : DJI_XXX_D.LRF doit aller sur DJI_XXX_D.MP4.
        - `base_stem_matches` : base_stem identique (variantes croisées).
          Fallback si aucun main à stem exact n'existe.
        """
        main_p = Path(main_path)
        stem_lower = main_p.stem.lower()
        parent = main_p.parent
        base_stem = DJIClassifier._strip_variant(stem_lower)

        exact: list[str] = []
        base: list[str] = []
        for f in all_files_in_dir:
            fp = Path(f)
            if str(fp) == str(main_p):
                continue
            if fp.parent != parent:
                continue
            fstem_lower = fp.stem.lower()
            if fstem_lower == stem_lower:
                exact.append(str(fp))
            elif DJIClassifier._strip_variant(fstem_lower) == base_stem:
                base.append(str(fp))
        return exact, base

    @staticmethod
    def find_companions(main_path: str, all_files_in_dir: list[str]) -> list[str]:
        """Trouve tous les fichiers avec le même stem (ou variantes DJI).

        Version plate (exact puis base) — préférer `find_companions_split` pour
        un contrôle fin de la priorité dans le scanner.
        """
        exact, base = DJIClassifier.find_companions_split(main_path, all_files_in_dir)
        return exact + base


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

    def _detect_reality_scan_projects(self, all_files: list[str]) -> tuple[list[MediaUnit], set[str]]:
        """Détecte les projets RealityScan et retourne (units, fichiers_consommés).

        Un projet = dossier contenant un `.rsproj`. Tous les fichiers (et sous-dossiers,
        y compris un DJI_XXX imbriqué) sous ce dossier sont regroupés en une unité et
        SORTIS du scan normal. Structure interne préservée à la destination.
        """
        # 1. Trouver tous les .rsproj
        rsproj_files = [f for f in all_files if Path(f).suffix.lower() in RS_PROJECT_MARKER_EXTS]
        if not rsproj_files:
            return [], set()

        # 2. Détermine les racines de projet distinctes (dossier contenant le .rsproj)
        # Si plusieurs .rsproj sont imbriqués, on garde le dossier PARENT le plus haut.
        # Refuse aussi qu'un projet ait pour racine le source_dir lui-même
        # (cela consommerait tout le scan).
        src_root_norm = os.path.normpath(self.source_dir)
        project_roots_all = [os.path.normpath(str(Path(f).parent)) for f in rsproj_files]
        project_roots_all = [p for p in project_roots_all if p.lower() != src_root_norm.lower()]
        project_roots_all = sorted(set(project_roots_all), key=len)  # court d'abord
        project_roots: list[str] = []
        for pr in project_roots_all:
            if not any(pr.lower().startswith(existing.lower() + os.sep) for existing in project_roots):
                project_roots.append(pr)

        # 3. Constitue une unité par projet
        units: list[MediaUnit] = []
        consumed: set[str] = set()
        for proj_root in project_roots:
            proj_lower = proj_root.lower() + os.sep
            files_in_proj = [
                f for f in all_files
                if os.path.normpath(f).lower().startswith(proj_lower)
            ]
            if not files_in_proj:
                continue
            consumed.update(files_in_proj)

            # Fichier principal : le .rsproj à la racine du projet, sinon le premier trouvé
            main = None
            for f in files_in_proj:
                p = Path(f)
                if p.suffix.lower() in RS_PROJECT_MARKER_EXTS and \
                        os.path.normpath(str(p.parent)).lower() == proj_root.lower():
                    main = f
                    break
            if main is None:
                # Fallback : premier .rsproj
                for f in files_in_proj:
                    if Path(f).suffix.lower() in RS_PROJECT_MARKER_EXTS:
                        main = f
                        break
            if main is None:
                continue

            companions = [f for f in files_in_proj if f != main]

            # Détection drone : cherche un fichier au nom DJI-typique dans le projet
            drone_id = "UNKNOWN"
            drone_folder = UNKNOWN_DRONE_DIR
            drone_reason = "RealityScan: aucun indice DJI trouvé"
            drone_meta: dict[str, Any] = {}
            fname_blob = " ".join(Path(f).name.lower() for f in files_in_proj)
            # Motifs de modèle
            model_hints = [
                ("fc8482", "MINI4PRO-PEDRO"),
                ("fc7303", "MINI2-MEO"),
                ("fc7203", "MINI2-MEO"),
                ("fc8283", "NEO2-CLEO"),
                ("fc9470", "NEO2-CLEO"),
            ]
            for hint, did in model_hints:
                if hint in fname_blob:
                    drone_id = did
                    drone_folder = self.classifier._folder_for(did)
                    drone_reason = f"RealityScan: filename contient {hint.upper()}"
                    break
            else:
                # Fallback : extrait EXIF Model depuis 1 photo nested (DJI_*.JPG / .DNG)
                for f in files_in_proj:
                    if Path(f).suffix.lower() in {".jpg", ".jpeg", ".dng"}:
                        try:
                            m = self.extractor.extract_batch([f])
                            drone_meta = m.get(os.path.normpath(f), {})
                            did, folder, reason = self.classifier.detect_drone(drone_meta, f)
                            if did != "UNKNOWN":
                                drone_id = did
                                drone_folder = folder
                                drone_reason = f"RealityScan: {reason} (via {Path(f).name})"
                                break
                        except Exception:
                            pass

            # Date : mtime du .rsproj (date de création/dernière sauvegarde du projet)
            try:
                date_str = datetime.fromtimestamp(os.path.getmtime(main)).strftime("%Y-%m-%d")
            except OSError:
                date_str = datetime.now().strftime("%Y-%m-%d")

            proj_name = Path(proj_root).name

            unit = MediaUnit(
                main_path=main,
                companions=companions,
                metadata=drone_meta,
                drone_id=drone_id,
                drone_folder=drone_folder,
                category="REALITY_SCAN",
                capture_date=date_str,
                group_subdir=proj_name,
                project_root=proj_root,
                action="move",
                detection_reason=(
                    f"Drone: {drone_reason} | "
                    f"Cat: projet RealityScan (.rsproj) — {len(files_in_proj)} fichier(s)"
                ),
            )
            units.append(unit)
        return units, consumed

    def scan(self, progress_cb: Optional[callable] = None) -> list[MediaUnit]:
        media_files, all_files = self._list_all_files()

        # ── Détection préalable des projets RealityScan ──
        # Retire leurs fichiers du scan média normal (incluant DJI_XXX imbriqué).
        rs_units, rs_consumed = self._detect_reality_scan_projects(all_files)
        if rs_consumed:
            media_files = [f for f in media_files if f not in rs_consumed]
            all_files = [f for f in all_files if f not in rs_consumed]

        if not media_files:
            return rs_units

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

        # ── 2 passes : d'abord tout ce qui est un compagnon avec stem EXACT,
        # puis les stems "base" (variantes croisées). Ainsi DJI_XXX_D.LRF est
        # correctement rattaché à DJI_XXX_D.MP4 même si DJI_XXX.MP4 existe.
        exact_map: dict[str, list[str]] = {}
        base_map: dict[str, list[str]] = {}
        for media_path in media_files:
            ex, ba = self.classifier.find_companions_split(media_path, all_files)
            # Ignore les fichiers qui sont eux-mêmes des médias principaux
            ex = [c for c in ex if c not in media_files]
            ba = [c for c in ba if c not in media_files]
            exact_map[media_path] = ex
            base_map[media_path] = ba

        # Passe 1 : compagnons EXACTS (priorité absolue)
        companions_per_media: dict[str, list[str]] = {mp: [] for mp in media_files}
        for media_path, ex in exact_map.items():
            for c in ex:
                if c in used_companions:
                    continue
                companions_per_media[media_path].append(c)
                used_companions.add(c)

        # Passe 2 : compagnons via base_stem (variantes croisées)
        for media_path, ba in base_map.items():
            for c in ba:
                if c in used_companions:
                    continue
                companions_per_media[media_path].append(c)
                used_companions.add(c)

        for idx, media_path in enumerate(media_files):
            if progress_cb:
                progress_cb("Classification et groupement…", idx + 1, len(media_files))
            meta = metadata_map.get(os.path.normpath(media_path), {})
            drone_id, drone_folder, drone_reason = self.classifier.detect_drone(meta, media_path)
            category, cat_reason = self.classifier.detect_category(media_path, meta)

            # Si catégorie = GOGGLES et aucun drone n'a été identifié via des
            # métadonnées fiables, on assigne au drone Goggles configuré.
            # (Les .mov des goggles/manettes n'ont généralement pas de tag EXIF.)
            if category == "GOGGLES" and (
                drone_id == "UNKNOWN" or drone_reason.startswith("Aucun match")
            ):
                goggles = self.classifier._folder_for(GOGGLES_DRONE_ID)
                if goggles != UNKNOWN_DRONE_DIR:  # entrée Goggles configurée
                    drone_id = GOGGLES_DRONE_ID
                    drone_folder = goggles
                    drone_reason = "auto: .mov → Goggles"

            date_str = self.classifier.detect_capture_date(media_path, meta)
            companions = companions_per_media.get(media_path, [])

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

        # ── Promotion WAYPOINTS (mission GPS multi-position, avant PANORAMA) ──
        self._promote_folder_waypoints(units)

        # ── Promotion PANORAMA basée sur le comptage/pattern du dossier ──
        self._promote_folder_panoramas(units)

        # ── Assignation des sous-dossiers de groupe pour PANORAMA / HYPERLAPSE ──
        self._assign_group_subdirs(units)

        # Concatène les unités RealityScan détectées en début de scan
        return rs_units + units

    def _promote_folder_waypoints(self, units: list[MediaUnit]) -> None:
        """Promeut en WAYPOINTS des groupes de PHOTOs prises en mission GPS.

        Signature d'une mission "waypoints" (trajet prédéterminé pour recon 3D) :
          - ≥ 8 photos PHOTO dans le même dossier
          - la MAJORITÉ (≥ 70 %) des paires consécutives a une distance GPS
            > 3 m (le drone se déplace, contrairement à un panorama fixe)
          - au moins 10 m d'étendue totale (bbox lat/lon)
        Cette passe tourne AVANT `_promote_folder_panoramas` pour éviter qu'un
        vol de reconstruction ne soit confondu avec un panorama.
        """
        if not units:
            return
        src_root = os.path.normpath(self.source_dir).lower()

        def _ts(u: MediaUnit) -> Optional[datetime]:
            for k in ("EXIF:DateTimeOriginal", "EXIF:CreateDate",
                      "QuickTime:CreateDate", "QuickTime:MediaCreateDate",
                      "XMP:DateTimeOriginal", "XMP:CreateDate",
                      "File:FileModifyDate"):
                v = u.metadata.get(k)
                if not v:
                    continue
                s = str(v).strip()
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

        def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
            # Distance grande-cercle en mètres
            import math
            lat1, lon1 = math.radians(a[0]), math.radians(a[1])
            lat2, lon2 = math.radians(b[0]), math.radians(b[1])
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
            return 2 * 6371000.0 * math.asin(math.sqrt(h))

        # Regroupe PHOTOs par dossier parent
        by_parent: dict[str, list[MediaUnit]] = {}
        for u in units:
            if u.category != "PHOTO":
                continue
            parent = os.path.normpath(str(Path(u.main_path).parent)).lower()
            if parent == src_root:
                continue
            by_parent.setdefault(parent, []).append(u)

        for parent, group in by_parent.items():
            if len(group) < 8:
                continue
            # Récupère GPS + timestamp
            enriched: list[tuple[Optional[datetime], tuple[float, float], MediaUnit]] = []
            for u in group:
                gps = extract_gps(u.metadata)
                if gps is None:
                    continue
                enriched.append((_ts(u), gps, u))
            if len(enriched) < 8:
                continue
            # Trie par timestamp (fallback nom fichier)
            enriched.sort(key=lambda t: (t[0] or datetime.max, Path(t[2].main_path).name.lower()))

            # Étendue bbox
            lats = [g[0] for _, g, _ in enriched]
            lons = [g[1] for _, g, _ in enriched]
            # Grand-cercle diagonale bbox
            bbox_span = _haversine_m((min(lats), min(lons)), (max(lats), max(lons)))
            if bbox_span < 10.0:
                continue  # tout est concentré → probable panorama

            # Ratio de paires "mobiles" (> 3 m entre 2 shots consécutifs)
            moving_pairs = 0
            total_pairs = 0
            for i in range(1, len(enriched)):
                d = _haversine_m(enriched[i - 1][1], enriched[i][1])
                total_pairs += 1
                if d > 3.0:
                    moving_pairs += 1
            if total_pairs == 0:
                continue
            move_ratio = moving_pairs / total_pairs
            if move_ratio < 0.7:
                continue

            parent_name = Path(group[0].main_path).parent.name
            for _, _, u in enriched:
                u.category = "WAYPOINTS"
                u.detection_reason = (
                    f"{u.detection_reason} | promu WAYPOINTS: "
                    f"{len(enriched)} photos GPS dans {parent_name} "
                    f"(bbox {int(bbox_span)}m, {int(move_ratio*100)}% paires mobiles)"
                )

    def _promote_folder_panoramas(self, units: list[MediaUnit]) -> None:
        """Promeut en PANORAMA des groupes de PHOTOs isolés dans un dossier.

        Heuristiques (chaque photo du groupe doit rester PHOTO au départ) :
          - Dossier parent = pattern DCIM DJI `NNN_NNNN` (ex: 100_0089) et ≥ 3
            photos captées à < 5 s d'écart consécutivement → PANORAMA.
          - Sinon, dossier parent (hors racine source) contenant ≥ 9 photos où
            toutes tiennent dans une fenêtre de 60 s → PANORAMA.
        Compte de référence DJI : sphère=34, 180°=21, wide=9, vertical=3.
        """
        if not units:
            return
        src_root = os.path.normpath(self.source_dir).lower()
        dcim_pat = re.compile(r"^\d{3}_\d{4}$")

        # Timestamp helper (identique à celui de _assign_group_subdirs)
        def _ts(u: MediaUnit) -> Optional[datetime]:
            for k in ("EXIF:DateTimeOriginal", "EXIF:CreateDate",
                      "QuickTime:CreateDate", "QuickTime:MediaCreateDate",
                      "XMP:DateTimeOriginal", "XMP:CreateDate",
                      "File:FileModifyDate"):
                v = u.metadata.get(k)
                if not v:
                    continue
                s = str(v).strip()
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

        # Group PHOTO units by parent folder
        by_parent: dict[str, list[MediaUnit]] = {}
        for u in units:
            if u.category != "PHOTO":
                continue
            parent = os.path.normpath(str(Path(u.main_path).parent)).lower()
            if parent == src_root:
                continue  # racine = pas un groupe pano typique
            by_parent.setdefault(parent, []).append(u)

        for parent, group in by_parent.items():
            if len(group) < 3:
                continue
            parent_name = Path(group[0].main_path).parent.name
            is_dcim = bool(dcim_pat.match(parent_name))

            # Récupère timestamps et trie
            enriched = [(_ts(u), u) for u in group]
            # Photos sans TS → mtime fallback dans _ts, quasi jamais None
            enriched.sort(key=lambda t: (t[0] or datetime.max, Path(t[1].main_path).name.lower()))

            # Cas A : dossier DCIM DJI `NNN_NNNN`
            # → si ≥ 3 photos consécutives à < 5 s d'écart : PANORAMA
            if is_dcim and len(enriched) >= 3:
                # Vérifie qu'au moins 3 photos consécutives sont dans une fenêtre serrée
                tight_run = 1
                max_run = 1
                prev_ts = enriched[0][0]
                for ts, _ in enriched[1:]:
                    if ts and prev_ts and (ts - prev_ts).total_seconds() <= 5:
                        tight_run += 1
                        max_run = max(max_run, tight_run)
                    else:
                        tight_run = 1
                    prev_ts = ts
                if max_run >= 3:
                    for _, u in enriched:
                        u.category = "PANORAMA"
                        u.detection_reason = (
                            f"{u.detection_reason} | promu PANORAMA: "
                            f"dossier DCIM {parent_name} ({len(enriched)} photos, "
                            f"burst {max_run}×≤5s)"
                        )
                    continue  # évite double promotion

            # Cas B : ≥ 9 photos et toutes dans une fenêtre de 60 s
            if len(enriched) >= 9:
                ts_list = [t for t, _ in enriched if t]
                if len(ts_list) >= 9:
                    window = (max(ts_list) - min(ts_list)).total_seconds()
                    if window <= 60:
                        for _, u in enriched:
                            u.category = "PANORAMA"
                            u.detection_reason = (
                                f"{u.detection_reason} | promu PANORAMA: "
                                f"{len(enriched)} photos en {int(window)}s dans {parent_name}"
                            )

    def _assign_group_subdirs(self, units: list[MediaUnit]) -> None:
        """Attribue un sous-dossier de groupe aux médias PANORAMA, HYPERLAPSE et WAYPOINTS.

        Règle : on prend le nom du dossier parent immédiat de la source si celui-ci
        n'est PAS le dossier racine `00-DJI-A-TRIER` (ni un dossier générique
        PANORAMA/HYPERLAPSE seul). Si aucun sous-dossier utile, on groupe par
        proximité temporelle (photos prises à moins de 90 s d'écart sur le même
        drone/jour = même panorama) et on assigne PANO_1, PANO_2…
        """
        src_root = os.path.normpath(self.source_dir).lower()
        generic_names = {"panorama", "pano", "hyperlapse", "hyper", "photo", "photos",
                         "video", "videos", "dcim", "media", "waypoints", "waypoint",
                         "mission", "missions"}

        # 1re passe : essayer d'utiliser le dossier parent source
        for u in units:
            if u.category not in ("PANORAMA", "HYPERLAPSE", "WAYPOINTS"):
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
            if u.category not in ("PANORAMA", "HYPERLAPSE", "WAYPOINTS") or u.group_subdir:
                continue
            key = (u.drone_folder, u.capture_date, u.category)
            buckets.setdefault(key, []).append((_ts(u), u))

        for (drone_folder, date, cat), lst in buckets.items():
            prefix = {"PANORAMA": "PANO", "HYPERLAPSE": "HYPER", "WAYPOINTS": "WPT"}[cat]
            # Tri par timestamp (None en dernier), puis par nom pour stabilité
            lst.sort(key=lambda t: (t[0] or datetime.max, Path(t[1].main_path).name.lower()))
            # Regroupe : nouveau groupe si écart > 90 s avec le précédent (180 s pour WAYPOINTS)
            gap_threshold = 180.0 if cat == "WAYPOINTS" else 90.0
            group_idx = 0
            last_ts: Optional[datetime] = None
            for ts, u in lst:
                if last_ts is None or ts is None or (ts - last_ts).total_seconds() > gap_threshold:
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
        self.on_duplicate: Optional[callable] = None  # callback(src, existing_target)

    def _destination_for(self, unit: MediaUnit, file_path: str) -> Path:
        # Pour le drone Goggles, on omet le sous-dossier de catégorie
        # (les .mov des goggles/manettes vont directement dans YYYY-MM-DD/).
        if unit.drone_id == GOGGLES_DRONE_ID:
            dest_root = Path(self.destination_dir) / unit.drone_folder / unit.capture_date
        else:
            dest_root = Path(self.destination_dir) / unit.drone_folder / unit.capture_date / unit.category
            # Sous-dossier de groupe pour PANORAMA/HYPERLAPSE/WAYPOINTS
            if unit.group_subdir and unit.category in ("PANORAMA", "HYPERLAPSE", "WAYPOINTS"):
                dest_root = dest_root / unit.group_subdir

        # ── RealityScan : préserve l'arborescence interne du projet ──
        # dest = {drone}/{date}/REALITY_SCAN/{project_name}/{chemin_relatif_au_project_root}
        if unit.category == "REALITY_SCAN" and unit.project_root:
            proj_name = unit.group_subdir or Path(unit.project_root).name
            rs_root = Path(self.destination_dir) / unit.drone_folder / unit.capture_date / \
                unit.category / proj_name
            try:
                rel = os.path.relpath(file_path, unit.project_root)
            except ValueError:
                rel = Path(file_path).name  # cross-drive fallback
            return rs_root / rel

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

                # action == "move" (rename si même volume, sinon copie+corbeille)
                copied: list[dict[str, str]] = []
                moved_directly: list[dict[str, str]] = []
                duplicates: list[dict[str, str]] = []  # source identique à un fichier destination existant
                main_final_target: Optional[Path] = None  # pour écrire le sidecar à côté du média principal
                for fp in unit.all_files:
                    if not os.path.exists(fp):
                        continue
                    target = self._destination_for(unit, fp)
                    target.parent.mkdir(parents=True, exist_ok=True)

                    # ── Détection doublon : si un fichier avec le même nom existe déjà
                    # à destination ET a exactement le même contenu, on ne copie pas.
                    # Le source sera envoyé à la corbeille comme un doublon confirmé.
                    if target.exists() and _files_identical(fp, target):
                        duplicates.append({
                            "source": fp,
                            "existing": str(target),
                            "size": str(os.path.getsize(fp)),
                            "status": "identical",
                        })
                        if self.on_duplicate:
                            try:
                                self.on_duplicate(fp, str(target))
                            except Exception:
                                pass
                        # Le sidecar reste attaché au fichier destination existant
                        if os.path.normpath(fp) == os.path.normpath(unit.main_path):
                            main_final_target = target
                        continue

                    final_target = self._resolve_conflict(target)
                    if final_target is None:
                        copied.append({"source": fp, "target": str(target), "status": "skipped_conflict"})
                        continue

                    src_size = os.path.getsize(fp)
                    if _same_volume(fp, final_target):
                        # Déplacement direct (instantané, pas de copie)
                        os.replace(fp, final_target)
                        moved_directly.append({"source": fp, "target": str(final_target), "status": "ok"})
                    else:
                        # Copie + envoi corbeille du source (plus tard)
                        shutil.copy2(fp, final_target)
                        if os.path.getsize(final_target) != src_size:
                            raise IOError(f"Taille différente après copie: {fp} → {final_target}")
                        copied.append({"source": fp, "target": str(final_target), "status": "ok"})

                    if os.path.normpath(fp) == os.path.normpath(unit.main_path):
                        main_final_target = final_target

                # Écrit le sidecar {stem}.dji.json à côté du fichier média principal
                sidecar_path: Optional[str] = None
                if main_final_target is not None:
                    try:
                        sidecar_path = self._write_sidecar_json(unit, main_final_target)
                    except Exception as e:
                        self.errors.append({"file": str(main_final_target), "error": f"sidecar failed: {e}"})

                # Envoi corbeille des originaux copiés (les moved_directly n'existent déjà plus)
                trashed: list[str] = []
                if self.send_to_trash and send2trash:
                    for c in copied:
                        if c.get("status") == "ok":
                            try:
                                send2trash(c["source"])
                                trashed.append(c["source"])
                            except Exception as e:
                                self.errors.append({"file": c["source"], "error": f"trash failed: {e}"})
                    # Doublons : source envoyé à la corbeille même sans copie
                    for d in duplicates:
                        try:
                            send2trash(d["source"])
                            trashed.append(d["source"])
                        except Exception as e:
                            self.errors.append({"file": d["source"], "error": f"trash duplicate failed: {e}"})

                self.results.append({
                    "unit": unit.main_path,
                    "action": "move",
                    "drone": unit.drone_id,
                    "category": unit.category,
                    "date": unit.capture_date,
                    "files": unit.all_files,
                    "copied": copied,
                    "moved_directly": moved_directly,
                    "duplicates": duplicates,
                    "trashed": trashed,
                    "sidecar_json": sidecar_path,
                })
            except Exception as e:
                self.errors.append({
                    "unit": unit.main_path,
                    "error": str(e),
                    "trace": traceback.format_exc(),
                })
        return self._build_summary()

    def _write_sidecar_json(self, unit: MediaUnit, main_dest: Path) -> str:
        """Écrit `{stem}.dji.json` à côté du média destination avec TOUTES les métadonnées.

        Ce fichier sert pour la recherche/indexation ultérieure. Il contient :
        - Info de classement (drone, catégorie, date, group_subdir, action)
        - Chemins source/destination
        - Companions (LRF/SRT/LUT/...)
        - Dump ExifTool complet (metadata_exiftool)
        """
        sidecar = main_dest.with_suffix(main_dest.suffix + ".dji.json")
        # Alternative plus courte : `.dji.json` remplace l'extension
        # Ici on garde le format `{nom_complet}.dji.json` pour éviter les collisions
        # (ex: DJI_XXX.MP4 + DJI_XXX.JPG → DJI_XXX.MP4.dji.json + DJI_XXX.JPG.dji.json)

        payload: dict[str, Any] = {
            "schema": "dji_organizator.metadata.v1",
            "generated_at": datetime.now().isoformat(),
            "classification": {
                "drone_id": unit.drone_id,
                "drone_folder": unit.drone_folder,
                "category": unit.category,
                "capture_date": unit.capture_date,
                "group_subdir": unit.group_subdir,
                "detection_reason": unit.detection_reason,
                "tags": list(unit.tags or []),
                "custom_name": unit.custom_name or "",
            },
            "files": {
                "source": unit.main_path,
                "destination": str(main_dest),
                "companions": unit.companions,
                "size_bytes": (os.path.getsize(main_dest) if main_dest.exists() else None),
            },
            "metadata_exiftool": unit.metadata,
        }
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
        return str(sidecar)

    def _build_summary(self) -> dict[str, Any]:
        by_drone: dict[str, int] = {}
        by_cat: dict[str, int] = {}
        moved = deleted = skipped = 0
        duplicates_files = 0
        duplicates_units = 0
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
            dup_list = r.get("duplicates") or []
            if dup_list:
                duplicates_units += 1
                duplicates_files += len(dup_list)
        return {
            "total_units": len(self.units),
            "moved": moved,
            "deleted": deleted,
            "skipped": skipped,
            "duplicates_units": duplicates_units,
            "duplicates_files": duplicates_files,
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
                for m in r.get("moved_directly", []):
                    txt_lines.append(f"    ⚡ {m['status']:16s} déplacé: {m['source']} -> {m['target']}")
                for c in r.get("copied", []):
                    txt_lines.append(f"    → {c['status']:16s} copié:   {c['source']} -> {c['target']}")
                for t in r.get("trashed", []):
                    txt_lines.append(f"    🗑️ corbeille: {t}")
                if r.get("sidecar_json"):
                    txt_lines.append(f"    📄 sidecar:   {r['sidecar_json']}")
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


def _dms_to_decimal(dms: str) -> Optional[float]:
    """Convertit "45 deg 30' 6.12\" N" en décimal signé. Retourne None si échec."""
    if not dms or not isinstance(dms, str):
        return None
    m = re.match(
        r"^\s*(-?\d+(?:\.\d+)?)\s*deg?\s*(\d+(?:\.\d+)?)?\s*'?\s*(\d+(?:\.\d+)?)?\s*\"?\s*([NSEW])?\s*$",
        dms.strip(),
        re.IGNORECASE,
    )
    if not m:
        # tente format simple "45.5017"
        try:
            return float(dms.strip())
        except ValueError:
            return None
    deg = float(m.group(1) or 0)
    minutes = float(m.group(2) or 0)
    seconds = float(m.group(3) or 0)
    ref = (m.group(4) or "").upper()
    val = abs(deg) + minutes / 60 + seconds / 3600
    if deg < 0 or ref in ("S", "W"):
        val = -val
    return val


def extract_gps(metadata: dict[str, Any]) -> Optional[tuple[float, float]]:
    """Extrait (lat, lon) depuis les métadonnées ExifTool. None si absent/invalide."""
    if not metadata:
        return None
    # 1. Composite:GPSPosition (souvent "lat lon" en décimal si -n a été passé)
    pos = metadata.get("Composite:GPSPosition")
    if pos and isinstance(pos, str):
        parts = pos.strip().split()
        if len(parts) >= 2:
            try:
                lat = float(parts[0])
                lon = float(parts[1])
                if lat != 0 or lon != 0:
                    return (lat, lon)
            except ValueError:
                pass
    # 2. Champs séparés (essaie plusieurs préfixes)
    for lat_key, lon_key, lat_ref_key, lon_ref_key in (
        ("Composite:GPSLatitude", "Composite:GPSLongitude", None, None),
        ("EXIF:GPSLatitude", "EXIF:GPSLongitude", "EXIF:GPSLatitudeRef", "EXIF:GPSLongitudeRef"),
        ("QuickTime:GPSLatitude", "QuickTime:GPSLongitude", None, None),
        ("XMP:GPSLatitude", "XMP:GPSLongitude", None, None),
        ("XMP-drone-dji:GpsLatitude", "XMP-drone-dji:GpsLongitude", None, None),
        ("XMP-drone-dji:Latitude", "XMP-drone-dji:Longitude", None, None),
    ):
        lat_raw = metadata.get(lat_key)
        lon_raw = metadata.get(lon_key)
        if lat_raw is None or lon_raw is None:
            continue
        try:
            lat = float(lat_raw) if not isinstance(lat_raw, str) else (_dms_to_decimal(lat_raw) or 0.0)
            lon = float(lon_raw) if not isinstance(lon_raw, str) else (_dms_to_decimal(lon_raw) or 0.0)
        except (TypeError, ValueError):
            continue
        if lat_ref_key and metadata.get(lat_ref_key, "").upper().startswith("S"):
            lat = -abs(lat)
        if lon_ref_key and metadata.get(lon_ref_key, "").upper().startswith("W"):
            lon = -abs(lon)
        if lat != 0 or lon != 0:
            return (lat, lon)
    return None


# Regex pour extraire les tuples GPS des sous-titres SRT DJI Neo/Neo2/Avata etc.
# Format typique : "[latitude: 45.604436] [longitude: -73.454247] [rel_alt: 2.400 abs_alt: 137.543]"
_SRT_GPS_RE = re.compile(
    r"\[\s*latitude\s*:\s*(-?\d+\.?\d*)\s*\][^\[]*\[\s*longitude\s*:\s*(-?\d+\.?\d*)\s*\]"
    r"(?:[^\[]*\[\s*(?:rel_alt|relative_alt)\s*:\s*(-?\d+\.?\d*)"
    r"(?:\s+abs_alt\s*:\s*(-?\d+\.?\d*))?\s*\])?",
    re.IGNORECASE,
)
# Fallback : ancien format Mavic "GPS(-73.454247,45.604436,137.5)"
_SRT_GPS_PAREN_RE = re.compile(
    r"GPS\s*\(\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*(?:,\s*(-?\d+\.?\d*))?\s*\)",
    re.IGNORECASE,
)


def parse_srt_gps_track(srt_path: str | os.PathLike) -> list[tuple[float, float, Optional[float]]]:
    """Extrait la piste GPS d'un sous-titre SRT DJI.

    Retourne une liste de (lat, lon, alt_rel_ou_abs). Déduplique les points
    consécutifs identiques pour alléger la carte.
    """
    p = Path(srt_path)
    if not p.exists():
        return []
    try:
        # Certains DJI encodent en UTF-8 avec BOM, d'autres en cp1252
        raw = p.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return []
    except Exception:
        return []

    points: list[tuple[float, float, Optional[float]]] = []
    last: Optional[tuple[float, float]] = None
    for m in _SRT_GPS_RE.finditer(text):
        try:
            lat = float(m.group(1))
            lon = float(m.group(2))
        except (TypeError, ValueError):
            continue
        if lat == 0 and lon == 0:
            continue
        alt_raw = m.group(3) or m.group(4)
        try:
            alt = float(alt_raw) if alt_raw is not None else None
        except (TypeError, ValueError):
            alt = None
        if last is not None and abs(last[0] - lat) < 1e-7 and abs(last[1] - lon) < 1e-7:
            continue  # même point
        points.append((lat, lon, alt))
        last = (lat, lon)

    if not points:
        # Fallback format Mavic (lon,lat,alt)
        for m in _SRT_GPS_PAREN_RE.finditer(text):
            try:
                lon = float(m.group(1))
                lat = float(m.group(2))
            except (TypeError, ValueError):
                continue
            if lat == 0 and lon == 0:
                continue
            alt_raw = m.group(3)
            try:
                alt = float(alt_raw) if alt_raw is not None else None
            except (TypeError, ValueError):
                alt = None
            if last is not None and abs(last[0] - lat) < 1e-7 and abs(last[1] - lon) < 1e-7:
                continue
            points.append((lat, lon, alt))
            last = (lat, lon)
    return points


def find_companion_srt(main_path: str, companions: list[str]) -> Optional[str]:
    """Retourne le chemin du fichier SRT compagnon (s'il existe)."""
    # 1. Dans la liste des compagnons détectés par le scan
    for c in companions:
        if c.lower().endswith(".srt") and os.path.exists(c):
            return c
    # 2. Vérifie à côté du .mp4 (même stem)
    p = Path(main_path)
    guess = p.with_suffix(".SRT")
    if guess.exists():
        return str(guess)
    guess = p.with_suffix(".srt")
    if guess.exists():
        return str(guess)
    return None


def _same_volume(src: os.PathLike | str, dst: os.PathLike | str) -> bool:
    """Détecte si src et dst sont sur le même volume/drive.

    Sur Windows : compare la lettre de drive de os.path.splitdrive.
    Sur POSIX : compare st_dev via os.stat du parent (le fichier dst n'existe
    peut-être pas encore, donc on utilise son dossier parent).
    """
    src_p = os.fspath(src)
    dst_p = os.fspath(dst)
    if os.name == "nt":
        src_drive = os.path.splitdrive(os.path.abspath(src_p))[0].lower()
        dst_drive = os.path.splitdrive(os.path.abspath(dst_p))[0].lower()
        # UNC paths : compare host+share
        return bool(src_drive) and src_drive == dst_drive
    try:
        src_dev = os.stat(src_p).st_dev
        dst_parent = os.path.dirname(dst_p) or "."
        dst_dev = os.stat(dst_parent).st_dev
        return src_dev == dst_dev
    except OSError:
        return False


def _files_identical(a: os.PathLike | str, b: os.PathLike | str,
                     chunk_size: int = 1 << 20) -> bool:
    """Retourne True si les deux fichiers ont exactement le même contenu.

    Fast-path : compare la taille (via stat), puis lit par blocs de 1 Mio en
    parallèle et compare byte-à-byte. Court-circuite dès qu'un bloc diffère.
    Aucun hash calculé — moins de CPU, moins d'I/O que blake2/sha256.
    """
    a_p, b_p = os.fspath(a), os.fspath(b)
    try:
        sa = os.path.getsize(a_p)
        sb = os.path.getsize(b_p)
    except OSError:
        return False
    if sa != sb:
        return False
    if sa == 0:
        return True
    try:
        with open(a_p, "rb") as fa, open(b_p, "rb") as fb:
            while True:
                ca = fa.read(chunk_size)
                cb = fb.read(chunk_size)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


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
class _TabController:
    """Petit shim compatible avec l'API ancienne du stepper : .next() / .previous() / .set().

    Gère deux niveaux d'onglets : outer (Workflow / Drones / Tags / Visualisateur / Recherche)
    + inner (Config / Scan / Revue / Confirmation / Exécution) à l'intérieur de Workflow.
    """
    OUTER = ["workflow", "drones", "tags", "viewer", "search"]
    WORKFLOW = ["config", "scan", "review", "confirm", "execute"]

    def __init__(self, outer_tabs, outer_panels, inner_tabs, inner_panels) -> None:
        self.outer_tabs = outer_tabs
        self.outer_panels = outer_panels
        self.inner_tabs = inner_tabs
        self.inner_panels = inner_panels

    def _current_idx(self) -> int:
        try:
            return self.WORKFLOW.index(self.inner_tabs.value)
        except (ValueError, TypeError):
            return 0

    def next(self) -> None:
        i = self._current_idx()
        if i < len(self.WORKFLOW) - 1:
            self._goto(self.WORKFLOW[i + 1])

    def previous(self) -> None:
        i = self._current_idx()
        if i > 0:
            self._goto(self.WORKFLOW[i - 1])

    def set(self, name: str) -> None:
        if name in self.WORKFLOW:
            self._goto(name)
        elif name in self.OUTER:
            self._set_outer(name)

    def _goto(self, name: str) -> None:
        # Assure que Workflow est actif
        self._set_outer("workflow")
        try:
            self.inner_tabs.set_value(name)
        except Exception:
            self.inner_tabs.value = name
        try:
            self.inner_panels.set_value(name)
        except Exception:
            self.inner_panels.value = name

    def _set_outer(self, name: str) -> None:
        try:
            self.outer_tabs.set_value(name)
        except Exception:
            self.outer_tabs.value = name
        try:
            self.outer_panels.set_value(name)
        except Exception:
            self.outer_panels.value = name

    # Rétrocompat : `.tabs` renvoie les outer tabs (pour set_value externe)
    @property
    def tabs(self):
        return self.outer_tabs


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
        self._selected_units: set[str] = set()          # main_paths sélectionnés (étape Review)
        self._selection_bar_container = None            # placeholder pour la barre de sélection Review
        self._selected_viewer_files: set[str] = set()   # chemins sélectionnés dans le visualiseur
        self._viewer_selection_bar = None               # placeholder pour la barre de sélection Viewer
        # Recherche
        self._search_results_container = None
        self._search_name_input = None
        self._search_date_from = None
        self._search_date_to = None
        self._search_tag_sel = None
        self._search_cat_sel = None
        self._search_drone_sel = None
        self._search_count_label = None
        self._search_tag_mode = None
        # Pagination
        self._page_size = 24
        self._current_page = 0
        self._pagination_container = None
        self._page_info_label = None
        # Fournisseur de tuiles pour la carte GPS (peut être modifié via le dialog)
        self._map_tile: str = CONFIG.get("map_tile_provider", "osm")
        # Média-preview : mémorise les urls déjà enregistrées auprès du serveur statique
        self._media_url_prefix: Optional[str] = None
        self._media_url_root: Optional[str] = None
        # Tabs Drones + Viewer
        self._drones_container = None
        self._viewer_container = None
        self._viewer_state: dict[str, Any] = {"level": "drones", "drone": None, "date": None}
        # Calendrier (onglet interne du visualiseur)
        self._calendar_container = None
        _now = datetime.now()
        self._calendar_state: dict[str, Any] = {
            "year": _now.year,
            "month": _now.month,
            "drone_filter": "TOUS",     # ID de drone ou "TOUS"
            "cat_filter": "TOUTES",     # nom de catégorie ou "TOUTES"
            "group_mode": "cat",        # "cat" : compte par catégorie ;
                                        # "tag" : compte par tag (chips MEO 3sunset…)
            "tag_filter": [],           # tags à conserver (liste multi-sélection)
        }
        self._calendar_index_cache: Optional[dict[str, Any]] = None
        self._calendar_tag_index_cache: Optional[dict[str, Any]] = None
        # Assets déjà enregistrés ?
        self._assets_registered = False
        # Racines destination déjà enregistrées pour l'aperçu viewer
        self._dest_url_prefix: Optional[str] = None
        self._dest_url_root: Optional[str] = None

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
        # Enregistre le dossier source pour l'aperçu média (idempotent)
        if self.source_dir and os.path.isdir(self.source_dir):
            self._register_media_root(self.source_dir)
        # Enregistre le dossier destination pour l'aperçu viewer
        if self.destination_dir and os.path.isdir(self.destination_dir):
            self._register_destination_root(self.destination_dir)
        # Sert les photos de drones (assets/)
        self._register_assets()
        ui.colors(primary="#1976d2", secondary="#26a69a")

        # CSS custom : effet hover sur les cellules calendrier
        ui.add_head_html("""
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
        <style>
        body, .q-page, .q-tab, .q-btn, .q-field, .q-select {
            font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif !important;
            font-feature-settings: 'cv02','cv03','cv04','cv11';
        }
        .cal-day-clickable {
            transition: transform 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
        }
        .cal-day-clickable:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 16px rgba(33, 150, 243, 0.35);
            background: rgba(33, 150, 243, 0.12) !important;
            border-color: rgba(33, 150, 243, 0.5) !important;
        }
        .cal-mono {
            font-family: 'JetBrains Mono', ui-monospace, 'Cascadia Code', 'Consolas', monospace !important;
        }
        /* Mini-checkbox de sélection sur les vignettes */
        .sel-checkbox .q-checkbox__inner {
            font-size: 18px;
            min-width: 18px;
            width: 18px;
            height: 18px;
        }
        .sel-checkbox .q-checkbox__bg {
            border-width: 1.5px;
        }
        .sel-checkbox .q-checkbox__label {
            display: none;
        }
        .sel-overlay {
            position: absolute;
            top: 4px;
            left: 4px;
            background: rgba(0, 0, 0, 0.35);
            border-radius: 4px;
            padding: 0;
            line-height: 0;
            transition: background 0.15s ease, opacity 0.15s ease;
            opacity: 0.55;
        }
        .sel-overlay:hover, .sel-overlay.sel-active {
            background: rgba(0, 0, 0, 0.65);
            opacity: 1;
        }
        /* Onglets : masque les flèches de scroll horizontal (on veut tout voir) */
        .q-tabs__arrow { display: none !important; }
        /* Onglets un poil plus compacts pour tenir sur une ligne */
        .q-tab { padding: 0 10px !important; min-height: 40px !important; }
        .q-tab__label { font-size: 12.5px !important; letter-spacing: 0.2px; }
        .q-tab__icon { font-size: 18px !important; }
        </style>
        """)

        with ui.header().classes("bg-primary text-white items-center"):
            ui.icon("flight").classes("text-2xl")
            ui.label("DJI Organizator").classes("text-h5")
            ui.space()
            ui.label(f"v1.0 · port {app.config.port if hasattr(app.config, 'port') else 8192}").classes("text-caption")

        with ui.column().classes("w-full max-w-7xl mx-auto p-4 gap-4"):
            # Barre d'onglets principale — 5 sections seulement (le workflow
            # est groupé en sous-onglets à l'intérieur de "Workflow")
            with ui.tabs().props(
                "dense inline-label active-color=primary indicator-color=primary "
                "align=left no-caps"
            ).classes("w-full") as tabs:
                ui.tab("workflow", label="Workflow",     icon="rocket_launch")
                ui.tab("drones",   label="Drones",        icon="flight_takeoff")
                ui.tab("tags",     label="Tags",          icon="sell")
                ui.tab("viewer",   label="Visualiseur",   icon="collections")
                ui.tab("search",   label="Recherche",     icon="manage_search")

            with ui.tab_panels(tabs, value="workflow").classes("w-full") as panels:
                # Onglet Workflow contient les 5 sous-étapes
                with ui.tab_panel("workflow"):
                    with ui.tabs().props(
                        "dense inline-label active-color=primary "
                        "indicator-color=primary align=left no-caps"
                    ).classes("w-full") as inner_tabs:
                        ui.tab("config",  label="Configuration", icon="folder_open")
                        ui.tab("scan",    label="Scan",          icon="search")
                        ui.tab("review",  label="Revue",         icon="preview")
                        ui.tab("confirm", label="Confirmation",  icon="fact_check")
                        ui.tab("execute", label="Exécution",     icon="done_all")
                    with ui.tab_panels(inner_tabs, value="config").classes(
                        "w-full"
                    ) as inner_panels:
                        self._stepper = _TabController(
                            tabs, panels, inner_tabs, inner_panels
                        )
                        self._step_config()
                        self._step_scan()
                        self._step_review()
                        self._step_confirm()
                        self._step_execute()
                self._step_drones()
                self._step_tags()
                self._step_viewer()
                self._step_search()

            # Ouvre automatiquement le viewer quand on clique sur son tab
            tabs.on_value_change(self._on_tab_change)

    # ── ÉTAPE 1 : Configuration ────────────────────────────────────────────
    def _step_config(self) -> None:
        with ui.tab_panel("config"):
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
                    self._register_media_root(self.source_dir)
                    self._register_destination_root(self.destination_dir)
                    self._stepper.next()

            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Suivant →", on_click=save_and_next).props("color=primary icon-right=arrow_forward")

    # ── ÉTAPE 2 : Scan ─────────────────────────────────────────────────────
    def _step_scan(self) -> None:
        with ui.tab_panel("scan"):
            with ui.card().classes("w-full"):
                ui.label("Extraction des métadonnées via ExifTool").classes("text-h6")
                self._scan_progress_label = ui.label("En attente…").classes("text-body2")
                self._scan_progress_bar = ui.linear_progress(0.0, show_value=False).classes("w-full")

            async def run_scan() -> None:
                self._scan_progress_label.text = "Initialisation…"
                self._scan_progress_bar.value = 0.0
                # Vérifier que le dossier source existe (gère lecteur réseau/externe déconnecté)
                if not self.source_dir or not os.path.isdir(self.source_dir):
                    ui.notify(
                        f"Dossier source introuvable ou inaccessible : {self.source_dir!r}\n"
                        "Vérifiez que le lecteur est connecté.",
                        type="negative",
                        timeout=10000,
                    )
                    self.units = []
                    self._scan_progress_label.text = "❌ Dossier inaccessible"
                    return
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

            with ui.row().classes("w-full justify-between gap-2 q-mt-md"):
                ui.button("← Retour", on_click=self._stepper.previous).props("flat icon=arrow_back")
                ui.button("Scanner", on_click=run_scan).props("color=primary icon=play_arrow")

    # ── ÉTAPE 3 : Revue avec thumbnails ────────────────────────────────────
    def _step_review(self) -> None:
        with ui.tab_panel("review"):
            with ui.card().classes("w-full"):
                # Si la liste de résultats est vide (scan non effectué ou dossier inaccessible),
                # afficher un message clair au lieu de construire des widgets de filtre qui plantent.
                if not self.units:
                    with ui.row().classes("w-full items-center gap-3"):
                        ui.icon("info").classes("text-grey-5")
                        ui.label(
                            "Aucun média à afficher. \n"
                            "Vérifiez que le dossier source est bien configuré et accessible."
                        ).classes("text-body2 text-grey-6")
                    return
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label("Filtres :").classes("text-body2")
                    _drone_opts = ["TOUS"] + list({u.drone_id for u in self.units if u.drone_id})
                    _cat_opts = ["TOUTES"] + list({u.category for u in self.units if u.category})
                    _tag_opts  = ["TOUS"] + self._tag_names()
                    self._drone_filter_sel = ui.select(
                        options=_drone_opts,
                        value=_drone_opts[0],
                        on_change=lambda e: self._apply_filters(),
                        label="Drone",
                    ).classes("min-w-32")
                    self._cat_filter_sel = ui.select(
                        options=_cat_opts,
                        value=_cat_opts[0],
                        on_change=lambda e: self._apply_filters(),
                        label="Catégorie",
                    ).classes("min-w-32")
                    self._tag_filter_sel = ui.select(
                        options=_tag_opts,
                        value=_tag_opts[0],
                        on_change=lambda e: self._apply_filters(),
                        label="Tag",
                    ).classes("min-w-32")
                    ui.space()
                    ui.button("Tout → Déplacer", on_click=lambda: self._bulk_action("move")).props("flat")
                    ui.button("Tout → Effacer", on_click=lambda: self._bulk_action("delete")).props("flat color=negative")
                    ui.button("Tout → Ignorer", on_click=lambda: self._bulk_action("skip")).props("flat")

                # Ligne pour réassignement drone en masse (utile pour Mini 2 dont les métadonnées sont vides)
                with ui.row().classes("w-full items-center gap-2 mt-2"):
                    ui.label("Réassigner drone des filtrés :").classes("text-body2")
                    drone_ids = [d["id"] for d in CONFIG.get("drone_mapping", [])]
                    drone_ids = [d["id"] for d in CONFIG.get("drone_mapping", [])]
                    _bulk_val = drone_ids[0] if drone_ids else None
                    self._bulk_drone_target = ui.select(
                        options=drone_ids if drone_ids else ["—Aucun drone configuré—"],
                        value=_bulk_val,
                        label="→ Drone cible",
                    ).classes("min-w-40")
                    ui.button(
                        "Réassigner filtrés",
                        icon="swap_horiz",
                        on_click=self._bulk_reassign_drone,
                    ).props("color=primary")
                    ui.label("(applique le filtre courant Drone/Catégorie)").classes("text-caption text-grey-6")

                # Barre sélection multi-checkbox (dynamique, initialement vide)
                self._selection_bar_container = ui.column().classes("w-full q-mt-sm")

                self._review_container = ui.column().classes("w-full gap-2")

            with ui.row().classes("w-full justify-between gap-2 q-mt-md"):
                ui.button("← Retour", on_click=self._stepper.previous).props("flat icon=arrow_back")
                ui.button("Suivant : Confirmation →", on_click=self._goto_confirm).props("color=primary icon-right=arrow_forward")

    def _refresh_review(self) -> None:
        # Alimente les filtres
        drones = sorted({u.drone_id for u in self.units})
        try:
            self._drone_filter_sel.options = ["TOUS"] + drones
            self._drone_filter_sel.update()
        except Exception:
            pass
        # Alimente le filtre tag
        try:
            if hasattr(self, "_tag_filter_sel") and self._tag_filter_sel is not None:
                self._tag_filter_sel.options = ["TOUS"] + self._tag_names()
                if self._tag_filter_sel.value not in self._tag_filter_sel.options:
                    self._tag_filter_sel.value = "TOUS"
                self._tag_filter_sel.update()
        except Exception:
            pass
        self._apply_filters()

    def _apply_filters(self, reset_page: bool = True) -> None:
        if self._review_container is None:
            return
        drone_f = getattr(self._drone_filter_sel, "value", "TOUS")
        cat_f = getattr(self._cat_filter_sel, "value", "TOUTES")
        tag_f = getattr(self._tag_filter_sel, "value", "TOUS") \
            if hasattr(self, "_tag_filter_sel") else "TOUS"

        visible = [
            u for u in self.units
            if (drone_f == "TOUS" or u.drone_id == drone_f)
            and (cat_f == "TOUTES" or u.category == cat_f)
            and (tag_f == "TOUS" or tag_f in (u.tags or []))
        ]

        # Construction des items d'affichage : les PANO/HYPER/WAYPOINTS partageant le même
        # (drone_folder, capture_date, category, group_subdir) sont regroupés en un seul.
        display_items: list[dict[str, Any]] = []
        seen_groups: set[tuple[str, str, str, str]] = set()
        for u in visible:
            if u.category in ("PANORAMA", "HYPERLAPSE", "WAYPOINTS") and u.group_subdir:
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

        # Rafraîchit la barre de sélection multi-checkbox
        self._refresh_selection_bar()

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

    # ── prévisualisation plein écran + mini-carte GPS ──────────────────────
    def _register_media_root(self, source_dir: str) -> None:
        """Expose `source_dir` sous une URL statique pour l'aperçu média."""
        if not source_dir or not os.path.isdir(source_dir):
            return
        try:
            resolved = str(Path(source_dir).resolve())
        except Exception:
            return
        if self._media_url_root == resolved:
            return
        try:
            import hashlib
            key = hashlib.md5(resolved.encode("utf-8")).hexdigest()[:10]
            url_prefix = f"/dji-source-{key}"
            app.add_media_files(url_prefix, resolved)
            self._media_url_prefix = url_prefix
            self._media_url_root = resolved
            self.log(f"📺 Média servi sur {url_prefix} ← {resolved}")
        except Exception as e:
            self.log(f"⚠️ Impossible d'enregistrer le dossier média: {e}")
            self._media_url_prefix = None
            self._media_url_root = None

    def _register_destination_root(self, dest_dir: str) -> None:
        """Expose la racine destination sous une URL statique (pour l'onglet Visualiseur)."""
        if not dest_dir or not os.path.isdir(dest_dir):
            return
        try:
            resolved = str(Path(dest_dir).resolve())
        except Exception:
            return
        if self._dest_url_root == resolved:
            return
        try:
            import hashlib
            key = hashlib.md5(("dest:" + resolved).encode("utf-8")).hexdigest()[:10]
            url_prefix = f"/dji-dest-{key}"
            app.add_media_files(url_prefix, resolved)
            self._dest_url_prefix = url_prefix
            self._dest_url_root = resolved
            self.log(f"🗄️ Destination servie sur {url_prefix} ← {resolved}")
        except Exception as e:
            self.log(f"⚠️ Impossible d'enregistrer le dossier destination: {e}")

    def _register_assets(self) -> None:
        """Expose le dossier assets/ (photos des drones)."""
        if self._assets_registered:
            return
        assets_dir = APP_DIR / "assets"
        if not assets_dir.is_dir():
            return
        try:
            app.add_static_files("/assets", str(assets_dir))
            self._assets_registered = True
        except Exception as e:
            self.log(f"⚠️ Impossible d'exposer assets/: {e}")

    def _media_url_for(self, file_path: str) -> Optional[str]:
        """Retourne une URL servable pour `file_path` (source OU destination)."""
        import urllib.parse
        for prefix, root in (
            (self._media_url_prefix, self._media_url_root),
            (self._dest_url_prefix, self._dest_url_root),
        ):
            if not prefix or not root:
                continue
            try:
                rel = Path(file_path).resolve().relative_to(Path(root))
            except (ValueError, OSError):
                continue
            parts = [urllib.parse.quote(p) for p in rel.parts]
            return f"{prefix}/" + "/".join(parts)
        return None

    def _open_group_grid_dialog(self, members: list[MediaUnit]) -> None:
        """Ouvre un dialog plein écran affichant toutes les vignettes du groupe.

        Utilisé pour la micro-validation d'un panorama, hyperlapse ou trajet
        waypoints. Clic sur une vignette → aperçu détaillé de ce média.
        """
        try:
            self.log(f"🖼️ Ouverture grille groupe: {len(members) if members else 0} médias")
            if not members:
                self.log("⚠️ Groupe vide, abandon")
                return
            first = members[0]
            dlg = ui.dialog().props("maximized")
            with dlg:
                with ui.card().classes("w-full h-full no-shadow column no-wrap"):
                    # Header
                    with ui.row().classes("w-full items-center gap-2 q-pa-sm bg-primary text-white"):
                        ui.icon("photo_library")
                        ui.label(f"{first.category} — {first.group_subdir}").classes("text-h6 flex-grow")
                        ui.badge(first.drone_id, color="white text-primary")
                        ui.badge(first.capture_date, color="white text-primary")
                        ui.badge(f"{len(members)} photos", color="orange")
                        ui.button(icon="close", on_click=dlg.close).props("flat round color=white").tooltip("Fermer")

                    # Actions groupées rapides
                    with ui.row().classes("w-full items-center gap-2 q-pa-sm bg-grey-2"):
                        ui.label("Action pour tout le groupe :").classes("text-body2")

                        def _set_all(action: str) -> None:
                            for m in members:
                                m.action = action
                            ui.notify(f"{len(members)} → {action}", type="info")
                            dlg.close()
                            self._apply_filters()

                        ui.button("📥 Tout déplacer", on_click=lambda: _set_all("move")).props("dense color=positive")
                        ui.button("⏭️ Tout ignorer", on_click=lambda: _set_all("skip")).props("dense color=grey")
                        ui.button("🗑️ Tout effacer", on_click=lambda: _set_all("delete")).props("dense color=negative")
                        ui.space()
                        ui.label(f"Taille totale : {human_size(sum(m.total_size for m in members))}").classes("text-caption")

                    # Grille de vignettes (scroll interne)
                    with ui.scroll_area().classes("w-full col"):
                        with ui.grid(columns=6).classes("w-full gap-2 q-pa-sm"):
                            for m in members:
                                thumb = generate_thumbnail(m.main_path, size=256)
                                card = ui.card().classes("cursor-pointer hover:bg-orange-1 p-1")
                                with card:
                                    if thumb:
                                        ui.image(image_to_data_uri(thumb)).classes(
                                            "w-full h-32 object-cover rounded"
                                        )
                                    else:
                                        with ui.element("div").classes(
                                            "w-full h-32 flex items-center justify-center bg-grey-3 rounded"
                                        ):
                                            ui.icon("broken_image").classes("text-3xl text-grey-7")
                                    ui.label(Path(m.main_path).name).classes(
                                        "text-caption ellipsis"
                                    ).style("max-width:100%")
                                    with ui.row().classes("items-center gap-1 no-wrap"):
                                        icon = {"move": "download", "delete": "delete",
                                                "skip": "block"}.get(m.action, "help")
                                        color = {"move": "positive", "delete": "negative",
                                                 "skip": "grey"}.get(m.action, "primary")
                                        ui.icon(icon).classes(f"text-{color}")
                                        ui.label(human_size(m.total_size)).classes("text-caption")
                                card.on("click", lambda u=m, d=dlg: (d.close(), self._open_preview_dialog(u)))
                                card.tooltip(Path(m.main_path).name)
            dlg.open()
            self.log(f"✅ Dialog groupe ouvert ({len(members)} vignettes)")
        except Exception as e:
            import traceback
            self.log(f"❌ Erreur ouverture grille groupe: {e}")
            self.log(traceback.format_exc())
            ui.notify(f"Erreur: {e}", type="negative")

    def _open_preview_dialog(
        self,
        unit: MediaUnit,
        source: str = "review",
        on_deleted: Optional[Callable[[], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        """Ouvre un dialog plein écran avec l'image/vidéo et une mini-carte GPS.

        `source` :
            - "review"  → suppression retire l'unit de `self.units` (avant copie)
            - "viewer"  → suppression envoie le fichier disque (+ sidecar) à la corbeille
        `on_deleted` : callback optionnel appelé après suppression réussie.
        `on_close`   : callback optionnel appelé à la fermeture du dialog
                       (utile pour rafraîchir la vue parente si les tags/nom
                       du média ont été modifiés depuis l'aperçu).
        """
        is_video = Path(unit.main_path).suffix.lower() in VIDEO_EXTS
        media_url = self._media_url_for(unit.main_path)
        gps = extract_gps(unit.metadata)

        # Piste GPS depuis le SRT compagnon (DJI Neo/Neo2/Avata…)
        srt_path = find_companion_srt(unit.main_path, unit.companions)
        track: list[tuple[float, float, Optional[float]]] = []
        if srt_path:
            try:
                track = parse_srt_gps_track(srt_path)
            except Exception as e:
                self.log(f"⚠️ SRT parse échoué ({srt_path}): {e}")
        # Si pas de GPS EXIF mais une piste SRT, prendre le premier point
        if gps is None and track:
            gps = (track[0][0], track[0][1])

        with ui.dialog().props("maximized") as dlg, ui.card().classes("w-full h-full no-shadow"):
            with ui.row().classes("w-full items-center gap-2 q-pa-sm bg-primary text-white"):
                ui.icon("movie" if is_video else "image")
                ui.label(Path(unit.main_path).name).classes("text-body1 truncate flex-grow")
                ui.badge(unit.drone_id, color="white text-primary")
                ui.badge(unit.category, color="secondary")
                if gps:
                    ui.badge(f"📍 {gps[0]:.5f}, {gps[1]:.5f}", color="teal")
                else:
                    ui.badge("📍 pas de GPS", color="grey")
                if track:
                    ui.badge(f"🛰️ SRT: {len(track)} pts", color="deep-purple")
                ui.button(
                    icon="delete_forever",
                    on_click=lambda: self._preview_dialog_delete(
                        unit, source, dlg, on_deleted
                    ),
                ).props("flat round dense color=white").tooltip(
                    "Envoyer ce média à la corbeille"
                )
                ui.button(icon="close", on_click=dlg.close).props("flat round dense color=white")

            with ui.row().classes("w-full h-full items-stretch gap-2 q-pa-sm no-wrap"):
                # Colonne média
                with ui.column().classes("flex-grow items-center justify-center h-full min-w-0"):
                    if media_url:
                        if is_video:
                            ui.video(media_url, controls=True, autoplay=False).classes(
                                "max-h-[80vh] max-w-full rounded"
                            )
                        else:
                            ui.image(media_url).classes(
                                "max-h-[80vh] max-w-full object-contain rounded"
                            )
                    else:
                        with ui.card().classes("bg-grey-2"):
                            ui.icon("warning").classes("text-4xl text-orange")
                            ui.label("Aperçu indisponible").classes("text-body2")
                            ui.label(unit.main_path).classes("text-caption font-mono")

                # Colonne carte + infos
                with ui.column().classes("w-96 h-full gap-2"):
                    # Nom personnalisé (persisté dans le sidecar quand on est en mode viewer)
                    with ui.card().classes("w-full q-pa-sm"):
                        ui.label("✏️ Nom personnalisé").classes("text-subtitle2")
                        self._render_custom_name_row(
                            unit,
                            on_change=(
                                self._apply_filters if source == "review" else None
                            ),
                            on_disk=(source == "viewer"),
                        )
                    # Ligne tags (persistée dans le sidecar quand on est en mode viewer)
                    with ui.card().classes("w-full q-pa-sm"):
                        ui.label("🏷️ Tags").classes("text-subtitle2")
                        self._render_tags_row(
                            [unit],
                            on_change=(
                                self._apply_filters if source == "review" else None
                            ),
                            on_disk=(source == "viewer"),
                        )
                    if gps:
                        with ui.row().classes("w-full items-center gap-1"):
                            ui.label("Mini-carte GPS").classes("text-subtitle2 flex-grow")
                            if track:
                                ui.badge(f"trajet {len(track)} pts", color="deep-purple")
                        tile_sel = ui.select(
                            options={k: v[0] for k, v in MAP_TILE_PROVIDERS.items()},
                            value=self._map_tile,
                            label="Fond de carte",
                        ).props("dense options-dense").classes("w-full")

                        map_holder = ui.column().classes("w-full h-72 rounded overflow-hidden")

                        def _rebuild_map(tile_kind: str) -> None:
                            map_holder.clear()
                            with map_holder:
                                _label, url_tpl, attribution = MAP_TILE_PROVIDERS.get(
                                    tile_kind, MAP_TILE_PROVIDERS["osm"]
                                )
                                # Centre : milieu du bbox si trajet, sinon point unique
                                if track:
                                    lats = [p[0] for p in track]
                                    lons = [p[1] for p in track]
                                    center = ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)
                                else:
                                    center = gps
                                m = ui.leaflet(center=center, zoom=16).classes("w-full h-full")
                                m.clear_layers()
                                m.tile_layer(
                                    url_template=url_tpl,
                                    options={"attribution": attribution, "maxZoom": 19},
                                )
                                if tile_kind == "hybrid":
                                    m.tile_layer(
                                        url_template=_HYBRID_LABELS_URL,
                                        options={"attribution": "Labels &copy; Esri", "maxZoom": 19},
                                    )
                                # Trajet (polyline) si disponible
                                if track and len(track) > 1:
                                    latlngs = [[p[0], p[1]] for p in track]
                                    m.generic_layer(
                                        name="polyline",
                                        args=[latlngs, {"color": "#ff4081", "weight": 4, "opacity": 0.85}],
                                    )
                                    # Marqueur départ (vert) + arrivée (rouge)
                                    m.marker(
                                        latlng=(track[0][0], track[0][1]),
                                        options={"title": "Départ"},
                                    )
                                    m.marker(
                                        latlng=(track[-1][0], track[-1][1]),
                                        options={"title": "Arrivée"},
                                    )
                                    # Zoome sur la piste après init
                                    async def _fit() -> None:
                                        try:
                                            await m.initialized()
                                            lats2 = [p[0] for p in track]
                                            lons2 = [p[1] for p in track]
                                            bounds = [[min(lats2), min(lons2)], [max(lats2), max(lons2)]]
                                            m.run_map_method("fitBounds", bounds, {"padding": [20, 20]})
                                        except Exception:
                                            pass
                                    background_tasks.create(_fit())
                                else:
                                    m.marker(latlng=gps)

                        _rebuild_map(self._map_tile)

                        def _on_tile_change(e) -> None:
                            self._map_tile = e.value
                            CONFIG["map_tile_provider"] = e.value
                            try:
                                save_config(CONFIG)
                            except Exception:
                                pass
                            _rebuild_map(e.value)

                        tile_sel.on_value_change(_on_tile_change)

                        ui.button(
                            "Ouvrir dans Google Maps",
                            icon="open_in_new",
                            on_click=lambda: ui.navigate.to(
                                f"https://www.google.com/maps?q={gps[0]},{gps[1]}",
                                new_tab=True,
                            ),
                        ).props("flat dense").classes("w-full")
                    else:
                        with ui.card().classes("bg-grey-2 w-full"):
                            ui.icon("location_off").classes("text-3xl text-grey-6")
                            ui.label("Aucune donnée GPS").classes("text-body2")

                    ui.separator()
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(f"Toutes les métadonnées ({len(unit.metadata)})").classes(
                            "text-subtitle2 flex-grow"
                        )
                        meta_filter = ui.input(placeholder="filtrer…").props("dense clearable").classes("w-32")

                    meta_scroll = ui.scroll_area().classes("w-full flex-grow border rounded p-1")
                    meta_scroll.style("min-height: 200px; max-height: 45vh;")

                    def _render_meta(filter_txt: str = "") -> None:
                        meta_scroll.clear()
                        needle = (filter_txt or "").strip().lower()
                        with meta_scroll:
                            shown = 0
                            for key in sorted(unit.metadata.keys()):
                                val = unit.metadata.get(key)
                                if val is None:
                                    continue
                                try:
                                    val_str = str(val)
                                except Exception:
                                    continue
                                if needle and needle not in key.lower() and needle not in val_str.lower():
                                    continue
                                with ui.row().classes("w-full no-wrap gap-1 items-start q-py-none").style(
                                    "line-height:1.15; margin:0;"
                                ):
                                    ui.label(f"{key}:").classes(
                                        "font-mono text-primary truncate"
                                    ).style(
                                        "font-size:10px; width:180px; margin:0; padding:0;"
                                    ).tooltip(key)
                                    ui.label(val_str).classes(
                                        "font-mono flex-grow break-all"
                                    ).style(
                                        "font-size:10px; word-break:break-all; "
                                        "margin:0; padding:0;"
                                    )
                                shown += 1
                            if shown == 0:
                                ui.label("(aucun tag ne correspond au filtre)").classes(
                                    "text-caption text-grey-6"
                                )

                    _render_meta()
                    meta_filter.on_value_change(lambda e: _render_meta(e.value or ""))

                    with ui.row().classes("w-full gap-1 q-mt-xs"):
                        ui.button(
                            "📋 Copier JSON",
                            on_click=lambda: (
                                ui.run_javascript(
                                    f"navigator.clipboard.writeText({json.dumps(json.dumps(unit.metadata, indent=2, default=str, ensure_ascii=False))})"
                                ),
                                ui.notify("Métadonnées copiées", type="positive"),
                            ),
                        ).props("flat dense size=sm")

        # Rafraîchit la vue parente à la fermeture (tags/nom personnalisé
        # ont pu être modifiés depuis l'aperçu).
        if on_close is not None:
            def _fire_on_close(e) -> None:
                if not e.value:
                    try:
                        on_close()
                    except Exception:
                        pass
            dlg.on_value_change(_fire_on_close)

        dlg.open()

    def _render_unit_card(self, unit: MediaUnit) -> None:
        """Carte d'un média individuel (mode Revue)."""
        with ui.card().classes("w-full"):
            thumb = generate_thumbnail(unit.main_path, size=256)
            with ui.element("div").classes("relative w-full cursor-pointer").on(
                "click", lambda u=unit: self._open_preview_dialog(u)
            ).tooltip("Cliquer pour aperçu plein écran"):
                if thumb:
                    ui.image(image_to_data_uri(thumb)).classes("w-full h-40 object-cover rounded")
                else:
                    with ui.element("div").classes(
                        "w-full h-40 flex items-center justify-center bg-grey-3 rounded"
                    ):
                        ui.icon(
                            "movie" if Path(unit.main_path).suffix.lower() in VIDEO_EXTS else "image"
                        ).classes("text-4xl text-grey-6")
                # Overlay icône loupe/play
                with ui.element("div").classes(
                    "absolute top-1 right-1 bg-black bg-opacity-60 text-white rounded-full p-1"
                ):
                    ui.icon(
                        "play_circle" if Path(unit.main_path).suffix.lower() in VIDEO_EXTS
                        else "zoom_in"
                    ).classes("text-white")
                # Overlay checkbox (top-left) — stopPropagation empêche l'ouverture du preview
                is_sel = unit.main_path in self._selected_units
                sel_overlay = ui.element("div").classes(
                    "sel-overlay" + (" sel-active" if is_sel else "")
                )
                sel_overlay.on("click.stop", lambda: None)
                with sel_overlay:
                    cb = ui.checkbox(
                        value=is_sel,
                        on_change=lambda e, u=unit: self._toggle_unit_selection(
                            [u.main_path], bool(e.value)
                        ),
                    ).classes("sel-checkbox").props("dense size=xs color=red-4 dark")
                    cb.tooltip("Sélectionner pour effacer en lot")

            ui.label(Path(unit.main_path).name).classes("text-body2 truncate").tooltip(unit.main_path)
            # Nom personnalisé (édition inline)
            self._render_custom_name_row(unit, on_change=self._apply_filters)
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

                ui.button(
                    icon="delete_forever",
                    on_click=lambda u=unit: self._delete_units_to_trash([u], confirm=True),
                ).props("flat dense round color=negative").tooltip(
                    "Envoyer à la corbeille immédiatement"
                )

            # Ligne tags (chips + bouton ajouter)
            self._render_tags_row([unit], on_change=self._apply_filters)

    def _render_custom_name_row(
        self,
        unit: MediaUnit,
        on_change: Optional[Callable[[], None]] = None,
        on_disk: bool = False,
    ) -> None:
        """Affiche le nom personnalisé (si présent) + bouton crayon pour éditer.

        `on_disk=True` : écrit aussi dans le sidecar sur disque (mode viewer).
        Rangée auto-rafraîchissante — pas besoin de recharger la vue.
        """
        outer = ui.row().classes("w-full items-center gap-1 q-mt-xs").style(
            "min-height:20px;"
        )

        def _save(new_name: str) -> None:
            unit.custom_name = new_name
            if on_disk:
                self._update_sidecar_custom_name(unit.main_path, new_name)
            action = "défini" if new_name else "effacé"
            ui.notify(
                f"✏️ Nom personnalisé {action}"
                + (f" : « {new_name} »" if new_name else ""),
                type="positive",
            )
            _draw()
            if on_change:
                try:
                    on_change()
                except Exception as e:
                    self.log(f"⚠️ custom_name on_change: {e}")

        def _draw() -> None:
            outer.clear()
            with outer:
                if unit.custom_name:
                    with ui.element("div").style(
                        "background:rgba(76,175,80,0.15); color:#2E7D32; "
                        "padding:2px 8px; border-radius:6px; font-size:12px; "
                        "font-weight:600; display:inline-flex; align-items:center; "
                        "gap:4px; border:1px solid rgba(76,175,80,0.35);"
                    ):
                        ui.icon("edit_note").style("font-size:14px;")
                        ui.label(unit.custom_name)
                else:
                    ui.label("(pas de nom personnalisé)").classes(
                        "text-caption text-grey-5"
                    )
                ui.button(
                    icon="edit",
                    on_click=lambda: self._open_rename_dialog(
                        current=unit.custom_name,
                        title="Renommer",
                        subtitle=Path(unit.main_path).name,
                        on_save=_save,
                    ),
                ).props("flat dense round size=xs color=primary").tooltip(
                    "Modifier le nom personnalisé"
                )

        _draw()

    def _render_group_custom_name_row(
        self,
        members: list[MediaUnit],
        on_change: Optional[Callable[[], None]] = None,
        on_disk: bool = False,
    ) -> None:
        """Nom personnalisé partagé pour un groupe (PANO/HYPER/WAYPOINTS).

        Le nom est stocké dans chaque sidecar membre — on affiche le premier
        non vide et on écrit le même sur tous à la sauvegarde.
        """
        if not members:
            return
        outer = ui.row().classes("w-full items-center gap-1 q-mt-xs").style(
            "min-height:20px;"
        )

        def _current() -> str:
            for m in members:
                if m.custom_name:
                    return m.custom_name
            return ""

        def _save(new_name: str) -> None:
            for m in members:
                m.custom_name = new_name
                if on_disk:
                    self._update_sidecar_custom_name(m.main_path, new_name)
            action = "défini" if new_name else "effacé"
            ui.notify(
                f"✏️ Nom du groupe {action}"
                + (f" : « {new_name} »" if new_name else "")
                + f" ({len(members)} médias)",
                type="positive",
            )
            _draw()
            if on_change:
                try:
                    on_change()
                except Exception as e:
                    self.log(f"⚠️ custom_name on_change: {e}")

        def _draw() -> None:
            outer.clear()
            current = _current()
            with outer:
                if current:
                    with ui.element("div").style(
                        "background:rgba(255,152,0,0.15); color:#E65100; "
                        "padding:2px 8px; border-radius:6px; font-size:12px; "
                        "font-weight:600; display:inline-flex; align-items:center; "
                        "gap:4px; border:1px solid rgba(255,152,0,0.35);"
                    ):
                        ui.icon("edit_note").style("font-size:14px;")
                        ui.label(current)
                else:
                    ui.label("(pas de nom de groupe)").classes(
                        "text-caption text-grey-5"
                    )
                first = members[0]
                subtitle = f"{first.category} · {first.group_subdir or first.capture_date} · {len(members)} médias"
                ui.button(
                    icon="edit",
                    on_click=lambda: self._open_rename_dialog(
                        current=current,
                        title="Renommer le groupe",
                        subtitle=subtitle,
                        on_save=_save,
                    ),
                ).props("flat dense round size=xs color=primary").tooltip(
                    "Modifier le nom du groupe"
                )

        _draw()

    def _render_tags_row(
        self,
        units: list[MediaUnit],
        on_change: Optional[Callable[[], None]] = None,
        on_disk: bool = False,
        compact: bool = False,
    ) -> None:
        """Affiche les tags courants d'une (ou plusieurs) `units` sous forme de
        chips, avec un bouton pour ajouter un tag.

        Si toutes les units ont le même tag → il apparaît en solide.
        Si seulement certaines l'ont → il apparaît en pointillé (indicateur mixte).
        Le clic sur le × d'une chip retire le tag.

        `on_disk=True` : après modification, met aussi à jour les sidecars
        `.dji.json` correspondants sur le disque (mode viewer).

        `compact=True` : rendu plus petit (thumbnails de la vue viewer).

        La rangée est auto-rafraîchissante : ajouter/retirer un tag met à jour
        immédiatement les chips affichées, sans devoir fermer/rouvrir un dialog.
        """
        if not units:
            return

        # Conteneur outer que l'on va vider et re-remplir à chaque changement
        outer = ui.row().classes("w-full items-center gap-1").style("flex-wrap:wrap;")

        def _rerender_external() -> None:
            if on_change:
                try:
                    on_change()
                except Exception as e:
                    self.log(f"⚠️ tags on_change: {e}")

        def _persist_on_disk() -> None:
            if not on_disk:
                return
            for u in units:
                self._update_sidecar_tags(u.main_path, u.tags or [])

        def _remove_tag(name: str) -> None:
            total_local = len(units)
            self._apply_tag_to_units(units, name, remove=True)
            _persist_on_disk()
            ui.notify(f"— {name} retiré ({total_local} média(s))", type="info")
            _draw()
            _rerender_external()

        def _add_tag(name: str) -> None:
            if not name:
                return
            total_local = len(units)
            n = self._apply_tag_to_units(units, name, remove=False)
            _persist_on_disk()
            ui.notify(f"+ {name} appliqué ({n}/{total_local})", type="positive")
            _draw()
            _rerender_external()

        def _draw() -> None:
            outer.clear()
            # Recompte à chaque re-render
            counts: dict[str, int] = {}
            for u in units:
                for t in (u.tags or []):
                    counts[t] = counts.get(t, 0) + 1
            total = len(units)

            chip_pad = "1px 6px" if compact else "2px 8px"
            chip_font = "10px" if compact else "11px"
            icon_font = "10px" if compact else "12px"

            with outer:
                if not compact:
                    ui.icon("sell").classes("text-grey text-sm").tooltip("Tags")
                # Chips existantes
                for tag_name, cnt in sorted(counts.items()):
                    hex_c = self._tag_hex(tag_name)
                    icon = self._tag_icon(tag_name)
                    mixed = cnt < total
                    border_style = "dashed" if mixed else "solid"
                    chip = ui.element("div").style(
                        f"background:{hex_c}22; color:{hex_c}; "
                        f"padding:{chip_pad}; border-radius:10px; "
                        f"font-size:{chip_font}; font-weight:600; "
                        f"display:inline-flex; align-items:center; gap:4px; "
                        f"border:1px {border_style} {hex_c};"
                    )
                    with chip:
                        ui.label(icon).style(f"font-size:{icon_font};")
                        ui.label(tag_name)
                        if mixed:
                            ui.label(f"({cnt}/{total})").style("opacity:0.7; font-size:10px;")
                        ui.button(
                            icon="close",
                            on_click=lambda n=tag_name: _remove_tag(n),
                        ).props("flat dense round size=xs").style(
                            f"color:{hex_c}; margin:-4px -6px -4px 0;"
                        ).tooltip(f"Retirer « {tag_name} »")

                # Bouton + Ajouter
                available = [
                    n for n in self._tag_names()
                    if counts.get(n, 0) < total  # pas déjà sur toutes les units
                ]
                if available:
                    add_btn = ui.button(icon="add", on_click=None).props(
                        "flat dense round size=xs color=primary"
                    ).tooltip("Ajouter un tag")
                    with add_btn:
                        with ui.menu() as menu:
                            for name in sorted(available):
                                hex_c = self._tag_hex(name)
                                icon = self._tag_icon(name)
                                with ui.menu_item(on_click=lambda n=name, m=menu: (_add_tag(n), m.close())):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.label(icon).style("font-size:14px;")
                                        ui.label(name)
                                        ui.element("div").style(
                                            f"width:12px; height:12px; border-radius:50%; "
                                            f"background:{hex_c};"
                                        )
                elif not counts and not compact:
                    ui.label("(aucun tag)").classes("text-caption text-grey-5")

        _draw()

    def _render_group_card(self, members: list[MediaUnit]) -> None:
        """Carte représentant un groupe PANORAMA/HYPERLAPSE (plusieurs photos en 1)."""
        first = members[0]
        total_size = sum(u.total_size for u in members)
        total_companions = sum(len(u.companions) for u in members)

        with ui.card().classes("w-full border-2 border-orange-4"):
            thumb = generate_thumbnail(first.main_path, size=256)
            # Zone thumbnail cliquable (séparée des selects/actions en bas)
            thumb_zone = ui.element("div").classes("relative w-full cursor-pointer")
            thumb_zone.on("click", lambda ms=members: self._open_group_grid_dialog(ms))
            thumb_zone.tooltip("Cliquer pour voir toutes les vignettes du groupe")
            with thumb_zone:
                if thumb:
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
                # Overlay checkbox (sélectionne tous les membres du groupe)
                all_selected = all(m.main_path in self._selected_units for m in members)
                sel_overlay = ui.element("div").classes(
                    "sel-overlay" + (" sel-active" if all_selected else "")
                )
                sel_overlay.on("click.stop", lambda: None)
                with sel_overlay:
                    cb = ui.checkbox(
                        value=all_selected,
                        on_change=lambda e, ms=members: self._toggle_unit_selection(
                            [m.main_path for m in ms], bool(e.value)
                        ),
                    ).classes("sel-checkbox").props("dense size=xs color=red-4 dark")
                    cb.tooltip(f"Sélectionner les {len(members)} médias du groupe")

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

                ui.button(
                    icon="delete_forever",
                    on_click=lambda ms=members: self._delete_units_to_trash(ms, confirm=True),
                ).props("flat dense round color=negative").tooltip(
                    f"Envoyer les {len(members)} médias du groupe à la corbeille"
                )

            # Nom personnalisé du groupe (partagé entre tous les membres)
            self._render_group_custom_name_row(members, on_change=self._apply_filters)

            # Ligne tags — appliqués à TOUS les membres du groupe
            self._render_tags_row(members, on_change=self._apply_filters)


    # ── suppression immédiate (source → corbeille) ─────────────────────────
    def _trash_paths(self, paths: list[str]) -> tuple[int, list[str]]:
        """Envoie les chemins fournis à la corbeille système.

        Retourne (nb_supprimés, [erreurs]).
        """
        removed = 0
        errors: list[str] = []
        if send2trash is None:
            errors.append("Module send2trash non installé")
            return 0, errors
        for p in paths:
            try:
                if p and os.path.exists(p):
                    send2trash(os.path.normpath(p))
                    removed += 1
            except Exception as e:
                errors.append(f"{p}: {e}")
                self.log(f"⚠️ Corbeille échoué : {p} ({e})")
        return removed, errors

    def _delete_units_to_trash(
        self, units: list["MediaUnit"], confirm: bool = True
    ) -> None:
        """Envoie les fichiers source (+ compagnons) à la corbeille et
        retire les MediaUnit de la liste courante.

        Utilisé à l'étape Review — avant tout déplacement.
        """
        if not units:
            return

        def _do_delete() -> None:
            all_paths: list[str] = []
            main_paths: set[str] = set()
            for u in units:
                main_paths.add(u.main_path)
                all_paths.append(u.main_path)
                all_paths.extend(u.companions or [])
            removed, errors = self._trash_paths(all_paths)
            # Retire les units de la liste
            self.units = [x for x in self.units if x.main_path not in main_paths]
            for mp in main_paths:
                self._selected_units.discard(mp)
            if errors:
                ui.notify(
                    f"🗑️ {removed} fichier(s) supprimé(s), {len(errors)} erreur(s)",
                    type="warning",
                )
            else:
                ui.notify(
                    f"🗑️ {len(units)} média(s) envoyé(s) à la corbeille "
                    f"({removed} fichier(s))",
                    type="positive",
                )
            self._apply_filters(reset_page=False)

        if confirm:
            with ui.dialog() as d, ui.card():
                ui.label(
                    f"Envoyer {len(units)} média(s) à la corbeille ?"
                ).classes("text-h6")
                total_files = sum(1 + len(u.companions or []) for u in units)
                ui.label(
                    f"{total_files} fichier(s) au total (compagnons inclus) — "
                    "les fichiers source seront envoyés dans la corbeille "
                    "système (récupérables)."
                ).classes("text-caption text-grey-6")
                if len(units) <= 8:
                    with ui.column().classes("gap-0 q-mt-sm"):
                        for u in units:
                            ui.label(f"• {Path(u.main_path).name}").classes(
                                "text-caption font-mono"
                            )
                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Annuler", on_click=d.close).props("flat")
                    ui.button(
                        "🗑️ Envoyer à la corbeille",
                        on_click=lambda: (d.close(), _do_delete()),
                    ).props("color=negative unelevated")
            d.open()
        else:
            _do_delete()

    def _delete_disk_file_to_trash(
        self,
        file_path: str,
        companions: Optional[list[str]] = None,
        confirm: bool = True,
        on_deleted: Optional[Callable[[], None]] = None,
    ) -> None:
        """Envoie un fichier (déjà présent sur la destination) à la corbeille.

        Emporte compagnons + sidecar `.dji.json` du même stem si présents.
        Utilisé depuis le visualiseur ET depuis l'aperçu plein écran.
        """
        p = Path(file_path)
        if not p.exists():
            ui.notify(f"Introuvable : {p.name}", type="warning")
            return

        # Collecte cibles : fichier + compagnons + sidecar .dji.json
        targets: list[str] = [str(p)]
        for c in (companions or []):
            if c and c not in targets and os.path.exists(c):
                targets.append(c)
        for sc in (
            p.with_suffix(p.suffix + ".dji.json"),
            p.with_suffix(".dji.json"),
        ):
            if sc.exists() and str(sc) not in targets:
                targets.append(str(sc))

        def _do_delete() -> None:
            removed, errors = self._trash_paths(targets)
            # Invalide le cache calendrier et la sélection viewer
            self._calendar_index_cache = None
            self._selected_viewer_files.discard(str(p))
            if errors:
                ui.notify(
                    f"🗑️ {removed} fichier(s) — {len(errors)} erreur(s)",
                    type="warning",
                )
            else:
                ui.notify(
                    f"🗑️ {p.name} envoyé à la corbeille "
                    f"({removed} fichier(s))",
                    type="positive",
                )
            if on_deleted:
                try:
                    on_deleted()
                except Exception as e:
                    self.log(f"⚠️ on_deleted callback: {e}")

        if confirm:
            with ui.dialog() as d, ui.card():
                ui.label(f"Envoyer {p.name} à la corbeille ?").classes("text-h6")
                ui.label(
                    f"{len(targets)} fichier(s) au total "
                    "(compagnons + sidecar inclus). Récupérables depuis la "
                    "corbeille système."
                ).classes("text-caption text-grey-6")
                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Annuler", on_click=d.close).props("flat")
                    ui.button(
                        "🗑️ Envoyer à la corbeille",
                        on_click=lambda: (d.close(), _do_delete()),
                    ).props("color=negative unelevated")
            d.open()
        else:
            _do_delete()

    def _preview_dialog_delete(
        self,
        unit: MediaUnit,
        source: str,
        dlg: Any,
        on_deleted: Optional[Callable[[], None]],
    ) -> None:
        """Handler du bouton corbeille placé dans le dialog d'aperçu."""
        def _after() -> None:
            try:
                dlg.close()
            except Exception:
                pass
            if on_deleted:
                try:
                    on_deleted()
                except Exception as e:
                    self.log(f"⚠️ on_deleted preview callback: {e}")

        if source == "viewer":
            self._delete_disk_file_to_trash(
                unit.main_path,
                companions=unit.companions,
                confirm=True,
                on_deleted=_after,
            )
        else:
            # Review : retire de la liste + envoie source à la corbeille
            with ui.dialog() as d, ui.card():
                ui.label(f"Envoyer {Path(unit.main_path).name} à la corbeille ?").classes("text-h6")
                ui.label(
                    "Le fichier source (et ses compagnons) sera envoyé "
                    "dans la corbeille système."
                ).classes("text-caption text-grey-6")
                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Annuler", on_click=d.close).props("flat")

                    def _confirm_delete() -> None:
                        d.close()
                        self._delete_units_to_trash([unit], confirm=False)
                        _after()

                    ui.button(
                        "🗑️ Envoyer à la corbeille",
                        on_click=_confirm_delete,
                    ).props("color=negative unelevated")
            d.open()

    def _toggle_unit_selection(self, main_paths: list[str], checked: bool) -> None:
        """Ajoute/retire des main_paths à la sélection Review."""
        for mp in main_paths:
            if checked:
                self._selected_units.add(mp)
            else:
                self._selected_units.discard(mp)
        self._refresh_selection_bar()

    def _refresh_selection_bar(self) -> None:
        """Met à jour la barre de sélection au-dessus de la grille Review."""
        if self._selection_bar_container is None:
            return
        self._selection_bar_container.clear()
        n = len(self._selected_units)
        with self._selection_bar_container:
            if n == 0:
                return
            with ui.row().classes(
                "w-full items-center gap-2 q-pa-sm"
            ).style(
                "background:rgba(244,67,54,0.10); border-radius:8px; "
                "border:1px solid rgba(244,67,54,0.25);"
            ):
                ui.icon("check_box").classes("text-negative")
                ui.label(f"{n} média(s) sélectionné(s)").classes(
                    "text-body2 text-negative"
                )
                ui.space()
                # Menu tag bulk
                tag_names = self._tag_names()
                if tag_names:
                    add_tag_btn = ui.button(
                        "🏷️ Tag…", icon="sell",
                    ).props("dense color=primary")
                    with add_tag_btn:
                        with ui.menu() as menu:
                            ui.menu_item("Ajouter un tag…").classes(
                                "text-caption text-grey-6"
                            )
                            ui.separator()
                            for name in sorted(tag_names):
                                hex_c = self._tag_hex(name)
                                icon = self._tag_icon(name)

                                def _apply(n=name, m=menu) -> None:
                                    self._bulk_tag_selection(n, remove=False)
                                    m.close()

                                with ui.menu_item(on_click=_apply):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.label(icon).style("font-size:14px;")
                                        ui.label(name)
                                        ui.element("div").style(
                                            f"width:10px; height:10px; border-radius:50%; "
                                            f"background:{hex_c};"
                                        )
                            ui.separator()
                            ui.menu_item("Retirer un tag…").classes(
                                "text-caption text-grey-6"
                            )
                            for name in sorted(tag_names):
                                def _rem(n=name, m=menu) -> None:
                                    self._bulk_tag_selection(n, remove=True)
                                    m.close()

                                with ui.menu_item(on_click=_rem):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.icon("close").classes("text-red-4 text-sm")
                                        ui.label(name).style("font-size:13px;")
                ui.button(
                    "🗑️ Effacer la sélection",
                    icon="delete",
                    on_click=self._delete_selection,
                ).props("color=negative unelevated dense")
                ui.button(
                    "Vider la sélection",
                    icon="clear",
                    on_click=self._clear_selection,
                ).props("flat dense color=grey-7")

    def _bulk_tag_selection(self, tag_name: str, remove: bool = False) -> None:
        """Applique/retire `tag_name` à toutes les units actuellement sélectionnées."""
        sel = set(self._selected_units)
        if not sel:
            return
        units = [u for u in self.units if u.main_path in sel]
        changed = self._apply_tag_to_units(units, tag_name, remove=remove)
        action = "retiré" if remove else "appliqué"
        ui.notify(
            f"🏷️ « {tag_name} » {action} sur {changed}/{len(units)} média(s)",
            type="positive" if changed else "info",
        )
        self._apply_filters(reset_page=False)

    def _delete_selection(self) -> None:
        """Envoie tous les médias actuellement sélectionnés à la corbeille."""
        sel = set(self._selected_units)
        if not sel:
            return
        units = [u for u in self.units if u.main_path in sel]
        self._delete_units_to_trash(units, confirm=True)

    def _clear_selection(self) -> None:
        self._selected_units.clear()
        self._refresh_selection_bar()
        self._apply_filters(reset_page=False)

    # ── sélection multi-checkbox : Viewer (fichiers destination) ───────────
    def _toggle_viewer_selection(self, paths: list[str], checked: bool) -> None:
        for p in paths:
            if checked:
                self._selected_viewer_files.add(p)
            else:
                self._selected_viewer_files.discard(p)
        self._refresh_viewer_selection_bar()

    def _refresh_viewer_selection_bar(self) -> None:
        if self._viewer_selection_bar is None:
            return
        self._viewer_selection_bar.clear()
        n = len(self._selected_viewer_files)
        with self._viewer_selection_bar:
            if n == 0:
                return
            with ui.row().classes(
                "w-full items-center gap-2 q-pa-sm"
            ).style(
                "background:rgba(244,67,54,0.10); border-radius:8px; "
                "border:1px solid rgba(244,67,54,0.25);"
            ):
                ui.icon("check_box").classes("text-negative")
                ui.label(f"{n} fichier(s) sélectionné(s)").classes(
                    "text-body2 text-negative"
                )
                ui.space()
                # Menu tag bulk viewer (persiste dans sidecars sur disque)
                tag_names = self._tag_names()
                if tag_names:
                    add_tag_btn = ui.button(
                        "🏷️ Tag…", icon="sell",
                    ).props("dense color=primary")
                    with add_tag_btn:
                        with ui.menu() as menu:
                            ui.menu_item("Ajouter un tag…").classes(
                                "text-caption text-grey-6"
                            )
                            ui.separator()
                            for name in sorted(tag_names):
                                hex_c = self._tag_hex(name)
                                icon = self._tag_icon(name)

                                def _apply(n=name, m=menu) -> None:
                                    self._bulk_tag_viewer_selection(n, remove=False)
                                    m.close()

                                with ui.menu_item(on_click=_apply):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.label(icon).style("font-size:14px;")
                                        ui.label(name)
                                        ui.element("div").style(
                                            f"width:10px; height:10px; border-radius:50%; "
                                            f"background:{hex_c};"
                                        )
                            ui.separator()
                            ui.menu_item("Retirer un tag…").classes(
                                "text-caption text-grey-6"
                            )
                            for name in sorted(tag_names):
                                def _rem(n=name, m=menu) -> None:
                                    self._bulk_tag_viewer_selection(n, remove=True)
                                    m.close()

                                with ui.menu_item(on_click=_rem):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.icon("close").classes("text-red-4 text-sm")
                                        ui.label(name).style("font-size:13px;")
                ui.button(
                    "🗑️ Effacer la sélection",
                    icon="delete",
                    on_click=self._delete_viewer_selection,
                ).props("color=negative unelevated dense")
                ui.button(
                    "Vider la sélection",
                    icon="clear",
                    on_click=self._clear_viewer_selection,
                ).props("flat dense color=grey-7")

    def _bulk_tag_viewer_selection(self, tag_name: str, remove: bool = False) -> None:
        """Applique/retire `tag_name` sur les sidecars des fichiers sélectionnés dans le viewer."""
        sel = list(self._selected_viewer_files)
        if not sel:
            return
        changed = 0
        for p in sel:
            fp = Path(p)
            current = set(self._read_sidecar_tags(fp))
            if remove:
                if tag_name not in current:
                    continue
                current.discard(tag_name)
            else:
                if tag_name in current:
                    continue
                current.add(tag_name)
            if self._update_sidecar_tags(fp, sorted(current)):
                changed += 1
        action = "retiré" if remove else "appliqué"
        ui.notify(
            f"🏷️ « {tag_name} » {action} sur {changed}/{len(sel)} fichier(s)",
            type="positive" if changed else "info",
        )
        # Invalide le cache calendrier (impact possible sur tag caché)
        self._calendar_index_cache = None
        # Rafraîchit la vue courante
        try:
            self._render_viewer()
        except Exception:
            pass

    def _delete_viewer_selection(self) -> None:
        sel = list(self._selected_viewer_files)
        if not sel:
            return
        with ui.dialog() as d, ui.card():
            ui.label(f"Envoyer {len(sel)} fichier(s) à la corbeille ?").classes("text-h6")
            ui.label(
                "Fichiers + sidecars `.dji.json` compagnons. Récupérables depuis "
                "la corbeille système."
            ).classes("text-caption text-grey-6")
            if len(sel) <= 8:
                with ui.column().classes("gap-0 q-mt-sm"):
                    for p in sel:
                        ui.label(f"• {Path(p).name}").classes("text-caption font-mono")
            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Annuler", on_click=d.close).props("flat")

                def _do() -> None:
                    d.close()
                    all_targets: list[str] = []
                    for p in sel:
                        pp = Path(p)
                        all_targets.append(p)
                        for sc in (
                            pp.with_suffix(pp.suffix + ".dji.json"),
                            pp.with_suffix(".dji.json"),
                        ):
                            if sc.exists():
                                all_targets.append(str(sc))
                    removed, errors = self._trash_paths(all_targets)
                    self._selected_viewer_files.clear()
                    self._calendar_index_cache = None
                    if errors:
                        ui.notify(
                            f"🗑️ {removed} fichier(s) — {len(errors)} erreur(s)",
                            type="warning",
                        )
                    else:
                        ui.notify(
                            f"🗑️ {len(sel)} média(s) envoyés à la corbeille "
                            f"({removed} fichier(s))",
                            type="positive",
                        )
                    self._on_viewer_file_deleted()

                ui.button(
                    "🗑️ Envoyer à la corbeille",
                    on_click=_do,
                ).props("color=negative unelevated")
        d.open()

    def _clear_viewer_selection(self) -> None:
        self._selected_viewer_files.clear()
        self._refresh_viewer_selection_bar()

    def _on_viewer_file_deleted(self) -> None:
        """Rappel appelé après suppression d'un fichier depuis le viewer.
        Rafraîchit la vue courante en la re-rendant intégralement."""
        try:
            # Purge la sélection des fichiers qui n'existent plus
            self._selected_viewer_files = {
                p for p in self._selected_viewer_files if os.path.exists(p)
            }
            # Re-render complet (clear + rebuild) au lieu d'append
            self._render_viewer()
        except Exception as e:
            self.log(f"⚠️ Refresh viewer après delete: {e}")

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
        with ui.tab_panel("confirm"):
            with ui.card().classes("w-full"):
                self._confirm_container = ui.column().classes("w-full gap-1")

            with ui.row().classes("w-full justify-between gap-2 q-mt-md"):
                ui.button("← Retour", on_click=self._stepper.previous).props("flat icon=arrow_back")
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
        # Pour le drone Goggles, la catégorie est aplatie (pas de sous-dossier).
        tree: dict[str, dict[str, dict[str, dict[str, list[str]]]]] = {}
        for u in units:
            if u.drone_id == GOGGLES_DRONE_ID:
                cat_bucket = ""  # pas de sous-dossier catégorie
                group = ""
            else:
                cat_bucket = u.category
                group = u.group_subdir if u.category in ("PANORAMA", "HYPERLAPSE") and u.group_subdir else ""
            (tree.setdefault(u.drone_folder, {})
                 .setdefault(u.capture_date, {})
                 .setdefault(cat_bucket, {})
                 .setdefault(group, [])
                 .append(Path(u.main_path).name))
        lines: list[str] = []
        for drone, dates in sorted(tree.items()):
            lines.append(f"📂 {drone}/")
            for date, cats in sorted(dates.items()):
                lines.append(f"   📅 {date}/")
                for cat, groups in sorted(cats.items()):
                    total = sum(len(v) for v in groups.values())
                    if cat:
                        lines.append(f"      📁 {cat}/  ({total} fichier(s))")
                        indent = "         "
                    else:
                        # Fichiers directement dans le dossier date (cas Goggles)
                        indent = "      "
                    for group, files in sorted(groups.items()):
                        if group:
                            lines.append(f"{indent}📁 {group}/  ({len(files)})")
                            for fn in files[:5]:
                                lines.append(f"{indent}   · {fn}")
                            if len(files) > 5:
                                lines.append(f"{indent}   · … +{len(files) - 5} autres")
                        else:
                            for fn in files[:5]:
                                lines.append(f"{indent}· {fn}")
                            if len(files) > 5:
                                lines.append(f"{indent}· … +{len(files) - 5} autres")
        if not lines:
            lines.append("(rien à déplacer)")
        return lines

    # ── ÉTAPE 5 : Exécution ────────────────────────────────────────────────
    def _step_execute(self) -> None:
        with ui.tab_panel("execute"):
            with ui.card().classes("w-full"):
                self._exec_container = ui.column().classes("w-full")
                with self._exec_container:
                    ui.label("En attente…").classes("text-body2")
            with ui.row().classes("w-full justify-start gap-2 q-mt-md"):
                ui.button("← Retour", on_click=self._stepper.previous).props("flat icon=arrow_back")

    async def _start_execute(self) -> None:
        # Confirmation par dialog
        with ui.dialog() as dialog, ui.card():
            ui.label("Confirmer l'exécution ?").classes("text-h6")
            ui.label("Les originaux copiés seront envoyés à la corbeille."
                     if self.send_to_trash else "Les originaux seront conservés.").classes("text-body2")
            ui.label(
                "♻️ Détection de doublons active : si un fichier destination existe "
                "et est strictement identique (byte-à-byte), la source sera envoyée "
                "à la corbeille sans re-copie."
            ).classes("text-caption text-grey-7")
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

        def on_duplicate(src: str, existing: str) -> None:
            self.log(f"♻️ Doublon identique : {Path(src).name} — {existing}")

        organizer.on_duplicate = on_duplicate

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
            dup_units = summary.get("duplicates_units", 0)
            dup_files = summary.get("duplicates_files", 0)
            ui.label(f"Total: {summary['total_units']}  |  📥 Déplacés: {summary['moved']}  |  "
                     f"🗑️ Effacés: {summary['deleted']}  |  ⏭️ Ignorés: {summary['skipped']}  |  "
                     f"♻️ Doublons: {dup_units} ({dup_files} fichier(s))  |  "
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

            ui.separator()
            with ui.row().classes("w-full gap-2 justify-center q-mt-md"):
                ui.button(
                    "🔄 Nouvelle session (retour au début)",
                    on_click=self._reset_and_restart,
                    icon="restart_alt",
                ).props("color=primary size=lg")
                ui.button(
                    "🚪 Quitter",
                    on_click=lambda: (ui.notify("À bientôt !", type="info"), app.shutdown()),
                    icon="logout",
                ).props("color=grey outline")
        ui.notify("✅ Exécution terminée !", type="positive", timeout=10000)

        # Popup récapitulatif des doublons détectés (fichier identique déjà à destination)
        if summary.get("duplicates_files", 0) > 0:
            self._show_duplicates_dialog(organizer)

    def _show_duplicates_dialog(self, organizer: "DJIOrganizer") -> None:
        """Affiche un dialog listant les fichiers doublons détectés
        (source identique à un fichier destination déjà présent)."""
        rows: list[tuple[str, str, int]] = []
        for r in organizer.results:
            for d in r.get("duplicates", []) or []:
                try:
                    size = int(d.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                rows.append((d.get("source", ""), d.get("existing", ""), size))
        if not rows:
            return
        total_bytes = sum(r[2] for r in rows)
        trashed_count = sum(
            1 for r in organizer.results
            for d in (r.get("duplicates") or [])
            if d.get("source") in (r.get("trashed") or [])
        )
        with ui.dialog() as dlg, ui.card().classes("w-full").style("max-width:900px;"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.icon("content_copy").classes("text-warning text-2xl")
                ui.label("Doublons détectés").classes("text-h6 flex-grow")
                ui.badge(f"{len(rows)} fichier(s)", color="warning")
                ui.badge(f"~{human_size(total_bytes)} économisé(s)", color="positive")
            ui.label(
                "Ces fichiers étaient déjà présents à destination avec un contenu "
                "strictement identique (comparaison byte-à-byte). Les sources ont "
                f"été envoyées à la corbeille sans re-copie ({trashed_count}/{len(rows)})."
            ).classes("text-body2 text-grey-7")
            with ui.scroll_area().classes("w-full").style("max-height:50vh;"):
                for src, existing, size in rows:
                    with ui.card().classes("w-full q-my-xs").style(
                        "background:rgba(255,152,0,0.05); border-left:3px solid #FF9800;"
                    ):
                        with ui.column().classes("gap-0 q-pa-xs"):
                            with ui.row().classes("items-center gap-1"):
                                ui.icon("upload_file").classes("text-orange text-sm")
                                ui.label(Path(src).name).classes(
                                    "text-body2 font-medium truncate"
                                ).tooltip(src)
                                ui.space()
                                ui.label(human_size(size)).classes("text-caption text-grey-7")
                            with ui.row().classes("items-center gap-1"):
                                ui.icon("folder").classes("text-blue text-sm")
                                ui.label(existing).classes(
                                    "text-caption font-mono text-grey-6 truncate"
                                ).tooltip(existing)
            with ui.row().classes("w-full justify-end q-mt-sm"):
                ui.button("Fermer", on_click=dlg.close).props("unelevated color=primary")
        dlg.open()

    # ── ONGLET Drones : gestion CRUD du mapping ───────────────────────────
    def _step_drones(self) -> None:
        with ui.tab_panel("drones"):
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.icon("flight_takeoff").classes("text-2xl text-primary")
                    ui.label("Configuration des drones").classes("text-h6 flex-grow")
                    ui.button(
                        "➕ Ajouter un drone",
                        on_click=self._add_drone_entry,
                        icon="add",
                    ).props("color=primary")
                ui.label(
                    "Chaque drone possède : un identifiant unique, un motif (regex) "
                    "de détection sur les métadonnées, un dossier de destination et une "
                    "photo (dans assets/)."
                ).classes("text-caption text-grey-7")

                self._drones_container = ui.column().classes("w-full gap-3")
                self._refresh_drones_panel()

    def _refresh_drones_panel(self) -> None:
        if self._drones_container is None:
            return
        self._drones_container.clear()
        # Liste des images disponibles dans assets/
        assets_dir = APP_DIR / "assets"
        available_images = {}
        if assets_dir.is_dir():
            for p in sorted(assets_dir.iterdir()):
                if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    available_images[f"assets/{p.name}"] = p.name
        available_images[""] = "(aucune)"

        with self._drones_container:
            drones = CONFIG.get("drone_mapping", [])
            if not drones:
                ui.label("(aucun drone configuré)").classes("text-caption text-grey-6")

            with ui.grid(columns=2).classes("w-full gap-3"):
                for idx, drone in enumerate(list(drones)):
                    self._render_drone_card(idx, drone, available_images)

    def _render_drone_card(self, idx: int, drone: dict, available_images: dict[str, str]) -> None:
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-start gap-2 no-wrap"):
                # Photo
                img_rel = drone.get("image", "")
                with ui.column().classes("items-center gap-1"):
                    if img_rel and (APP_DIR / img_rel).exists():
                        ui.image(f"/{img_rel}").classes("w-32 h-32 object-cover rounded")
                    else:
                        with ui.element("div").classes(
                            "w-32 h-32 flex items-center justify-center bg-grey-3 rounded"
                        ):
                            ui.icon("flight").classes("text-5xl text-grey-6")
                    img_sel = ui.select(
                        options=available_images,
                        value=img_rel if img_rel in available_images else (available_images[0] if available_images else None),
                        label="Image",
                    ).props("dense options-dense").classes("w-32")

                # Champs
                with ui.column().classes("flex-grow gap-1 min-w-0"):
                    label_in = ui.input("Nom affiché", value=drone.get("label", drone.get("id", ""))).classes("w-full")
                    id_in = ui.input("ID (unique, MAJ)", value=drone.get("id", "")).classes("w-full")
                    pattern_in = ui.input("Motif regex détection", value=drone.get("pattern", "")).classes("w-full")
                    folder_in = ui.input("Dossier destination", value=drone.get("folder", "")).classes("w-full")

                    with ui.row().classes("w-full gap-1 items-center"):
                        def _save(i=idx, li=label_in, ii=id_in, pi=pattern_in, fi=folder_in, si=img_sel):
                            new_id = ii.value.strip().upper()
                            new_pattern = pi.value.strip()
                            new_folder = fi.value.strip()
                            if not new_id or not new_pattern or not new_folder:
                                ui.notify("ID, motif et dossier sont requis", type="warning")
                                return
                            try:
                                re.compile(new_pattern, re.IGNORECASE)
                            except re.error as e:
                                ui.notify(f"Regex invalide : {e}", type="negative")
                                return
                            CONFIG["drone_mapping"][i] = {
                                "id": new_id,
                                "label": li.value.strip() or new_id,
                                "pattern": new_pattern,
                                "folder": new_folder,
                                "image": si.value or "",
                            }
                            save_config(CONFIG)
                            ui.notify(f"✅ {new_id} enregistré", type="positive")
                            self._refresh_drones_panel()

                        def _delete(i=idx, dn=drone):
                            CONFIG["drone_mapping"].pop(i)
                            save_config(CONFIG)
                            ui.notify(f"🗑️ {dn.get('id', '?')} supprimé", type="info")
                            self._refresh_drones_panel()

                        ui.button("💾 Enregistrer", on_click=_save).props("color=primary dense")
                        ui.button(icon="delete", on_click=_delete).props(
                            "color=negative dense flat round"
                        ).tooltip("Supprimer ce drone")

    def _add_drone_entry(self) -> None:
        # Génère un ID par défaut unique
        base = "DRONE"
        existing = {d.get("id", "") for d in CONFIG.get("drone_mapping", [])}
        n = 1
        while f"{base}{n}" in existing:
            n += 1
        CONFIG.setdefault("drone_mapping", []).append({
            "id": f"{base}{n}",
            "label": f"Nouveau drone {n}",
            "pattern": "",
            "folder": f"00-DJI-{base}{n}",
            "image": "",
        })
        save_config(CONFIG)
        self._refresh_drones_panel()

    # ── ONGLET Tags : CRUD + visibilité viewer ─────────────────────────────
    def _step_tags(self) -> None:
        with ui.tab_panel("tags"):
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.icon("sell").classes("text-2xl text-primary")
                    ui.label("Gestion des tags").classes("text-h6 flex-grow")
                    ui.button(
                        "➕ Nouveau tag",
                        on_click=self._add_tag_entry,
                        icon="add",
                    ).props("color=primary")
                ui.label(
                    "Chaque média peut porter plusieurs tags. Cochez « Masquer dans le "
                    "visualiseur » pour cacher automatiquement les médias porteurs d'un "
                    "tag donné (ex. NSFW). Le drapeau est global au visualiseur."
                ).classes("text-caption text-grey-7")

                # Toggle global : afficher les tags masqués
                with ui.row().classes("w-full items-center gap-2 q-mt-sm"):
                    ui.icon("visibility_off").classes("text-grey")
                    show_hidden = ui.switch(
                        "Afficher aussi les médias avec des tags masqués (dans le visualiseur)",
                        value=CONFIG.get("viewer_show_hidden_tags", False),
                    )

                    def _on_show_hidden(e) -> None:
                        CONFIG["viewer_show_hidden_tags"] = bool(e.value)
                        save_config(CONFIG)
                        self._calendar_index_cache = None
                        ui.notify(
                            "Médias masqués " + ("visibles" if e.value else "cachés"),
                            type="info",
                        )

                    show_hidden.on_value_change(_on_show_hidden)

                self._tags_container = ui.column().classes("w-full gap-2 q-mt-sm")
                self._refresh_tags_panel()

    def _refresh_tags_panel(self) -> None:
        if getattr(self, "_tags_container", None) is None:
            return
        self._tags_container.clear()
        with self._tags_container:
            tags = list(self._tag_defs())
            if not tags:
                ui.label("(aucun tag défini)").classes("text-caption text-grey-6")
                return
            with ui.grid(columns=2).classes("w-full gap-2"):
                for idx, t in enumerate(tags):
                    self._render_tag_card(idx, t)

    def _render_tag_card(self, idx: int, tag: dict) -> None:
        color = tag.get("color", "#78909C")
        with ui.card().classes("w-full").style(
            f"border-left:4px solid {color};"
        ):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                # Aperçu chip
                with ui.element("div").style(
                    f"background:{color}22; color:{color}; "
                    "padding:4px 10px; border-radius:12px; "
                    "font-weight:600; display:inline-flex; "
                    "align-items:center; gap:4px; font-size:13px;"
                ):
                    ui.label(tag.get("icon", "#")).style("font-size:14px;")
                    ui.label(tag.get("name", "?"))
                ui.space()
                if tag.get("hidden"):
                    ui.icon("visibility_off").classes("text-red-4").tooltip(
                        "Masqué dans le visualiseur"
                    )

            with ui.row().classes("w-full items-center gap-1 no-wrap q-mt-xs"):
                name_in = ui.input("Nom", value=tag.get("name", "")).props(
                    "dense outlined"
                ).classes("flex-grow")
                icon_in = ui.input("Icône", value=tag.get("icon", "#")).props(
                    "dense outlined"
                ).classes("w-20")
                color_in = ui.input("Couleur", value=color).props(
                    "dense outlined"
                ).classes("w-28")
                hidden_sw = ui.switch("Masquer", value=bool(tag.get("hidden", False)))

            with ui.row().classes("w-full items-center gap-1 q-mt-xs"):
                def _save(
                    i=idx, ni=name_in, ii=icon_in, ci=color_in, hi=hidden_sw
                ) -> None:
                    new_name = (ni.value or "").strip()
                    if not new_name:
                        ui.notify("Le nom est requis", type="warning")
                        return
                    # Anti-collision (autre index)
                    for j, other in enumerate(self._tag_defs()):
                        if j != i and (other.get("name") or "").strip().lower() == new_name.lower():
                            ui.notify(f"Un tag « {new_name} » existe déjà", type="warning")
                            return
                    self._tag_defs()[i] = {
                        "name": new_name,
                        "icon": (ii.value or "#").strip()[:4] or "#",
                        "color": (ci.value or "#78909C").strip(),
                        "hidden": bool(hi.value),
                    }
                    save_config(CONFIG)
                    self._calendar_index_cache = None
                    ui.notify(f"✅ Tag « {new_name} » enregistré", type="positive")
                    self._refresh_tags_panel()

                def _delete(i=idx, dn=tag) -> None:
                    self._tag_defs().pop(i)
                    save_config(CONFIG)
                    self._calendar_index_cache = None
                    ui.notify(
                        f"🗑️ Tag « {dn.get('name', '?')} » supprimé",
                        type="info",
                    )
                    self._refresh_tags_panel()

                ui.button("💾 Enregistrer", on_click=_save).props(
                    "color=primary dense"
                )
                ui.button(icon="delete", on_click=_delete).props(
                    "color=negative dense flat round"
                ).tooltip("Supprimer ce tag (ne modifie pas les sidecars existants)")

    def _add_tag_entry(self) -> None:
        existing = {(t.get("name") or "").lower() for t in self._tag_defs()}
        n = 1
        while f"tag_{n}" in existing:
            n += 1
        self._tag_defs().append({
            "name": f"tag_{n}",
            "icon": "🏷️",
            "color": "#78909C",
            "hidden": False,
        })
        save_config(CONFIG)
        self._refresh_tags_panel()

    # ── ONGLET Visualiseur : parcourir les médias déjà classés ─────────────
    def _step_viewer(self) -> None:
        with ui.tab_panel("viewer"):
            # Onglets internes : Parcourir (hiérarchique) + Calendrier
            with ui.tabs().props("dense inline-label active-color=primary indicator-color=primary").classes("w-full") as v_tabs:
                ui.tab("browse", label="Parcourir", icon="folder_open")
                ui.tab("calendar", label="Calendrier", icon="calendar_month")
            with ui.tab_panels(v_tabs, value="browse").classes("w-full"):
                with ui.tab_panel("browse"):
                    self._viewer_container = ui.column().classes("w-full gap-3")
                    self._render_viewer()
                with ui.tab_panel("calendar"):
                    self._calendar_container = ui.column().classes("w-full gap-3")
                    self._render_calendar()
            v_tabs.on_value_change(self._on_viewer_subtab_change)

    def _on_viewer_subtab_change(self, e) -> None:
        # Force refresh à chaque activation
        if e.value == "browse":
            self._render_viewer()
        elif e.value == "calendar":
            # Invalide le cache pour refléter le disque
            self._calendar_index_cache = None
            self._render_calendar()

    # ── ÉTAPE 8 : Recherche ────────────────────────────────────────────────
    def _step_search(self) -> None:
        with ui.tab_panel("search"):
            with ui.card().classes("w-full"):
                ui.label("🔍 Recherche dans la destination").classes("text-h6")
                ui.label(
                    "Filtre les fichiers déjà organisés par nom, date, tag, "
                    "catégorie ou drone. La recherche scanne les sidecars "
                    "`.dji.json` du dossier destination."
                ).classes("text-caption text-grey-6")

                # Ligne 1 : nom + dates
                with ui.row().classes("w-full items-center gap-2 q-mt-md"):
                    self._search_name_input = ui.input(
                        label="Nom (fichier ou nom personnalisé)",
                        placeholder="ex. DJI_0212, coucher soleil, vol_test…",
                    ).props("dense outlined clearable").classes("flex-grow")
                    self._search_name_input.on(
                        "keydown.enter", lambda: self._run_search()
                    )
                    self._search_date_from = ui.input(
                        label="Du (YYYY-MM-DD)",
                    ).props(
                        "dense outlined clearable mask='####-##-##' fill-mask"
                    ).classes("min-w-40")
                    self._search_date_to = ui.input(
                        label="Au (YYYY-MM-DD)",
                    ).props(
                        "dense outlined clearable mask='####-##-##' fill-mask"
                    ).classes("min-w-40")

                # Ligne 2 : tag + catégorie + drone
                with ui.row().classes("w-full items-center gap-2 q-mt-sm"):
                    self._search_tag_sel = ui.select(
                        options=self._tag_names(),
                        value=[],
                        label="Tags (multi)",
                        multiple=True,
                    ).props(
                        "dense outlined options-dense use-chips clearable"
                    ).classes("min-w-60")
                    # Mode ET / OU pour combiner plusieurs tags
                    self._search_tag_mode = ui.toggle(
                        options={"any": "OU", "all": "ET"},
                        value="any",
                    ).props("dense").tooltip(
                        "OU = au moins un tag / ET = tous les tags"
                    )
                    self._search_cat_sel = ui.select(
                        options=["TOUTES"] + CATEGORIES,
                        value="TOUTES",
                        label="Catégorie",
                    ).props("dense outlined options-dense").classes("min-w-40")
                    drone_ids = ["TOUS"] + [d["id"] for d in CONFIG.get("drone_mapping", [])]
                    self._search_drone_sel = ui.select(
                        options=drone_ids,
                        value="TOUS",
                        label="Drone",
                    ).props("dense outlined options-dense").classes("min-w-40")
                    ui.space()
                    ui.button(
                        "🔍 Rechercher",
                        icon="search",
                        on_click=self._run_search,
                    ).props("unelevated color=primary")
                    ui.button(
                        "Réinit.",
                        icon="restart_alt",
                        on_click=self._reset_search_filters,
                    ).props("flat dense color=grey-7")

            # Résultats
            with ui.card().classes("w-full q-mt-sm"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.icon("list").classes("text-primary")
                    self._search_count_label = ui.label(
                        "Utilise les filtres puis clique sur Rechercher."
                    ).classes("text-body2")
                self._search_results_container = ui.column().classes(
                    "w-full gap-2 q-mt-sm"
                )

    def _refresh_search_filters(self) -> None:
        """Met à jour les options des selects (tags/drones) selon la config."""
        try:
            if self._search_tag_sel is not None:
                names = self._tag_names()
                self._search_tag_sel.options = names
                # Nettoie les tags disparus
                cur = list(self._search_tag_sel.value or [])
                self._search_tag_sel.value = [t for t in cur if t in names]
                self._search_tag_sel.update()
            if self._search_drone_sel is not None:
                drone_ids = ["TOUS"] + [d["id"] for d in CONFIG.get("drone_mapping", [])]
                self._search_drone_sel.options = drone_ids
                if self._search_drone_sel.value not in drone_ids:
                    self._search_drone_sel.value = "TOUS"
                self._search_drone_sel.update()
        except Exception as e:
            self.log(f"⚠️ Refresh filtres recherche: {e}")

    def _reset_search_filters(self) -> None:
        for widget, default in (
            (self._search_name_input, ""),
            (self._search_date_from, ""),
            (self._search_date_to, ""),
            (self._search_tag_sel, []),
            (self._search_cat_sel, "TOUTES"),
            (self._search_drone_sel, "TOUS"),
        ):
            if widget is not None:
                try:
                    widget.value = default
                    widget.update()
                except Exception:
                    pass
        if self._search_results_container is not None:
            self._search_results_container.clear()
        if self._search_count_label is not None:
            self._search_count_label.text = "Filtres réinitialisés."

    def _run_search(self) -> None:
        """Scan la destination + applique les filtres + rend la grille de résultats."""
        if self._search_results_container is None:
            return
        name_q = ((self._search_name_input.value or "") if self._search_name_input else "").strip().lower()
        date_from = ((self._search_date_from.value or "") if self._search_date_from else "").strip()
        date_to = ((self._search_date_to.value or "") if self._search_date_to else "").strip()
        # Multi-tags : liste + mode ET/OU
        tag_sel_val = self._search_tag_sel.value if self._search_tag_sel else []
        selected_tags: set[str] = set(tag_sel_val or [])
        tag_mode = (
            getattr(self, "_search_tag_mode", None).value
            if hasattr(self, "_search_tag_mode") and self._search_tag_mode is not None
            else "any"
        )
        cat_f = (self._search_cat_sel.value if self._search_cat_sel else "TOUTES") or "TOUTES"
        drone_f = (self._search_drone_sel.value if self._search_drone_sel else "TOUS") or "TOUS"

        # Placeholder loading
        self._search_results_container.clear()
        with self._search_results_container:
            loading = ui.column().classes("w-full items-center q-pa-lg")
            with loading:
                ui.spinner(size="lg", color="primary")
                ui.label("Recherche en cours…").classes("text-body2 q-mt-sm")

        def _scan() -> list[tuple[Path, dict]]:
            """Retourne [(file_path, classification_dict), …] filtré."""
            dest = Path(self.destination_dir)
            if not dest.is_dir():
                return []
            date_pat = re.compile(r"^\d{4}-\d{2}-\d{2}$")
            results: list[tuple[Path, dict]] = []
            hidden_tags = set(self._hidden_tag_names())
            show_hidden = bool(CONFIG.get("viewer_show_hidden_tags", False))

            # Scan drone → date → catégorie
            for drone in CONFIG.get("drone_mapping", []):
                drone_id = drone.get("id", "?")
                if drone_f != "TOUS" and drone_f != drone_id:
                    continue
                folder = drone.get("folder", "")
                if not folder:
                    continue
                drone_dir = dest / folder
                if not drone_dir.is_dir():
                    continue
                for date_dir in drone_dir.iterdir():
                    if not date_dir.is_dir():
                        continue
                    date_str = date_dir.name
                    if not date_pat.match(date_str):
                        continue
                    # Filtre date
                    if date_from and date_str < date_from:
                        continue
                    if date_to and date_str > date_to:
                        continue
                    for f in date_dir.rglob("*"):
                        if not f.is_file() or f.suffix.lower() not in ALL_MEDIA_EXTS:
                            continue
                        # Récupère la catégorie via le chemin relatif
                        try:
                            rel = f.relative_to(date_dir)
                            cat = rel.parts[0] if len(rel.parts) > 1 else ""
                        except ValueError:
                            cat = ""
                        if cat_f != "TOUTES" and cat != cat_f:
                            continue
                        # Charge sidecar minimal pour tags + custom_name
                        classification: dict = {}
                        for sc in (
                            f.with_suffix(f.suffix + ".dji.json"),
                            f.with_suffix(".dji.json"),
                        ):
                            if sc.exists():
                                try:
                                    with open(sc, "r", encoding="utf-8") as fh:
                                        payload = json.load(fh)
                                    classification = payload.get("classification", {}) or {}
                                except Exception:
                                    classification = {}
                                break
                        # Masquage tags cachés
                        file_tags = set(classification.get("tags") or [])
                        if hidden_tags and not show_hidden and (file_tags & hidden_tags):
                            continue
                        # Filtre tags (multi + mode ET/OU)
                        if selected_tags:
                            if tag_mode == "all":
                                if not selected_tags.issubset(file_tags):
                                    continue
                            else:  # any (OU)
                                if not (selected_tags & file_tags):
                                    continue
                        # Filtre nom (fichier ou custom_name)
                        if name_q:
                            haystack = (
                                f.name.lower()
                                + " "
                                + str(classification.get("custom_name", "") or "").lower()
                            )
                            if name_q not in haystack:
                                continue
                        # Injecte le drone_id / date / cat au cas où le sidecar est absent
                        classification.setdefault("drone_id", drone_id)
                        classification.setdefault("capture_date", date_str)
                        classification.setdefault("category", cat or "VIDEO")
                        results.append((f, classification))
            # Tri : date desc puis nom
            results.sort(key=lambda t: (t[1].get("capture_date", ""), t[0].name), reverse=True)
            return results

        async def _load_and_render() -> None:
            try:
                results = await run.io_bound(_scan)
            except Exception as e:
                self.log(f"❌ Recherche échouée: {e}")
                self._search_results_container.clear()
                with self._search_results_container:
                    ui.label(f"Erreur : {e}").classes("text-caption text-negative")
                return
            # Précalcule les thumbnails hors event-loop
            try:
                await run.io_bound(
                    lambda: [generate_thumbnail(str(fp), size=200) for fp, _ in results]
                )
            except Exception:
                pass

            self._search_results_container.clear()
            n = len(results)
            if self._search_count_label is not None:
                self._search_count_label.text = f"{n} résultat(s)"
            with self._search_results_container:
                if not results:
                    ui.label(
                        "(aucun résultat — ajuste les filtres et réessaie)"
                    ).classes("text-caption text-grey-6")
                    return
                # Regroupe par (drone, date) pour rendre visuellement structuré
                grouped: dict[tuple[str, str], list[tuple[Path, dict]]] = {}
                for fp, cls in results:
                    key = (cls.get("drone_id", "?"), cls.get("capture_date", ""))
                    grouped.setdefault(key, []).append((fp, cls))

                for (drone_id, date_str), items in grouped.items():
                    with ui.row().classes(
                        "w-full items-center gap-2 q-mt-sm q-pa-xs"
                    ).style(
                        f"background:{self._drone_hex(drone_id)}18; "
                        f"border-left:3px solid {self._drone_hex(drone_id)}; "
                        f"border-radius:6px;"
                    ):
                        ui.icon("flight").style(f"color:{self._drone_hex(drone_id)};")
                        ui.label(f"{drone_id} · {date_str}").classes(
                            "text-subtitle2"
                        )
                        ui.badge(f"{len(items)}", color="primary")
                    # Regroupe PANO/HYPER/WAYPOINTS
                    disk_items = self._partition_disk_files(
                        [fp for fp, _ in items]
                    )
                    with ui.grid(columns=4).classes("w-full gap-2"):
                        for it in disk_items:
                            if it["kind"] == "single":
                                self._render_viewer_media_card(it["path"])
                            else:
                                self._render_viewer_group_card(
                                    it["category"],
                                    it["group_subdir"],
                                    it["files"],
                                )

        background_tasks.create(_load_and_render(), name="search_run")

    def _on_tab_change(self, e) -> None:
        # Rafraîchit automatiquement le contenu selon l'onglet actif
        if e.value == "viewer":
            self._render_viewer()
        elif e.value == "drones":
            self._refresh_drones_panel()
        elif e.value == "search":
            self._refresh_search_filters()

    def _render_viewer(self) -> None:
        if self._viewer_container is None:
            return
        self._viewer_container.clear()

        with self._viewer_container:
            # Breadcrumb
            with ui.row().classes("w-full items-center gap-1"):
                ui.icon("collections").classes("text-primary")
                ui.button(
                    "🚁 Drones",
                    on_click=lambda: self._viewer_goto("drones"),
                ).props("flat dense")
                if self._viewer_state["drone"]:
                    ui.icon("chevron_right").classes("text-grey")
                    d = self._viewer_state["drone"]
                    ui.button(
                        d.get("label", d.get("id", "?")),
                        on_click=lambda: self._viewer_goto("dates"),
                    ).props("flat dense")
                if self._viewer_state["date"]:
                    ui.icon("chevron_right").classes("text-grey")
                    ui.label(self._viewer_state["date"]).classes("text-body2")
                ui.space()
                if not os.path.isdir(self.destination_dir):
                    ui.badge("destination introuvable", color="negative")

            # Barre sélection multi-checkbox (dynamique)
            self._viewer_selection_bar = ui.column().classes("w-full")
            self._refresh_viewer_selection_bar()

            level = self._viewer_state.get("level", "drones")
            if level == "drones":
                self._render_viewer_drones()
            elif level == "dates":
                self._render_viewer_dates()
            elif level == "media":
                self._render_viewer_media()

    def _viewer_goto(self, level: str, **kwargs) -> None:
        if level == "drones":
            self._viewer_state = {"level": "drones", "drone": None, "date": None}
        elif level == "dates":
            self._viewer_state["level"] = "dates"
            self._viewer_state["date"] = None
        elif level == "media":
            self._viewer_state["level"] = "media"
        self._viewer_state.update(kwargs)
        self._render_viewer()

    def _render_viewer_drones(self) -> None:
        ui.label("Sélectionne un drone").classes("text-h6")
        drones = CONFIG.get("drone_mapping", [])
        if not drones:
            ui.label("(aucun drone configuré — ouvre l'onglet Drones)").classes("text-caption")
            return
        with ui.grid(columns=4).classes("w-full gap-3"):
            for drone in drones:
                folder = Path(self.destination_dir) / drone.get("folder", "")
                # Compte les dates disponibles
                n_dates = 0
                if folder.is_dir():
                    n_dates = sum(1 for p in folder.iterdir() if p.is_dir())

                with ui.card().classes("w-full cursor-pointer hover:shadow-lg").on(
                    "click", lambda d=drone: self._viewer_goto("dates", drone=d)
                ):
                    img_rel = drone.get("image", "")
                    if img_rel and (APP_DIR / img_rel).exists():
                        ui.image(f"/{img_rel}").classes("w-full h-32 object-cover rounded")
                    else:
                        with ui.element("div").classes(
                            "w-full h-32 flex items-center justify-center bg-grey-3 rounded"
                        ):
                            ui.icon("flight").classes("text-5xl text-grey-6")
                    ui.label(drone.get("label", drone.get("id", "?"))).classes(
                        "text-body1 font-bold truncate"
                    ).tooltip(drone.get("id", ""))
                    with ui.row().classes("items-center gap-1"):
                        ui.icon("event").classes("text-grey-7 text-sm")
                        ui.label(f"{n_dates} date(s)").classes("text-caption text-grey-7")

    def _render_viewer_dates(self) -> None:
        drone = self._viewer_state["drone"]
        if not drone:
            return
        folder = Path(self.destination_dir) / drone.get("folder", "")
        ui.label(f"Dates disponibles — {drone.get('label', drone.get('id', '?'))}").classes("text-h6")
        if not folder.is_dir():
            ui.label(f"Dossier introuvable : {folder}").classes("text-caption text-orange")
            return

        loading_col = ui.column().classes("w-full items-center q-pa-lg")
        with loading_col:
            ui.spinner(size="lg", color="primary")
            ui.label("Analyse des dates…").classes("text-body2 q-mt-sm")

        results_col = ui.column().classes("w-full gap-2")

        def _scan_dates() -> list[tuple[str, int, dict[str, int]]]:
            dates: list[tuple[str, int, dict[str, int]]] = []
            for p in sorted(folder.iterdir(), reverse=True):
                if not p.is_dir():
                    continue
                n_media = 0
                tag_counts: dict[str, int] = {}
                for f in p.rglob("*"):
                    if not f.is_file():
                        continue
                    if f.suffix.lower() in ALL_MEDIA_EXTS:
                        n_media += 1
                        try:
                            for t in self._read_sidecar_tags(f):
                                tag_counts[t] = tag_counts.get(t, 0) + 1
                        except Exception:
                            pass
                if n_media > 0:
                    dates.append((p.name, n_media, tag_counts))
            return dates

        async def _load() -> None:
            try:
                dates = await run.io_bound(_scan_dates)
            except Exception as e:
                self.log(f"❌ Scan dates échoué: {e}")
                loading_col.clear()
                with loading_col:
                    ui.label(f"Erreur : {e}").classes("text-caption text-negative")
                return

            loading_col.delete()

            if not dates:
                with results_col:
                    ui.label("(aucune date trouvée)").classes("text-caption text-grey-6")
                return

            with results_col:
                # Tags cachés à filtrer sauf si l'utilisateur les affiche
                show_hidden = bool(CONFIG.get("viewer_show_hidden_tags", False))
                hidden_names = self._hidden_tag_names() if not show_hidden else set()
                with ui.grid(columns=4).classes("w-full gap-2"):
                    for date_str, n, tag_counts in dates:
                        with ui.card().classes("w-full cursor-pointer hover:shadow-md").on(
                            "click", lambda d=date_str: self._viewer_goto("media", date=d)
                        ):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("calendar_month").classes("text-primary text-2xl")
                                with ui.column().classes("gap-0"):
                                    ui.label(date_str).classes("text-body1 font-bold")
                                    ui.label(f"{n} média(s)").classes("text-caption text-grey-7")
                            # Résumé des tags (chips triés par occurrence décroissante),
                            # sans les tags marqués "à masquer" si l'option est désactivée.
                            visible_tag_counts = {
                                k: v for k, v in tag_counts.items()
                                if k not in hidden_names
                            }
                            if visible_tag_counts:
                                with ui.row().classes(
                                    "w-full items-center gap-1 q-mt-xs"
                                ).style("flex-wrap:wrap;"):
                                    sorted_tags = sorted(
                                        visible_tag_counts.items(),
                                        key=lambda kv: (-kv[1], kv[0]),
                                    )
                                    for tag_name, count in sorted_tags:
                                        hex_c = self._tag_hex(tag_name)
                                        icon = self._tag_icon(tag_name)
                                        with ui.element("div").style(
                                            f"background:{hex_c}22; color:{hex_c}; "
                                            f"padding:1px 6px; border-radius:8px; "
                                            f"font-size:10px; font-weight:600; "
                                            f"display:inline-flex; align-items:center; "
                                            f"gap:3px; border:1px solid {hex_c};"
                                        ).tooltip(f"{tag_name} — {count} média(s)"):
                                            ui.label(icon).style("font-size:10px;")
                                            ui.label(tag_name)
                                            ui.label(f"×{count}").style(
                                                "opacity:0.75; margin-left:2px;"
                                            )

        background_tasks.create(_load(), name=f"viewer_dates_{drone.get('id', '')}")

    def _render_viewer_media(self) -> None:
        drone = self._viewer_state["drone"]
        date_str = self._viewer_state["date"]
        if not drone or not date_str:
            return
        base = Path(self.destination_dir) / drone.get("folder", "") / date_str
        header_row = ui.row().classes("w-full items-center gap-2")
        with header_row:
            ui.label(f"{drone.get('label', drone.get('id', '?'))} · {date_str}").classes("text-h6 flex-grow")

        if not base.is_dir():
            ui.label(f"Dossier introuvable : {base}").classes("text-caption text-orange")
            return

        # Placeholder de chargement — remplacé quand le scan est terminé
        loading_col = ui.column().classes("w-full items-center q-pa-xl")
        with loading_col:
            ui.spinner(size="xl", color="primary")
            ui.label("Chargement des médias…").classes("text-body1 q-mt-md")
            loading_progress = ui.label("").classes("text-caption text-grey-7")

        # Conteneur où les vignettes seront insérées après scan
        media_col = ui.column().classes("w-full gap-1")

        # Scan I/O + génération thumbnails hors event-loop (io_bound)
        def _scan_and_prepare() -> tuple[dict[str, list[Path]], dict[str, str], int]:
            hidden_tags = set(self._hidden_tag_names())
            show_hidden = bool(CONFIG.get("viewer_show_hidden_tags", False))
            by_cat: dict[str, list[Path]] = {}
            for f in base.rglob("*"):
                if f.is_file() and f.suffix.lower() in ALL_MEDIA_EXTS:
                    # Masquage : si le sidecar mentionne un tag caché, on skip
                    if hidden_tags and not show_hidden:
                        try:
                            file_tags = set(self._read_sidecar_tags(f))
                            if file_tags & hidden_tags:
                                continue
                        except Exception:
                            pass
                    try:
                        rel = f.relative_to(base)
                        cat = rel.parts[0] if len(rel.parts) > 1 else ""
                    except ValueError:
                        cat = ""
                    by_cat.setdefault(cat, []).append(f)
            # Précalcule thumbnails (utilise le cache disque si déjà générés)
            thumbs: dict[str, str] = {}
            total = sum(len(v) for v in by_cat.values())
            for cat_files in by_cat.values():
                for f in cat_files:
                    t = generate_thumbnail(str(f), size=200)
                    if t:
                        thumbs[str(f)] = t
            return by_cat, thumbs, total

        async def _load_and_render() -> None:
            try:
                by_category, thumbs, total_media = await run.io_bound(_scan_and_prepare)
            except Exception as e:
                self.log(f"❌ Scan viewer échoué: {e}")
                loading_col.clear()
                with loading_col:
                    ui.label(f"Erreur : {e}").classes("text-caption text-negative")
                return

            loading_col.delete()

            if not by_category:
                with media_col:
                    ui.label("(aucun média)").classes("text-caption")
                return

            # Collecte tous les chemins visibles (pour tout sélectionner/désélectionner)
            all_paths: list[str] = []
            for cat_files in by_category.values():
                for f in cat_files:
                    all_paths.append(str(f))

            with header_row:
                ui.badge(f"{total_media} média(s)", color="primary")
                ui.space()

                def _select_all() -> None:
                    self._selected_viewer_files.update(all_paths)
                    ui.notify(
                        f"✓ {len(all_paths)} média(s) sélectionné(s)",
                        type="positive",
                    )
                    self._render_viewer()

                def _deselect_all() -> None:
                    before = len(self._selected_viewer_files & set(all_paths))
                    self._selected_viewer_files.difference_update(all_paths)
                    ui.notify(
                        f"✗ {before} média(s) désélectionné(s)",
                        type="info",
                    )
                    self._render_viewer()

                ui.button(
                    "Tout sélectionner",
                    icon="select_all",
                    on_click=_select_all,
                ).props("flat dense color=primary").tooltip(
                    f"Sélectionner les {total_media} médias de cette date"
                )
                ui.button(
                    "Tout désélectionner",
                    icon="deselect",
                    on_click=_deselect_all,
                ).props("flat dense color=grey-7").tooltip(
                    "Désélectionner tous les médias de cette date"
                )

            with media_col:
                for cat in sorted(by_category.keys()):
                    files = sorted(by_category[cat])
                    # Regroupe PANO/HYPER/WAYPOINTS par sous-dossier
                    items = self._partition_disk_files(files)
                    title = f"📁 {cat} ({len(items)})" if cat else f"📼 Racine ({len(items)})"
                    ui.label(title).classes("text-subtitle1 mt-2")
                    with ui.grid(columns=4).classes("w-full gap-2"):
                        for item in items:
                            if item["kind"] == "single":
                                fp = item["path"]
                                self._render_viewer_media_card(
                                    fp, precomputed_thumb=thumbs.get(str(fp))
                                )
                            else:
                                self._render_viewer_group_card(
                                    item["category"],
                                    item["group_subdir"],
                                    item["files"],
                                )

        background_tasks.create(_load_and_render(), name=f"viewer_media_{date_str}")

    # ── Regroupement disk-side (PANO/HYPER/WAYPOINTS/REALITY_SCAN) ────────
    GROUPED_CATEGORIES_DISK = ("PANORAMA", "HYPERLAPSE", "WAYPOINTS", "REALITY_SCAN")

    def _disk_group_key(self, f: Path) -> Optional[tuple[str, str]]:
        """Si le fichier est dans `.../<CATEGORY>/<subdir>/file`, retourne
        `(category, subdir)`. Sinon `None` (fichier individuel)."""
        parts = f.parts
        for cat in self.GROUPED_CATEGORIES_DISK:
            try:
                idx = parts.index(cat)
            except ValueError:
                continue
            # cat = parts[idx] ; il faut parts[idx+1] = group_subdir puis file plus bas
            if idx + 2 < len(parts):
                return (cat, parts[idx + 1])
        return None

    def _partition_disk_files(
        self, files: list[Path]
    ) -> list[dict[str, Any]]:
        """Convertit une liste plate de fichiers en items :
        - `{"kind": "single", "path": Path}` pour un média isolé
        - `{"kind": "group", "category": str, "group_subdir": str,
             "files": list[Path]}` pour PANO/HYPER/WAYPOINTS regroupés
        Préserve globalement l'ordre d'apparition.
        """
        items: list[dict[str, Any]] = []
        groups_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for f in files:
            key = self._disk_group_key(f)
            if key is None:
                items.append({"kind": "single", "path": f})
                continue
            grp = groups_by_key.get(key)
            if grp is None:
                grp = {
                    "kind": "group",
                    "category": key[0],
                    "group_subdir": key[1],
                    "files": [f],
                }
                groups_by_key[key] = grp
                items.append(grp)
            else:
                grp["files"].append(f)
        # Trie les fichiers de chaque groupe pour un rendu stable
        for grp in groups_by_key.values():
            grp["files"].sort()
        return items

    def _render_viewer_group_card(
        self,
        category: str,
        group_subdir: str,
        files: list[Path],
    ) -> None:
        """Carte de groupe côté viewer (PANO/HYPER/WAYPOINTS déjà organisés)."""
        if not files:
            return
        first = files[0]
        # Couleurs par catégorie
        cat_hex = {
            "PANORAMA": "#FB8C00",
            "HYPERLAPSE": "#8E24AA",
            "WAYPOINTS": "#00897B",
            "REALITY_SCAN": "#7C4DFF",
        }.get(category, "#FB8C00")
        cat_icon = {
            "PANORAMA": "panorama",
            "HYPERLAPSE": "timelapse",
            "WAYPOINTS": "route",
            "REALITY_SCAN": "view_in_ar",
        }.get(category, "photo_library")

        card = ui.card().classes("w-full hover:shadow-lg").style(
            f"border:2px solid {cat_hex};"
        )
        # Holder pour le redraw de la ligne de tags du groupe — assigné
        # après création (chip line rendue en bas de carte).
        group_tag_redraw_holder: dict[str, Callable[[], None]] = {}

        def _open_group_from_card() -> None:
            self._open_disk_group_dialog(
                category, group_subdir, files,
                on_close=group_tag_redraw_holder.get("draw"),
            )

        with card:
            thumb_zone = ui.element("div").classes("relative w-full cursor-pointer")
            thumb_zone.on("click", lambda: _open_group_from_card())
            thumb_zone.tooltip(
                f"Cliquer pour voir toutes les {len(files)} vignettes du groupe"
            )
            with thumb_zone:
                thumb = generate_thumbnail(str(first), size=200)
                if thumb:
                    ui.image(image_to_data_uri(thumb)).classes(
                        "w-full h-28 object-cover rounded"
                    )
                else:
                    with ui.element("div").classes(
                        "w-full h-28 flex items-center justify-center bg-grey-3 rounded"
                    ):
                        ui.icon(cat_icon).classes("text-4xl text-grey-6")
                # Badge nombre
                with ui.element("div").classes(
                    "absolute top-1 right-1 text-white text-caption px-2 py-1 rounded-lg"
                ).style(f"background:{cat_hex}; font-weight:700;"):
                    ui.label(f"×{len(files)}")
                # Étiquette catégorie
                with ui.element("div").classes(
                    "absolute bottom-1 left-1 bg-black bg-opacity-70 text-white text-caption px-2 py-1 rounded"
                ):
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.icon(cat_icon).classes("text-white text-sm")
                        ui.label(category)
                # Overlay checkbox multi-sélection (sélectionne TOUS les fichiers du groupe)
                paths_str = [str(f) for f in files]
                all_selected = all(
                    p in self._selected_viewer_files for p in paths_str
                )
                sel_overlay = ui.element("div").classes(
                    "sel-overlay" + (" sel-active" if all_selected else "")
                )
                sel_overlay.on("click.stop", lambda: None)
                with sel_overlay:
                    cb = ui.checkbox(
                        value=all_selected,
                        on_change=lambda e, ps=paths_str:
                            self._toggle_viewer_selection(ps, bool(e.value)),
                    ).classes("sel-checkbox").props("dense size=xs color=red-4 dark")
                    cb.tooltip(
                        f"Sélectionner les {len(files)} fichiers du groupe"
                    )
            ui.label(f"{category} · {group_subdir}").classes(
                "text-body2 truncate"
            ).tooltip(str(first.parent))
            try:
                total = sum(f.stat().st_size for f in files)
                ui.label(
                    f"{len(files)} fichiers · {human_size(total)}"
                ).classes("text-caption text-grey-7")
            except OSError:
                pass
            # Tags appliqués au groupe entier (intersection affichée)
            # — le redraw est mémorisé pour être appelé à la fermeture du dialog du groupe.
            group_tag_redraw_holder["draw"] = self._render_viewer_group_tags(files)

    def _open_disk_group_dialog(
        self,
        category: str,
        group_subdir: str,
        files: list[Path],
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        """Dialog plein écran affichant toutes les vignettes d'un groupe disk.

        `on_close` : callback optionnel appelé lorsque le dialog se ferme
        (utile pour rafraîchir la carte du groupe si les tags des membres
        ont été modifiés).
        """
        try:
            if not files:
                return
            first = files[0]
            cat_hex = {
                "PANORAMA": "#FB8C00",
                "HYPERLAPSE": "#8E24AA",
                "WAYPOINTS": "#00897B",
                "REALITY_SCAN": "#7C4DFF",
            }.get(category, "#FB8C00")
            cat_icon = {
                "PANORAMA": "panorama",
                "HYPERLAPSE": "timelapse",
                "WAYPOINTS": "route",
                "REALITY_SCAN": "view_in_ar",
            }.get(category, "photo_library")

            dlg = ui.dialog().props("maximized")
            with dlg:
                with ui.card().classes("w-full h-full no-shadow column no-wrap"):
                    with ui.row().classes(
                        "w-full items-center gap-2 q-pa-sm text-white"
                    ).style(f"background:{cat_hex};"):
                        ui.icon(cat_icon)
                        ui.label(f"{category} — {group_subdir}").classes(
                            "text-h6 flex-grow"
                        )
                        ui.badge(f"{len(files)} médias", color="white text-black")
                        try:
                            total = sum(f.stat().st_size for f in files)
                            ui.badge(
                                human_size(total),
                                color="white text-black",
                            )
                        except OSError:
                            pass
                        ui.button(icon="close", on_click=dlg.close).props(
                            "flat round color=white"
                        )

                    with ui.scroll_area().classes("w-full col"):
                        with ui.grid(columns=6).classes("w-full gap-2 q-pa-sm"):
                            for f in files:
                                self._render_viewer_media_card(f)
            if on_close is not None:
                def _fire_group_on_close(e) -> None:
                    if not e.value:
                        try:
                            on_close()
                        except Exception:
                            pass
                dlg.on_value_change(_fire_group_on_close)
            dlg.open()
        except Exception as e:
            import traceback
            self.log(f"❌ Ouverture groupe disque: {e}")
            self.log(traceback.format_exc())
            ui.notify(f"Erreur : {e}", type="negative")

    def _render_viewer_media_card(self, file_path: Path, precomputed_thumb: Optional[str] = None) -> None:
        is_video = file_path.suffix.lower() in VIDEO_EXTS
        fp_str = str(file_path)
        card = ui.card().classes("w-full hover:shadow-lg")
        # Références aux redraws des chips (renseignées plus bas) pour
        # rafraîchir la carte quand le dialog d'aperçu se ferme.
        redraws: dict[str, Callable[[], None]] = {}

        def _refresh_card() -> None:
            for fn in redraws.values():
                try:
                    fn()
                except Exception:
                    pass

        with card:
            thumb_zone = ui.element("div").classes("relative w-full cursor-pointer")
            thumb_zone.on(
                "click",
                lambda p=file_path: self._open_preview_from_disk(
                    p,
                    on_deleted=self._on_viewer_file_deleted,
                    on_close=_refresh_card,
                ),
            )
            with thumb_zone:
                thumb = precomputed_thumb if precomputed_thumb else generate_thumbnail(str(file_path), size=200)
                if thumb:
                    ui.image(image_to_data_uri(thumb)).classes(
                        "w-full h-28 object-cover rounded"
                    )
                else:
                    with ui.element("div").classes(
                        "w-full h-28 flex items-center justify-center bg-grey-3 rounded"
                    ):
                        ui.icon("movie" if is_video else "image").classes(
                            "text-4xl text-grey-6"
                        )
                with ui.element("div").classes(
                    "absolute top-1 right-1 bg-black bg-opacity-60 text-white rounded-full p-1"
                ):
                    ui.icon("play_circle" if is_video else "zoom_in").classes("text-white")
                # Overlay checkbox multi-sélection
                is_sel = fp_str in self._selected_viewer_files
                sel_overlay = ui.element("div").classes(
                    "sel-overlay" + (" sel-active" if is_sel else "")
                )
                sel_overlay.on("click.stop", lambda: None)
                with sel_overlay:
                    cb = ui.checkbox(
                        value=is_sel,
                        on_change=lambda e, fp=fp_str: self._toggle_viewer_selection(
                            [fp], bool(e.value)
                        ),
                    ).classes("sel-checkbox").props("dense size=xs color=red-4 dark")
                    cb.tooltip("Sélectionner pour effacer en lot")
                # Overlay bouton corbeille (bas-droit)
                trash_overlay = ui.element("div").classes(
                    "absolute bottom-1 right-1"
                ).style("opacity:0.7;")
                trash_overlay.on("click.stop", lambda: None)
                with trash_overlay:
                    ui.button(
                        icon="delete_forever",
                        on_click=lambda p=file_path: self._delete_disk_file_to_trash(
                            str(p), confirm=True,
                            on_deleted=self._on_viewer_file_deleted,
                        ),
                    ).props("dense round flat color=red-4 size=xs").tooltip(
                        "Envoyer à la corbeille"
                    )
            ui.label(file_path.name).classes("text-caption truncate").tooltip(str(file_path))
            try:
                size = file_path.stat().st_size
                ui.label(human_size(size)).classes("text-caption text-grey-7")
            except OSError:
                pass
            # Nom personnalisé (lit et persiste directement le sidecar)
            redraws["name"] = self._render_viewer_thumb_custom_name(file_path)
            # Ligne tags compacte (lit et persiste directement le sidecar)
            redraws["tags"] = self._render_viewer_thumb_tags(file_path)

    def _render_viewer_thumb_custom_name(self, file_path: Path) -> Callable[[], None]:
        """Chip nom personnalisé auto-rafraîchissante sous la vignette du viewer.

        Retourne le callable `_draw` pour permettre un rafraîchissement externe.
        """
        outer = ui.row().classes("w-full items-center gap-1 q-mt-xs").style(
            "flex-wrap:wrap; min-height:18px;"
        )

        def _draw() -> None:
            outer.clear()
            current = self._read_sidecar_custom_name(file_path)
            with outer:
                if current:
                    with ui.element("div").style(
                        "background:rgba(76,175,80,0.15); color:#2E7D32; "
                        "padding:1px 6px; border-radius:6px; font-size:10px; "
                        "font-weight:600; display:inline-flex; align-items:center; "
                        "gap:3px; border:1px solid rgba(76,175,80,0.35); "
                        "max-width:100%;"
                    ):
                        ui.icon("edit_note").style("font-size:11px;")
                        ui.label(current).classes("truncate")

                def _save(new_name: str) -> None:
                    if self._update_sidecar_custom_name(str(file_path), new_name):
                        action = "défini" if new_name else "effacé"
                        ui.notify(
                            f"✏️ Nom {action}"
                            + (f" : « {new_name} »" if new_name else ""),
                            type="positive",
                        )
                        _draw()
                    else:
                        ui.notify(
                            "⚠️ Sidecar introuvable — impossible d'enregistrer",
                            type="warning",
                        )

                ui.button(
                    icon="edit",
                    on_click=lambda: self._open_rename_dialog(
                        current=current,
                        title="Renommer",
                        subtitle=file_path.name,
                        on_save=_save,
                    ),
                ).props("flat dense round size=xs color=primary").tooltip(
                    "Modifier le nom personnalisé"
                )

        _draw()
        return _draw

    def _render_viewer_thumb_tags(self, file_path: Path) -> Callable[[], None]:
        """Rangée de tags compacte auto-rafraîchissante sous la vignette du viewer.

        Retourne le callable `_draw` pour permettre un rafraîchissement externe
        (ex : à la fermeture d'un dialog d'aperçu qui a modifié les tags).
        """
        outer = ui.row().classes("w-full items-center gap-1 q-mt-xs").style(
            "flex-wrap:wrap; min-height:20px;"
        )

        def _draw() -> None:
            outer.clear()
            current = self._read_sidecar_tags(file_path)
            hidden = set(self._hidden_tag_names())
            with outer:
                # Chips existantes
                for tag_name in sorted(current):
                    hex_c = self._tag_hex(tag_name)
                    icon = self._tag_icon(tag_name)
                    chip = ui.element("div").style(
                        f"background:{hex_c}22; color:{hex_c}; "
                        f"padding:1px 5px; border-radius:8px; "
                        f"font-size:10px; font-weight:600; "
                        f"display:inline-flex; align-items:center; gap:3px; "
                        f"border:1px solid {hex_c};"
                    )
                    with chip:
                        ui.label(icon).style("font-size:10px;")
                        ui.label(tag_name)

                        def _remove(n=tag_name) -> None:
                            new_tags = [t for t in self._read_sidecar_tags(file_path) if t != n]
                            if self._update_sidecar_tags(file_path, new_tags):
                                ui.notify(f"— {n} retiré", type="info")
                                self._calendar_index_cache = None
                                _draw()

                        ui.button(
                            icon="close",
                            on_click=_remove,
                        ).props("flat dense round size=xs").style(
                            f"color:{hex_c}; margin:-4px -6px -4px 0;"
                        ).tooltip(f"Retirer « {tag_name} »")

                # Bouton + Ajouter (tags disponibles)
                available = [n for n in self._tag_names() if n not in current]
                if available:
                    add_btn = ui.button(icon="add").props(
                        "flat dense round size=xs color=primary"
                    ).tooltip("Ajouter un tag")
                    with add_btn:
                        with ui.menu() as menu:
                            for name in sorted(available):
                                hex_c = self._tag_hex(name)
                                icon = self._tag_icon(name)

                                def _add(n=name, m=menu) -> None:
                                    new_tags = sorted(
                                        set(self._read_sidecar_tags(file_path)) | {n}
                                    )
                                    if self._update_sidecar_tags(file_path, new_tags):
                                        ui.notify(f"+ {n} appliqué", type="positive")
                                        # Si le tag est caché, la vignette peut disparaître
                                        self._calendar_index_cache = None
                                        # Tag caché + masquage actif → rerender complet
                                        # pour retirer la vignette de la grille
                                        if (
                                            n in hidden
                                            and not CONFIG.get("viewer_show_hidden_tags", False)
                                        ):
                                            try:
                                                self._render_viewer()
                                            except Exception:
                                                _draw()
                                        else:
                                            _draw()
                                    m.close()

                                with ui.menu_item(on_click=_add):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.label(icon).style("font-size:14px;")
                                        ui.label(name)
                                        if name in hidden:
                                            ui.icon("visibility_off").classes(
                                                "text-grey-6 text-sm"
                                            ).tooltip("Tag caché")
                                        ui.element("div").style(
                                            f"width:10px; height:10px; border-radius:50%; "
                                            f"background:{hex_c};"
                                        )

        _draw()
        return _draw

    def _render_viewer_group_tags(self, files: list[Path]) -> Callable[[], None]:
        """Rangée de tags compacte pour un GROUPE disk (PANO/HYPER/WAYPOINTS/REALITY_SCAN).

        Les tags s'appliquent à *tous* les fichiers du groupe. Les chips affichées
        représentent l'intersection (tags communs à tous les membres). Le bouton
        « + » ajoute le tag choisi à tous les fichiers du groupe ; le « × » sur
        une chip le retire de tous.

        Retourne le callable `_draw` pour permettre un rafraîchissement externe.
        """
        outer = ui.row().classes("w-full items-center gap-1 q-mt-xs").style(
            "flex-wrap:wrap; min-height:20px;"
        )

        def _common_tags() -> set[str]:
            per_file = [set(self._read_sidecar_tags(f)) for f in files]
            if not per_file:
                return set()
            common = set(per_file[0])
            for s in per_file[1:]:
                common &= s
            return common

        def _apply_add(tag_name: str) -> int:
            n_ok = 0
            for f in files:
                cur = set(self._read_sidecar_tags(f))
                if tag_name in cur:
                    continue
                new_tags = sorted(cur | {tag_name})
                if self._update_sidecar_tags(f, new_tags):
                    n_ok += 1
            return n_ok

        def _apply_remove(tag_name: str) -> int:
            n_ok = 0
            for f in files:
                cur = set(self._read_sidecar_tags(f))
                if tag_name not in cur:
                    continue
                new_tags = sorted(cur - {tag_name})
                if self._update_sidecar_tags(f, new_tags):
                    n_ok += 1
            return n_ok

        def _draw() -> None:
            outer.clear()
            current = _common_tags()
            hidden = set(self._hidden_tag_names())
            with outer:
                for tag_name in sorted(current):
                    hex_c = self._tag_hex(tag_name)
                    icon = self._tag_icon(tag_name)
                    chip = ui.element("div").style(
                        f"background:{hex_c}22; color:{hex_c}; "
                        f"padding:1px 5px; border-radius:8px; "
                        f"font-size:10px; font-weight:600; "
                        f"display:inline-flex; align-items:center; gap:3px; "
                        f"border:1px solid {hex_c};"
                    )
                    with chip:
                        ui.label(icon).style("font-size:10px;")
                        ui.label(tag_name)

                        def _remove(n=tag_name) -> None:
                            n_ok = _apply_remove(n)
                            if n_ok:
                                ui.notify(
                                    f"— {n} retiré de {n_ok} fichier(s)",
                                    type="info",
                                )
                                self._calendar_index_cache = None
                                _draw()

                        ui.button(
                            icon="close",
                            on_click=_remove,
                        ).props("flat dense round size=xs").style(
                            f"color:{hex_c}; margin:-4px -6px -4px 0;"
                        ).tooltip(f"Retirer « {tag_name} » des {len(files)} fichiers")

                available = [n for n in self._tag_names() if n not in current]
                if available:
                    add_btn = ui.button(icon="add").props(
                        "flat dense round size=xs color=primary"
                    ).tooltip(f"Ajouter un tag aux {len(files)} fichiers du groupe")
                    with add_btn:
                        with ui.menu() as menu:
                            for name in sorted(available):
                                hex_c = self._tag_hex(name)
                                icon = self._tag_icon(name)

                                def _add(n=name, m=menu) -> None:
                                    n_ok = _apply_add(n)
                                    if n_ok:
                                        ui.notify(
                                            f"+ {n} appliqué à {n_ok} fichier(s)",
                                            type="positive",
                                        )
                                        self._calendar_index_cache = None
                                        if (
                                            n in hidden
                                            and not CONFIG.get(
                                                "viewer_show_hidden_tags", False
                                            )
                                        ):
                                            try:
                                                self._render_viewer()
                                            except Exception:
                                                _draw()
                                        else:
                                            _draw()
                                    m.close()

                                with ui.menu_item(on_click=_add):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.label(icon).style("font-size:14px;")
                                        ui.label(name)
                                        if name in hidden:
                                            ui.icon("visibility_off").classes(
                                                "text-grey-6 text-sm"
                                            ).tooltip("Tag caché")
                                        ui.element("div").style(
                                            f"width:10px; height:10px; border-radius:50%; "
                                            f"background:{hex_c};"
                                        )

        _draw()
        return _draw

    # ═══════════════════════════════════════════════════════════════════════
    # CALENDRIER (onglet interne du visualiseur)
    # ═══════════════════════════════════════════════════════════════════════
    def _build_calendar_tag_index(
        self,
    ) -> dict[str, dict[str, dict[str, int]]]:
        """Retourne `{YYYY-MM-DD: {drone_id: {tag_name: count}}}` en lisant les sidecars.

        Un peu plus coûteux que `_build_calendar_index` car il ouvre chaque `.dji.json`
        présent. Cache local `self._calendar_tag_index_cache`.

        Les fichiers avec un tag marqué « à masquer » (ex : nsfw) sont exclus,
        sauf si l'utilisateur a activé `viewer_show_hidden_tags`.
        """
        if self._calendar_tag_index_cache is not None:
            return self._calendar_tag_index_cache

        index: dict[str, dict[str, dict[str, int]]] = {}
        dest = Path(self.destination_dir)
        if not dest.is_dir():
            self._calendar_tag_index_cache = index
            return index

        hidden_tags = set(self._hidden_tag_names())
        show_hidden = bool(CONFIG.get("viewer_show_hidden_tags", False))
        filter_hidden = bool(hidden_tags and not show_hidden)

        date_pat = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for drone in CONFIG.get("drone_mapping", []):
            drone_id = drone.get("id", "?")
            folder = drone.get("folder", "")
            if not folder:
                continue
            drone_dir = dest / folder
            if not drone_dir.is_dir():
                continue
            for date_dir in drone_dir.iterdir():
                if not date_dir.is_dir():
                    continue
                date_str = date_dir.name
                if not date_pat.match(date_str):
                    continue
                for f in date_dir.rglob("*"):
                    if not f.is_file():
                        continue
                    if f.suffix.lower() not in ALL_MEDIA_EXTS:
                        continue
                    try:
                        file_tags = set(self._read_sidecar_tags(f))
                    except Exception:
                        continue
                    if filter_hidden and (file_tags & hidden_tags):
                        continue
                    # Tags visibles (hors tags cachés) pour compter
                    visible = file_tags - hidden_tags if filter_hidden else file_tags
                    if not visible:
                        continue
                    drones = index.setdefault(date_str, {})
                    tag_counts = drones.setdefault(drone_id, {})
                    for t in visible:
                        tag_counts[t] = tag_counts.get(t, 0) + 1

        self._calendar_tag_index_cache = index
        return index

    def _build_calendar_index(self) -> dict[str, dict[str, dict[str, int]]]:
        """Retourne `{YYYY-MM-DD: {drone_id: {category: count}}}` en scannant la destination.

        Rapide : n'ouvre aucun fichier, juste `iterdir()` / `rglob()` pour compter.
        Résultat mis en cache dans `self._calendar_index_cache`.
        """
        if self._calendar_index_cache is not None:
            return self._calendar_index_cache

        index: dict[str, dict[str, dict[str, int]]] = {}
        dest = Path(self.destination_dir)
        if not dest.is_dir():
            self._calendar_index_cache = index
            return index

        hidden_tags = set(self._hidden_tag_names())
        show_hidden = bool(CONFIG.get("viewer_show_hidden_tags", False))
        filter_hidden = bool(hidden_tags and not show_hidden)

        def _file_visible(f: Path) -> bool:
            if not filter_hidden:
                return True
            try:
                file_tags = set(self._read_sidecar_tags(f))
            except Exception:
                return True
            return not (file_tags & hidden_tags)

        date_pat = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for drone in CONFIG.get("drone_mapping", []):
            drone_id = drone.get("id", "?")
            folder = drone.get("folder", "")
            if not folder:
                continue
            drone_dir = dest / folder
            if not drone_dir.is_dir():
                continue
            for date_dir in drone_dir.iterdir():
                if not date_dir.is_dir():
                    continue
                date_str = date_dir.name
                if not date_pat.match(date_str):
                    continue
                # Sous-dossiers = catégories ; fichiers à la racine = catégorie ""
                for child in date_dir.iterdir():
                    if child.is_dir():
                        cat = child.name
                        n = sum(
                            1 for f in child.rglob("*")
                            if f.is_file()
                            and f.suffix.lower() in ALL_MEDIA_EXTS
                            and _file_visible(f)
                        )
                        if n:
                            index.setdefault(date_str, {}).setdefault(drone_id, {})[cat] = (
                                index.setdefault(date_str, {}).setdefault(drone_id, {}).get(cat, 0) + n
                            )
                    elif child.is_file() and child.suffix.lower() in ALL_MEDIA_EXTS:
                        if not _file_visible(child):
                            continue
                        # Fichier à la racine (Goggles = pas de sous-dossier catégorie)
                        cat = ""
                        index.setdefault(date_str, {}).setdefault(drone_id, {})[cat] = (
                            index.setdefault(date_str, {}).setdefault(drone_id, {}).get(cat, 0) + 1
                        )

        self._calendar_index_cache = index
        return index

    _MONTHS_FR = [
        "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
        "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
    ]

    def _render_calendar(self) -> None:
        if self._calendar_container is None:
            return
        self._calendar_container.clear()

        with self._calendar_container:
            y = self._calendar_state["year"]
            m = self._calendar_state["month"]
            month_label = f"{self._MONTHS_FR[m]} {y}"

            # Barre navigation mois — carte élégante
            with ui.element("div").style(
                "background:rgba(255,255,255,0.03); border-radius:12px; "
                "padding:12px 16px; border:1px solid rgba(255,255,255,0.08);"
            ).classes("w-full"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.button(icon="chevron_left", on_click=self._calendar_prev_month).props(
                        "flat round color=blue-4"
                    ).tooltip("Mois précédent")
                    ui.button("Aujourd'hui", on_click=self._calendar_today).props(
                        "outline dense color=blue-4"
                    )
                    ui.button(icon="chevron_right", on_click=self._calendar_next_month).props(
                        "flat round color=blue-4"
                    ).tooltip("Mois suivant")
                    ui.space()
                    ui.label(month_label).style(
                        "font-size:28px; font-weight:700; letter-spacing:-0.5px; "
                        "background:linear-gradient(90deg,#64B5F6,#7C4DFF); "
                        "-webkit-background-clip:text; -webkit-text-fill-color:transparent;"
                    )
                    ui.space()
                    ui.button(icon="refresh", on_click=self._calendar_refresh).props(
                        "flat round color=blue-4"
                    ).tooltip("Rescanner la destination")

            # ── Filtres drones + catégories ──
            drone_opts = {"TOUS": "Tous les drones"}
            for d in CONFIG.get("drone_mapping", []):
                drone_opts[d.get("id", "?")] = d.get("label", d.get("id", "?"))
            cat_opts = {"TOUTES": "Toutes catégories", **{c: c for c in CATEGORIES}}
            hidden_names = self._hidden_tag_names()
            show_hidden = bool(CONFIG.get("viewer_show_hidden_tags", False))
            all_tag_names = [
                n for n in self._tag_names()
                if show_hidden or n not in hidden_names
            ]
            tag_opts = {n: n for n in sorted(all_tag_names)}

            # Normalise si drone/cat n'existe plus dans les options
            if self._calendar_state["drone_filter"] not in drone_opts:
                self._calendar_state["drone_filter"] = "TOUS"
            if self._calendar_state["cat_filter"] not in cat_opts:
                self._calendar_state["cat_filter"] = "TOUTES"
            if self._calendar_state.get("group_mode") not in ("cat", "tag"):
                self._calendar_state["group_mode"] = "cat"
            # Nettoie les tags disparus / masqués
            self._calendar_state["tag_filter"] = [
                t for t in (self._calendar_state.get("tag_filter") or [])
                if t in tag_opts
            ]
            group_mode = self._calendar_state["group_mode"]

            with ui.element("div").style(
                "background:rgba(255,255,255,0.02); border-radius:10px; "
                "padding:8px 12px; border:1px solid rgba(255,255,255,0.06);"
            ).classes("w-full"):
                with ui.row().classes("w-full items-center gap-3").style(
                    "flex-wrap:wrap;"
                ):
                    ui.icon("filter_list").classes("text-blue-4")
                    ui.label("Filtres :").classes("text-caption text-grey-5")
                    ui.select(
                        options=drone_opts,
                        value=self._calendar_state["drone_filter"],
                        on_change=self._on_calendar_drone_filter,
                    ).props("dense options-dense outlined color=blue-4").classes("min-w-40")
                    # Toggle de regroupement Cat / Tag
                    ui.label("Vue :").classes("text-caption text-grey-5 q-ml-md")
                    ui.toggle(
                        options={"cat": "Par catégorie", "tag": "Par tag"},
                        value=group_mode,
                        on_change=self._on_calendar_group_mode,
                    ).props("dense color=blue-4")
                    if group_mode == "cat":
                        ui.select(
                            options=cat_opts,
                            value=self._calendar_state["cat_filter"],
                            on_change=self._on_calendar_cat_filter,
                        ).props(
                            "dense options-dense outlined color=blue-4"
                        ).classes("min-w-40")
                    # Multi-sélecteur de tags (agit comme filtre en mode "cat"
                    # et comme filtre + choix de chips en mode "tag")
                    if tag_opts:
                        ui.select(
                            options=tag_opts,
                            value=self._calendar_state["tag_filter"],
                            multiple=True,
                            clearable=True,
                            label="Tags",
                            on_change=self._on_calendar_tag_filter,
                        ).props(
                            "dense options-dense outlined color=blue-4 use-chips"
                        ).classes("min-w-56")
                    ui.space()
                    active_filters = []
                    if self._calendar_state["drone_filter"] != "TOUS":
                        active_filters.append(f"drone={self._calendar_state['drone_filter']}")
                    if group_mode == "cat" and self._calendar_state["cat_filter"] != "TOUTES":
                        active_filters.append(f"cat={self._calendar_state['cat_filter']}")
                    if self._calendar_state["tag_filter"]:
                        active_filters.append(
                            f"tags={','.join(self._calendar_state['tag_filter'])}"
                        )
                    if active_filters:
                        ui.button(
                            "Réinitialiser",
                            icon="clear",
                            on_click=self._on_calendar_filters_reset,
                        ).props("flat dense color=blue-4")

            if not os.path.isdir(self.destination_dir):
                ui.label(f"Destination introuvable : {self.destination_dir}").classes("text-caption text-orange")
                return

            # Chargement asynchrone
            loading_col = ui.column().classes("w-full items-center q-pa-lg")
            with loading_col:
                ui.spinner(size="lg", color="primary")
                ui.label("Analyse de la destination…").classes("text-body2 q-mt-sm")

            body = ui.column().classes("w-full")

            async def _load() -> None:
                try:
                    if group_mode == "tag":
                        # Cache dédié tag_index : plus coûteux mais mis en cache
                        idx = await run.io_bound(self._build_calendar_tag_index)
                    else:
                        idx = await run.io_bound(self._build_calendar_index)
                        # Si un filtre de tags est actif en mode catégorie, on doit
                        # aussi charger l'index tag pour restreindre les compteurs.
                        if self._calendar_state.get("tag_filter"):
                            tag_idx = await run.io_bound(
                                self._build_calendar_tag_index
                            )
                            idx = self._intersect_cat_with_tags(idx, tag_idx)
                except Exception as e:
                    self.log(f"❌ Calendar index build failed: {e}")
                    loading_col.clear()
                    with loading_col:
                        ui.label(f"Erreur : {e}").classes("text-caption text-negative")
                    return

                loading_col.delete()
                # Applique les filtres drone/catégorie/tag
                filtered = self._apply_calendar_filters(idx)
                with body:
                    self._render_calendar_month_grid(filtered, y, m, group_mode)

            background_tasks.create(_load(), name=f"calendar_load_{y}_{m}")

    def _intersect_cat_with_tags(
        self,
        cat_index: dict[str, dict[str, dict[str, int]]],
        tag_index: dict[str, dict[str, dict[str, int]]],
    ) -> dict[str, dict[str, dict[str, int]]]:
        """Ne conserve dans `cat_index` que les jours/drones qui ont au moins
        un des tags sélectionnés dans `tag_filter`. Le nombre affiché reste
        celui de la catégorie (approximation acceptable — l'utilisateur veut
        surtout voir les jours pertinents).
        """
        wanted = set(self._calendar_state.get("tag_filter") or [])
        if not wanted:
            return cat_index
        out: dict[str, dict[str, dict[str, int]]] = {}
        for date_str, drones in cat_index.items():
            tag_drones = tag_index.get(date_str, {})
            new_drones: dict[str, dict[str, int]] = {}
            for drone_id, cats in drones.items():
                dtags = set(tag_drones.get(drone_id, {}).keys())
                if wanted & dtags:
                    new_drones[drone_id] = cats
            if new_drones:
                out[date_str] = new_drones
        return out

    def _apply_calendar_filters(
        self, index: dict[str, dict[str, dict[str, int]]]
    ) -> dict[str, dict[str, dict[str, int]]]:
        """Retourne une copie de l'index filtrée par drone / catégorie / tags.

        En mode `group_mode == "cat"`, les clés internes sont des catégories
        et `cat_filter` s'applique. En mode `"tag"`, les clés sont des tags
        et `tag_filter` (multi) restreint les chips affichées.
        """
        group_mode = self._calendar_state.get("group_mode", "cat")
        drone_f = self._calendar_state.get("drone_filter", "TOUS")
        cat_f = self._calendar_state.get("cat_filter", "TOUTES")
        tag_f = set(self._calendar_state.get("tag_filter") or [])
        out: dict[str, dict[str, dict[str, int]]] = {}
        for date_str, drones in index.items():
            new_drones: dict[str, dict[str, int]] = {}
            for drone_id, entries in drones.items():
                if drone_f != "TOUS" and drone_id != drone_f:
                    continue
                if group_mode == "tag":
                    new_entries = {
                        t: n for t, n in entries.items()
                        if (not tag_f) or t in tag_f
                    }
                else:
                    new_entries = {
                        c: n for c, n in entries.items()
                        if cat_f == "TOUTES" or c == cat_f
                    }
                if new_entries:
                    new_drones[drone_id] = new_entries
            if new_drones:
                out[date_str] = new_drones
        return out

    def _on_calendar_drone_filter(self, e) -> None:
        self._calendar_state["drone_filter"] = e.value
        self._render_calendar()

    def _on_calendar_cat_filter(self, e) -> None:
        self._calendar_state["cat_filter"] = e.value
        self._render_calendar()

    def _on_calendar_group_mode(self, e) -> None:
        self._calendar_state["group_mode"] = e.value or "cat"
        self._render_calendar()

    def _on_calendar_tag_filter(self, e) -> None:
        self._calendar_state["tag_filter"] = list(e.value or [])
        self._render_calendar()

    def _on_calendar_filters_reset(self) -> None:
        self._calendar_state["drone_filter"] = "TOUS"
        self._calendar_state["cat_filter"] = "TOUTES"
        self._calendar_state["tag_filter"] = []
        self._render_calendar()

    def _calendar_prev_month(self) -> None:
        y = self._calendar_state["year"]
        m = self._calendar_state["month"]
        m -= 1
        if m < 1:
            m = 12
            y -= 1
        self._calendar_state["year"] = y
        self._calendar_state["month"] = m
        self._render_calendar()

    def _calendar_next_month(self) -> None:
        y = self._calendar_state["year"]
        m = self._calendar_state["month"]
        m += 1
        if m > 12:
            m = 1
            y += 1
        self._calendar_state["year"] = y
        self._calendar_state["month"] = m
        self._render_calendar()

    def _calendar_today(self) -> None:
        now = datetime.now()
        self._calendar_state["year"] = now.year
        self._calendar_state["month"] = now.month
        self._render_calendar()

    def _calendar_refresh(self) -> None:
        self._calendar_index_cache = None
        self._calendar_tag_index_cache = None
        self._render_calendar()

    _CAT_ABBREV = {
        "VIDEO": "vid", "PHOTO": "pho", "PANORAMA": "pano",
        "HYPERLAPSE": "hyp", "GOGGLES": "gog", "REALITY_SCAN": "rs",
        "WAYPOINTS": "wpt", "": "misc",
    }
    # Palette hex haute-visibilité (compatible dark mode)
    _CAT_HEX = {
        "VIDEO":        "#2196F3",  # bleu
        "PHOTO":        "#4CAF50",  # vert
        "PANORAMA":     "#FF9800",  # orange
        "HYPERLAPSE":   "#9C27B0",  # violet
        "GOGGLES":      "#00BCD4",  # cyan
        "REALITY_SCAN": "#7C4DFF",  # indigo
        "WAYPOINTS":    "#8D6E63",  # brun
        "":             "#78909C",  # gris-bleu
    }
    # Ancien mapping Quasar conservé pour la légende
    _CAT_COLOR = {
        "VIDEO": "primary", "PHOTO": "green-6", "PANORAMA": "orange-8",
        "HYPERLAPSE": "purple-6", "GOGGLES": "cyan-6", "REALITY_SCAN": "deep-purple-6",
        "WAYPOINTS": "brown-6", "": "grey-7",
    }
    # Palette drone (assignation stable par hash court)
    _DRONE_HEX_PALETTE = [
        "#FF6B6B", "#4ECDC4", "#FFD93D", "#95E1D3",
        "#F38181", "#AA96DA", "#FCBAD3", "#A8D8EA",
    ]

    def _drone_hex(self, drone_id: str) -> str:
        """Retourne une couleur stable pour un drone donné."""
        idx = sum(ord(c) for c in drone_id) % len(self._DRONE_HEX_PALETTE)
        return self._DRONE_HEX_PALETTE[idx]

    def _drone_short_id(self, drone_id: str) -> str:
        """Retourne un ID court affichable dans une cellule calendrier."""
        # ex "MINI4PRO-PEDRO" → "PEDRO", "MINI2-MEO" → "MEO"
        parts = drone_id.split("-")
        return parts[-1] if len(parts) > 1 else drone_id

    def _render_calendar_month_grid(
        self,
        index: dict[str, dict[str, dict[str, int]]],
        year: int,
        month: int,
        group_mode: str = "cat",
    ) -> None:
        """Dessine une grille 7×N pour le mois donné avec les compteurs par cellule.

        `group_mode` : "cat" (chips = catégories) ou "tag" (chips = tags).
        """
        cal = calendar.Calendar(firstweekday=0)  # lundi
        weeks = cal.monthdatescalendar(year, month)

        # En-tête jours de la semaine — style dark-friendly
        with ui.row().classes("w-full gap-1 q-mb-xs"):
            for wd in ["LUN", "MAR", "MER", "JEU", "VEN", "SAM", "DIM"]:
                is_weekend = wd in ("SAM", "DIM")
                color = "#FFD93D" if is_weekend else "#B0BEC5"
                ui.label(wd).classes(
                    "text-caption font-bold text-center"
                ).style(
                    f"flex:1 1 0; min-width:0; letter-spacing:2px; "
                    f"color:{color}; padding:6px 0;"
                )

        today = datetime.now().date()
        for week in weeks:
            with ui.row().classes("w-full gap-1 q-mb-xs items-stretch"):
                for day in week:
                    is_current_month = day.month == month
                    is_today = day == today
                    date_str = day.strftime("%Y-%m-%d")
                    day_data = index.get(date_str, {})
                    self._render_calendar_day_cell(
                        day, date_str, day_data,
                        is_current_month, is_today, group_mode,
                    )

        # Légende — catégories ou tags selon le mode
        with ui.row().classes("w-full q-mt-lg items-center gap-2 justify-center").style(
            "flex-wrap:wrap;"
        ):
            if group_mode == "tag":
                ui.label("Tags :").classes("text-caption text-grey-5")
                # Récupère les tags réellement présents dans l'index filtré
                present_tags: set[str] = set()
                for drones in index.values():
                    for entries in drones.values():
                        present_tags.update(entries.keys())
                for tag_name in sorted(present_tags):
                    hex_c = self._tag_hex(tag_name)
                    icon = self._tag_icon(tag_name)
                    with ui.element("div").style(
                        f"background:{hex_c}; color:white; padding:4px 10px; "
                        f"border-radius:12px; font-size:11px; font-weight:600; "
                        f"letter-spacing:0.5px; display:inline-flex; "
                        f"align-items:center; gap:4px;"
                    ):
                        ui.label(icon).style("font-size:12px;")
                        ui.label(tag_name)
            else:
                ui.label("Catégories :").classes("text-caption text-grey-5")
                for cat, abbr in self._CAT_ABBREV.items():
                    if cat == "":
                        continue
                    hex_c = self._CAT_HEX.get(cat, "#78909C")
                    with ui.element("div").style(
                        f"background:{hex_c}; color:white; padding:4px 10px; "
                        f"border-radius:12px; font-size:11px; font-weight:600; "
                        f"letter-spacing:0.5px;"
                    ):
                        ui.label(f"{abbr.upper()} · {cat}")

    def _tag_abbrev(self, tag_name: str) -> str:
        """Abréviation compacte d'un tag pour l'affichage dans une cellule de calendrier."""
        s = "".join(ch for ch in tag_name if ch.isalnum())
        if not s:
            return "tag"
        return s[:5].lower()

    def _render_calendar_day_cell(
        self,
        day,
        date_str: str,
        day_data: dict[str, dict[str, int]],
        is_current_month: bool,
        is_today: bool,
        group_mode: str = "cat",
    ) -> None:
        """Cellule d'un jour dans la grille calendrier — version stylée."""
        has_data = bool(day_data)

        # Palette de fond selon état
        if not is_current_month:
            bg = "rgba(255,255,255,0.02)"
            border = "1px solid rgba(255,255,255,0.05)"
            day_color = "#5a5a5a"
        elif is_today:
            bg = "linear-gradient(135deg, rgba(33,150,243,0.25), rgba(33,150,243,0.10))"
            border = "2px solid #2196F3"
            day_color = "#64B5F6"
        elif has_data:
            bg = "rgba(255,255,255,0.05)"
            border = "1px solid rgba(255,255,255,0.10)"
            day_color = "#E0E0E0"
        else:
            bg = "rgba(255,255,255,0.02)"
            border = "1px solid rgba(255,255,255,0.06)"
            day_color = "#9E9E9E"

        cell_style = (
            f"flex:1 1 0; min-width:0; min-height:110px; "
            f"background:{bg}; border:{border}; border-radius:8px; "
            f"padding:6px; overflow:hidden;"
        )
        if has_data:
            cell_style += " cursor:pointer;"

        cell = ui.column().classes(
            "gap-1 no-wrap" + (" cal-day-clickable" if has_data else "")
        ).style(cell_style)
        if has_data:
            cell.on("click", lambda ds=date_str, d=day_data: self._open_calendar_day_dialog(ds, d))
            cell.tooltip(f"{date_str} — voir les médias")

        with cell:
            # Ligne haut : numéro du jour + total (badge)
            with ui.row().classes("items-center no-wrap w-full gap-1").style("min-height:22px;"):
                ui.label(str(day.day)).classes("cal-mono").style(
                    f"font-size:20px; font-weight:700; color:{day_color}; "
                    f"line-height:1; letter-spacing:-0.5px;"
                )
                ui.space()
                if has_data:
                    total = sum(cnt for entries in day_data.values() for cnt in entries.values())
                    with ui.element("div").classes("cal-mono").style(
                        "background:rgba(33,150,243,0.85); color:white; "
                        "padding:1px 8px; border-radius:10px; font-size:11px; "
                        "font-weight:700;"
                    ):
                        ui.label(str(total))

            # Corps : lignes drone + chips (catégorie ou tag selon le mode)
            if has_data:
                for drone_id, entries in day_data.items():
                    short = self._drone_short_id(drone_id)
                    d_hex = self._drone_hex(drone_id)
                    with ui.row().classes("items-center no-wrap w-full gap-1").style(
                        "flex-wrap:nowrap; overflow:hidden;"
                    ):
                        # Nom du drone (pill coloré)
                        ui.label(short).style(
                            f"background:{d_hex}; color:#1a1a1a; padding:1px 6px; "
                            f"border-radius:6px; font-size:10px; font-weight:700; "
                            f"letter-spacing:0.5px; flex-shrink:0;"
                        )
                        # Chips : catégories OU tags (tri par occurrence descendante)
                        if group_mode == "tag":
                            sorted_items = sorted(
                                entries.items(),
                                key=lambda kv: (-kv[1], kv[0]),
                            )
                            for tag_name, n in sorted_items:
                                hex_c = self._tag_hex(tag_name)
                                abbr = self._tag_abbrev(tag_name)
                                ui.label(f"{n}{abbr}").classes("cal-mono").style(
                                    f"color:{hex_c}; font-size:11px; font-weight:600; "
                                    f"padding:0 3px; flex-shrink:0;"
                                ).tooltip(f"{tag_name} — {n} média(s)")
                        else:
                            for cat, n in entries.items():
                                abbr = self._CAT_ABBREV.get(cat, cat.lower() or "misc")
                                hex_c = self._CAT_HEX.get(cat, "#78909C")
                                ui.label(f"{n}{abbr}").classes("cal-mono").style(
                                    f"color:{hex_c}; font-size:11px; font-weight:600; "
                                    f"padding:0 3px; flex-shrink:0;"
                                )

    def _open_calendar_day_dialog(
        self,
        date_str: str,
        day_data: dict[str, dict[str, int]],
    ) -> None:
        """Ouvre un dialog listant les médias d'un jour, groupés par drone/catégorie.

        En mode « par tag », `day_data` contient `{drone: {tag: count}}` — on
        scanne alors tous les fichiers du jour et on filtre ceux qui portent
        au moins un des tags demandés.
        """
        dest = Path(self.destination_dir)
        drone_by_id = {d.get("id"): d for d in CONFIG.get("drone_mapping", [])}
        group_mode = self._calendar_state.get("group_mode", "cat")

        loading_placeholder: list[Any] = []

        dlg = ui.dialog().props("maximized")
        with dlg:
            with ui.card().classes("w-full h-full no-shadow column no-wrap"):
                with ui.row().classes("w-full items-center gap-2 q-pa-sm bg-primary text-white"):
                    ui.icon("calendar_today")
                    ui.label(date_str).classes("text-h6 flex-grow")
                    total = sum(cnt for cats in day_data.values() for cnt in cats.values())
                    ui.badge(f"{total} média(s)", color="orange")
                    # Boutons Tout sélectionner / désélectionner (les fichiers seront
                    # remplis dans `all_paths_holder` par le loader async)
                    all_paths_holder: list[str] = []

                    def _select_all() -> None:
                        if not all_paths_holder:
                            return
                        self._selected_viewer_files.update(all_paths_holder)
                        ui.notify(
                            f"✓ {len(all_paths_holder)} média(s) sélectionné(s)",
                            type="positive",
                        )
                        _rebuild_content()

                    def _deselect_all() -> None:
                        if not all_paths_holder:
                            return
                        before = len(
                            self._selected_viewer_files & set(all_paths_holder)
                        )
                        self._selected_viewer_files.difference_update(all_paths_holder)
                        ui.notify(
                            f"✗ {before} média(s) désélectionné(s)",
                            type="info",
                        )
                        _rebuild_content()

                    ui.button(
                        icon="select_all",
                        on_click=_select_all,
                    ).props("flat round color=white dense").tooltip("Tout sélectionner")
                    ui.button(
                        icon="deselect",
                        on_click=_deselect_all,
                    ).props("flat round color=white dense").tooltip("Tout désélectionner")
                    ui.button(icon="close", on_click=dlg.close).props("flat round color=white")

                body = ui.scroll_area().classes("w-full col")
                with body:
                    loading = ui.column().classes("w-full items-center q-pa-lg")
                    with loading:
                        ui.spinner(size="lg", color="primary")
                        ui.label("Chargement des vignettes…").classes("text-body2 q-mt-sm")
                    loading_placeholder.append(loading)
                    content = ui.column().classes("w-full")

        def _collect_files() -> dict[str, dict[str, list[Path]]]:
            """{drone_id: {category: [paths...]}}"""
            hidden_tags = set(self._hidden_tag_names())
            show_hidden = bool(CONFIG.get("viewer_show_hidden_tags", False))
            filter_hidden = bool(hidden_tags and not show_hidden)

            def _visible(f: Path) -> bool:
                if not filter_hidden:
                    return True
                try:
                    return not (set(self._read_sidecar_tags(f)) & hidden_tags)
                except Exception:
                    return True

            result: dict[str, dict[str, list[Path]]] = {}
            for drone_id, entries in day_data.items():
                drone = drone_by_id.get(drone_id)
                if not drone:
                    continue
                base = dest / drone.get("folder", "") / date_str
                if not base.is_dir():
                    continue
                if group_mode == "tag":
                    # `entries` = {tag_name: count} — scanne tous les fichiers
                    # du jour et ne garde que ceux qui portent au moins un
                    # des tags demandés (== clés de entries).
                    wanted_tags = set(entries.keys())
                    files_by_cat: dict[str, list[Path]] = {}
                    for f in base.rglob("*"):
                        if not f.is_file():
                            continue
                        if f.suffix.lower() not in ALL_MEDIA_EXTS:
                            continue
                        if not _visible(f):
                            continue
                        try:
                            file_tags = set(self._read_sidecar_tags(f))
                        except Exception:
                            continue
                        if not (file_tags & wanted_tags):
                            continue
                        try:
                            rel = f.relative_to(base)
                            cat = rel.parts[0] if len(rel.parts) > 1 else ""
                        except ValueError:
                            cat = ""
                        files_by_cat.setdefault(cat, []).append(f)
                    for cat, fs in files_by_cat.items():
                        fs.sort()
                        result.setdefault(drone_id, {})[cat] = fs
                    continue
                # Mode catégorie : entries = {cat: count}
                for cat in entries.keys():
                    if cat:
                        cat_dir = base / cat
                        if cat_dir.is_dir():
                            files = [
                                f for f in cat_dir.rglob("*")
                                if f.is_file() and f.suffix.lower() in ALL_MEDIA_EXTS
                                and _visible(f)
                            ]
                            files.sort()
                            if files:
                                result.setdefault(drone_id, {})[cat] = files
                    else:
                        # Fichiers à la racine (Goggles flat)
                        files = [
                            f for f in base.iterdir()
                            if f.is_file() and f.suffix.lower() in ALL_MEDIA_EXTS
                            and _visible(f)
                        ]
                        files.sort()
                        if files:
                            result.setdefault(drone_id, {})[""] = files
            # Précalcule thumbnails
            for cats in result.values():
                for files in cats.values():
                    for f in files:
                        generate_thumbnail(str(f), size=200)
            return result

        async def _load() -> None:
            try:
                files_by_drone = await run.io_bound(_collect_files)
            except Exception as e:
                self.log(f"❌ Chargement jour {date_str} échoué: {e}")
                if loading_placeholder:
                    loading_placeholder[0].clear()
                    with loading_placeholder[0]:
                        ui.label(f"Erreur : {e}").classes("text-caption text-negative")
                return

            if loading_placeholder:
                loading_placeholder[0].delete()

            # Alimente le holder pour les boutons select/deselect
            all_paths_holder.clear()
            for cats in files_by_drone.values():
                for files in cats.values():
                    for f in files:
                        all_paths_holder.append(str(f))
            # Mémorise les fichiers pour rebuild
            files_by_drone_holder.clear()
            files_by_drone_holder.update(files_by_drone)

            _rebuild_content()

        def _rebuild_content() -> None:
            files_by_drone = files_by_drone_holder
            content.clear()
            with content:
                if not files_by_drone:
                    ui.label("(aucun fichier trouvé)").classes("text-caption")
                    return
                for drone_id, cats in files_by_drone.items():
                    drone = drone_by_id.get(drone_id, {})
                    d_hex = self._drone_hex(drone_id)
                    n_files = sum(len(files) for files in cats.values())
                    # Bandeau drone : image ronde + label + total
                    with ui.row().classes("w-full items-center gap-3 q-mt-md q-mb-xs").style(
                        f"background:linear-gradient(90deg, {d_hex}22, transparent); "
                        f"padding:8px 12px; border-radius:10px; "
                        f"border-left:4px solid {d_hex};"
                    ):
                        img_rel = drone.get("image", "")
                        if img_rel and (APP_DIR / img_rel).exists():
                            ui.image(f"/{img_rel}").classes("rounded-full").style(
                                "width:48px; height:48px; object-fit:cover; "
                                f"border:2px solid {d_hex};"
                            )
                        else:
                            with ui.element("div").style(
                                f"width:48px; height:48px; border-radius:50%; "
                                f"background:{d_hex}; display:flex; align-items:center; "
                                f"justify-content:center;"
                            ):
                                ui.icon("flight").style("color:white; font-size:22px;")
                        with ui.column().classes("gap-0"):
                            ui.label(drone.get("label", drone_id)).style(
                                "font-size:16px; font-weight:600; letter-spacing:0.3px;"
                            )
                            ui.label(f"{n_files} fichier(s)").classes(
                                "text-caption text-grey-5"
                            )
                    for cat, files in cats.items():
                        items = self._partition_disk_files(files)
                        title = f"📁 {cat} ({len(items)})" if cat else f"📼 Racine ({len(items)})"
                        ui.label(title).classes("text-subtitle2 q-mt-sm")
                        with ui.grid(columns=5).classes("w-full gap-2"):
                            for item in items:
                                if item["kind"] == "single":
                                    self._render_viewer_media_card(item["path"])
                                else:
                                    self._render_viewer_group_card(
                                        item["category"],
                                        item["group_subdir"],
                                        item["files"],
                                    )

        # Holder mémorisant les fichiers chargés — permet _rebuild_content
        files_by_drone_holder: dict[str, dict[str, list[Path]]] = {}

        background_tasks.create(_load(), name=f"calendar_day_{date_str}")
        dlg.open()

    def _open_preview_from_disk(
        self,
        file_path: Path,
        on_deleted: Optional[Callable[[], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        """Charge un MediaUnit depuis le sidecar .dji.json (ou fallback minimal)."""
        unit = self._build_unit_from_disk(file_path)
        self._open_preview_dialog(
            unit, source="viewer", on_deleted=on_deleted, on_close=on_close
        )

    def _build_unit_from_disk(self, file_path: Path) -> MediaUnit:
        """Reconstruit un MediaUnit à partir du sidecar .dji.json (ou minimal)."""
        # Cherche le sidecar : {full_name}.dji.json puis {stem}.dji.json
        sidecar_candidates = [
            file_path.with_suffix(file_path.suffix + ".dji.json"),
            file_path.with_suffix(".dji.json"),
        ]
        payload = None
        for sc in sidecar_candidates:
            if sc.exists():
                try:
                    with open(sc, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                    break
                except Exception:
                    continue

        # Cherche les compagnons dans le même dossier avec le même stem
        stem = file_path.stem
        companions: list[str] = []
        for p in file_path.parent.iterdir():
            if p == file_path or not p.is_file():
                continue
            if p.stem == stem or p.stem.startswith(stem + "_"):
                if p.suffix.lower() in COMPANION_EXTS:
                    companions.append(str(p))

        if payload:
            cls = payload.get("classification", {})
            return MediaUnit(
                main_path=str(file_path),
                companions=companions or payload.get("files", {}).get("companions", []),
                metadata=payload.get("metadata_exiftool", {}),
                drone_id=cls.get("drone_id", "UNKNOWN"),
                drone_folder=cls.get("drone_folder", ""),
                category=cls.get("category", "VIDEO"),
                capture_date=cls.get("capture_date", ""),
                group_subdir=cls.get("group_subdir", ""),
                detection_reason=cls.get("detection_reason", ""),
                action="skip",
                tags=list(cls.get("tags", []) or []),
                custom_name=str(cls.get("custom_name", "") or ""),
            )

        # Fallback minimal — pas de sidecar
        return MediaUnit(
            main_path=str(file_path),
            companions=companions,
            metadata={},
            category="VIDEO" if file_path.suffix.lower() in VIDEO_EXTS else "PHOTO",
            action="skip",
            detection_reason="(pas de sidecar .dji.json)",
        )

    # ── réinitialisation session ───────────────────────────────────────────
    def _reset_and_restart(self) -> None:
        """Réinitialise l'état et recharge la page pour repartir à l'étape Configuration."""
        self.units = []
        self.log_lines = []
        self.report_paths = {}
        self.summary = {}
        self._filter_drone = "TOUS"
        self._filter_category = "TOUTES"
        self._selected_units = set()
        self._current_page = 0
        # Reset des références UI (nouveaux containers seront créés par build())
        self._stepper = None
        self._scan_progress_label = None
        self._scan_progress_bar = None
        self._review_container = None
        self._confirm_container = None
        self._exec_container = None
        self._exec_log = None
        ui.notify("↻ Nouvelle session", type="info")
        ui.navigate.reload()

    # ═══════════════════════════════════════════════════════════════════════
    # SYSTÈME DE TAGS
    # ═══════════════════════════════════════════════════════════════════════
    def _tag_defs(self) -> list[dict[str, Any]]:
        """Retourne la liste des définitions de tags dans CONFIG."""
        return CONFIG.setdefault("tags", [])

    def _tag_def(self, name: str) -> Optional[dict[str, Any]]:
        """Cherche la définition d'un tag par son nom (insensible à la casse)."""
        if not name:
            return None
        needle = name.strip().lower()
        for t in self._tag_defs():
            if (t.get("name") or "").strip().lower() == needle:
                return t
        return None

    def _tag_names(self) -> list[str]:
        return [t.get("name", "") for t in self._tag_defs() if t.get("name")]

    def _hidden_tag_names(self) -> set[str]:
        """Noms des tags marqués `hidden` dans CONFIG (à masquer dans le viewer)."""
        return {t.get("name", "") for t in self._tag_defs() if t.get("hidden")}

    def _tag_hex(self, name: str) -> str:
        d = self._tag_def(name)
        return (d or {}).get("color") or "#78909C"

    def _tag_icon(self, name: str) -> str:
        d = self._tag_def(name)
        return (d or {}).get("icon") or "#"

    def _apply_tag_to_units(self, units: list[MediaUnit], tag_name: str,
                            remove: bool = False) -> int:
        """Ajoute (ou retire) `tag_name` sur toutes les `units`.
        Retourne le nombre d'unités modifiées."""
        tag_name = (tag_name or "").strip()
        if not tag_name:
            return 0
        changed = 0
        for u in units:
            cur = list(u.tags or [])
            has = tag_name in cur
            if remove and has:
                cur.remove(tag_name)
                u.tags = cur
                changed += 1
            elif (not remove) and (not has):
                cur.append(tag_name)
                u.tags = cur
                changed += 1
        return changed

    def _open_rename_dialog(
        self,
        current: str,
        title: str,
        subtitle: str,
        on_save: Callable[[str], None],
    ) -> None:
        """Ouvre un petit dialog pour saisir/modifier un nom personnalisé."""
        with ui.dialog() as d, ui.card().classes("q-pa-md").style("min-width:380px;"):
            ui.label(title).classes("text-h6")
            if subtitle:
                ui.label(subtitle).classes("text-caption text-grey-6")
            name_input = ui.input(
                label="Nom personnalisé",
                value=current or "",
                placeholder="ex. Vol tôt matin, coucher soleil…",
            ).props("dense autofocus outlined clearable").classes("w-full q-mt-sm")
            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Annuler", on_click=d.close).props("flat")

                def _save() -> None:
                    val = (name_input.value or "").strip()
                    d.close()
                    on_save(val)

                ui.button("Enregistrer", on_click=_save).props(
                    "unelevated color=primary"
                )
            name_input.on("keydown.enter", lambda: _save())
        d.open()

    def _sidecar_path_for(self, file_path: str) -> Optional[Path]:
        """Retourne le chemin du sidecar `.dji.json` pour un média destination."""
        p = Path(file_path)
        for cand in (
            p.with_suffix(p.suffix + ".dji.json"),
            p.with_suffix(".dji.json"),
        ):
            if cand.exists():
                return cand
        return None

    def _update_sidecar_tags(self, file_path: str, tags: list[str]) -> bool:
        """Met à jour la liste `tags` dans le sidecar `.dji.json` d'un fichier
        déjà présent à destination.

        Retourne True si le sidecar existait et a été mis à jour."""
        sc = self._sidecar_path_for(file_path)
        if sc is None:
            return False
        try:
            with open(sc, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            self.log(f"⚠️ Lecture sidecar échouée ({sc}): {e}")
            return False
        cls = payload.setdefault("classification", {})
        cls["tags"] = list(dict.fromkeys(tags or []))  # dédup en préservant l'ordre
        payload["tags_updated_at"] = datetime.now().isoformat()
        try:
            with open(sc, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
            # Le cache de l'index-par-tag n'est plus valide.
            self._calendar_tag_index_cache = None
            return True
        except Exception as e:
            self.log(f"⚠️ Écriture sidecar échouée ({sc}): {e}")
            return False

    def _read_sidecar_tags(self, file_path: Path) -> list[str]:
        """Lit les tags depuis le sidecar (rapide, sans reconstruction complète)."""
        for cand in (
            file_path.with_suffix(file_path.suffix + ".dji.json"),
            file_path.with_suffix(".dji.json"),
        ):
            if cand.exists():
                try:
                    with open(cand, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                    return list(payload.get("classification", {}).get("tags", []) or [])
                except Exception:
                    return []
        return []

    def _read_sidecar_custom_name(self, file_path: Path) -> str:
        """Lit le nom personnalisé depuis le sidecar (chaîne vide si absent)."""
        for cand in (
            file_path.with_suffix(file_path.suffix + ".dji.json"),
            file_path.with_suffix(".dji.json"),
        ):
            if cand.exists():
                try:
                    with open(cand, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                    return str(
                        payload.get("classification", {}).get("custom_name", "") or ""
                    )
                except Exception:
                    return ""
        return ""

    def _update_sidecar_custom_name(self, file_path: str, name: str) -> bool:
        """Met à jour `custom_name` dans le sidecar `.dji.json`.

        Retourne True si le sidecar existait et a été mis à jour."""
        sc = self._sidecar_path_for(file_path)
        if sc is None:
            return False
        try:
            with open(sc, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            self.log(f"⚠️ Lecture sidecar échouée ({sc}): {e}")
            return False
        cls = payload.setdefault("classification", {})
        cls["custom_name"] = (name or "").strip()
        payload["custom_name_updated_at"] = datetime.now().isoformat()
        try:
            with open(sc, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
            return True
        except Exception as e:
            self.log(f"⚠️ Écriture sidecar échouée ({sc}): {e}")
            return False


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

    # ── Filtre l'exception Windows bruyante mais bénigne :
    #   ConnectionResetError: [WinError 10054] An existing connection was
    #   forcibly closed by the remote host — se produit quand le navigateur
    #   (ou pywebview) ferme brusquement une connexion HTTP/WS. Sans impact
    #   fonctionnel — on la fait juste taire.
    def _install_quiet_asyncio_handler() -> None:
        try:
            import asyncio as _asyncio
            import logging as _logging

            def _quiet_handler(loop, ctx):
                exc = ctx.get("exception")
                msg = ctx.get("message", "")
                if isinstance(exc, ConnectionResetError):
                    return
                if "forcibly closed by the remote host" in str(msg):
                    return
                if "connection lost" in str(msg).lower() and isinstance(
                    exc, (ConnectionError, OSError)
                ):
                    return
                loop.default_exception_handler(ctx)

            try:
                loop = _asyncio.get_event_loop()
                loop.set_exception_handler(_quiet_handler)
            except RuntimeError:
                pass

            # Filtre au niveau logging aussi (asyncio.log)
            class _AsyncioQuietFilter(_logging.Filter):
                def filter(self, record: _logging.LogRecord) -> bool:
                    msg = record.getMessage()
                    if "ConnectionResetError" in msg:
                        return False
                    if "forcibly closed by the remote host" in msg:
                        return False
                    if "_call_connection_lost" in msg:
                        return False
                    return True

            _logging.getLogger("asyncio").addFilter(_AsyncioQuietFilter())
        except Exception:
            pass

    _install_quiet_asyncio_handler()

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
