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

bashio::log.info "Démarrage d'Enveloppe sur le port 8099 (ingress)."

exec python3 -m uvicorn app.main:app \
  --host 0.0.0.0 --port 8099 \
  --proxy-headers --forwarded-allow-ips '*'
