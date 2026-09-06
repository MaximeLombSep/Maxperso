---
name: enveloppe-conventions
description: Conventions et invariants de l'add-on Home Assistant « Enveloppe — Budget » (FastAPI + SQLAlchemy + Jinja, SQLite). À lire avant toute modification du code de `enveloppe/` — ajout de route, de service, de colonne, de template, ou changement de configuration. Couvre le découpage routers/services, les montants en centimes, la migration de schéma sous décision explicite, la sécurité (CSRF, CSP, session signée, secrets) et les contraintes de l'ingress Home Assistant.
---

# Enveloppe — Budget : conventions du projet

Add-on Home Assistant, servi derrière l'ingress HA. Python 3 / FastAPI / SQLAlchemy 2.0 /
Jinja2, base SQLite dans `/data`, aucune dépendance front (JS et CSS écrits à la main, PWA).

## Découpage

```
enveloppe/app/
  main.py          middlewares, démarrage, montage des routes
  config.py        Settings — TOUTE valeur configurable passe par ici
  db.py            engine SQLite + PRAGMA (foreign_keys, WAL)
  models.py        modèles SQLAlchemy
  schema_check.py  constat d'écart de schéma, sans jamais y toucher
  security.py      hachage scrypt, session signée, CSRF
  routers/         HTTP : validation d'entrée, réponse, redirection
  services/        logique métier, testable sans requête HTTP
  templates/       Jinja
```

**La règle de découpage** : un routeur ne contient pas de règle de gestion. Il lit la requête,
appelle un service, rend un template. Toute logique qui mériterait un test unitaire va dans
`services/`. Un service ne connaît ni `Request`, ni `Response`, ni template.

## Invariants à ne jamais casser

### Les montants sont des entiers de centimes

Jamais de `float` pour de l'argent, nulle part : ni en base, ni en calcul, ni en paramètre de
fonction. `services/money.py` fait la conversion depuis le texte (`parse_amount`) et vers
l'affichage. Une somme de centimes reste un `int`. Le formatage n'intervient qu'au dernier
moment, dans le template.

### Le schéma ne se modifie pas tout seul

`create_all` crée les tables absentes, il n'ajoute **pas** une colonne à une table existante.
`schema_check.py` constate l'écart et coupe l'application avec une page qui explique quoi
faire — c'est délibéré : une page claire vaut mieux qu'une pile d'erreurs SQL sur chaque
écran. L'application des colonnes manquantes reste derrière l'option `apply_migrations`,
désactivée par défaut, et suppose une sauvegarde préalable.

Ajouter une colonne au modèle implique donc : le champ dans `models.py`, la note dans
`docs/migrations/`, et la vérification que `schema_check` la détecte. Ne jamais contourner ce
mécanisme en écrivant un `ALTER TABLE` au démarrage.

### La configuration vient de l'environnement

Toute valeur ajustable est un champ de `Settings` alimenté par une variable `BUDGET_*`, avec
une valeur par défaut sûre, et remontée dans `config.yaml` de l'add-on si l'utilisateur doit
pouvoir la changer. Aucune constante de configuration éparpillée dans le code.

### Aucun secret en clair

`BUDGET_SECRET_KEY` par l'environnement, ou clé générée une fois dans `/data/secret.key` en
0600. Le mot de passe utilisateur n'est stocké que sous forme de condensat scrypt salé. Ne
jamais journaliser un mot de passe, une clé de session ou un cookie — y compris dans un
message d'erreur ou une trace de débogage.

## Sécurité web — l'existant est délibéré

- **CSP stricte** dans `main.py` : `script-src 'self'`, pas de CDN, pas de `unsafe-eval`.
  Ajouter une bibliothèque externe demanderait d'affaiblir la CSP : le projet préfère écrire
  le comportement à la main.
- **CSRF** : jeton en cookie + champ de formulaire, vérifié sur toute méthode non-GET.
  Un nouveau formulaire sans jeton CSRF est un bug, pas un oubli acceptable.
- **Session** signée (itsdangerous), durée bornée par `BUDGET_SESSION_MAX_AGE`.
- **`require_login`** est à `false` par défaut **parce que** l'ingress HA authentifie déjà
  l'utilisateur. Ce défaut ne vaut que derrière l'ingress : en accès direct par port publié,
  c'est la seule protection, et l'option doit être activée. Ne pas « simplifier » ce
  raisonnement dans le code ou la doc.

## Contraintes de l'ingress Home Assistant

- L'interface est servie sous un préfixe de chemin variable : **jamais d'URL absolue en dur**.
  Passer par `templating.path_for`.
- L'interface tourne dans une iframe HA : la directive de cadrage doit rester compatible avec
  l'hôte qui proxifie.
- `/data` est le seul emplacement persistant, et il est inclus dans les sauvegardes HA. Rien
  d'important ne s'écrit ailleurs.
- Aucun port n'est publié par défaut. Toute proposition d'ouvrir un port s'accompagne de
  `require_login` activé et d'un avertissement sur l'absence de TLS.
- Modifier le comportement de l'add-on implique de faire monter `version` dans `config.yaml` —
  sans quoi Home Assistant ne proposera pas la mise à jour.

## Style

Français partout : docstrings, commentaires, libellés d'interface, messages d'erreur. Les
commentaires du projet expliquent **pourquoi**, pas quoi — ils documentent une décision ou un
piège (« sans ce type MIME, le navigateur refuse d'installer l'application »). Écrire dans ce
registre, ou ne rien écrire.

`from __future__ import annotations` en tête de chaque module, annotations de type sur les
signatures publiques, `dataclass` pour les structures de transport entre services.
