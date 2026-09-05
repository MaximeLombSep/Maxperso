-- Migration 001 — suivi des cartes à débit différé
--
-- À N'EXÉCUTER QUE SUR UNE BASE DÉJÀ EN SERVICE, créée avant l'ajout du
-- suivi des encours de carte. Une installation neuve crée le schéma complet
-- au premier démarrage : cette migration lui est inutile.
--
-- Le script n'est lancé par personne automatiquement. Sauvegardez d'abord le
-- fichier de base (`enveloppe.db`, `enveloppe.db-wal`, `enveloppe.db-shm`),
-- application arrêtée, puis exécutez :
--
--     sqlite3 /chemin/vers/data/enveloppe.db < docs/migrations/001-cartes-differees.sql
--
-- Les six colonnes ajoutées sont nullables ou pourvues d'une valeur par
-- défaut : aucune donnée existante n'est modifiée, aucune ligne supprimée.

BEGIN TRANSACTION;

-- Paramètres du cycle de facturation, portés par le compte de type « credit ».
ALTER TABLE accounts ADD COLUMN settlement_account_id INTEGER
    REFERENCES accounts (id) ON DELETE SET NULL;
ALTER TABLE accounts ADD COLUMN cutoff_day INTEGER NOT NULL DEFAULT 0;
ALTER TABLE accounts ADD COLUMN settlement_day INTEGER NOT NULL DEFAULT 0;

-- Rattachement d'un achat de carte au prélèvement qui l'a soldé.
ALTER TABLE transactions ADD COLUMN settlement_id INTEGER
    REFERENCES transactions (id) ON DELETE SET NULL;

-- Côté prélèvement : la carte soldée et le cycle concerné.
ALTER TABLE transactions ADD COLUMN settles_account_id INTEGER
    REFERENCES accounts (id) ON DELETE SET NULL;
ALTER TABLE transactions ADD COLUMN settles_period VARCHAR(7);

CREATE INDEX IF NOT EXISTS ix_tx_settlement ON transactions (settlement_id);

COMMIT;

-- Vérification après exécution (doit renvoyer les six colonnes) :
--   SELECT name FROM pragma_table_info('accounts')
--    WHERE name IN ('settlement_account_id', 'cutoff_day', 'settlement_day');
--   SELECT name FROM pragma_table_info('transactions')
--    WHERE name IN ('settlement_id', 'settles_account_id', 'settles_period');
