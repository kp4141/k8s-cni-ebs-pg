# Architecture

Complete design of the lab: what runs where, how the layers stack, and why each
significant choice was made. Values shown are from a live build, not idealised.

Diagrams are ASCII so they render in any viewer — VS Code, a terminal, GitHub, or
a plain text editor.

---

## 1. The virtualisation stack

Kubernetes nodes are Linux machines. macOS cannot run them directly, so every
local Kubernetes setup is "Linux in a VM, with something on top". This lab is
four layers deep, and knowing which layer you are debugging saves most of the
time.

```
┌───────────────────────────────────────────────────────────────────────────┐
│  macOS  ·  Apple Silicon (arm64)                                          │
│                                                                           │
│   brew tools:  kubectl   kind   helm   colima                             │
│   .venv     :  pytest validation harness  ──────────────┐                 │
│                                                         │                 │
│  ┌──────────────────────────────────────────────────────┼───────────────┐ │
│  │  Colima VM  ·  4 vCPU / 7.74 GiB / 60 GB disk        │               │ │
│  │  ONE Linux kernel 6.8.0  ·  Virtualization.framework │               │ │
│  │                                                      │               │ │
│  │  ┌───────────────────────────────────────────────────┼─────────────┐ │ │
│  │  │  Docker 29.5.2 (aarch64)                          │             │ │ │
│  │  │                                                   │             │ │ │
│  │  │  ┌────────────────────────────────────────────────┼───────────┐ │ │ │
│  │  │  │  kind cluster  ·  Kubernetes v1.36.1           │           │ │ │ │
│  │  │  │                                                ▼           │ │ │ │
│  │  │  │   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐       │ │ │ │
│  │  │  │   │ control-plane│ │   worker     │ │   worker2    │       │ │ │ │
│  │  │  │   │ 172.18.0.3   │ │ 172.18.0.2   │ │ 172.18.0.4   │       │ │ │ │
│  │  │  │   └──────────────┘ └──────────────┘ └──────────────┘       │ │ │ │
│  │  │  │        (each node is a Docker container, not a machine)    │ │ │ │
│  │  │  └────────────────────────────────────────────────────────────┘ │ │ │
│  │  └──────────────────────────────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────────────────┘│
└───────────────────────────────────────────────────────────────────────────┘

  Access path from a browser:
    localhost:30030 ──► Colima port forward ──► kind extraPortMapping ──► Service
```

**The consequence that matters most:** the three kind "nodes" are containers
sharing the VM's *single* kernel. They are not three machines. Every
node-exporter therefore reports the same hardware, so summing node metrics
multiplies by three. Covered in §7.

---

## 2. Cluster topology

```
                        ┌───────────────────────────────────┐
                        │   k8s-cni-lab-control-plane       │
                        │   172.18.0.3                      │
                        │   pod block  10.244.40.128/26     │
                        │                                   │
                        │   etcd · apiserver · scheduler    │
                        │   controller-manager · kube-proxy │
                        │   coredns ×2                      │
                        └─────────────────┬─────────────────┘
                                          │
                     Docker bridge  172.18.0.0/16
                        ┌─────────────────┴─────────────────┐
                        │                                   │
        ┌───────────────▼──────────────┐   ┌────────────────▼─────────────┐
        │   k8s-cni-lab-worker         │   │   k8s-cni-lab-worker2        │
        │   172.18.0.2                 │   │   172.18.0.4                 │
        │   pod block 10.244.247.192/26│   │   pod block 10.244.67.0/26   │
        │                              │   │                              │
        │   ledger-0      (1Gi PVC)    │   │   prometheus-0   (8Gi PVC)   │
        │   alertmanager-0(1Gi PVC)    │   │   kps-grafana    (2Gi PVC)   │
        └──────────────────────────────┘   └──────────────────────────────┘

   every node also runs:  calico-node · csi-node-driver · kube-proxy
                          node-exporter · openebs-hostpath-exporter
```

Networks:

| Range | Purpose |
|---|---|
| `172.18.0.0/16` | Docker bridge — node IPs |
| `10.244.0.0/16` | Pod CIDR — kind `podSubnet` **and** Calico IPPool |
| `10.96.0.0/16` | Service CIDR (`kubernetes` API at `10.96.0.1`) |

Two workers exist so pod-to-pod traffic genuinely crosses a node boundary. On a
single node the packets never leave one bridge, and networking tests would pass
against a completely broken overlay.

The cluster is created **without a CNI**. Nodes stay `NotReady` with
`cni plugin not initialized`, and CoreDNS stays `Pending`, until Calico is
installed. Host-networked components run regardless — which is itself a useful
map of what depends on the pod network:

```
   BEFORE the CNI is installed
   ───────────────────────────────────────────────────────────
   etcd                    Running    hostNetwork — no CNI needed
   kube-apiserver          Running    hostNetwork
   kube-scheduler          Running    hostNetwork
   kube-controller-manager Running    hostNetwork
   kube-proxy       ×3     Running    hostNetwork
   coredns          ×2     Pending    ordinary pod — BLOCKED
   ───────────────────────────────────────────────────────────
   nodes                   NotReady   "cni plugin not initialized"
```

---

## 3. Network layer — Calico v3.32.1

### Install sequence — the order is not optional

```
   STEP 1                    STEP 2                     STEP 3
   ┌──────────────────┐      ┌──────────────────┐      ┌─────────────────────┐
   │ operator-crds    │ ───► │ tigera-operator  │ ───► │ Installation CR     │
   │ .yaml            │      │ .yaml            │      │ (our config)        │
   │                  │      │                  │      │                     │
   │ teaches the API  │      │ runs a controller│      │ declares intent —   │
   │ what an          │      │ that waits for   │      │ operator now builds │
   │ "Installation" is│      │ instructions     │      │ the CNI             │
   └──────────────────┘      └──────────────────┘      └─────────────────────┘
        │                                                        │
        │ skip this and step 3 fails with:                       ▼
        │   no matches for kind "Installation"           calico-system pods
        └───────────────────────────────────────►        appear, nodes go Ready
```

Calico v3.30 split the CRDs into their own manifest. Guides written before that
apply only `tigera-operator.yaml`, leaving a running operator with nothing to
configure it. Both applies need `--server-side` — the CRDs exceed the
262144-byte cap on the annotation client-side apply writes.

### What gets deployed

```
   ┌─ tigera-operator ns ────────────────────────────────────────────┐
   │   tigera-operator (Deployment)                                  │
   │        │  watches Installation CR, reconciles everything below  │
   └────────┼────────────────────────────────────────────────────────┘
            ▼
   ┌─ calico-system ns ──────────────────────────────────────────────┐
   │                                                                 │
   │   calico-node         DaemonSet ×3   hostNetwork                │
   │        └─ programs routes + iptables on each node               │
   │           THE data plane. Host-networked because it is what     │
   │           creates the pod network in the first place.           │
   │                                                                 │
   │   calico-typha        Deployment ×2  datastore fan-out          │
   │   calico-kube-ctrl    Deployment     IPAM + policy controllers  │
   │   calico-apiserver    Deployment ×2  projectcalico.org API      │
   │   csi-node-driver     DaemonSet ×3                              │
   └─────────────────────────────────────────────────────────────────┘
```

### Configuration

| Setting | Value | Reason |
|---|---|---|
| `cidr` | `10.244.0.0/16` | Must equal kind's `podSubnet`. Calico's stock `192.168.0.0/16` collides with home routers |
| `blockSize` | `/26` | 64 addresses per node; stops one node monopolising the pool |
| `encapsulation` | `VXLANCrossSubnet` | kind nodes share one bridge, so traffic routes natively with no encap overhead |
| `nodeAddressAutodetectionV4` | `kubernetes: NodeInternalIP` | Default "first found" can pick a Docker interface instead of the node's real one |

### Cross-node packet path

This is the path the validation suite exercises directly, by IP, bypassing
Services:

```
   client pod                                          web pod
   10.244.247.239                                      10.244.67.18
   on worker                                           on worker2
        │                                                   ▲
        │  1. packet leaves pod's veth                      │ 6. delivered
        ▼                                                   │
   ┌─────────────────────┐                         ┌────────┴────────────┐
   │ worker node         │                         │ worker2 node        │
   │ calico routes:      │  2. routed to peer node │ calico routes:      │
   │  10.244.67.0/26 ──► │ ───────────────────────►│  local pod veth     │
   │  via 172.18.0.4     │    over Docker bridge   │                     │
   └─────────────────────┘    (same subnet, so     └─────────────────────┘
                               CrossSubnet = no encapsulation)
```

---

## 4. Storage layer — OpenEBS 4.5.1

Only **LocalPV Hostpath** is enabled:

| Engine | State | Blocker |
|---|---|---|
| LocalPV Hostpath | **enabled** | none — just a directory |
| LocalPV LVM | disabled | needs a real volume group |
| LocalPV ZFS | disabled | needs a zpool + kernel module |
| LocalPV Rawfile | disabled | pre-stable upstream |
| Replicated (Mayastor) | disabled | needs hugepages, NVMe, dedicated devices |
| Loki + Alloy | disabled | duplicate collector; memory pressure on 8 GB |

### Provisioning sequence

`WaitForFirstConsumer` is required, not stylistic. The diagram shows why:

```
   TIME ──────────────────────────────────────────────────────────────────►

   1. PVC created
      ┌────────────────┐
      │ PVC: Pending   │   Provisioner CANNOT act yet.
      │ 1Gi            │   A hostpath volume is a directory on ONE node,
      └────────────────┘   and nobody knows which node yet.
              │
   2. Pod created that mounts the PVC
              │
              ▼
      ┌────────────────┐
      │ scheduler picks│   NOW the node is known: worker
      │ node = worker  │
      └────────┬───────┘
               │
   3. Provisioner acts
               ▼
      ┌──────────────────────────────────────────────────┐
      │ mkdir /var/openebs/local/pvc-cbd3ec04-…          │  on worker
      │ create PV  +  nodeAffinity → worker              │
      └──────────────────────────────────────────────────┘
               │
   4.          ▼
      ┌────────────────┐
      │ PVC: Bound     │   Pod starts, volume mounted at /data
      └────────────────┘
```

A PVC sitting `Pending` with no consuming pod is correct behaviour, not a fault.

### Node affinity is load-bearing

```
   ┌─────────────────────────────────────────────────────────────┐
   │  PV pvc-cbd3ec04-…                                          │
   │    nodeAffinity: kubernetes.io/hostname In [worker]         │
   └───────────────────────────┬─────────────────────────────────┘
                               │ pins
                               ▼
   ┌──────────────────────┐         ┌──────────────────────┐
   │ worker               │         │ worker2              │
   │  /var/openebs/local/ │         │  (no such directory) │
   │    pvc-cbd3ec04-…/   │         │                      │
   │      boots.txt   ✓   │   ✗ ──► │  pod would start with│
   │      ledger.log  ✓   │  never  │  an EMPTY dir and    │
   │      bulk.dat    ✓   │         │  report no error     │
   └──────────────────────┘         └──────────────────────┘
```

Without that affinity the pod could restart on worker2, find nothing, and
continue — silent data loss with no error anywhere.

### Live volume placement

| PVC | Size | Node |
|---|---|---|
| `monitoring/prometheus-kps-prometheus-db-…-0` | 8Gi | worker2 |
| `monitoring/kps-grafana` | 2Gi | worker2 |
| `monitoring/alertmanager-…-db-…-0` | 1Gi | worker |
| `storage-demo/data-ledger-0` | 1Gi | worker |

`openebs-hostpath` is made the cluster default and kind's
`rancher.io/local-path` demoted — otherwise a PVC omitting `storageClassName`
silently bypasses OpenEBS entirely.

**Limits by design:** no quota enforcement (the `1Gi` request is advisory; a
volume can grow until the node's `/var` fills, taking out every volume on that
node), no replication, no portability, and `kind delete cluster` destroys the
data with the node filesystems.

---

## 5. Observability layer

```
  ┌──── SCRAPE TARGETS — 34 across 14 jobs ───────────────────────────┐
  │                                                                   │
  │  CONTROL PLANE  (only reachable because of the kubeadm patches)   │
  │    kube-controller-manager :10257    kube-scheduler   :10259      │
  │    kube-proxy              :10249    etcd             :2381       │
  │                                                                   │
  │  NODE / KUBELET                                                   │
  │    kubelet ×4 endpoints ×3 nodes     node-exporter    ×3          │
  │      (main · cAdvisor · probes · resource)                        │
  │                                                                   │
  │  CLUSTER STATE                                                    │
  │    kube-state-metrics                apiserver                    │
  │                                                                   │
  │  THIS REPO'S OWN                                                  │
  │    openebs-hostpath-exporter ×3      ledger app /metrics          │
  └──────────────────────────┬────────────────────────────────────────┘
                             │  scraped every 15–30s
                             ▼
              ┌──────────────────────────────────┐
              │  PROMETHEUS                      │
              │  TSDB on an 8Gi OpenEBS PVC      │
              │  retention 6h / 5GB              │
              │                                  │
              │  recording rules ──► openebs:*   │
              │  alert rules     ──► 5 alerts    │
              └───────┬──────────────────┬───────┘
                      │                  │
                      ▼                  ▼
        ┌─────────────────────┐   ┌──────────────────┐
        │  GRAFANA            │   │  ALERTMANAGER    │
        │  2Gi OpenEBS PVC    │   │  1Gi OpenEBS PVC │
        │  32 dashboards      │   └──────────────────┘
        └──────────▲──────────┘
                   │ sidecar imports
        ┌──────────┴─────────────────────────┐
        │  ConfigMaps labelled               │
        │  grafana_dashboard="1"             │
        │  (any namespace)                   │
        └────────────────────────────────────┘
```

Prometheus, Grafana and Alertmanager all store on OpenEBS, so the monitoring
stack is itself the storage layer's most demanding consumer — storage failures
surface as monitoring failures rather than hiding until something needs them.

Two settings are load-bearing:

- **`serviceMonitorSelectorNilUsesHelmValues: false`** — without it the operator
  adopts only monitors carrying the chart's release label, and hand-written ones
  are ignored silently.
- **`kubelet.serviceMonitor.resourcePath: /metrics/resource`** — the chart still
  defaults to the `v1alpha1` path removed in Kubernetes 1.24, which 404s.

---

## 6. The three metric layers

Conflating these is the most common observability mistake in this stack.

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │  HEIGHT 3 — INSIDE THE PROCESS          source: app's own /metrics  │
   │                                                                     │
   │   ledger_writes_total          ledger_write_duration_seconds        │
   │   ledger_write_errors_total    ledger_boots_total                   │
   │                                                                     │
   │   ANSWERS: is it actually working? how slow? how many failed?       │
   │   BLIND TO: anything outside the process                            │
   │   DASHBOARD: app-ledger                                             │
   ├─────────────────────────────────────────────────────────────────────┤
   │  HEIGHT 2 — THE CONTAINER, FROM OUTSIDE                             │
   │                          source: cAdvisor, kube-state-metrics,      │
   │                                  our hostpath exporter              │
   │   container_cpu_usage_seconds_total   kube_pod_status_phase         │
   │   openebs:volume_used_bytes                                         │
   │                                                                     │
   │   ANSWERS: what is this pod/volume consuming?                       │
   │   BLIND TO: whether the app works — a pod at 2% CPU can be          │
   │             failing every request, silently                         │
   │   DASHBOARD: openebs-storage                                        │
   ├─────────────────────────────────────────────────────────────────────┤
   │  HEIGHT 1 — THE HOST / VM               source: node-exporter       │
   │                                                                     │
   │   node_cpu_seconds_total    node_memory_MemAvailable_bytes          │
   │   node_filesystem_avail_bytes                                       │
   │                                                                     │
   │   ANSWERS: is the VM out of CPU / RAM / disk?                       │
   │   BLIND TO: which workload caused it                                │
   │   DASHBOARD: vm-infra                                               │
   └─────────────────────────────────────────────────────────────────────┘
```

There is one dashboard per height — that is why there are three.

---

## 7. Two measurement corrections

Both were found by measurement, not assumed.

### Per-volume usage cannot come from the kubelet

```
   WHAT THE KUBELET REPORTS                    WHAT IS ACTUALLY THERE

   ┌──────────────────────────┐              ┌──────────────────────────┐
   │ node filesystem /var     │              │ node filesystem /var     │
   │ 58.76 GiB                │              │ 58.76 GiB                │
   │                          │              │  ┌────────────────────┐  │
   │  statfs() on ANY volume  │              │  │ pvc-A/  34.52 MiB  │  │
   │  path returns THIS       │              │  ├────────────────────┤  │
   │                          │              │  │ pvc-B/  49.58 MiB  │  │
   │  ledger-0    → 58.76 GiB │              │  ├────────────────────┤  │
   │  grafana     → 58.76 GiB │              │  │ pvc-C/  11.36 MiB  │  │
   │  prometheus  → 58.76 GiB │              │  └────────────────────┘  │
   └──────────────────────────┘              └──────────────────────────┘
      identical, useless                        real, per-volume
```

A hostpath volume is a directory on a shared filesystem — there is no wall to
measure, so `statfs()` returns the whole filesystem. A 1Gi, 2Gi and 8Gi PVC all
reported 58.76 GiB.

The fix is a DaemonSet measuring each PV directory with `st_blocks * 512`, then
joining to kube-state-metrics for identity:

```
   openebs_hostpath_volume_used_bytes        kube_persistentvolume_claim_ref
   { persistentvolume="pvc-cbd3…" }          { persistentvolume="pvc-cbd3…",
              │                                 claim_namespace="storage-demo",
              │                                 name="data-ledger-0" }
              │                                          │
              └────────── * on(persistentvolume) ────────┘
                            group_left(...)
                                  │
                                  ▼
                    openebs:volume_used_bytes
                    { namespace="storage-demo",
                      persistentvolumeclaim="data-ledger-0" }
```

`kube_persistentvolume_claim_ref` is an info metric (value always 1) whose job is
carrying labels; multiplying leaves the value untouched while `group_left` grafts
identity on. `label_replace` then renames `claim_namespace`/`name` to the
`namespace`/`persistentvolumeclaim` spelling the rest of the metric namespace
uses.

### Node metrics must not be summed

```
   THREE node-exporters                    ONE actual machine
   ┌───────────────────┐
   │ 172.18.0.2:9100   │──┐
   │ MemTotal 7.74 GiB │  │              ┌────────────────────────┐
   │ boot 1785204879   │  │              │  Colima VM             │
   ├───────────────────┤  │              │  ONE Linux kernel      │
   │ 172.18.0.3:9100   │  ├─── all       │  7.74 GiB total        │
   │ MemTotal 7.74 GiB │  │    describe─►│  4 vCPU                │
   │ boot 1785204879   │  │              │  booted once           │
   ├───────────────────┤  │              └────────────────────────┘
   │ 172.18.0.4:9100   │──┘
   │ MemTotal 7.74 GiB │        identical boot_time = one kernel
   │ boot 1785204879   │
   └───────────────────┘

   sum() ► 23.2 GiB   ✗ WRONG — three windows, one room
   max() ►  7.74 GiB  ✓ correct
```

Identical `node_boot_time_seconds` is the proof. `sum()` is exactly what the
stock upstream node dashboards do, so treat their totals as unreliable here.

Genuinely per-node and safe to aggregate separately: pod counts, network
namespaces (each kind node has its own), and volume directories.

---

## 8. Validation architecture

53 pytest tests run against the live cluster. No mocks — a pass means the cluster
did the thing.

```
   ┌────────────────────────┐
   │  .venv/bin/pytest      │
   └───┬────────┬───────┬───┘
       │        │       │
       │        │       └──────────────► HTTP :30030  Grafana
       │        │                          └─ /api/datasources/proxy/…
       │        │                             runs every panel's PromQL
       │        │
       │        └──────────────────────► HTTP :30090  Prometheus
       │                                   └─ /api/v1/targets, /query, /rules
       │
       └───────────────────────────────► Kubernetes API
                                           ├─ read nodes / pods / PVCs
                                           ├─ exec INTO pods (network tests)
                                           └─ delete pods (persistence test)
```

Files are numbered so pytest collects them in dependency order — no point testing
volume metrics before a volume exists — but each re-applies what it needs so it
also runs standalone.

```
   test_01_cluster      nodes Ready · Calico everywhere · pod IPs in CIDR
   test_02_networking   cross-node · DNS · Service · NetworkPolicy ENFORCED
   test_03_storage      SC config · WaitForFirstConsumer · PV node pinning
   test_04_workload     data survives pod deletion · pod follows its volume
   test_05_prometheus   34 targets up · rules produce series · app metrics
   test_06_grafana      datasource healthy · every panel query returns data
```

Design choices worth preserving:

- **Fixtures are shaped so the assertions mean something.** `web` uses required
  anti-affinity to force replicas onto different nodes, and a test asserts that
  happened. Otherwise "cross-node" results might be same-node results.
- **NetworkPolicy is tested by observing traffic stop.** Kubernetes accepts a
  policy on a cluster with no policy engine and traffic keeps flowing, so
  "applied cleanly" proves nothing.
- **Persistence is tested by UID.** `test_04` records the pod UID before deleting
  it and waits for a *different* UID, so it cannot accidentally read the original
  pod's filesystem while it is still terminating.
- **Every dashboard panel's PromQL is executed** through Grafana's datasource
  proxy. A panel referencing a non-existent metric renders as an empty chart, not
  an error — nothing else would notice. Going via the proxy also tests Grafana's
  own in-cluster connection, which can break while the NodePort path works.
- **Targets are polled until settled, not sampled once.** `test_04` deletes a
  pod, so its target is legitimately down for a few seconds afterwards.

---

## 9. Component inventory

| Namespace | Workload | Kind | Purpose |
|---|---|---|---|
| `tigera-operator` | `tigera-operator` | Deployment | Reconciles the Calico Installation |
| `calico-system` | `calico-node` | DaemonSet ×3 | Data plane; programs routes and policy |
| `calico-system` | `calico-typha` | Deployment ×2 | Datastore fan-out |
| `calico-system` | `calico-kube-controllers` | Deployment | IPAM and policy controllers |
| `calico-system` | `calico-apiserver` | Deployment ×2 | `projectcalico.org` API group |
| `calico-system` | `csi-node-driver` | DaemonSet ×3 | Calico CSI |
| `openebs` | `openebs-localpv-provisioner` | Deployment | Creates hostpath dirs and PVs |
| `monitoring` | `kps-operator` | Deployment | Prometheus Operator |
| `monitoring` | `prometheus-kps-prometheus` | StatefulSet | TSDB, 8Gi OpenEBS PVC |
| `monitoring` | `kps-grafana` | Deployment | Dashboards, 2Gi OpenEBS PVC |
| `monitoring` | `alertmanager-kps-alertmanager` | StatefulSet | Alerts, 1Gi OpenEBS PVC |
| `monitoring` | `kps-kube-state-metrics` | Deployment | Kubernetes object metrics |
| `monitoring` | `kps-prometheus-node-exporter` | DaemonSet ×3 | Host/VM metrics |
| `monitoring` | `openebs-hostpath-exporter` | DaemonSet ×3 | Per-volume usage (this repo) |
| `storage-demo` | `ledger` | StatefulSet | Persistence proof + app metrics |
| `net-test` | `web`, `client` | Deployment / DaemonSet | CNI test fixtures (transient) |

Endpoints:

| Service | URL | Auth |
|---|---|---|
| Prometheus | http://localhost:30090 | none |
| Grafana | http://localhost:30030 | `admin` / `admin` |

---

## 10. Build order and why it is fixed

```
   preflight ─► venv ─► cluster ─► cni ─► openebs ─► monitoring ─► workload ─► validate
       │         │        │         │        │           │            │           │
       │         │        │         │        │           │            │           └─ metrics need
       │         │        │         │        │           │            │              a scrape or two
       │         │        │         │        │           │            └─ ServiceMonitor CRD
       │         │        │         │        │           │               must already exist
       │         │        │         │        │           └─ Prometheus would sit Pending
       │         │        │         │        │              on an unbindable PVC
       │         │        │         │        └─ needs a working pod network
       │         │        │         └─ nothing can be scheduled without a CNI
       │         │        └─ needs Docker running
       │         └─ needs python3.12
       └─ needs the VM up
```

Each step fails confusingly if skipped, so `40-install-monitoring.sh` checks for
the StorageClass up front and fails fast with the fix rather than letting
Prometheus hang.

`scripts/lib.sh` holds the version pins and shared helpers; every script sources
it and uses `kc()` so a stray `kubectl config use-context` elsewhere on the
machine cannot redirect the lab at the wrong cluster.

---

## Related documents

- [EXPLAINED.md](EXPLAINED.md) — the same system explained with no prior knowledge assumed
- [MANUAL-SETUP.md](MANUAL-SETUP.md) — the same build by hand, no scripts
- [docs/07](docs/07-metrics-and-dashboards.md) — metric derivation in full
- [docs/08](docs/08-troubleshooting.md) — failure modes with verbatim errors
