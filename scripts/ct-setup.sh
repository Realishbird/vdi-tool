#!/usr/bin/env bash
# Runs inside the vdi-tool container on install and on every update.
set -euo pipefail
cd /opt/vdi-tool
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq python3 python3-venv genisoimage openssl curl >/dev/null
mkdir -p /var/lib/vdi-tool && chmod 700 /var/lib/vdi-tool
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
install -m 644 scripts/vdi-tool.service /etc/systemd/system/vdi-tool.service
systemctl daemon-reload
systemctl enable -q vdi-tool
systemctl restart vdi-tool
echo "vdi-tool $(git rev-parse --short HEAD) running"
# DiskSpd is the disk workload tool for Windows desktops; they download it from this tool.
if [[ ! -s /var/lib/vdi-tool/diskspd.exe ]]; then
  if curl -fsSL -m 120 -o /tmp/diskspd.zip https://github.com/microsoft/diskspd/releases/latest/download/DiskSpd.ZIP; then
    python3 -c "import zipfile; open('/var/lib/vdi-tool/diskspd.exe','wb').write(zipfile.ZipFile('/tmp/diskspd.zip').read('amd64/diskspd.exe'))" \
      || echo "WARNING: could not unpack DiskSpd; Windows disk workloads will not run."
    rm -f /tmp/diskspd.zip
  else
    echo "WARNING: could not download DiskSpd; Windows disk workloads will not run until the next update."
  fi
fi
# The metrics stack is optional: a failure here must never take the GUI down.
bash scripts/metrics-setup.sh || echo "WARNING: metrics stack setup failed; the GUI still works. Re-run the update to retry."
