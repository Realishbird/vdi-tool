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

## Guide

**[Read the guide (PDF)](docs/vdi-tool-guide.pdf)**: how the tool works, with screenshots and diagrams. It covers the
architecture, templates, batch deploy, workloads, metrics, reports, measured results and known limits.
The source is `docs/guide/guide.html`; rebuild the PDF with `node docs/guide/build.js`.

## What it does

- **Golden templates**: builds Ubuntu 24.04 (XFCE, xrdp) and Windows 11 templates hands-free, or uses any existing Proxmox template.
- **Batch deploy**: linked clones (100 desktops in about a minute) or full clones, with options for disk size, cores, memory,
  target storage and filling disks with real data for capacity tests.
- **Workloads**: office worker, VDI knowledge worker disk, disk stress, database, sequential read and write, max throughput,
  CPU stress, fill disk and boot storm, plus your own fio patterns. Every desktop in a batch runs the workload at the same moment.
- **Metrics**: per desktop, per host and per run. Prometheus, the Proxmox exporter and Grafana (port 3000) are installed with a dashboard.
- **Reports**: a printable report per run (validity, what limited the run, key results, charts, outliers), side by side
  comparison of runs with percentage change and a pass or fail target, and CSV and JSON export.

## How a run works, in short

1. The tool starts the workload in every desktop through the QEMU guest agent.
2. Each desktop downloads the workload script from the tool and, for disk tests, writes its test file with real random data.
3. When all desktops are ready the tool says go, so they all load the storage together.
4. Desktops report their results. The tool also samples Proxmox's own counters and flags a run when the two disagree.

## Status

- [x] Installer, Proxmox connection, inventory GUI (nodes, storage, networks, guests)
- [x] Golden templates (Linux and Windows 11), batch deploy and teardown
- [x] Workload harness on Linux desktops, workload library, capacity fill, boot storm
- [x] Prometheus and Grafana, run reports, run comparison, density tests, export
- [x] Windows: unique computer name per clone and disk workloads with DiskSpd (office, CPU and fill are Linux only; clones still share a machine ID)
- [ ] Array testing: storage targets per protocol (NFS, iSCSI, NVMe-oF, FC), per-link network metrics, host-direct fio
- [ ] Real-applications workload, GUI login, narrower API token

## Security notes

The GUI has no login yet and the API token has the PVEAdmin role. Keep port 8080 on a trusted network.
The tool also serves cloud-init data to the desktops on that port, including the template build config
(with the `vdi` user's password) while a template exists.

## Layout

- `install.sh`: host-side installer and updater
- `scripts/`: container setup and systemd unit
- `app/`: FastAPI backend (Proxmox API client)
- `web/`: GUI and the report page (static HTML, CSS, JS)
- `agent/`: workload script the desktops download for benchmark runs
- `grafana/`: provisioned dashboard
- `docs/`: the guide (PDF) and its source
