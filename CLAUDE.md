# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Kubernetes lab that builds a 3-node kind cluster on macOS with **no CNI**, then
installs Calico, OpenEBS and kube-prometheus-stack, and validates each layer with
pytest running against the live cluster.

There is no application to build. The "source" is declarative manifests, shell
scripts that apply them in order, and tests that assert the cluster actually
behaves as claimed. Changes are verified by running against a real cluster, not
by unit tests.

## Commands

```bash
make all              # full build: preflight → venv → cluster → cni → openebs → monitoring → workload → validate
make validate         # all 53 tests
make urls             # print endpoints, probe that they answer
make teardown         # delete the cluster (leaves Colima and brew tools)
```

Stages run individually and in dependency order: `preflight`, `venv`, `cluster`,
`cni`, `openebs`, `monitoring`, `workload`.

Test subsets:

```bash
make validate-net              # test_02
make validate-storage          # test_03 + test_04
make validate-metrics          # test_05 + test_06

.venv/bin/pytest validation/test_02_networking.py -v
.venv/bin/pytest validation/test_02_networking.py::test_networkpolicy_is_actually_enforced -v
```

Prerequisite outside the Makefile — the VM must be running first:

```bash
colima start --cpu 4 --memory 8 --disk 60 --runtime docker
```

Overrides honoured by the harness: `KCTX`, `PROM_URL`, `GRAFANA_URL`,
`GRAFANA_PASSWORD`. Component versions are pinned in `scripts/lib.sh`.

## Architecture

`macOS → Colima VM → Docker → kind node containers → Kubernetes`. Each layer
matters when debugging; a failure at a lower one presents as a mystery at every
layer above. See `ARCHITECTURE.md` for the full picture and `MANUAL-SETUP.md` for
the same build done by hand without the scripts.

**Scripts** (`scripts/`) are numbered by execution order and all source
`lib.sh`, which pins the kubectl context (`kc()`), holds version pins, and
provides `retry`/`step`/`die`. Never add a bare `kubectl` call to a script —
use `kc` so a stray `use-context` elsewhere cannot redirect the lab.

**Validation** (`validation/`) is numbered so pytest collects it in dependency
order; there is no point testing volume metrics before a volume exists. Files
still run standalone because each re-applies the manifests it needs. `conftest.py`
holds the shared machinery: `pod_exec` (parses the exit code off the exec API's
error channel — without this a failed in-pod command is indistinguishable from a
successful one), `wait_until`, `prom_query`, and fixtures that fail with the next
command to run rather than a bare connection error.

These tests mutate live infrastructure. `test_02` creates and deletes a
namespace; `test_04` deletes a running pod. Never point the suite at a cluster
that matters.

## Four things that are deliberate, not bugs

**The cluster comes up broken.** `disableDefaultCNI: true` in
`cluster/kind-config.yaml` means nodes sit `NotReady` with `cni plugin not
initialized` until `make cni` runs. That is the expected state after
`make cluster`.

**Pod CIDR is pinned to `10.244.0.0/16` in two places** — kind's `podSubnet` and
Calico's IPPool in `cluster/calico-installation.yaml`. They must stay identical.
Calico's stock `192.168.0.0/16` is avoided because it collides with common home
router ranges.

**Control-plane metrics binding is a cluster-creation-time fix.** The
`kubeadmConfigPatches` in `cluster/kind-config.yaml` rebind
kube-controller-manager, kube-scheduler, kube-proxy and etcd off `127.0.0.1`.
This cannot be changed later without editing static pod manifests inside the
node, so altering it means recreating the cluster. Note `extraArgs` is a **list**
under kubeadm v1beta4 (Kubernetes 1.31+); the old map form is silently ignored.

**`kubelet_volume_stats_*` is wrong here and is intentionally unused.** A
LocalPV Hostpath volume is a directory on a shared filesystem, so the kubelet
reports that filesystem's figures for every volume on the node — 1Gi, 2Gi and 8Gi
PVCs all report ~58.76 GiB capacity. Per-volume usage comes from the DaemonSet in
`manifests/monitoring/openebs-hostpath-exporter.yaml`, joined to kube-state-metrics
by the recording rules in `manifests/monitoring/openebs-monitoring.yaml`.
`docs/07` has the derivation.

## Conventions

**Dashboards** are ConfigMaps labelled `grafana_dashboard: "1"`, imported by the
Grafana sidecar from any namespace. Adding one means dropping a file into
`manifests/monitoring/dashboards/` and adding its UID to `DASHBOARD_UIDS` in
`validation/test_06_grafana.py`, which runs every panel's PromQL through
Grafana's datasource proxy and fails if any returns no data.

**Node metrics must not be summed.** kind nodes share the Colima VM's single
kernel — identical `MemTotal`, CPU count and `node_boot_time_seconds` across all
three. `sum(node_memory_MemTotal_bytes)` reports ~23 GiB for a 7.74 GiB VM. Use
`max()` or `avg()`. Only pod counts, network namespaces and volume directories
are genuinely per-node.

**Hand-written exporters**: Prometheus histogram buckets are cumulative and
nothing validates them. Record into the narrowest matching bucket only, then
accumulate at render; doing both produces buckets exceeding `_count` and
`histogram_quantile()` returns wrong numbers with no error. This bit the ledger
app once already.

**New scrape targets** need `serviceMonitorSelectorNilUsesHelmValues: false`
(already set in `manifests/monitoring/kube-prometheus-values.yaml`); without it
the operator ignores any ServiceMonitor not carrying the chart's release label,
silently.

## Documentation

`docs/00`–`docs/08` cover prerequisites through troubleshooting. When a build
step fails, `docs/08-troubleshooting.md` has verbatim error strings for every
failure encountered building this lab — check it before investigating from
scratch. Add new failures there as they are found.

Versions in `README.md`, `CLAUDE.md` and the docs are written against the pins in
`scripts/lib.sh`; bumping a pin means checking the docs still describe reality.
