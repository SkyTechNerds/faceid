#!/usr/bin/env bashio
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
cfg() { jq -r "$1 // empty" "${OPT}"; }

MQTT_HOST=$(cfg '.mqtt_host')
MQTT_PORT=$(cfg '.mqtt_port')
MQTT_USER=$(cfg '.mqtt_user')
MQTT_PASSWORD=$(cfg '.mqtt_password')

# Kein Broker konfiguriert -> Mosquitto-Add-on über die Supervisor services API beziehen
TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"
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

CAMERAS=$(cfg '.cameras | join(", ")')
DISCOVERY=$(cfg '.discovery_cameras | join(", ")')

cat > /opt/faceid/config.yaml << EOF
frigate:
  url: $(cfg '.frigate_url')
mqtt:
  host: ${MQTT_HOST}
  port: ${MQTT_PORT:-1883}
  user: "${MQTT_USER}"
  password: "${MQTT_PASSWORD}"
faceid:
  port: 8600
  mqtt_prefix: $(cfg '.mqtt_prefix')
  match_threshold: $(cfg '.match_threshold')
  unknown_threshold: $(cfg '.unknown_threshold')
  cluster_eps: $(cfg '.cluster_eps')
  presence_window: $(cfg '.presence_window')
  set_sub_label: $(cfg '.set_sub_label')
  min_face_px: 48
  det_size: 640
  max_attempts: 6
  retry_seconds: 2.5
  cameras: [${CAMERAS}]
  discovery_cameras: [${DISCOVERY}]
EOF

# Galerie + Modell-Cache im persistenten /data-Volume (überlebt Updates)
mkdir -p /data/faceid /data/model-cache
ln -sfn /data/faceid /opt/faceid/data
export HOME=/data/model-cache

bashio::log.info "Starting FaceID..."
cd /opt/faceid
exec venv/bin/python -m app.main
