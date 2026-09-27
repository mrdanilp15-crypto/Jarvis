#!/usr/bin/env bash
# JARVIS mit einem Befehl starten (macOS, Linux, Windows über WSL/Git Bash).
#   ./deploy/start.sh            Kern + Postgres + Redis + Ollama starten, Modelle laden
#   ./deploy/start.sh stop       alles stoppen (Daten bleiben erhalten)
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "stop" ]; then
  docker compose down
  exit 0
fi

command -v docker >/dev/null || { echo "Docker fehlt: https://docs.docker.com/get-docker/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 fehlt (docker compose …)."; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker läuft nicht – bitte Docker Desktop bzw. den Docker-Dienst starten."; exit 1; }

random_hex() { openssl rand -hex 16 2>/dev/null || od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }

if [ ! -f .env ]; then
  cp .env.example .env
  password="$(random_hex)"
  token="jarvis-$(random_hex)"
  sed -i.bak -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${password}/" -e "s/dev-alex-token/${token}/" .env
  rm -f .env.bak
  echo "→ deploy/.env angelegt (zufälliges Datenbank-Passwort und API-Token)."
fi
token="$(sed -n 's/.*{"\([^"]*\)": {"actor".*/\1/p' .env | head -n 1)"

echo "→ Baue und starte jarvis-core, Postgres, Redis und Ollama …"
docker compose up -d --build jarvis-core

config_value() {  # liest einen Wert aus der aktiven JARVIS-Konfiguration im Container
  docker compose exec -T jarvis-core python -c \
    "import os, yaml; c = yaml.safe_load(open(os.environ['JARVIS_CONFIG'])); print($1)"
}
llm_model="$(config_value "c['llm']['providers'][c['llm']['default_local']]['model']")"
embed_model="$(config_value "c['llm']['embeddings']['model']")"

for model in "$llm_model" "$embed_model"; do
  echo "→ Lade Modell ${model} (beim ersten Mal kann das einige Minuten dauern) …"
  if ! docker compose exec -T ollama ollama pull "$model"; then
    echo "✖ Modell ${model} konnte nicht geladen werden (Internetverbindung?)."
    echo "  Später erneut: docker compose -f deploy/docker-compose.yml exec ollama ollama pull ${model}"
  fi
done

echo "→ Warte auf den Server …"
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8080/v1/system/health >/dev/null 2>&1; then
    cat <<EOF

✔ JARVIS läuft.

Im Browser öffnen (Chrome oder Edge) und mit JARVIS sprechen:

  http://127.0.0.1:8080/#token=${token}

API-Beschreibung: http://127.0.0.1:8080/docs  (oben rechts „Authorize“, Token: ${token})

Erste Frage im Terminal:
  curl -X POST http://127.0.0.1:8080/v1/conversations/test/messages \\
    -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \\
    -d '{"text": "Hallo Jarvis, was kannst du?"}'

Im Terminal chatten (Node.js >= 22):
  JARVIS_URL=ws://127.0.0.1:8080 JARVIS_TOKEN=${token} node reference/node/jarvis-client.mjs

Stoppen: ./deploy/start.sh stop
EOF
    exit 0
  fi
  sleep 2
done
echo "✖ Server antwortet nicht. Logs: docker compose -f deploy/docker-compose.yml logs jarvis-core"
exit 1
