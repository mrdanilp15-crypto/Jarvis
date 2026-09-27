#!/usr/bin/env bash
# JARVIS mit einem Befehl starten (macOS, Linux, Windows über WSL/Git Bash).
#   ./deploy/start.sh                    Kern + Postgres + Redis + Ollama starten, Modelle laden
#   ./deploy/start.sh stop               alles stoppen (Daten bleiben erhalten)
#   ./deploy/start.sh autostart [edge]   Windows: beim Anmelden automatisch starten + Desktop-Verknüpfung
#                                        (edge: JARVIS in Microsoft Edge öffnen – mit der Stimme „Conrad“)
#   ./deploy/start.sh autostart-remove   Autostart wieder entfernen
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "stop" ]; then
  docker compose down
  exit 0
fi

# ---------------------------------------------------------------- Hilfsfunktionen
env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }

set_env() {  # KEY VALUE: Zeile in .env ersetzen oder anhängen
  if grep -q "^$1=" .env; then
    sed -i.bak "s|^$1=.*|$1=$2|" .env && rm -f .env.bak
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

unset_env() { sed -i.bak "/^$1=/d" .env && rm -f .env.bak; }

gpu_memory_mib() {  # Speicher der größten NVIDIA-GPU in MiB; leer ohne NVIDIA-Treiber
  local smi
  smi="$(command -v nvidia-smi || command -v nvidia-smi.exe || true)"
  if [ -z "$smi" ] && [ -x /usr/lib/wsl/lib/nvidia-smi ]; then smi=/usr/lib/wsl/lib/nvidia-smi; fi
  [ -n "$smi" ] || return 0
  "$smi" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | tr -d '\r ' | sort -n | tail -n 1 || true
}

docker_memory_gib() {  # Arbeitsspeicher, den Docker nutzen darf (Docker Desktop: meist die Hälfte des RAM)
  local bytes
  bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
  echo $(( ${bytes:-0} / 1073741824 ))
}

choose_model() {  # $1 = GPU-Speicher in MiB (leer = keine GPU)
  local vram="${1:-0}"
  if [ "$vram" -ge 11000 ] 2>/dev/null; then echo "qwen2.5:14b-instruct"
  elif [ "$vram" -ge 6000 ] 2>/dev/null; then echo "qwen2.5:7b-instruct"
  elif [ "$(docker_memory_gib)" -ge 12 ]; then echo "qwen2.5:7b-instruct"
  else echo "qwen2.5:3b-instruct"
  fi
}

autostart() {  # $1 = install | remove, $2 = Browser (chrome | edge)
  local repo mode distro="" script
  if grep -qi microsoft /proc/version 2>/dev/null; then
    mode=wsl
    repo="$(cd .. && pwd -P)"
    distro="${WSL_DISTRO_NAME:-}"
    script="$(wslpath -w "$PWD/windows/install-autostart.ps1")"
  else
    case "$(uname -s)" in
      MINGW*|MSYS*|CYGWIN*)
        mode=windows
        repo="$(cd .. && pwd -W)"
        script="$(cygpath -w "$PWD/windows/install-autostart.ps1")"
        ;;
      *)
        cat <<'EOF'
Der Autostart per Skript ist für Windows gedacht. Auf Linux und macOS reicht:
  • Die Container starten mit Docker von selbst wieder (restart: unless-stopped).
  • Docker beim Hochfahren starten – Linux: sudo systemctl enable docker;
    macOS: Docker Desktop → Einstellungen → „Start Docker Desktop when you sign in“.
  • http://127.0.0.1:8080 im Browser öffnen (Chrome/Edge: Menü → „Als App installieren“).
EOF
        return 0
        ;;
    esac
  fi
  local args=(-NoProfile -ExecutionPolicy Bypass -File "$script")
  if [ "$1" = remove ]; then
    args+=(-Remove)
  else
    args+=(-Mode "$mode" -Repo "$repo" -Browser "${2:-chrome}")
    if [ -n "$distro" ]; then args+=(-Distro "$distro"); fi
    if [ -n "$token" ]; then args+=(-Token "$token"); fi
  fi
  MSYS_NO_PATHCONV=1 powershell.exe "${args[@]}"
}

# ---------------------------------------------------------------- Zugangsdaten
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

case "${1:-}" in
  autostart)
    case "${2:-chrome}" in chrome|edge) ;; *) echo "Browser: chrome oder edge"; exit 1 ;; esac
    autostart install "${2:-chrome}"; exit 0 ;;
  autostart-remove) autostart remove; exit 0 ;;
  "") ;;
  *) echo "Unbekannter Befehl: $1 (möglich: stop, autostart, autostart-remove)"; exit 1 ;;
esac

# ---------------------------------------------------------------- Voraussetzungen
command -v docker >/dev/null || { echo "Docker fehlt: https://docs.docker.com/get-docker/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 fehlt (docker compose …)."; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker läuft nicht – bitte Docker Desktop bzw. den Docker-Dienst starten."; exit 1; }

# ---------------------------------------------------------------- Hardware: GPU und Modellgröße
vram="$(gpu_memory_mib)"
auto_model=0
if [ -n "$vram" ] && ! grep -q '^COMPOSE_FILE=' .env && ! grep -q '^JARVIS_GPU=off' .env; then
  set_env COMPOSE_PATH_SEPARATOR ":"
  set_env COMPOSE_FILE "docker-compose.yml:docker-compose.gpu.yml"
  echo "→ NVIDIA-Grafikkarte gefunden (${vram} MiB) – das Sprachmodell rechnet auf der GPU."
fi
if [ -z "$(env_value JARVIS_LLM_MODEL)" ]; then
  auto_model=1
  set_env JARVIS_LLM_MODEL "$(choose_model "$vram")"
  echo "→ Sprachmodell passend zur Hardware: $(env_value JARVIS_LLM_MODEL) (ändern: JARVIS_LLM_MODEL in deploy/.env)"
fi

echo "→ Baue und starte jarvis-core, Postgres, Redis, Ollama und die JARVIS-Stimme (Piper) …"
if ! docker compose up -d --build jarvis-core wyoming-piper; then
  grep -q '^COMPOSE_FILE=' .env || exit 1
  echo "✖ Start mit GPU fehlgeschlagen – starte ohne GPU (erneut versuchen: JARVIS_GPU=off aus deploy/.env löschen)."
  unset_env COMPOSE_FILE
  unset_env COMPOSE_PATH_SEPARATOR
  set_env JARVIS_GPU off
  if [ "$auto_model" = 1 ]; then set_env JARVIS_LLM_MODEL "$(choose_model "")"; fi
  docker compose up -d --build jarvis-core wyoming-piper
fi

config_value() {  # liest einen Wert aus der aktiven JARVIS-Konfiguration im Container
  docker compose exec -T jarvis-core python -c \
    "import os, yaml; c = yaml.safe_load(open(os.environ['JARVIS_CONFIG'])); print($1)"
}
llm_model="$(config_value "os.environ.get('JARVIS_LLM_MODEL') or c['llm']['providers'][c['llm']['default_local']]['model']")"
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
    echo "→ Lade das Sprachmodell in den Speicher (danach antwortet JARVIS ohne Wartezeit) …"
    for _ in $(seq 1 90); do
      curl -fsS http://127.0.0.1:8080/v1/system/health 2>/dev/null | grep -q '"local_llm":"ready"' && break
      sleep 2
    done
    cat <<EOF

✔ JARVIS läuft.

Im Browser öffnen (Chrome oder Edge) und mit JARVIS sprechen:

  http://127.0.0.1:8080/#token=${token}

Beim Anmelden automatisch starten (Windows, ohne Konsole): ./deploy/start.sh autostart
API-Beschreibung: http://127.0.0.1:8080/docs  (oben rechts „Authorize“, Token: ${token})

Erste Frage im Terminal:
  curl -X POST http://127.0.0.1:8080/v1/conversations/test/messages \\
    -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \\
    -d '{"text": "Hallo Jarvis, was kannst du?"}'

Stoppen: ./deploy/start.sh stop
EOF
    exit 0
  fi
  sleep 2
done
echo "✖ Server antwortet nicht. Logs: docker compose -f deploy/docker-compose.yml logs jarvis-core"
exit 1
