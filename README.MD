<div align="center">

<h1>🗂️ Organizator</h1>

<p><strong>Suite locale d'organisation et d'analyse de médias — pilotée par IA</strong></p>

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![NiceGUI](https://img.shields.io/badge/NiceGUI-2.x-00C7B7?style=for-the-badge&logo=vuedotjs&logoColor=white)](https://nicegui.io/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen?style=for-the-badge)]()
[![Last Commit](https://img.shields.io/github/last-commit/lawiz22/Organizator?style=for-the-badge&logo=github)](https://github.com/lawiz22/Organizator/commits/master)

</div>

---

## 🧭 Vue d'ensemble

**Organizator** est une application desktop locale construite avec **NiceGUI + Python**. Elle combine deux outils complémentaires en une seule suite :

| Module | Description |
|--------|-------------|
| 🗂️ **Organizator** | Scanne, catégorise et organise tes fichiers en quelques clics avec un workflow guidé en 7 étapes |
| 🤖 **Media Mind AI** | Recherche sémantique, scoring esthétique, détection NSFW et indexation vectorielle de ta médiathèque |

> Tout tourne **localement** — aucune donnée ne quitte ta machine.

---

## ✨ Fonctionnalités

### 🗂️ Organizator — Organisation de fichiers

- **Scan multi-sources** : dossiers, disques entiers, chemins multiples
- **7 catégories intelligentes** : Musique, Photo/Vidéo, Documents, Projets, Exécutables, Archives, Divers
- **Sélection granulaire** : checkboxes par fichier, tout sélectionner/désélectionner par sous-catégorie
- **Actions par fichier** : Organiser • Ignorer • Corbeille • Suppression définitive
- **Mode copie ou déplacement**
- **Nettoyage post-exécution** : suppression des dossiers source vides
- **Rapports JSON horodatés** : complet + compact pour exploitation automatisée
- **Archivage PostgreSQL** (optionnel)

### 🤖 Media Mind AI — Analyse IA

- **Recherche sémantique** — Retrouve tes images et vidéos par description textuelle (modèles `Qwen3-VL-Embedding-2B/8B`)
- **Reranker** — Reclassement précis des résultats (`Qwen3-VL-Reranker-2B`)
- **Scoring esthétique** — Évalue la qualité visuelle de tes images
- **Détection NSFW** — Taggering multi-modèles (`WD-EVA02`, `WD-SwinV2`, `WD-ViT`, `JoyTag`, `Camie`)
- **Indexation vectorielle** — Cache SQLite haute performance pour requêtes instantanées
- **Export HTML/JSON** des résultats de recherche

---

L'interface tourne sur **http://127.0.0.1:8191** (Organizator) et **http://127.0.0.1:8888** (Media Mind AI).

---

## 🚀 Installation rapide

### Prérequis

- **Windows 10/11**
- **Python 3.10+** (ou laisser `run_organizator.bat` tout gérer via `uv`)
- Accès Internet au premier lancement (téléchargement des dépendances)
- *(Optionnel)* Docker Desktop pour PostgreSQL local

### Lancement en un clic

```bat
run_organizator.bat
```

Le script est **entièrement autonome** : il télécharge `uv`, crée le `.venv`, installe toutes les dépendances, puis lance l'application.

### Mode serveur (accès réseau local)

```bat
run_organizator.bat --server-only --host 0.0.0.0 --port 8191
```

### Media Mind AI

```bat
run.bat
```

---

## 🔄 Workflow Organizator

```
┌─────────────────────────────────────────────────────────┐
│  1. ⚙️  Configuration      Sources + Destination          │
│  2. 🔍  Scan               Analyse récursive des fichiers │
│  3. ✅  Sélection           Catégories + checkboxes/fichier│
│  4. ❓  Non catégorisés     Traitement des inclassables    │
│  5. 📦  Exécutables/Archiv  .exe .msi .zip .rar           │
│  6. ▶️  Exécution           Modal de confirmation          │
│  7. 📊  Rapport             JSON complet + compact         │
└─────────────────────────────────────────────────────────┘
```

---

## 🤖 Modèles IA inclus

| Modèle | Tâche | Taille |
|--------|-------|--------|
| `Qwen3-VL-Embedding-2B` | Embeddings image/texte | ~2B params |
| `Qwen3-VL-Embedding-8B` | Embeddings haute précision | ~8B params |
| `Qwen3-VL-Reranker-2B` | Reranking sémantique | ~2B params |
| `WD-EVA02-large-tagger-v3` | Tagging NSFW/général | ONNX |
| `WD-SwinV2-tagger-v3` | Tagging NSFW/général | ONNX |
| `WD-ViT-tagger-v3` | Tagging NSFW/général | ONNX |
| `JoyTag` | Tagging multimodal | ONNX |
| `Camie-tagger-v2` | Tagging anime/général | ONNX |
| `SigLIP-so400m` | Classification visuelle | Google |
| `SigLIP2-x256-explicit` | Détection contenu explicite | SafeTensors |

---

## 🐘 PostgreSQL (optionnel)

Lance une instance locale via Docker :

```bash
docker compose up -d
```

Connexion par défaut :

```
postgresql://organizator:organizator2026@localhost:5432/organizator
```

> Sans PostgreSQL, l'application fonctionne normalement — l'archivage BD est simplement désactivé.

---

## 📁 Structure du projet

```
Organizator/
├── organizator.py              # App principale (workflow 7 étapes)
├── media_mind_ai.py            # Suite IA (recherche, NSFW, esthétique)
├── nsfw_batch_processor.py     # Traitement NSFW en batch
├── config.json                 # Configuration générale
├── docker-compose.yml          # PostgreSQL local
├── run_organizator.bat         # Lancement Organizator (auto-bootstrap)
├── run.bat                     # Lancement Media Mind AI
├── assets/
│   └── preview.jpg
├── models/                     # Modèles IA (non versionnés)
│   ├── Qwen_Qwen3-VL-Embedding-2B/
│   ├── Qwen_Qwen3-VL-Embedding-8B/
│   ├── SmilingWolf_wd-*/
│   └── ...
├── requirements.txt            # Dépendances Media Mind AI
└── requirements_organizator.txt # Dépendances Organizator
```

---

## 📊 Rapports d'exécution

Après chaque exécution, Organizator génère automatiquement :

- `rapport_organizator_YYYYMMDD_HHMMSS.json` — rapport complet
- `rapport_organizator_compact_YYYYMMDD_HHMMSS.json` — version allégée

Contenu des rapports :
- Opérations effectuées (copie/déplacement/suppression)
- Éléments mis en corbeille ou supprimés
- Vérification des archives `.zip`/`.rar`
- Résultat du nettoyage des dossiers source vides

---

## 🛠️ Stack technique

[![NiceGUI](https://img.shields.io/badge/NiceGUI-UI_Framework-00C7B7?style=flat-square&logo=vuedotjs)](https://nicegui.io/)
[![PyTorch](https://img.shields.io/badge/PyTorch-Deep_Learning-EE4C2C?style=flat-square&logo=pytorch)](https://pytorch.org/)
[![HuggingFace](https://img.shields.io/badge/HuggingFace-Models-FFD21E?style=flat-square&logo=huggingface&logoColor=black)](https://huggingface.co/)
[![ONNX](https://img.shields.io/badge/ONNX-Runtime-005CED?style=flat-square&logo=onnx)](https://onnxruntime.ai/)
[![SQLite](https://img.shields.io/badge/SQLite-Cache-003B57?style=flat-square&logo=sqlite)](https://sqlite.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Archive-4169E1?style=flat-square&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white)](https://docker.com/)

---

## 📄 Licence

Distribué sous licence **MIT**. Voir [`LICENSE`](LICENSE) pour plus d'informations.

---

<div align="center">
  <sub>Fait avec ❤️ par <a href="https://github.com/lawiz22">lawiz22</a></sub>
</div>

Organizator est une application locale (NiceGUI + Python) pour scanner, trier et organiser automatiquement des dossiers/disques.

Ce workspace contient:
- `organizator.py`: interface principale d'organisation (workflow guide)
- `run_organizator.bat`: bootstrap + lancement Organizator
- `media_mind_ai.py`: composant AI historique du workspace
- `requirements.txt` et `requirements_organizator.txt`: dependances

## Fonctionnalites principales

- Scan multi-sources (dossiers/disques)
- Classification par categories (musique, photo-video, documents, projets)
- Gestion des executables/archives (.exe/.msi/.zip/.rar)
- Actions par fichier: categoriser, ignorer, corbeille, suppression
- Mode copie ou deplacement
- Option de suppression des dossiers source vides apres deplacement
- Rapport detaille + export JSON horodate
- Export JSON compact pour exploitation automatisable
- Archivage metadonnees dans PostgreSQL (si disponible)

## Workflow utilisateur

1. Configuration des sources/destination
2. Scan
3. Selection categories
4. Non categorises
5. Executables & Archives
6. Execution (avec modal de confirmation)
7. Rapport final

## Prerequis

- Windows 10/11
- Acces Internet au premier lancement (telechargement uv/dependances)
- Optionnel: Docker Desktop pour PostgreSQL local

## Installation rapide (Windows)

Le script `run_organizator.bat` est autonome: il telecharge `uv`, cree `.venv`, installe les dependances, puis lance l'application.

```bat
run_organizator.bat
```

Mode serveur web:

```bat
run_organizator.bat --server-only --host 127.0.0.1 --port 8191
```

URL:

```text
http://127.0.0.1:8191
```

## PostgreSQL (optionnel)

Le projet inclut `docker-compose.yml` pour une instance locale.

```bash
docker compose up -d
```

Connexion utilisee par defaut dans l'app:

```text
postgresql://organizator:organizator2026@localhost:5432/organizator
```

Si PostgreSQL est indisponible, l'application continue de fonctionner sans archivage BD.

## Exports de rapport

Apres execution, Organizator produit automatiquement:
- un JSON complet (horodate)
- un JSON compact (horodate)

Ces rapports sont ecrits dans le repertoire cible (ou dans `Organizator` sur le disque source en mode sur place).

Le rapport inclut notamment:
- operations organisees
- elements corbeille/supprimes
- verification des archives `.zip/.rar`
- resultat du nettoyage des dossiers source vides (si active)

## Structure du projet

```text
.
|-- assets/
|-- media_mind_ai.py
|-- organizator.py
|-- docker-compose.yml
|-- requirements.txt
|-- requirements_organizator.txt
|-- run.bat
|-- run_organizator.bat
|-- run_server.bat
`-- README.MD
```

## Conseils pour publier sur GitHub

- Verifier que `.env`, `.venv`, caches et rapports locaux sont ignores (voir `.gitignore`)
- Garder les secrets hors du depot
- Ajouter des captures d'ecran dans `assets/` si souhaite

Sequence type:

```bash
git init
git add .
git commit -m "feat: initial Organizator workspace"
git branch -M main
git remote add origin https://github.com/<user>/<repo>.git
git push -u origin main
```

## Roadmap courte

- Finaliser l'enrichissement IA (passe LLM)
- Ajouter tests automatises de scenarios critiques
- Ajouter CI GitHub Actions (lint + smoke test)
