"""
QUICK REFERENCE: NSFW Batch Processing avec Validation de Contrats
====================================================================

Ce fichier est un aide-mémoire rapide pour utiliser le module de batch processing.

## ⚡ QUICKSTART (5 minutes)

1. Ajouter l'import:
   from nsfw_batch_processor import NSFWBatchProcessor, NSFWContractWriter

2. Créer les lots:
   processor = NSFWBatchProcessor(batch_size=10)
   batches = processor.split_results_into_batches(state.nsfw_results)

3. Traiter chaque lot:
   writer = NSFWContractWriter(state.nsfw_model, state.nsfw_threshold)
   for batch in batches:
       results = writer.write_batch_contracts(batch, skip_if_exists=True)
       print(f"✅ {results['success']} | ❌ {results['errors']}")


## 🎯 PRINCIPALES CLASSES

### NSFWContractValidator
Validation et lecture de contrats existants

Méthodes:
  - contract_exists(photo_path: str) -> bool
    Vérifie si un contrat existe
  
  - read_contract(photo_path: str) -> Dict | None
    Lit le contenu JSON du contrat
  
  - is_contract_validated(photo_path: str) -> (bool, str)
    Retourne (is_validated, tier)

Utilisation:
  validator = NSFWContractValidator()
  if validator.contract_exists("/path/to/photo.jpg"):
      contract = validator.read_contract("/path/to/photo.jpg")
      print(contract['result']['tier'])


### NSFWBatchProcessor
Divise les résultats en lots avec validation

Méthodes:
  - split_results_into_batches(results, skip_existing=True) -> List[List]
    Crée les lots
  
  - get_batch_info(batch) -> Dict
    Info sur un lot (danger moyen, distribution tier, etc.)
  
  - generate_batch_report(batches) -> str
    Génère un rapport texte formaté

Utilisation:
  processor = NSFWBatchProcessor(batch_size=15)
  batches = processor.split_results_into_batches(results)
  report = processor.generate_batch_report(batches)
  print(report)


### NSFWContractWriter
Écrit les contrats de validation

Méthodes:
  - write_contract(path, danger, tier, details, skip_if_exists=True)
    -> (success: bool, message: str)
    Écrit UN contrat
  
  - write_batch_contracts(batch, skip_if_exists=True) -> Dict
    Écrit les contrats pour UN LOT
    Returns: {'success': int, 'skipped': int, 'errors': int, 'details': [...]}

Utilisation:
  writer = NSFWContractWriter("model_name", 0.42)
  success, msg = writer.write_contract(path, 0.8, "EXPLICITE", {})
  print(f"{msg}: {success}")


## 📊 CONFIGURATION

### Paramètres recommandés par GPU:
GPU              | Batch Size | Notes
─────────────────┼────────────┼─────────────────────────────
CPU only         | 1-3        | Très lent
RTX 3060 (12GB)  | 10-15      | Standard
RTX 4060 (8GB)   | 5-10       | Limité
RTX 4090 (24GB)  | 30-50      | Maximum
Google Colab T4  | 15-20      | GPU gratuit
### Pour mémoire limitée: réduire batch_size
### Pour vitesse maximum: augmenter batch_size


## 🔍 VÉRIFIER LES CONTRATS

Code pour lister tous les contrats:
```python
from pathlib import Path
import os

validator = NSFWContractValidator()
contracts = []

for root, dirs, files in os.walk("/media/folder"):
    for file in files:
        if file.endswith(('.jpg', '.png', '.webp')):
            path = os.path.join(root, file)
            if validator.contract_exists(path):
                contract = validator.read_contract(path)
                tier = contract['result']['tier']
                contracts.append((file, tier))

# Afficher
for fname, tier in contracts:
    print(f"  {'🟢' if tier=='SAIN' else '🟡' if tier=='SENSUEL' else '🔴'} {fname}: {tier}")
```


## 📋 STRUCTURE DU CONTRAT

Fichier créé: `photo.jpg` → `photo_validation.json`

```json
{
  "schema": "organizador.nsfw.validation.v1",
  "validated_at": "2026-05-17T15:30:45+00:00",
  "source_file": "/full/path/to/photo.jpg",
  "file_name": "photo.jpg",
  "result": {
    "tier": "SAIN|SENSUEL|EXPLICITE",
    "danger": 0.0-1.0,
    "model": "model_name",
    "raw_top_label": "label_string",
    "explicit_threshold": 0.42,
    "details": {
      "safe": 0.87,
      "normal": 0.08,
      "suggestive": 0.04,
      "explicit": 0.01
    }
  }
}
```


## ⚠️ ERREURS COURANTES

### ❌ Erreur: "Fichier introuvable"
Cause: Chemin invalide ou fichier supprimé
Solution: Vérifier os.path.exists(path)

### ❌ Erreur: "Permission refusée"
Cause: Pas d'accès en écriture au dossier
Solution: Vérifier les permissions (chmod 755)

### ❌ Aucun contrat créé
Cause: Extension de fichier non reconnue
Solution: Vérifier que c'est .jpg/.png/.webp (pas de minuscules)

### ❌ Lots vides après split
Cause: Tous les fichiers ont skip_existing=True et contrats existants
Solution: Passer skip_existing=False

### ❌ RuntimeError: CUDA memory
Cause: batch_size trop grand
Solution: Réduire batch_size (10 → 5)


## 🚀 OPTIMISATION

### Vitesse
- Augmenter batch_size (si VRAM disponible)
- Utiliser GPU (CUDA)
- Pré-filtrer les fichiers invalides

### Mémoire
- Diminuer batch_size
- skip_existing=True (évite relecture contrats)
- Traiter un dossier à la fois

### Fiabilité
- skip_if_exists=True (par défaut)
- Vérifier os.path.exists() avant write_contract()
- Utiliser try/except autour de write_batch_contracts()


## 📈 PERFORMANCES TYPIQUES

Cas                      | Temps/lot | RAM utilisée
─────────────────────────┼──────────┼─────────────
100 photos, batch=10     | 2-5s     | ~200MB
1000 photos, batch=20    | 15-30s   | ~300MB
5000 photos, batch=50    | 60-120s  | ~400MB

Temps = O(n) linéaire avec nombre de fichiers
RAM ~ batch_size * 4MB (moyenne)


## 💡 ASTUCES

### Traiter SEULEMENT les EXPLICITE:
```python
explicit = [(d,p,l,dt) for d,p,l,dt in results if l == "EXPLICITE"]
processor.split_results_into_batches(explicit)
```

### Ignorer les contrats existants:
```python
processor.split_results_into_batches(results, skip_existing=True)
```

### Compter les tiers:
```python
from collections import Counter
tiers = [item[2] for item in batch]
Counter(tiers)  # {'SAIN': 7, 'EXPLICITE': 3}
```

### Rapport détaillé par lot:
```python
for batch in batches:
    info = processor.get_batch_info(batch)
    print(f"Danger moyen: {info['avg_danger']*100:.1f}%")
```


## 🔗 INTÉGRATION AVEC media_mind_ai.py

À ajouter dans la section NSFW UI:

```python
# Import
from nsfw_batch_processor import NSFWBatchProcessor, NSFWContractWriter

# Bouton
ui.button('Contrats par lot', on_click=write_nsfw_contracts_with_batch_action)

# Fonction
def write_nsfw_contracts_with_batch_action():
    batch_size = 10  # À configurer
    processor = NSFWBatchProcessor(batch_size=batch_size)
    batches = processor.split_results_into_batches(state.nsfw_results)
    
    writer = NSFWContractWriter(state.nsfw_model, state.nsfw_threshold)
    for batch in batches:
        results = writer.write_batch_contracts(batch)
        state.add_log(f"✅ {results['success']} | ❌ {results['errors']}")
```


## 📚 DOCUMENTATION COMPLÈTE

- `nsfw_batch_processor.py` - Code source complet
- `BATCH_INTEGRATION_GUIDE.md` - Guide d'intégration détaillé
- `NSFW_BATCH_EXAMPLES.py` - Exemples de code à adapter


## 🎓 RESSOURCES

Python docs:
- Path (pathlib): https://docs.python.org/3/library/pathlib.html
- JSON: https://docs.python.org/3/library/json.html

NiceGUI (pour l'UI):
- Buttons: https://nicegui.io/documentation/button
- Dialogs: https://nicegui.io/documentation/dialog


════════════════════════════════════════════════════════════════════
Créé pour Organizator Media Manager
Version: 1.0
Date: 2026-05-17
════════════════════════════════════════════════════════════════════
"""


# ============================================================
# TEST RAPIDE: Copier/coller ce code pour tester le module
# ============================================================

if __name__ == "__main__":
    from nsfw_batch_processor import NSFWBatchProcessor, NSFWContractValidator
    
    # Données de test
    test_results = [
        (0.12, "/path/photo1.jpg", "SAIN", {"safe": 0.87}),
        (0.45, "/path/photo2.jpg", "EXPLICITE", {"explicit": 0.55}),
        (0.35, "/path/photo3.jpg", "SENSUEL", {"sensual": 0.38}),
    ]
    
    # Test 1: Créer des lots
    processor = NSFWBatchProcessor(batch_size=2)
    batches = processor.split_results_into_batches(test_results)
    print(f"✅ {len(batches)} lot(s) créé(s)")
    
    # Test 2: Générer rapport
    report = processor.generate_batch_report(batches)
    print(report)
    
    # Test 3: Info par lot
    for i, batch in enumerate(batches):
        info = processor.get_batch_info(batch)
        print(f"Lot {i+1}: {info}")
    
    print("✅ Tests réussis!")
