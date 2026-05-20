"""
TEST SUITE: nsfw_batch_processor.py

Script de test complet pour valider le module avant intégration
Run: python test_nsfw_batch_processor.py
"""

import os
import json
import tempfile
from pathlib import Path
from nsfw_batch_processor import (
    NSFWContractValidator,
    NSFWBatchProcessor,
    NSFWContractWriter,
    create_nsfw_batch_summary,
    process_nsfw_batch_with_validation
)


# ============================================================
# TEST DATA
# ============================================================

def create_test_data():
    """Crée des données de test"""
    return [
        (0.12, "/photos/safe_photo1.jpg", "SAIN", {"safe": 0.87, "normal": 0.08}),
        (0.45, "/photos/explicit_photo1.jpg", "EXPLICITE", {"explicit": 0.55, "suggestive": 0.30}),
        (0.35, "/photos/sensual_photo1.jpg", "SENSUEL", {"sensual": 0.65, "safe": 0.20}),
        (0.08, "/photos/safe_photo2.jpg", "SAIN", {"safe": 0.92}),
        (0.78, "/photos/explicit_photo2.jpg", "EXPLICITE", {"explicit": 0.82}),
        (0.25, "/photos/sensual_photo2.jpg", "SENSUEL", {"sensual": 0.50}),
        (0.15, "/photos/safe_photo3.jpg", "SAIN", {"safe": 0.85}),
        (0.55, "/photos/explicit_photo3.jpg", "EXPLICITE", {"explicit": 0.65}),
        (0.32, "/photos/sensual_photo3.jpg", "SENSUEL", {"sensual": 0.58}),
        (0.10, "/photos/safe_photo4.jpg", "SAIN", {"safe": 0.90}),
    ]


# ============================================================
# TEST 1: NSFWBatchProcessor
# ============================================================

def test_batch_processor():
    """Test: Création et division en lots"""
    print("\n" + "="*70)
    print("TEST 1: NSFWBatchProcessor")
    print("="*70)
    
    test_data = create_test_data()
    processor = NSFWBatchProcessor(batch_size=3)
    
    # Test split
    batches = processor.split_results_into_batches(test_data)
    print(f"✅ {len(batches)} lot(s) créé(s) pour {len(test_data)} fichiers")
    print(f"   Tailles: {[len(b) for b in batches]}")
    
    # Test batch info
    for i, batch in enumerate(batches):
        info = processor.get_batch_info(batch)
        print(f"\n📦 Lot {i+1}:")
        print(f"   - Fichiers: {info['total_files']}")
        print(f"   - Danger moyen: {info['avg_danger']*100:.1f}%")
        print(f"   - Danger max: {info['max_danger']*100:.1f}%")
        print(f"   - Distribution: {info['tier_distribution']}")
    
    # Test report
    report = processor.generate_batch_report(batches)
    print(f"\n📋 Rapport généré ({len(report)} caractères)")
    print(report)
    
    return True


# ============================================================
# TEST 2: NSFWContractValidator
# ============================================================

def test_contract_validator():
    """Test: Validation et lecture de contrats"""
    print("\n" + "="*70)
    print("TEST 2: NSFWContractValidator")
    print("="*70)
    
    validator = NSFWContractValidator()
    
    # Créer un fichier temporaire de test
    with tempfile.TemporaryDirectory() as tmpdir:
        test_photo = Path(tmpdir) / "test_photo.jpg"
        test_photo.touch()  # Créer le fichier
        
        print(f"📁 Fichier de test: {test_photo}")
        
        # Test 1: contract_exists (avant création)
        exists = validator.contract_exists(str(test_photo))
        print(f"✅ contract_exists (avant): {exists} (attendu: False)")
        assert not exists, "Le contrat ne devrait pas exister"
        
        # Test 2: get_contract_path
        contract_path = validator.get_contract_path(str(test_photo))
        print(f"✅ get_contract_path: {contract_path}")
        assert str(contract_path).endswith("_validation.json"), "Mauvais format"
        
        # Test 3: Créer un contrat de test
        test_contract = {
            "schema": "organizador.nsfw.validation.v1",
            "result": {
                "tier": "SAIN",
                "danger": 0.12
            }
        }
        
        with open(contract_path, 'w') as f:
            json.dump(test_contract, f)
        
        # Test 4: contract_exists (après création)
        exists = validator.contract_exists(str(test_photo))
        print(f"✅ contract_exists (après): {exists} (attendu: True)")
        assert exists, "Le contrat devrait exister"
        
        # Test 5: read_contract
        read_contract = validator.read_contract(str(test_photo))
        print(f"✅ read_contract: {read_contract['result']['tier']}")
        assert read_contract['result']['tier'] == "SAIN", "Erreur lecture contrat"
        
        # Test 6: is_contract_validated
        is_validated, tier = validator.is_contract_validated(str(test_photo))
        print(f"✅ is_contract_validated: ({is_validated}, {tier})")
        assert is_validated and tier == "SAIN", "Erreur validation"
        
        print("\n✅ Tous les tests du validateur réussis!")
        return True


# ============================================================
# TEST 3: NSFWContractWriter
# ============================================================

def test_contract_writer():
    """Test: Écriture de contrats"""
    print("\n" + "="*70)
    print("TEST 3: NSFWContractWriter")
    print("="*70)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = NSFWContractWriter("test-model-v1", 0.42)
        
        # Test 1: Créer un contrat
        test_photo = Path(tmpdir) / "test_photo.jpg"
        test_photo.touch()
        
        success, msg = writer.write_contract(
            str(test_photo),
            danger=0.75,
            tier="EXPLICITE",
            details={"explicit": 0.8, "safe": 0.15}
        )
        
        print(f"✅ write_contract: success={success}, msg='{msg}'")
        assert success, "L'écriture devrait réussir"
        
        # Vérifier que le fichier a été créé
        contract_path = Path(tmpdir) / "test_photo_validation.json"
        assert contract_path.exists(), "Le fichier contrat n'existe pas"
        
        # Vérifier le contenu
        with open(contract_path) as f:
            contract = json.load(f)
        
        print(f"✅ Contrat créé:")
        print(f"   - Schema: {contract['schema']}")
        print(f"   - Tier: {contract['result']['tier']}")
        print(f"   - Danger: {contract['result']['danger']}")
        
        # Test 2: skip_if_exists
        success2, msg2 = writer.write_contract(
            str(test_photo),
            danger=0.50,
            tier="SAIN",
            details={"safe": 0.9},
            skip_if_exists=True
        )
        
        print(f"✅ write_contract (skip_if_exists): success={success2}, msg='{msg2}'")
        assert success2, "Skip devrait retourner success=True"
        
        # Vérifier que le contenu n'a pas changé
        with open(contract_path) as f:
            contract_after = json.load(f)
        
        assert contract_after['result']['tier'] == "EXPLICITE", "Le contrat a été modifié!"
        print(f"✅ Skip fonctionne: le contrat n'a pas été overwrite")
        
        print("\n✅ Tous les tests du writer réussis!")
        return True


# ============================================================
# TEST 4: Batch Writing
# ============================================================

def test_batch_writing():
    """Test: Écriture par lot"""
    print("\n" + "="*70)
    print("TEST 4: Batch Writing")
    print("="*70)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = NSFWContractWriter("test-model-batch", 0.42)
        
        # Créer des fichiers de test
        batch_data = []
        for i in range(5):
            photo_path = Path(tmpdir) / f"photo_{i}.jpg"
            photo_path.touch()
            
            tier = ["SAIN", "SENSUEL", "EXPLICITE"][i % 3]
            danger = [0.1, 0.4, 0.8][i % 3]
            
            batch_data.append((
                danger,
                str(photo_path),
                tier,
                {"test": "data"}
            ))
        
        # Écrire le lot
        results = writer.write_batch_contracts(batch_data, skip_if_exists=False)
        
        print(f"✅ write_batch_contracts:")
        print(f"   - Succès: {results['success']}")
        print(f"   - Ignorés: {results['skipped']}")
        print(f"   - Erreurs: {results['errors']}")
        print(f"   - Total traitements: {len(results['details'])}")
        
        assert results['success'] == 5, "Tous devrait être écrits"
        assert results['skipped'] == 0, "Aucun ne devrait être ignoré"
        assert results['errors'] == 0, "Aucune erreur"
        
        # Vérifier que les fichiers existent
        contract_count = len(list(Path(tmpdir).glob("*_validation.json")))
        print(f"✅ Contrats créés: {contract_count}")
        assert contract_count == 5, "Tous les contrats doivent exister"
        
        # Test skip_if_exists
        results2 = writer.write_batch_contracts(batch_data, skip_if_exists=True)
        print(f"\n✅ Seconde écriture (skip_if_exists):")
        print(f"   - Succès: {results2['success']}")
        print(f"   - Ignorés: {results2['skipped']} (attendu: 5)")
        print(f"   - Erreurs: {results2['errors']}")
        
        assert results2['success'] == 0, "Aucun ne devrait être écrit"
        assert results2['skipped'] == 5, "Tous devraient être ignorés"
        
        print("\n✅ Tous les tests de batch writing réussis!")
        return True


# ============================================================
# TEST 5: Helper Functions
# ============================================================

def test_helper_functions():
    """Test: Fonctions helper"""
    print("\n" + "="*70)
    print("TEST 5: Helper Functions")
    print("="*70)
    
    test_data = create_test_data()
    
    # Test create_nsfw_batch_summary
    summary, batches = create_nsfw_batch_summary(
        test_data,
        batch_size=4,
        skip_existing=False
    )
    
    print(f"✅ create_nsfw_batch_summary:")
    print(f"   - Lots créés: {len(batches)}")
    print(f"   - Résumé généré: {len(summary)} caractères")
    
    assert len(batches) > 0, "Au moins 1 lot devrait exister"
    
    # Test process_nsfw_batch_with_validation
    log_messages = []
    
    def mock_log(msg):
        log_messages.append(msg)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Adapter les données de test avec des vrais chemins
        test_batch = []
        for i in range(3):
            photo = Path(tmpdir) / f"photo_{i}.jpg"
            photo.touch()
            test_batch.append((0.5, str(photo), "EXPLICITE", {}))
        
        results = process_nsfw_batch_with_validation(
            test_batch,
            "test-model",
            0.42,
            skip_if_exists=False,
            log_callback=mock_log
        )
        
        print(f"✅ process_nsfw_batch_with_validation:")
        print(f"   - Résultats: {results['success']} succès")
        print(f"   - Messages de log: {len(log_messages)}")
    
    print("\n✅ Tous les tests helper réussis!")
    return True


# ============================================================
# MAIN TEST RUNNER
# ============================================================

def run_all_tests():
    """Lance tous les tests"""
    
    print("""
╔════════════════════════════════════════════════════════════════════╗
║        TEST SUITE: nsfw_batch_processor.py                        ║
╚════════════════════════════════════════════════════════════════════╝
""")
    
    tests = [
        ("NSFWBatchProcessor", test_batch_processor),
        ("NSFWContractValidator", test_contract_validator),
        ("NSFWContractWriter", test_contract_writer),
        ("Batch Writing", test_batch_writing),
        ("Helper Functions", test_helper_functions),
    ]
    
    results = []
    
    for test_name, test_func in tests:
        try:
            success = test_func()
            results.append((test_name, "✅ PASS"))
        except AssertionError as e:
            results.append((test_name, f"❌ FAIL: {str(e)}"))
        except Exception as e:
            results.append((test_name, f"❌ ERROR: {str(e)}"))
    
    # Résumé final
    print("\n" + "="*70)
    print("RÉSUMÉ DES TESTS")
    print("="*70)
    
    for test_name, status in results:
        print(f"{status:20} | {test_name}")
    
    print("="*70)
    
    passed = sum(1 for _, s in results if "PASS" in s)
    total = len(results)
    
    print(f"\n📊 Résultat: {passed}/{total} tests réussis")
    
    if passed == total:
        print("🎉 TOUS LES TESTS RÉUSSIS! Le module est prêt pour l'intégration.")
        return True
    else:
        print("❌ Certains tests ont échoué. Vérifier les erreurs ci-dessus.")
        return False


if __name__ == "__main__":
    success = run_all_tests()
    exit(0 if success else 1)
