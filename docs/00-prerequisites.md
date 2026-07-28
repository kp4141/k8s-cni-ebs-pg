# 00 — Prerequisites

## The shape of the problem

Kubernetes nodes are Linux machines. macOS cannot run them directly, so every
local Kubernetes option is really "Linux in a VM, with something on top":

```
macOS  →  Linux VM (Colima)  →  Docker  →  kind node containers  →  Kubernetes
```

Each layer matters when things break. A pod that cannot reach the internet might
be failing at the CNI, at Docker's bridge, at the VM's NAT, or at the Mac's own
network — and the symptom looks identical from inside the pod.

## Host tooling

```bash
brew install colima docker kubectl kind helm python@3.12
```

| Tool | Role |
|---|---|
| `colima` | Runs the Linux VM that hosts Docker |
| `docker` | CLI only — the daemon lives in the VM |
| `kind` | Creates Kubernetes nodes as Docker containers |
| `kubectl` | Cluster client |
| `helm` | Installs the OpenEBS and kube-prometheus-stack charts |
| `python@3.12` | Builds the validation virtualenv |

`make preflight` verifies all of it, plus VM sizing, and fails with a specific
remedy rather than a generic error.

## Why Colima rather than Docker Desktop

Both work. Colima was chosen because:

- **It installs and starts entirely from the CLI.** No GUI step, no admin
  prompt, no licence acceptance. The whole lab stays scriptable.
- **VM sizing is an explicit flag.** `--cpu 4 --memory 8 --disk 60` is visible
  in the docs and in shell history, rather than set in a preferences pane.
- **No licensing question.** Docker Desktop requires a paid subscription for
  larger organisations; Colima is MIT.

If you already run Docker Desktop, OrbStack, or Rancher Desktop, everything here
works unchanged — skip `colima start` and make sure `docker info` succeeds.

## Sizing

```bash
colima start --cpu 4 --memory 8 --disk 60 --runtime docker
```

The full stack is three kind nodes, Calico, kube-prometheus-stack, OpenEBS and
the demo workload. Measured steady-state on this lab is roughly 2.5 GB of the
VM's 8 GB, which leaves comfortable headroom for Prometheus to grow.

Undersizing does not produce a clear error. It produces OOMKills and pod
evictions minutes into the install, which read as unrelated component failures.
`make preflight` warns below 4 CPU or 7 GB for that reason.

Disk matters more than it looks: kind node images, Calico, the Prometheus stack
and OpenEBS volume data all live inside the VM's 60 GB.

## Verifying the runtime

```bash
docker info --format 'Server {{.ServerVersion}} · {{.Architecture}} · {{.NCPU}} CPU'
```

Observed here:

```
Server 29.5.2 · aarch64 · 4 CPU
```

`aarch64` is expected on Apple Silicon. Every image this lab uses publishes
`arm64` builds — worth knowing, because an `exec format error` in a pod almost
always means an `amd64`-only image rather than anything wrong with the cluster.

## Reclaiming everything

```bash
make teardown                  # the cluster only
colima stop                    # pause the VM
colima delete                  # delete the VM and its 60 GB disk
brew uninstall colima docker kubectl kind helm python@3.12
```

Next: [01 — Python environment](01-python-environment.md)
