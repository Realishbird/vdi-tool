#!/usr/bin/env bash
# Installs the metrics stack inside the vdi-tool container:
#   prometheus-pve-exporter (Proxmox API -> metrics), Prometheus (storage), Grafana (dashboards).
# Safe to run again; ct-setup.sh calls it on every install and update.
set -euo pipefail
cd /opt/vdi-tool
export DEBIAN_FRONTEND=noninteractive
ENVFILE=/etc/vdi-tool/vdi-tool.env
# shellcheck disable=SC1090
set -a; . "$ENVFILE"; set +a

# ---- Proxmox exporter (runs from the tool's virtualenv)
.venv/bin/pip install -q 'prometheus-pve-exporter>=3.4,<4'
umask 077
cat > /etc/vdi-tool/pve-exporter.yml <<YML
default:
  user: "${PVE_TOKEN_ID%%!*}"
  token_name: "${PVE_TOKEN_ID##*!}"
  token_value: "${PVE_TOKEN_SECRET}"
  verify_ssl: false
YML
umask 022
install -m 644 scripts/vdi-pve-exporter.service /etc/systemd/system/vdi-pve-exporter.service

# ---- Prometheus
apt-get install -y -qq prometheus >/dev/null
cat > /etc/prometheus/prometheus.yml <<YML
# Managed by vdi-tool (scripts/metrics-setup.sh); local edits are overwritten on update.
global:
  scrape_interval: 15s
scrape_configs:
  - job_name: pve
    metrics_path: /pve
    params:
      module: [default]
      cluster: ['1']
      node: ['1']
      target: ['${PVE_HOST}']
    static_configs:
      - targets: ['127.0.0.1:9221']
  - job_name: vdi-tool
    static_configs:
      - targets: ['127.0.0.1:8080']
  # Optional: prometheus-node-exporter on the Proxmox host gives per-port network numbers.
  # If it is not installed the target just shows as down.
  - job_name: host
    static_configs:
      - targets: ['${PVE_HOST}:9100']
YML

# ---- Grafana (from Grafana's own apt repository)
if ! dpkg -s grafana >/dev/null 2>&1; then
  apt-get install -y -qq gnupg curl ca-certificates >/dev/null
  install -d -m 755 /etc/apt/keyrings
  curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/grafana.gpg
  echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" > /etc/apt/sources.list.d/grafana.list
  apt-get update -qq
  apt-get install -y -qq grafana >/dev/null
fi
install -d /etc/grafana/provisioning/datasources /etc/grafana/provisioning/dashboards
# Grafana runs as its own user and cannot read /opt/vdi-tool, so give it a copy of the dashboards.
install -d -o grafana -g grafana -m 755 /var/lib/grafana/dashboards
install -o grafana -g grafana -m 644 grafana/dashboards/*.json /var/lib/grafana/dashboards/
cat > /etc/grafana/provisioning/datasources/vdi-tool.yaml <<YML
apiVersion: 1
datasources:
  - name: Prometheus
    uid: vdi-prom
    type: prometheus
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
YML
cat > /etc/grafana/provisioning/dashboards/vdi-tool.yaml <<YML
apiVersion: 1
providers:
  - name: vdi-tool
    type: file
    allowUiUpdates: false
    options:
      path: /var/lib/grafana/dashboards
YML
# Read-only viewing without a login, so the GUI can link straight to the dashboard.
install -d /etc/systemd/system/grafana-server.service.d
cat > /etc/systemd/system/grafana-server.service.d/vdi-tool.conf <<UNIT
[Service]
Environment=GF_AUTH_ANONYMOUS_ENABLED=true
Environment=GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
Environment=GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/vdi-overview.json
UNIT

systemctl daemon-reload
systemctl enable -q vdi-pve-exporter prometheus grafana-server
systemctl restart vdi-pve-exporter prometheus grafana-server
echo "metrics stack running (Grafana on port 3000)"
