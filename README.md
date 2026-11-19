# vdi-tool

Mass-deploy and benchmark VDI desktops on Proxmox VE, from a small LXC with a web GUI.
Works on a single node or a cluster.

## Install or update

Run on a Proxmox VE host as root. Running it again updates the existing install.

```
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Realishbird/vdi-tool/main/install.sh)"
```

The installer creates a Debian 12 container, a `vdi@pve` API token, and starts the GUI on port 8080.
Options are environment variables, for example:

```
VDI_HOSTNAME=vdi-tool VDI_STORAGE=local-lvm VDI_IP=10.0.0.50/24 VDI_GW=10.0.0.1 bash -c "$(curl -fsSL .../install.sh)"
```

See the top of `install.sh` for the full list.

## Status

- [x] Step 1: installer, Proxmox connection, inventory GUI (nodes, storage, networks, guests)
- [ ] Step 2: golden images and fast batch deploy (linked clones)
- [ ] Step 3: workload harness (agent in the desktops, real-world profiles)
- [ ] Step 4: metrics and reports (Prometheus, Grafana, run comparisons)

## Security notes

The GUI has no login yet and the API token has the PVEAdmin role. Keep port 8080 on a trusted network.

## Layout

- `install.sh`: host-side installer and updater
- `scripts/`: container setup and systemd unit
- `app/`: FastAPI backend (Proxmox API client)
- `web/`: GUI (static HTML, CSS, JS)
