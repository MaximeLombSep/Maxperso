# Enveloppe — Budget (add-on Home Assistant)

Suivi de budget en mode enveloppe, servi dans l'interface Home Assistant via
l'ingress : aucun port n'est ouvert, l'accès hérite de l'authentification HA,
et l'application ajoute son propre mot de passe par-dessus.

## Installation

Ce dossier est autonome : il contient le manifeste, le Dockerfile et les
sources de l'application. Rien à préparer.

1. Copier ce dossier dans le partage `addons` de Home Assistant
   (`\\homeassistant\addons\enveloppe`), à la main ou avec
   `./scripts/install-addon.sh //homeassistant/addons`.
   Le partage s'ouvre avec l'add-on **Samba share** ou **File editor**.
2. Dans Home Assistant : **Paramètres → Modules complémentaires → Boutique →
   ⋮ → Vérifier les mises à jour**.
3. Installer **Enveloppe — Budget** depuis la section « Local add-ons »,
   puis démarrer. L'entrée « Budget » apparaît dans la barre latérale.

Le dépôt porte aussi un `repository.yaml` : si le dépôt Git est accessible au
Superviseur, l'add-on peut être ajouté comme dépôt d'add-ons plutôt que copié.

## Options

| Option | Rôle |
| --- | --- |
| `currency` | Devise affichée (`EUR` par défaut). |
| `max_upload_mb` | Taille maximale d'une pièce jointe de contrat. |
| `allow_insecure_cookies` | À laisser à `true` si vous ouvrez Home Assistant en HTTP sur votre réseau local ; sinon le cookie de session est rejeté par le navigateur. Passez à `false` dès que HA est servi en HTTPS. |

## Données et sauvegarde

Tout est écrit dans `/data` : base SQLite, pièces jointes des contrats, clé de
session. Ce dossier est inclus dans les sauvegardes Home Assistant — une
sauvegarde HA suffit donc à restaurer l'intégralité du budget.

## Performances sur Raspberry Pi

L'application est servie par un seul processus Python et une base SQLite ;
elle tient sans difficulté sur un Pi 4. L'interface est rendue côté serveur,
sans framework JavaScript à télécharger : elle reste fluide même sur mobile
via l'application Home Assistant.
