# Enveloppe — add-on Home Assistant

Suivi de budget en mode enveloppe, auto-hébergé et en français : import de
relevés bancaires (CSV, OFX, PDF), cartes à débit différé, contrats
d'assurance avec échéances et documents, objectifs d'épargne.

Aucune connexion à votre banque, aucun agrégateur, aucun identifiant bancaire
stocké : vous déposez l'export que vous avez téléchargé vous-même. Toutes vos
données restent dans le dossier `/data` de l'add-on, inclus dans les
sauvegardes Home Assistant.

## Installation

1. **Paramètres → Modules complémentaires → Boutique → ⋮ → Dépôts**
2. Ajouter : `https://github.com/MaximeLombSep/maxperso`
3. Installer **Enveloppe — Budget**, puis démarrer.
4. Activer « Afficher dans la barre latérale ».

L'interface est servie par l'ingress : aucun port n'est ouvert, l'accès hérite
de l'authentification Home Assistant, et l'application ajoute son propre mot
de passe par-dessus.

## Options

| Option | Rôle |
| --- | --- |
| `currency` | Devise affichée (`EUR` par défaut). |
| `max_upload_mb` | Taille maximale d'une pièce jointe de contrat. |
| `allow_insecure_cookies` | À laisser à `true` si vous ouvrez Home Assistant en HTTP sur votre réseau local ; sinon le cookie de session est rejeté par le navigateur. |

## Hors Home Assistant

Le même dossier fait office de projet Docker classique :

```sh
docker build -f enveloppe/Dockerfile.standalone -t enveloppe enveloppe
docker run -d -p 127.0.0.1:8099:8099 -v ./data:/data \
  -e BUDGET_ALLOW_INSECURE_COOKIES=1 enveloppe
```
