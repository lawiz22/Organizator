# Organizator Media Workspace

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
