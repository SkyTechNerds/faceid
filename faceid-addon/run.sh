#!/usr/bin/env bashio
set -e

# MQTT: explicit options win; otherwise use the broker provided by the
# Mosquitto add-on via the Supervisor services API.
MQTT_HOST=$(bashio::config 'mqtt_host')
MQTT_PORT=$(bashio::config 'mqtt_port')
MQTT_USER=$(bashio::config 'mqtt_user')
MQTT_PASSWORD=$(bashio::config 'mqtt_password')

if ! bashio::var.has_value "${MQTT_HOST}" && bashio::services.available mqtt; then
    bashio::log.info "Using MQTT broker from Supervisor services API"
    MQTT_HOST=$(bashio::services mqtt "host")
    MQTT_PORT=$(bashio::services mqtt "port")
    MQTT_USER=$(bashio::services mqtt "username")
    MQTT_PASSWORD=$(bashio::services mqtt "password")
fi

if ! bashio::var.has_value "${MQTT_HOST}"; then
    bashio::log.fatal "No MQTT broker configured and none provided by Home Assistant."
    bashio::log.fatal "Set mqtt_host in the add-on options or install the Mosquitto add-on."
    exit 1
fi

CAMERAS=$(bashio::config 'cameras // [] | join(", ")')
DISCOVERY=$(bashio::config 'discovery_cameras // [] | join(", ")')

cat > /opt/faceid/config.yaml << EOF
frigate:
  url: $(bashio::config 'frigate_url')
mqtt:
  host: ${MQTT_HOST}
  port: ${MQTT_PORT}
  user: "${MQTT_USER}"
  password: "${MQTT_PASSWORD}"
faceid:
  port: 8600
  mqtt_prefix: $(bashio::config 'mqtt_prefix')
  match_threshold: $(bashio::config 'match_threshold')
  unknown_threshold: $(bashio::config 'unknown_threshold')
  cluster_eps: $(bashio::config 'cluster_eps')
  presence_window: $(bashio::config 'presence_window')
  set_sub_label: $(bashio::config 'set_sub_label')
  min_face_px: 48
  det_size: 640
  max_attempts: 6
  retry_seconds: 2.5
  cameras: [${CAMERAS}]
  discovery_cameras: [${DISCOVERY}]
EOF

# Persist gallery + model cache in the add-on data volume (survives updates)
mkdir -p /data/faceid /data/model-cache
ln -sfn /data/faceid /opt/faceid/data
export HOME=/data/model-cache

bashio::log.info "Starting FaceID..."
cd /opt/faceid
exec venv/bin/python -m app.main
