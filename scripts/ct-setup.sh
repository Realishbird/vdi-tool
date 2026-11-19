#!/usr/bin/env bash
# Runs inside the vdi-tool container on install and on every update.
set -euo pipefail
cd /opt/vdi-tool
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq python3 python3-venv genisoimage openssl >/dev/null
mkdir -p /var/lib/vdi-tool && chmod 700 /var/lib/vdi-tool
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
install -m 644 scripts/vdi-tool.service /etc/systemd/system/vdi-tool.service
systemctl daemon-reload
systemctl enable -q vdi-tool
systemctl restart vdi-tool
echo "vdi-tool $(git rev-parse --short HEAD) running"
# The metrics stack is optional: a failure here must never take the GUI down.
bash scripts/metrics-setup.sh || echo "WARNING: metrics stack setup failed; the GUI still works. Re-run the update to retry."
