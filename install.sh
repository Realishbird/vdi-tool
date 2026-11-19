#!/usr/bin/env bash
# vdi-tool installer / updater. Run on a Proxmox VE host as root:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Realishbird/vdi-tool/main/install.sh)"
# Re-running it updates an existing install in place.
#
# Optional environment overrides:
#   VDI_BRANCH=main        git branch to install
#   VDI_CTID=<id>          container id (default: next free id)
#   VDI_HOSTNAME=vdi-tool  container hostname
#   VDI_STORAGE=local-lvm  storage for the container root disk
#   VDI_BRIDGE=vmbr0       network bridge
#   VDI_IP=dhcp            container IPv4 (dhcp or CIDR like 10.0.0.50/24)
#   VDI_GW=                gateway when VDI_IP is static
#   VDI_CORES=2 VDI_RAM=4096 VDI_DISK=16
set -euo pipefail

REPO="${VDI_REPO:-https://github.com/Realishbird/vdi-tool}"
BRANCH="${VDI_BRANCH:-main}"
HOSTNAME_CT="${VDI_HOSTNAME:-vdi-tool}"
STORAGE="${VDI_STORAGE:-local-lvm}"
BRIDGE="${VDI_BRIDGE:-vmbr0}"
IPCFG="${VDI_IP:-dhcp}"
GW="${VDI_GW:-}"
CORES="${VDI_CORES:-2}"
RAM="${VDI_RAM:-4096}"
DISK="${VDI_DISK:-16}"
TOKEN_USER="vdi@pve"
TOKEN_NAME="vdi-tool"
MARKER="vdi-tool-managed"

c_ok=$'\e[32m'; c_info=$'\e[36m'; c_err=$'\e[31m'; c_off=$'\e[0m'
info() { echo "${c_info}==>${c_off} $*"; }
ok()   { echo "${c_ok}OK${c_off}  $*"; }
die()  { echo "${c_err}ERROR${c_off} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root on the Proxmox host"
command -v pveversion >/dev/null || die "this is not a Proxmox VE host (pveversion not found)"

# Find an existing install by the marker in the container description.
find_ct() {
  local id
  for id in $(pct list | awk 'NR>1{print $1}'); do
    if pct config "$id" | grep -q "$MARKER"; then echo "$id"; return; fi
  done
}

CTID="$(find_ct || true)"

if [[ -n "$CTID" ]]; then
  info "Existing vdi-tool container found (CT $CTID), updating to branch $BRANCH"
  pct status "$CTID" | grep -q running || pct start "$CTID"
  sleep 2
  pct exec "$CTID" -- bash -c "cd /opt/vdi-tool && git fetch --depth 1 origin '$BRANCH' && git checkout -B '$BRANCH' FETCH_HEAD && bash scripts/ct-setup.sh"
  IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
  ok "Updated. GUI: http://${IP}:8080"
  exit 0
fi

# ---------- fresh install ----------
CTID="${VDI_CTID:-$(pvesh get /cluster/nextid)}"
info "Creating vdi-tool container CT $CTID on $STORAGE"

pveam update >/dev/null 2>&1 || true
TEMPLATE="$(pveam available --section system | awk '{print $2}' | grep -E '^debian-12-standard_.*amd64' | sort -V | tail -1)"
[[ -n "$TEMPLATE" ]] || die "could not find a Debian 12 template via pveam"
TSTORE="local"
if ! pveam list "$TSTORE" | grep -q "$TEMPLATE"; then
  info "Downloading $TEMPLATE"
  pveam download "$TSTORE" "$TEMPLATE" >/dev/null
fi

NET="name=eth0,bridge=${BRIDGE},ip=${IPCFG}"
[[ "$IPCFG" != "dhcp" && -n "$GW" ]] && NET+=",gw=${GW}"

pct create "$CTID" "${TSTORE}:vztmpl/${TEMPLATE}" \
  --hostname "$HOSTNAME_CT" --cores "$CORES" --memory "$RAM" --swap 512 \
  --rootfs "${STORAGE}:${DISK}" --net0 "$NET" --unprivileged 1 \
  --features nesting=1 --onboot 1 --start 0 \
  --description "$MARKER: Proxmox VDI deploy and benchmark tool. ${REPO}" >/dev/null
ok "Container created"

# API user + token with the rights to clone, manage and read metrics.
info "Creating Proxmox API token ${TOKEN_USER}!${TOKEN_NAME}"
pveum user list --output-format json | grep -q "\"$TOKEN_USER\"" || pveum user add "$TOKEN_USER" --comment "vdi-tool service account"
pveum acl modify / --users "$TOKEN_USER" --roles PVEAdmin
if pveum user token list "$TOKEN_USER" --output-format json | grep -q "\"$TOKEN_NAME\""; then
  pveum user token remove "$TOKEN_USER" "$TOKEN_NAME"
fi
SECRET="$(pveum user token add "$TOKEN_USER" "$TOKEN_NAME" --privsep 0 --output-format json | python3 -c 'import sys,json;print(json.load(sys.stdin)["value"])')"

# Address the container uses to reach the Proxmox API.
PVE_HOST="$(ip -4 -o addr show "$BRIDGE" | awk '{print $4}' | cut -d/ -f1 | head -1)"
[[ -n "$PVE_HOST" ]] || PVE_HOST="$(hostname -I | awk '{print $1}')"

pct start "$CTID"
info "Waiting for the container network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- bash -c 'getent hosts deb.debian.org >/dev/null' && break
  sleep 2
done

ENVFILE="$(mktemp)"
umask 077
cat > "$ENVFILE" <<ENV
PVE_HOST=${PVE_HOST}
PVE_PORT=8006
PVE_TOKEN_ID=${TOKEN_USER}!${TOKEN_NAME}
PVE_TOKEN_SECRET=${SECRET}
PVE_VERIFY_TLS=false
ENV
pct exec "$CTID" -- mkdir -p /etc/vdi-tool
pct push "$CTID" "$ENVFILE" /etc/vdi-tool/vdi-tool.env --perms 600
rm -f "$ENVFILE"

info "Installing vdi-tool inside the container"
pct exec "$CTID" -- bash -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates >/dev/null \
  && git clone --depth 1 --branch '$BRANCH' '$REPO' /opt/vdi-tool && bash /opt/vdi-tool/scripts/ct-setup.sh"

IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo
ok "vdi-tool is running in CT $CTID"
echo "    GUI:     http://${IP}:8080"
echo "    Update:  re-run the same one-line command"
