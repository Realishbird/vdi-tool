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
- **Density tests**: run the same workload on a growing number of desktops and see how many fit under a latency target.
- **Metrics**: per desktop, per host and per run. Prometheus, the Proxmox exporter and Grafana (port 3000) are installed with
  four dashboards: overview, benchmark runs, desktop drilldown, and storage and network links. Run results are also
  exported at `/metrics`.
- **Reports**: a printable report per run (validity, what limited the run, key results, charts, outliers), side by side
  comparison of runs with percentage change and a pass or fail target, and CSV and JSON export.
- **Windows desktops**: disk workloads with Microsoft DiskSpd, a unique computer name per clone, and an option to bake
  the [EUC Score](https://eucscore.com/) base package into the Windows template for user experience tests.
- **Login**: an optional admin password for the GUI and API, with an API key for scripts.

## How a run works, in short

1. The tool starts the workload in every desktop through the QEMU guest agent.
2. Each desktop downloads the workload script from the tool and, for disk tests, writes its test file with real random data.
3. When all desktops are ready the tool says go, so they all load the storage together.
4. Desktops report their results. The tool also samples Proxmox's own counters and flags a run when the two disagree.

## Grafana dashboards

| Dashboard | What it shows |
|---|---|
| VDI overview | Desktops running, host CPU and memory, total desktop disk and network, top 10 desktops, storage used, the run in progress |
| VDI benchmark runs | Every finished run by workload, storage and clone type: IOPS, throughput, p95 latency, peak host CPU, validity |
| VDI desktop drilldown | CPU, memory, disk and network for one desktop or any group of them, and the spread between slowest and fastest |
| VDI storage and network links | Per port throughput against the link ceiling, errors and drops, per disk IOPS and latency on the host |

The storage and network links dashboard needs `prometheus-node-exporter` on the Proxmox host (port 9100):
`apt-get install -y prometheus-node-exporter`. The installer does not put anything on the host beyond the container and the API token.

## EUC Score

EUC Score measures what a user experiences in a desktop session. It is not part of this repository and the tool never
downloads it. Download `Install-EUCScoreBase.zip` from eucscore.com yourself, accept its agreement, and upload it on the
Templates page. A Windows template built with "Include EUC Score" installs it silently to `C:\EUCScore` and keeps the `vdi`
user logged on, because its tests need a desktop session. Check its licence terms for your organization before use.

## Status

- [x] Installer, Proxmox connection, inventory GUI (nodes, storage, networks, guests)
- [x] Golden templates (Linux and Windows 11), batch deploy and teardown
- [x] Workload harness on Linux desktops, workload library, capacity fill, boot storm
- [x] Prometheus and Grafana, run reports, run comparison, density tests, export
- [x] Windows: unique computer name per clone and disk workloads with DiskSpd (office, CPU and fill are Linux only; clones still share a machine ID)
- [x] Live run metrics, four Grafana dashboards, optional GUI login
- [x] EUC Score package upload and Windows template option
- [ ] EUC Score probe workload: run Simloads on chosen desktops and pull the results into the run report
- [ ] Array testing: storage targets per protocol (NFS, iSCSI, NVMe-oF, FC), host-direct fio
- [ ] Real-applications workload, narrower API token

## Security notes

The GUI is open until you set an admin password with the Login button; set one unless the network is trusted.
After that, pages and the API need a login or the API key in `/var/lib/vdi-tool/api-key` (sent as a Bearer token).
The paths desktops use (first-boot config, workload downloads, result reports) stay open, including the template
build config with the `vdi` user's password while a template exists. Grafana ships with its default admin login:
change it on first use. The API token has the PVEAdmin role. Templates built with EUC Score keep the `vdi` password
in the Windows registry for automatic logon, so use them for test desktops only.

## Layout

- `install.sh`: host-side installer and updater
- `scripts/`: container setup and systemd unit
- `app/`: FastAPI backend (Proxmox API client)
- `web/`: GUI and the report page (static HTML, CSS, JS)
- `agent/`: workload script the desktops download for benchmark runs
- `grafana/`: provisioned dashboards
- `docs/`: the guide (PDF) and its source


## License

[MIT](LICENSE). Tools the installer downloads keep their own licences: Microsoft DiskSpd (MIT), fio (GPL-2.0),
Prometheus and its exporters (Apache-2.0), Grafana (AGPL-3.0). EUC Score is a separate product with its own agreement.
