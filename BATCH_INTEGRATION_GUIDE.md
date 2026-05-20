"""
INTEGRATION GUIDE: NSFW Batch Processing avec Validation de Contrats
======================================================================

Cette documentation explique comment intégrer le traitement par lot
avec validation de contrats existants dans media_mind_ai.py

## 1. INSTALLATION ET IMPORT

Importer le module au début de media_mind_ai.py:

    from nsfw_batch_processor import (
        NSFWBatchProcessor,
        NSFWContractValidator, 
        NSFWContractWriter,
        create_nsfw_batch_summary,
        process_nsfw_batch_with_validation
    )


## 2. MODIFICATION DE write_nsfw_contracts_action()

Avant (code existant):
```python
def write_nsfw_contracts_action():
    source_items = state.nsfw_all_results if state.nsfw_all_results else state.nsfw_results
    if not source_items:
        return ui.notify("Aucun résultat NSFW à valider.", type='warning')
    
    # ... boucle simple sur tous les items ...
```

Après (avec batching):
```python
def write_nsfw_contracts_action():
    source_items = state.nsfw_all_results if state.nsfw_all_results else state.nsfw_results
    if not source_items:
        return ui.notify("Aucun résultat NSFW à valider.", type='warning')
    
    # 1. Créer le résumé et diviser en lots
    batch_size = int(getattr(state, 'nsfw_batch_size', 10))  # Configuration
    report, batches = create_nsfw_batch_summary(
        source_items,
        batch_size=batch_size,
        skip_existing=True  # Ignorer les contrats existants
    )
    
    # 2. Afficher le rapport
    state.add_log(report)
    
    # 3. Traiter lot par lot
    writer = NSFWContractWriter(state.nsfw_model, state.nsfw_threshold)
    
    total_success = 0
    total_skipped = 0
    total_errors = 0
    
    for batch_idx, batch in enumerate(batches, 1):
        state.add_log(f"\\n▶ Traitement lot {batch_idx}/{len(batches)}...")
        
        # Traiter le lot (traitement par lot pour économiser la RAM)
        results = process_nsfw_batch_with_validation(
            batch,
            state.nsfw_model,
            state.nsfw_threshold,
            skip_if_exists=True,
            log_callback=state.add_log
        )
        
        total_success += results['success']
        total_skipped += results['skipped']
        total_errors += results['errors']
    
    # 4. Résumé final
    state.add_log(f"""
╔════════════════════════════════════════════╗
║            RÉSUMÉ FINAL                    ║
╠════════════════════════════════════════════╣
║ ✅ Réussi: {total_success}
║ ⏭️  Ignorés: {total_skipped}
║ ❌ Erreurs: {total_errors}
╚════════════════════════════════════════════╝
""")
    
    if total_errors == 0:
        ui.notify(f"✅ Contrats écrits: {total_success} | Ignorés: {total_skipped}", type='positive')
    else:
        ui.notify(f"⚠️ Contrats: {total_success} | Ignorés: {total_skipped} | Erreurs: {total_errors}", type='warning')
```


## 3. AJOUTER DES PARAMÈTRES À L'UI NSFW

Ajouter à la section des paramètres NSFW:

```python
nsfw_batch_size = ui.number(
    'Taille du lot (batch)',
    value=cfg.get('nsfw_batch_size', 10),
    format='%.0f',
    min=1,
    max=100
).classes('w-full')

nsfw_skip_existing = ui.checkbox(
    'Ignorer les contrats existants',
    value=cfg.get('nsfw_skip_existing', True)
).classes('w-full')
```

Puis sauvegarder dans la config:

```python
save_config({
    'nsfw_batch_size': int(nsfw_batch_size.value),
    'nsfw_skip_existing': nsfw_skip_existing.value,
    # ... autres paramètres ...
})
```


## 4. VALIDER LES CONTRATS AVANT TRAITEMENT

Optionnel: Ajouter une vérification avant écriture:

```python
def validate_and_write_nsfw_contracts():
    source_items = state.nsfw_all_results if state.nsfw_all_results else state.nsfw_results
    
    # Compter les contrats existants
    validator = NSFWContractValidator()
    existing_count = 0
    for danger, path, label, details in source_items:
        if validator.contract_exists(path):
            existing_count += 1
    
    if existing_count > 0:
        state.add_log(f"⚠️ {existing_count} contrat(s) trouvé(s) - ces fichiers seront ignorés")
    
    # Continuer avec le traitement...
    write_nsfw_contracts_action()
```


## 5. UTILISATION AVANCÉE

### A. Traiter seulement les fichiers EXPLICITE

```python
explicit_only = [
    (d, p, l, dt) for d, p, l, dt in source_items 
    if l == 'EXPLICITE'
]
report, batches = create_nsfw_batch_summary(explicit_only, batch_size=5)
```

### B. Traiter les lots en parallèle (avec threading)

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def write_batch_async(batch, batch_idx):
    writer = NSFWContractWriter(state.nsfw_model, state.nsfw_threshold)
    return writer.write_batch_contracts(batch, skip_if_exists=True)

# Traiter 3 lots en parallèle
with ThreadPoolExecutor(max_workers=3) as executor:
    futures = {
        executor.submit(write_batch_async, batch, i): i 
        for i, batch in enumerate(batches)
    }
    
    for future in as_completed(futures):
        batch_idx = futures[future]
        results = future.result()
        state.add_log(f"Lot {batch_idx}: {results['success']} ✅ {results['errors']} ❌")
```

### C. Lister tous les contrats existants

```python
def list_nsfw_contracts(source_dir):
    validator = NSFWContractValidator()
    contracts = []
    
    for root, dirs, files in os.walk(source_dir):
        for file in files:
            if file.endswith('.jpg') or file.endswith('.png'):
                path = os.path.join(root, file)
                if validator.contract_exists(path):
                    contract = validator.read_contract(path)
                    contracts.append((path, contract))
    
    return contracts
```


## 6. STRUCTURE DU CONTRAT JSON

Exemple de contrat généré:

```json
{
  "schema": "organizador.nsfw.validation.v1",
  "validated_at": "2026-05-17T15:30:45.123456+00:00",
  "source_file": "/path/to/photo.jpg",
  "file_name": "photo.jpg",
  "result": {
    "tier": "SAIN",
    "danger": 0.12,
    "model": "strangerguardhf/nsfw-image-detection",
    "raw_top_label": "safe",
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


## 7. CONFIGURATION RECOMMANDÉE

Pour optimiser les performances:

- **batch_size = 10-20** : Standard, bon équilibre RAM/vitesse
- **batch_size = 5-10** : GPUs avec peu de VRAM
- **batch_size = 30-50** : Serveurs avec beaucoup de RAM

Exemples:
```python
# RTX 3060 (12GB VRAM): batch_size = 10-15
# RTX 4090 (24GB VRAM): batch_size = 30-50
# CPU only: batch_size = 5
# Google Colab T4: batch_size = 15-20
```


## 8. DÉPANNAGE

Q: Certains fichiers n'écrivent pas de contrats
R: Vérifier que le chemin est une image valide (.jpg, .png, .webp, etc.)

Q: Performance lente avec lots grands
R: Réduire batch_size (ex: 5 au lieu de 20)

Q: Contrats non créés malgré succès = 0
R: Vérifier les permissions d'écriture sur le dossier

Q: RAM pleine lors du traitement
R: Réduire top_n_nsfw ou batch_size


## 9. EXEMPLE COMPLET

```python
async def process_nsfw_with_batches():
    # Configuration
    batch_size = 15
    skip_existing = True
    
    # Récupérer les résultats
    source_items = state.nsfw_all_results or state.nsfw_results
    
    if not source_items:
        ui.notify("Aucun résultat", type='warning')
        return
    
    # Créer lots
    report, batches = create_nsfw_batch_summary(
        source_items,
        batch_size=batch_size,
        skip_existing=skip_existing
    )
    state.add_log(report)
    
    # Traiter
    writer = NSFWContractWriter(state.nsfw_model, state.nsfw_threshold)
    stats = {'success': 0, 'skipped': 0, 'errors': 0}
    
    for i, batch in enumerate(batches):
        state.add_log(f"\\n[LOT {i+1}/{len(batches)}] {len(batch)} fichiers...")
        results = process_nsfw_batch_with_validation(
            batch,
            state.nsfw_model,
            state.nsfw_threshold,
            skip_if_exists=skip_existing,
            log_callback=state.add_log
        )
        
        for k in stats:
            stats[k] += results[k]
    
    # Rapport final
    state.add_log(f"""
✅ TERMINÉ
━━━━━━━━━━━━━━━━━━━━━━━━
✓ Écrits: {stats['success']}
⊘ Ignorés: {stats['skipped']}  
✗ Erreurs: {stats['errors']}
━━━━━━━━━━━━━━━━━━━━━━━━
""")
```
"""
