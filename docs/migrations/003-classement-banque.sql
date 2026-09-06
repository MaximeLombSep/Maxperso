-- Migration 003 — classement fourni par la banque
--
-- À N'EXÉCUTER QUE SUR UNE BASE DÉJÀ EN SERVICE, créée avant que le lecteur
-- n'exploite les colonnes de rubrique des exports bancaires. Une installation
-- neuve crée le schéma complet au premier démarrage : cette migration lui est
-- inutile.
--
-- Le script n'est lancé par personne automatiquement. Sauvegardez d'abord le
-- fichier de base (`enveloppe.db`, `enveloppe.db-wal`, `enveloppe.db-shm`),
-- application arrêtée, puis exécutez :
--
--     sqlite3 /chemin/vers/data/enveloppe.db < docs/migrations/003-classement-banque.sql
--
-- Les deux colonnes ajoutées ont une valeur par défaut vide : aucune donnée
-- existante n'est modifiée, aucune ligne supprimée. Un profil d'import déjà
-- enregistré continue de fonctionner sans rubrique, exactement comme avant.

BEGIN TRANSACTION;

-- Colonnes du fichier portant le classement de la banque. Vides tant que
-- l'utilisateur n'a pas réimporté un relevé qui en contient.
ALTER TABLE import_profiles ADD COLUMN col_category VARCHAR(80) NOT NULL DEFAULT '';
ALTER TABLE import_profiles ADD COLUMN col_subcategory VARCHAR(80) NOT NULL DEFAULT '';

-- Rubrique conservée sur l'opération : sans elle, un reclassement ultérieur
-- devrait redemander les fichiers de relevé.
ALTER TABLE transactions ADD COLUMN bank_category VARCHAR(80) NOT NULL DEFAULT '';
ALTER TABLE transactions ADD COLUMN bank_subcategory VARCHAR(80) NOT NULL DEFAULT '';

COMMIT;

-- Vérification après exécution (doit renvoyer les deux colonnes) :
--   SELECT name FROM pragma_table_info('import_profiles')
--    WHERE name IN ('col_category', 'col_subcategory');
--   SELECT name FROM pragma_table_info('transactions')
--    WHERE name IN ('bank_category', 'bank_subcategory');
