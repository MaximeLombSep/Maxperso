---
name: enveloppe-import-releves
description: Chaîne d'import des relevés bancaires de l'add-on Enveloppe — CSV, OFX/QFX et PDF — puis catégorisation automatique et rapprochement. À utiliser dès qu'une tâche touche `services/importer.py`, `pdf_import.py`, `categorizer.py`, `reconcile.py` ou `money.py` : nouveau format de banque, doublon mal détecté, montant mal lu, règle de catégorisation, écart de rapprochement.
---

# Import de relevés, catégorisation, rapprochement

## Le principe qui gouverne tout le reste

**L'application ne se connecte à aucune banque.** L'utilisateur dépose un fichier qu'il a
lui-même téléchargé. Aucun identifiant bancaire n'est demandé, stocké ni transmis. Toute
proposition d'agrégation bancaire, de scraping ou d'API tierce sort du périmètre du projet —
le dire plutôt que l'implémenter.

## Les trois formats, par ordre de fiabilité

1. **OFX / QFX** — format d'échange, typé, avec identifiant d'opération fourni par la banque.
   À privilégier quand la banque le propose.
2. **CSV** — le cas courant. Le format varie d'une banque à l'autre : séparateur, encodage,
   ordre des colonnes, séparateur décimal, format de date, colonnes débit/crédit séparées ou
   montant signé unique. D'où le mécanisme de **profil** : `preview_csv` détecte
   (`detect_delimiter`, `find_header_row`, `guess_mapping`, `detect_decimal_sep`,
   `detect_date_format`), l'utilisateur confirme, le profil est mémorisé pour la fois suivante.
3. **PDF** — dernier recours. Un PDF est une mise en page, pas un format d'échange : les
   colonnes « débit » et « crédit » n'y existent que par leur position à l'écran.
   `pdf_import.py` s'appuie sur l'extraction *layout* de pypdf, repère la frontière entre les
   colonnes de montant, classe chaque montant selon sa position, rattache les lignes de
   continuation, et reconstitue l'année depuis la période du relevé. C'est fiable sur les
   relevés à colonnes séparées, faillible sur les mises en page exotiques : **l'écran de
   vérification affichant le total lu n'est pas une commodité, c'est le garde-fou**. Ne jamais
   le contourner ni insérer sans confirmation.

## Détection de doublon : deux filets, pas un

- **`fingerprint`** — empreinte déterministe (compte, date, montant, libellé normalisé). Un
  même fichier réimporté ne crée rien. C'est le filet exact.
- **`find_near_duplicate`** — similarité de libellé (`difflib`) sur une fenêtre de dates,
  pour l'opération déjà présente sous une formulation légèrement différente (saisie manuelle
  puis import, ou relevé PDF puis CSV du même mois). C'est le filet approché : il **propose**,
  il ne supprime pas.

Un doublon signalé se résout par l'écran de vérification, jamais par une suppression
silencieuse. Toucher à la fenêtre de rapprochement ou au seuil de similarité demande de
vérifier les deux risques symétriques : le doublon inséré, et l'opération légitime écartée.

`detect_transfers` reconnaît les virements internes (un débit et un crédit de même montant sur
deux comptes, à quelques jours d'écart) pour ne pas les compter comme dépense puis comme
revenu.

## Montants : `services/money.py` et rien d'autre

`parse_amount` tolère les formats bancaires français : `1 234,56`, `-1.234,56`, `(45,00)` pour
un débit entre parenthèses, `45,00 €`, `+12,30`. Il retourne des **centimes entiers**, ou
`None` si la cellule est vide ou illisible — `None` n'est pas zéro, et une ligne illisible se
signale, elle ne s'invente pas.

Un nouveau format bancaire s'ajoute ici, avec le cas réel qui l'a motivé, jamais par un
traitement particulier dans le routeur ou l'importeur.

## Catégorisation : règles explicites + apprentissage

Deux mécanismes se complètent, dans cet ordre :

1. Les **règles** (table `rules`), triées par priorité, appliquées sur le libellé normalisé
   et, si la règle le précise, sur le montant.
2. L'**apprentissage** : chaque affectation manuelle propose une règle dérivée du libellé
   (`suggest_pattern`), **que l'utilisateur valide**. Le projet ne crée jamais une règle dans
   le dos de l'utilisateur.

`normalize_label` retire le bruit propre aux libellés bancaires français (dates de facturation,
numéros de carte, codes d'autorisation) : ce bruit n'aide pas à identifier le commerçant et
fait échouer les rapprochements s'il reste. Ajouter un motif de bruit se fait dans
`_NOISE_PATTERNS`, avec l'exemple de libellé réel en commentaire.

Après modification des règles, `recategorize_all` rejoue l'ensemble : ne pas oublier de
l'appeler, sinon l'historique reste catégorisé selon les anciennes règles.

## Rapprochement : le geste qui rend le budget crédible

Trois notions, à ne pas confondre dans le code ni dans l'interface :

- **pointée** : l'opération a été retrouvée sur le relevé de la banque ;
- **solde pointé** : solde d'ouverture + les seules opérations pointées — c'est ce que la
  banque devrait afficher ;
- **écart** : différence entre ce solde et celui du relevé. Zéro clôt le rapprochement.

Un écart non nul se résout par un **ajustement proposé**, jamais imposé automatiquement :
l'écart vient d'une opération oubliée, d'un import incomplet ou d'une erreur de saisie, et
c'est à l'utilisateur de trancher lequel.

## Vérifier une modification de cette chaîne

Aucun jeu de test versionné (les relevés sont des données personnelles). Pour une
modification d'import : construire un CSV ou un PDF synthétique reproduisant le cas, le passer
par la prévisualisation, et comparer le **total lu** et le **nombre de lignes** avec la source.
Ne jamais valider une modification d'import sur la seule absence d'exception.
