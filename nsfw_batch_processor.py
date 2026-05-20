"""
NSFW Batch Processing with Contract Validation
Processeur NSFW par lot avec validation de contrats
"""

import os
import json
import datetime
from pathlib import Path
from typing import List, Tuple, Dict, Optional


class NSFWContractValidator:
    """Valide l'existence de contrats NSFW avant traitement"""
    
    @staticmethod
    def contract_exists(photo_path: str) -> bool:
        """Vérifie si un contrat de validation existe déjà pour une photo"""
        try:
            photo_path = Path(photo_path)
            contract_path = photo_path.with_name(f"{photo_path.stem}_validation.json")
            return contract_path.exists()
        except Exception:
            return False
    
    @staticmethod
    def get_contract_path(photo_path: str) -> Path:
        """Retourne le chemin du contrat pour une photo"""
        photo_path = Path(photo_path)
        return photo_path.with_name(f"{photo_path.stem}_validation.json")
    
    @staticmethod
    def read_contract(photo_path: str) -> Optional[Dict]:
        """Lit le contenu d'un contrat existant"""
        try:
            contract_path = NSFWContractValidator.get_contract_path(photo_path)
            if contract_path.exists():
                with open(contract_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return None
    
    @staticmethod
    def is_contract_validated(photo_path: str) -> Tuple[bool, Optional[str]]:
        """
        Vérifie si le contrat est déjà validé (statut)
        Returns: (is_validated, tier_or_error)
        """
        contract = NSFWContractValidator.read_contract(photo_path)
        if contract and 'result' in contract:
            tier = contract['result'].get('tier')
            return True, tier
        return False, None


class NSFWBatchProcessor:
    """Processeur NSFW par lot avec validation de contrats"""
    
    def __init__(self, batch_size: int = 10):
        """
        Args:
            batch_size: Nombre d'éléments à traiter par lot
        """
        self.batch_size = batch_size
        self.validator = NSFWContractValidator()
    
    def split_results_into_batches(
        self,
        nsfw_results: List[Tuple],
        skip_existing: bool = True
    ) -> List[List[Tuple]]:
        """
        Divise les résultats NSFW en petits lots
        
        Args:
            nsfw_results: Liste des résultats (danger, path, label, details)
            skip_existing: Si True, ignore les fichiers avec contrats existants
        
        Returns:
            Liste de lots, chaque lot contient batch_size éléments
        """
        filtered_results = []
        
        for item in nsfw_results:
            danger, path, label, details = item
            
            # Ignorer les fichiers temp
            if path.lower().endswith('.png') and Path(path).name.startswith('tmp'):
                continue
            
            # Optionnel: ignorer les contrats existants
            if skip_existing and self.validator.contract_exists(path):
                continue
            
            filtered_results.append(item)
        
        # Diviser en lots
        batches = []
        for i in range(0, len(filtered_results), self.batch_size):
            batch = filtered_results[i:i + self.batch_size]
            batches.append(batch)
        
        return batches
    
    def get_batch_info(self, batch: List[Tuple]) -> Dict:
        """Obtient des informations sur un lot"""
        total_files = len(batch)
        danger_scores = [item[0] for item in batch]
        avg_danger = sum(danger_scores) / len(danger_scores) if danger_scores else 0
        max_danger = max(danger_scores) if danger_scores else 0
        
        tier_counts = {}
        for item in batch:
            tier = item[2]  # label
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        
        return {
            'total_files': total_files,
            'avg_danger': avg_danger,
            'max_danger': max_danger,
            'tier_distribution': tier_counts
        }
    
    def generate_batch_report(self, batches: List[List[Tuple]]) -> str:
        """Génère un rapport texte sur tous les lots"""
        lines = [
            "╔════════════════════════════════════════════════════════════════════╗",
            "║              RAPPORT DE TRAITEMENT NSFW PAR LOT                   ║",
            "╚════════════════════════════════════════════════════════════════════╝",
            ""
        ]
        
        total_files = sum(len(batch) for batch in batches)
        lines.append(f"📊 Total des lots: {len(batches)}")
        lines.append(f"📁 Total des fichiers: {total_files}")
        lines.append("")
        
        for batch_idx, batch in enumerate(batches, 1):
            info = self.get_batch_info(batch)
            
            lines.append(f"┌─ Lot {batch_idx} (sur {len(batches)}) ─────────────────────────────────────────┐")
            lines.append(f"│  📁 Fichiers: {info['total_files']}")
            lines.append(f"│  ⚠️  Danger moyen: {info['avg_danger']*100:.1f}%")
            lines.append(f"│  🔴 Danger max: {info['max_danger']*100:.1f}%")
            lines.append(f"│  Distribution:")
            
            for tier, count in sorted(info['tier_distribution'].items()):
                icon = "🟢" if tier == "SAIN" else "🟡" if tier == "SENSUEL" else "🔴"
                lines.append(f"│    {icon} {tier}: {count} fichier(s)")
            
            lines.append(f"└────────────────────────────────────────────────────────────────────────────────┘")
            lines.append("")
        
        return "\n".join(lines)


class NSFWContractWriter:
    """Écrit les contrats de validation NSFW"""
    
    def __init__(self, model_name: str, threshold: float):
        self.model_name = model_name
        self.threshold = threshold
    
    def write_contract(
        self,
        photo_path: str,
        danger: float,
        tier: str,
        details: Dict,
        skip_if_exists: bool = True
    ) -> Tuple[bool, str]:
        """
        Écrit un contrat de validation pour une photo
        
        Returns:
            (success, message)
        """
        try:
            photo_path_obj = Path(photo_path)
            
            # Vérifier si on doit ignorer les fichiers existants
            if skip_if_exists and NSFWContractValidator.contract_exists(photo_path):
                return True, f"Contrat existe déjà"
            
            # Préparer le contenu du contrat
            numeric_details = {}
            for k, v in details.items():
                if k.startswith('_'):
                    continue
                try:
                    numeric_details[k] = float(v)
                except (ValueError, TypeError):
                    numeric_details[k] = str(v)
            
            payload = {
                "schema": "organizador.nsfw.validation.v1",
                "validated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source_file": str(photo_path_obj),
                "file_name": photo_path_obj.name,
                "result": {
                    "tier": tier,
                    "danger": float(danger),
                    "model": self.model_name,
                    "raw_top_label": str(details.get('_raw_top_label', tier)),
                    "explicit_threshold": float(self.threshold),
                    "details": numeric_details,
                },
            }
            
            # Écrire le contrat
            contract_path = NSFWContractValidator.get_contract_path(photo_path)
            with open(contract_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            
            return True, f"Contrat écrit"
        
        except Exception as e:
            return False, f"Erreur: {str(e)}"
    
    def write_batch_contracts(
        self,
        batch: List[Tuple],
        skip_if_exists: bool = True
    ) -> Dict:
        """
        Écrit les contrats pour un lot complet
        
        Returns:
            {
                'success': int,
                'skipped': int,
                'errors': int,
                'details': [(path, status, message), ...]
            }
        """
        results = {
            'success': 0,
            'skipped': 0,
            'errors': 0,
            'details': []
        }
        
        for danger, path, tier, details in batch:
            try:
                # Ignorer les fichiers non-image
                if not path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff')):
                    continue
                
                success, message = self.write_contract(
                    path,
                    danger,
                    tier,
                    details,
                    skip_if_exists=skip_if_exists
                )
                
                if success:
                    if "existe déjà" in message:
                        results['skipped'] += 1
                        status = "SKIPPED"
                    else:
                        results['success'] += 1
                        status = "SUCCESS"
                else:
                    results['errors'] += 1
                    status = "ERROR"
                
                results['details'].append((Path(path).name, status, message))
            
            except Exception as e:
                results['errors'] += 1
                results['details'].append((Path(path).name if isinstance(path, str) else "?", "ERROR", str(e)))
        
        return results


# ============================================================
# HELPER FUNCTIONS FOR UI INTEGRATION
# ============================================================

def create_nsfw_batch_summary(
    all_results: List[Tuple],
    batch_size: int = 10,
    skip_existing: bool = True
) -> Tuple[str, List[List[Tuple]]]:
    """
    Crée un résumé et divise en lots
    
    Returns:
        (summary_text, batches)
    """
    processor = NSFWBatchProcessor(batch_size=batch_size)
    batches = processor.split_results_into_batches(all_results, skip_existing=skip_existing)
    report = processor.generate_batch_report(batches)
    return report, batches


def process_nsfw_batch_with_validation(
    batch: List[Tuple],
    model_name: str,
    threshold: float,
    skip_if_exists: bool = True,
    log_callback=None
) -> Dict:
    """
    Traite un lot NSFW et écrit les contrats avec validation
    
    Args:
        batch: Lot d'éléments NSFW
        model_name: Nom du modèle NSFW utilisé
        threshold: Seuil NSFW
        skip_if_exists: Ignorer si le contrat existe
        log_callback: Fonction pour logger les messages
    
    Returns:
        Résultats du traitement
    """
    writer = NSFWContractWriter(model_name, threshold)
    results = writer.write_batch_contracts(batch, skip_if_exists=skip_if_exists)
    
    if log_callback:
        log_callback(f"[BATCH] Succès: {results['success']} | Ignorés: {results['skipped']} | Erreurs: {results['errors']}")
    
    return results
