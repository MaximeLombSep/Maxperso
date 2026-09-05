#!/usr/bin/with-contenv bashio
# Démarrage de l'add-on : les options Home Assistant sont converties en
# variables d'environnement. Aucun secret n'est lu ici — la clé de session
# est générée dans /data au premier lancement.
set -e

export BUDGET_DATA_DIR=/data
export BUDGET_CURRENCY="$(bashio::config 'currency')"
export BUDGET_MAX_UPLOAD_MB="$(bashio::config 'max_upload_mb')"

# Derrière l'ingress, Home Assistant peut être servi en HTTP sur le réseau
# local : le cookie de session ne doit alors pas être marqué « Secure ».
if bashio::config.true 'allow_insecure_cookies'; then
  export BUDGET_ALLOW_INSECURE_COOKIES=1
else
  export BUDGET_ALLOW_INSECURE_COOKIES=0
fi

# Derrière l'ingress, Home Assistant authentifie déjà l'utilisateur : le mot
# de passe applicatif n'est exigé que si l'option le demande explicitement.
if bashio::config.true 'require_login'; then
  export BUDGET_REQUIRE_LOGIN=1
else
  export BUDGET_REQUIRE_LOGIN=0
fi

# Ajout des colonnes manquantes au prochain démarrage. Volontairement
# désactivé par défaut : une modification de schéma se décide, elle ne se
# subit pas. L'add-on journalise chaque instruction exécutée.
if bashio::config.true 'apply_migrations'; then
  export BUDGET_APPLY_MIGRATIONS=1
  bashio::log.warning "Option « apply_migrations » active : les colonnes manquantes seront ajoutées au démarrage."
  bashio::log.warning "Sauvegardez avant, et repassez l'option sur off ensuite."
else
  export BUDGET_APPLY_MIGRATIONS=0
fi

bashio::log.info "Démarrage d'Enveloppe sur le port 8099 (ingress)."

exec python3 -m uvicorn app.main:app \
  --host 0.0.0.0 --port 8099 \
  --proxy-headers --forwarded-allow-ips '*'
