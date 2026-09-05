-- Migration 002 — rapprochement bancaire
--
-- À N'EXÉCUTER QUE SUR UNE BASE DÉJÀ EN SERVICE, créée avant l'ajout du
-- rapprochement. Une installation neuve crée le schéma complet au premier
-- démarrage : cette migration lui est inutile.
--
-- Le script n'est lancé par personne automatiquement. Arrêtez l'add-on,
-- sauvegardez le fichier de base (`enveloppe.db`, `enveloppe.db-wal`,
-- `enveloppe.db-shm`), puis exécutez :
--
--     sqlite3 /chemin/vers/data/enveloppe.db < 002-rapprochement.sql
--
-- Les trois colonnes ajoutées sont nullables ou pourvues d'une valeur par
-- défaut : aucune donnée existante n'est modifiée, aucune ligne supprimée.
-- Toutes les opérations déjà en base démarrent donc « non pointées », ce
-- qui est l'état correct : rien n'a encore été vérifié contre un relevé.

BEGIN TRANSACTION;

-- Repère du dernier rapprochement, porté par le compte.
ALTER TABLE accounts ADD COLUMN reconciled_on DATE;
ALTER TABLE accounts ADD COLUMN reconciled_balance_cents INTEGER NOT NULL DEFAULT 0;

-- Marque « retrouvée sur le relevé », portée par l'opération.
ALTER TABLE transactions ADD COLUMN cleared BOOLEAN NOT NULL DEFAULT 0;

COMMIT;

-- Vérification, à lancer ensuite :
--
--     sqlite3 /chemin/vers/data/enveloppe.db \
--       "SELECT COUNT(*) AS operations, SUM(cleared) AS pointees FROM transactions;"
--
-- La seconde colonne doit valoir 0 : aucune opération n'est pointée tant
-- que vous n'avez pas fait un premier rapprochement depuis l'application.
