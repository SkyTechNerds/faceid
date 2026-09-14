#!/usr/bin/with-contenv bashio
# with-contenv ist Pflicht: unter s6-overlay v3 erreichen die Container-Variablen
# (u. a. SUPERVISOR_TOKEN) das Skript sonst nicht — die MQTT-Erkennung ueber den
# Supervisor scheitert dann, obwohl Mosquitto laeuft und die Rechte stimmen.
set -e

# Optionen direkt aus /data/options.json lesen — robust gegenüber
# bashio/Supervisor-API-Versionsunterschieden.
# onnxruntime/NumPy brauchen mind. x86-64-v2 — in VMs oft vom CPU-Modell maskiert
if [ "$(uname -m)" = "x86_64" ] && ! grep -qm1 avx /proc/cpuinfo; then
    bashio::log.fatal "This CPU (or VM CPU model) lacks AVX, required by the recognition runtime."
    bashio::log.fatal "Running HAOS in a VM? Set the CPU type to 'host' (Proxmox: qm set <vmid> --cpu host, then cold-restart the VM)."
    exit 1
fi

OPT=/data/options.json
# Einmal fruehzeitig pruefen, statt jeden einzelnen Zugriff abzusichern: ist die Datei
# unlesbar oder kein gueltiges JSON, liefert JEDER jq-Aufruf unten nichts, und FaceID
# startete mit stillschweigenden Vorgabewerten statt zu sagen, dass die Konfiguration
# nicht ankam.
if ! jq -e . "${OPT}" >/dev/null 2>&1; then
    bashio::log.fatal "Cannot read ${OPT} — the add-on options are missing or not valid JSON."
    exit 1
fi
cfg() { jq -r "$1 // empty" "${OPT}"; }

# Freitext wird als JSON ausgegeben, nicht roh interpoliert: JSON ist gueltiges YAML und
# bringt das Escaping mit. Ein Passwort mit Anfuehrungszeichen zerlegte sonst die Datei —
# schlimmstenfalls liessen sich damit weitere Schluessel in die Konfiguration schreiben.
#
# Zwei Wege, weil es zwei Quellen gibt: ``yml`` liest direkt aus options.json, ``jstr``
# kodiert einen bereits ermittelten Wert. Die MQTT-Daten MUESSEN ueber ``jstr`` laufen —
# sie werden weiter unten von der Mosquitto-Erkennung ueberschrieben, und ein erneutes
# Lesen aus options.json wuerde genau diese Erkennung aushebeln.
yml() { jq "$1 // \"\"" "${OPT}"; }
jstr() { jq -Rn --arg v "$1" '$v'; }
# ⚠️ Regel fuer die Vorlage unten: Alles, was im Schema `str`, `password`, `url` oder eine
# Liste davon ist, MUSS ueber yml/jstr laufen. Direkt per cfg() eingesetzt werden nur
# Werte, die der Supervisor gegen einen Zahlen-, Bool- oder Auswahltyp geprueft hat — die
# koennen kein Anfuehrungszeichen und keinen Zeilenumbruch enthalten. Wird ein solches
# Feld im Schema spaeter zu `str`, gehoert es hier mit umgestellt.
MQTT_HOST=$(cfg '.mqtt_host')
MQTT_PORT=$(cfg '.mqtt_port')
MQTT_USER=$(cfg '.mqtt_user')
MQTT_PASSWORD=$(cfg '.mqtt_password')

# Kein Broker konfiguriert -> Mosquitto-Add-on über die Supervisor services API beziehen
TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"
if [ -z "${MQTT_HOST}" ] && bashio::services.available "mqtt"; then
    # Offizieller Weg, wenn der Supervisor den Dienst anbietet.
    bashio::log.info "Using the MQTT service offered by Home Assistant"
    MQTT_HOST=$(bashio::services "mqtt" "host")
    MQTT_PORT=$(bashio::services "mqtt" "port")
    MQTT_USER=$(bashio::services "mqtt" "username")
    MQTT_PASSWORD=$(bashio::services "mqtt" "password")
fi
if [ -z "${MQTT_HOST}" ] && [ -z "${TOKEN}" ]; then
    bashio::log.warning "No Supervisor token in the container environment - cannot auto-detect MQTT."
fi
if [ -z "${MQTT_HOST}" ] && [ -n "${TOKEN}" ]; then
    SVC=$(curl -s -H "Authorization: Bearer ${TOKEN}" http://supervisor/services/mqtt || true)
    if [ "$(echo "${SVC}" | jq -r '.result // empty')" = "ok" ]; then
        bashio::log.info "Using MQTT broker from the Supervisor services API"
        MQTT_HOST=$(echo "${SVC}" | jq -r '.data.host')
        MQTT_PORT=$(echo "${SVC}" | jq -r '.data.port')
        MQTT_USER=$(echo "${SVC}" | jq -r '.data.username')
        MQTT_PASSWORD=$(echo "${SVC}" | jq -r '.data.password')
    else
        bashio::log.warning "Supervisor services API answered: ${SVC:-<empty>}"
    fi
fi

if [ -z "${MQTT_HOST}" ]; then
    bashio::log.fatal "No MQTT broker configured and none provided by Home Assistant."
    bashio::log.fatal "Set mqtt_host in the add-on options or install the Mosquitto add-on."
    exit 1
fi

# Als JSON-Array, nicht als zusammengefuegte Zeichenkette: Kameranamen sind Freitext, und
# ein Komma oder Anfuehrungszeichen darin zerlegte die erzeugte Liste. JSON ist gueltiges
# YAML, die eckigen Klammern kommen deshalb aus jq und nicht aus der Vorlage.
#
# '// []' bleibt noetig: Kommt eine Liste neu dazu und steht noch nicht in options.json,
# liefert der Zugriff sonst null statt einer leeren Liste.
CAMERAS=$(jq -c '.cameras // []' "${OPT}")
DISCOVERY=$(jq -c '.discovery_cameras // []' "${OPT}")
CLIPCAMS=$(jq -c '.clip_fallback_cameras // []' "${OPT}")
LIVECAMS=$(jq -c '.live_hires_fallback_cameras // []' "${OPT}")

cat > /opt/faceid/config.yaml << EOF
frigate:
  url: $(yml '.frigate_url')
  # Nur fuer Frigates authentifizierten Port 8971 noetig. Leer lassen heisst offene API:
  # FrigateAPI macht aus dem leeren Wert None, genau wie im Standalone-Betrieb.
  user: $(yml '.frigate_user')
  password: $(yml '.frigate_password')
mqtt:
  host: $(jstr "${MQTT_HOST}")
  port: ${MQTT_PORT:-1883}
  user: $(jstr "${MQTT_USER}")
  password: $(jstr "${MQTT_PASSWORD}")
faceid:
  port: 8600
  mqtt_prefix: $(yml '.mqtt_prefix')
  match_threshold: $(cfg '.match_threshold')
  unknown_threshold: $(cfg '.unknown_threshold')
  cluster_eps: $(cfg '.cluster_eps')
  suggest_threshold: $(cfg '.suggest_threshold')
  max_faces_per_person: $(cfg '.max_faces_per_person')
  trimmed_keep: $(cfg '.trimmed_keep')
  dedupe_threshold: $(cfg '.dedupe_threshold')
  hires_enroll: $(cfg '.hires_enroll')
  clip_fallback: $(cfg '.clip_fallback')
  clip_fallback_cameras: ${CLIPCAMS}
  live_hires_fallback: $(cfg '.live_hires_fallback')
  live_hires_fallback_cameras: ${LIVECAMS}
  live_hires_mode: $(cfg '.live_hires_mode')
  live_hires_cooldown: $(cfg '.live_hires_cooldown')
  frigate_topic_prefix: $(yml '.frigate_topic_prefix')
  poll_interval: $(cfg '.poll_interval')
  backup_enabled: $(cfg '.backup_enabled')
  backup_hour: $(cfg '.backup_hour')
  backup_keep: $(cfg '.backup_keep')
  presence_window: $(cfg '.presence_window')
  set_sub_label: $(cfg '.set_sub_label')
  min_face_px: $(cfg '.min_face_px')
  det_size: $(cfg '.det_size')
  max_attempts: $(cfg '.max_attempts')
  retry_seconds: 2.5
  cameras: ${CAMERAS}
  discovery_cameras: ${DISCOVERY}
EOF

# Galerie + Modell-Cache im persistenten /data-Volume (überlebt Updates)
mkdir -p /data/faceid /data/model-cache
ln -sfn /data/faceid /opt/faceid/data
export HOME=/data/model-cache

bashio::log.info "Starting FaceID..."
cd /opt/faceid
exec venv/bin/python -m app.main
