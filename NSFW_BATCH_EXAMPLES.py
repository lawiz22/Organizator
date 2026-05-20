"""
EXEMPLE COMPLET: Intégration du traitement NSFW par lot
Ce fichier montre le code à ajouter/modifier dans media_mind_ai.py
"""

# ============================================================
# 1. AJOUTER CES IMPORTS AU DÉBUT DE media_mind_ai.py
# ============================================================

from nsfw_batch_processor import (
    NSFWBatchProcessor,
    NSFWContractValidator,
    NSFWContractWriter,
    create_nsfw_batch_summary,
    process_nsfw_batch_with_validation
)


# ============================================================
# 2. NOUVELLE FONCTION: write_nsfw_contracts_with_batch_action()
# (Remplace ou améliore write_nsfw_contracts_action())
# ============================================================

def write_nsfw_contracts_with_batch_action():
    """
    Écrit les contrats NSFW en traitant par lots avec validation
    """
    # Récupérer les résultats
    source_items = state.nsfw_all_results if state.nsfw_all_results else state.nsfw_results
    
    if not source_items:
        return ui.notify("Aucun résultat NSFW à valider.", type='warning')
    
    # Configuration
    batch_size = int(getattr(state, 'nsfw_batch_size_config', 10))
    skip_existing = bool(getattr(state, 'nsfw_skip_existing_config', True))
    
    # 1. Créer les lots
    state.add_log("📋 Analyse des résultats et création des lots...")
    report, batches = create_nsfw_batch_summary(
        source_items,
        batch_size=batch_size,
        skip_existing=skip_existing
    )
    
    # 2. Afficher le rapport récapitulatif
    state.add_log(report)
    
    if not batches:
        return ui.notify("Aucun fichier à traiter (tous ont des contrats existants ou sont invalides)", type='info')
    
    # 3. Initialiser le traitement
    writer = NSFWContractWriter(state.nsfw_model, state.nsfw_threshold)
    
    total_success = 0
    total_skipped = 0
    total_errors = 0
    
    # 4. Traiter chaque lot
    state.add_log("\n🚀 Début du traitement par lot...\n")
    
    for batch_idx, batch in enumerate(batches, 1):
        total_items = len(batch)
        state.add_log(f"{'='*70}")
        state.add_log(f"📦 LOT {batch_idx}/{len(batches)} — {total_items} fichier(s)")
        state.add_log(f"{'='*70}")
        
        # Traiter le lot
        results = process_nsfw_batch_with_validation(
            batch,
            state.nsfw_model,
            state.nsfw_threshold,
            skip_if_exists=skip_existing,
            log_callback=state.add_log
        )
        
        # Accumuler les statistiques
        total_success += results['success']
        total_skipped += results['skipped']
        total_errors += results['errors']
        
        # Afficher les détails du lot
        state.add_log(f"\n📊 Lot {batch_idx} résultats:")
        state.add_log(f"  ✅ Écrits: {results['success']}")
        state.add_log(f"  ⏭️  Ignorés: {results['skipped']}")
        state.add_log(f"  ❌ Erreurs: {results['errors']}")
        
        if results['details']:
            state.add_log(f"\n  Détails:")
            for fname, status, msg in results['details'][:5]:  # Afficher les 5 premiers
                icon = "✓" if status == "SUCCESS" else "⊘" if status == "SKIPPED" else "✗"
                state.add_log(f"    {icon} {fname}: {msg}")
            
            if len(results['details']) > 5:
                state.add_log(f"    ... et {len(results['details']) - 5} autre(s)")
    
    # 5. Afficher le résumé final
    state.add_log("\n" + "="*70)
    state.add_log("✨ TRAITEMENT TERMINÉ ✨")
    state.add_log("="*70)
    state.add_log(f"""
╔══════════════════════════════════════════════════════════════╗
║                 RÉSUMÉ FINAL DU TRAITEMENT                  ║
╠══════════════════════════════════════════════════════════════╣
║  ✅ Contrats écrits:        {total_success:>4} fichier(s)                    ║
║  ⏭️  Contrats ignorés:        {total_skipped:>4} fichier(s) (existants)        ║
║  ❌ Erreurs:                 {total_errors:>4} fichier(s)                    ║
║  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ║
║  📋 Total lots traités:      {len(batches):>4} lot(s)                       ║
║  📁 Total fichiers:          {total_success + total_skipped + total_errors:>4} fichier(s)                  ║
╚══════════════════════════════════════════════════════════════╝
""")
    
    # 6. Notification utilisateur
    if total_errors == 0:
        msg = f"✅ Contrats écrits: {total_success} | Ignorés: {total_skipped}"
        ui.notify(msg, type='positive', position='top')
    else:
        msg = f"⚠️ Contrats: {total_success} | Ignorés: {total_skipped} | Erreurs: {total_errors}"
        ui.notify(msg, type='warning', position='top')


# ============================================================
# 3. CODE UI À AJOUTER DANS LA SECTION NSFW
# (Ajouter aux paramètres de la section "NSFW Detector")
# ============================================================

# Ajouter après les paramètres existants du NSFW (vers la ligne 3038 dans media_mind_ai.py):

# --- SECTION: PARAMÈTRES DE TRAITEMENT PAR LOT ---
with ui.expansion('Paramètres de lot (batching)', icon='tune').classes('w-full bg-gray-800/50 rounded-lg border border-gray-700 mt-4'):
    with ui.row().classes('w-full gap-2 px-2 pt-2'):
        nsfw_batch_size_input = ui.number(
            'Taille du lot',
            value=cfg.get('nsfw_batch_size', 10),
            min=1,
            max=100,
            step=1,
            format='%.0f'
        ).classes('w-[45%]').tooltip('Nombre de fichiers à traiter par lot (1-100)')
        
        nsfw_skip_existing_check = ui.checkbox(
            'Ignorer contrats existants',
            value=cfg.get('nsfw_skip_existing', True)
        ).classes('w-[45%] text-sm').tooltip('Ne pas réécrire les contrats déjà existants')
    
    ui.label('Traiter les résultats NSFW en petits lots pour optimiser la mémoire').classes('text-xs text-gray-400 px-2 pb-2')


# ============================================================
# 4. BOUTON D'ACTION MODIFIÉ
# (Remplacer l'ancien bouton de contrats)
# ============================================================

# Dans le row avec les boutons d'action NSFW, remplacer:
# ui.button('Écrire contrats', ...).props(...)

# Par:
btn_write_contracts = ui.button(
    '📝 Écrire contrats (par lot)',
    on_click=write_nsfw_contracts_with_batch_action
).classes('w-full bg-green-700 hover:bg-green-600 font-bold text-lg').tooltip('Écrit les contrats de validation par lot')


# ============================================================
# 5. SAUVEGARDE DE LA CONFIGURATION
# (À ajouter dans la fonction save_global_settings() ou équivalent)
# ============================================================

def save_nsfw_batch_settings():
    """Sauvegarde les paramètres de lot NSFW"""
    state.nsfw_batch_size_config = int(nsfw_batch_size_input.value)
    state.nsfw_skip_existing_config = nsfw_skip_existing_check.value
    
    save_config({
        'nsfw_batch_size': state.nsfw_batch_size_config,
        'nsfw_skip_existing': state.nsfw_skip_existing_config,
        # ... autres paramètres ...
    })


# ============================================================
# 6. FONCTION AVANCÉE: Traiter seulement les EXPLICITE
# ============================================================

def write_nsfw_contracts_explicite_only():
    """Écrit les contrats seulement pour les fichiers EXPLICITE"""
    source_items = state.nsfw_all_results if state.nsfw_all_results else state.nsfw_results
    
    # Filtrer seulement EXPLICITE
    explicite_only = [
        (d, p, l, dt) for d, p, l, dt in source_items
        if l == 'EXPLICITE'
    ]
    
    if not explicite_only:
        return ui.notify("Aucun fichier EXPLICITE trouvé", type='info')
    
    state.add_log(f"🔴 {len(explicite_only)} fichier(s) EXPLICITE à traiter")
    
    # Traiter comme d'habitude
    batch_size = int(getattr(state, 'nsfw_batch_size_config', 10))
    report, batches = create_nsfw_batch_summary(explicite_only, batch_size=batch_size)
    state.add_log(report)
    
    # ... continuer avec le traitement standard ...


# ============================================================
# 7. FONCTION AVANCÉE: Lister les contrats existants
# ============================================================

def list_and_verify_nsfw_contracts():
    """Vérifie et liste tous les contrats NSFW existants"""
    import os
    
    nsfw_dir = state.nsfw_base_dir or "."
    validator = NSFWContractValidator()
    
    contracts_found = []
    
    for root, dirs, files in os.walk(nsfw_dir):
        for file in files:
            if file.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff')):
                full_path = os.path.join(root, file)
                
                if validator.contract_exists(full_path):
                    is_validated, tier = validator.is_contract_validated(full_path)
                    contracts_found.append({
                        'file': file,
                        'path': full_path,
                        'validated': is_validated,
                        'tier': tier
                    })
    
    # Afficher rapport
    state.add_log(f"\n📋 Contrats trouvés: {len(contracts_found)}")
    
    tier_counts = {}
    for contract_info in contracts_found:
        tier = contract_info['tier']
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        
        icon = "🟢" if tier == "SAIN" else "🟡" if tier == "SENSUEL" else "🔴"
        state.add_log(f"  {icon} {contract_info['file']} → {tier}")
    
    state.add_log(f"\n📊 Distribution:")
    for tier, count in sorted(tier_counts.items()):
        state.add_log(f"  {tier}: {count}")
    
    return contracts_found


# ============================================================
# 8. EXEMPLE: TRAITER UN LOT SPÉCIFIQUE
# ============================================================

async def process_specific_batch_ui():
    """Exemple: interface pour traiter un lot spécifique"""
    
    with ui.dialog() as batch_dialog:
        with ui.card().classes('w-[500px] max-w-full bg-gray-900'):
            ui.label('Sélectionner un lot à traiter').classes('text-lg font-bold')
            
            batch_size_select = ui.number('Taille du lot', value=10, min=1, max=50).classes('w-full')
            start_idx_select = ui.number('Démarrer à l\'index', value=0, min=0).classes('w-full')
            
            async def process_batch():
                source_items = state.nsfw_results
                batch_size = int(batch_size_select.value)
                start_idx = int(start_idx_select.value)
                
                batch_start = start_idx
                batch_end = start_idx + batch_size
                batch = source_items[batch_start:batch_end]
                
                if not batch:
                    ui.notify("Lot vide", type='warning')
                    return
                
                state.add_log(f"Traitement du lot personnalisé ({len(batch)} fichiers)...")
                writer = NSFWContractWriter(state.nsfw_model, state.nsfw_threshold)
                results = writer.write_batch_contracts(batch, skip_if_exists=True)
                
                ui.notify(f"✅ {results['success']} | ⏭️ {results['skipped']} | ❌ {results['errors']}", type='positive')
                batch_dialog.close()
            
            ui.button('Traiter ce lot', on_click=process_batch).classes('w-full')
            ui.button('Annuler', on_click=batch_dialog.close).props('outline').classes('w-full mt-2')
        
        batch_dialog.open()


# ============================================================
# 9. HELPER ASYNC POUR STATISTIQUES
# ============================================================

async def show_nsfw_batch_statistics():
    """Affiche les statistiques des lots NSFW"""
    
    source_items = state.nsfw_all_results or state.nsfw_results
    
    if not source_items:
        ui.notify("Aucun résultat", type='info')
        return
    
    batch_size = int(getattr(state, 'nsfw_batch_size_config', 10))
    processor = NSFWBatchProcessor(batch_size=batch_size)
    batches = processor.split_results_into_batches(source_items, skip_existing=True)
    
    state.add_log("""
╔════════════════════════════════════════════════════════════════════╗
║                  STATISTIQUES DES LOTS                            ║
╚════════════════════════════════════════════════════════════════════╝
""")
    
    for batch_idx, batch in enumerate(batches, 1):
        info = processor.get_batch_info(batch)
        state.add_log(f"\\nLot {batch_idx}:")
        state.add_log(f"  📁 Fichiers: {info['total_files']}")
        state.add_log(f"  📊 Danger moyen: {info['avg_danger']*100:.1f}%")
        state.add_log(f"  🔴 Danger max: {info['max_danger']*100:.1f}%")
        
        for tier, count in sorted(info['tier_distribution'].items()):
            state.add_log(f"  - {tier}: {count}")
