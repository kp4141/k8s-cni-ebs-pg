# 07 — OpenEBS metrics discovery and dashboards

This is the part of the lab where the obvious answer is wrong.

## Discovery: what OpenEBS actually exposes

The first instinct is to find the OpenEBS exporter and point a ServiceMonitor at
it. For the LocalPV Hostpath engine, there isn't one:

```bash
$ kubectl -n openebs get svc
No resources found in openebs namespace.

$ kubectl -n openebs get pods -o jsonpath='{..containers[*].ports}'
        # empty — the provisioner declares no ports at all
```

The provisioner is a control-loop that creates directories and PV objects. It
publishes no metrics endpoint and the chart creates no Service. A ServiceMonitor
aimed at it would select nothing and sit there looking correct.

(Replicated PV Mayastor *does* ship exporters. It is disabled here for the
reasons in [docs/04](04-openebs-storage.md#why-not-mayastor).)

So volume observability has to be assembled from what already exists.

## The trap: kubelet_volume_stats_*

Every "monitor your PVCs" guide reaches for `kubelet_volume_stats_used_bytes`,
and for a real block-backed volume it is correct. For hostpath it is actively
misleading. Measured on this cluster:

| PVC | Requested | `kubelet_volume_stats_capacity_bytes` |
|---|---|---|
| `data-ledger-0` | 1Gi | 58.76 GiB |
| `alertmanager-...-db` | 1Gi | 58.76 GiB |
| `kps-grafana` | 2Gi | 58.76 GiB |
| `prometheus-...-db` | 8Gi | 58.76 GiB |

All identical, and all equal to the node's filesystem:

```
$ docker exec k8s-cni-lab-worker df -h /var/openebs/local
Filesystem      Size  Used Avail Use% Mounted on
/dev/vdb1        59G  7.8G   48G  14% /var
```

The reason is structural. A hostpath volume is a **directory on a shared
filesystem**, not a device with its own boundary. When the kubelet is asked for
that volume's stats it can only `statfs()` the path, which returns the
filesystem's numbers. Every volume on the node reports the same thing.

A "% full per volume" panel built on this shows four identical lines that all
move together and describe none of the volumes. Worse, an alert on it fires for
every volume simultaneously the moment the node's disk fills — technically true,
but useless for finding the culprit.

The same applies to `kubelet_volume_stats_inodes*`, for the same reason.

## The fix: measure the directories

[manifests/monitoring/openebs-hostpath-exporter.yaml](../manifests/monitoring/openebs-hostpath-exporter.yaml)
is a DaemonSet that walks `/var/openebs/local/*` and reports each directory's
real size:

```
openebs_hostpath_volume_used_bytes{persistentvolume="pvc-cbd3...",node="k8s-cni-lab-worker"} 36196352
openebs_hostpath_volumes{node="k8s-cni-lab-worker"} 2
openebs_hostpath_exporter_scrape_duration_seconds{node="k8s-cni-lab-worker"} 0.014
```

Design notes:

- **`st_blocks * 512`, not `st_size`.** Counts blocks actually allocated, the way
  `du` does. `st_size` over-reports sparse files and under-reports block rounding.
- **Directory name is the join key.** The provisioner names each directory after
  the PV, which is exactly what `kube_persistentvolume_claim_ref` keys on.
- **Read-only hostPath mount.** The component only ever measures.
- **Errors are skipped, not fatal.** Files vanish mid-walk during normal
  operation; a scrape should not fail because of it.

Unlike the provisioner, this is a genuine scrape target, so it gets a real
ServiceMonitor — and all three replicas show `up`.

The payoff is data that actually differs per volume:

```
monitoring/prometheus-...-db      11.36 MiB
monitoring/kps-grafana            49.58 MiB
monitoring/alertmanager-...-db     0.00 MiB
storage-demo/data-ledger-0        34.52 MiB   ← climbing
```

## Recording rules

[manifests/monitoring/openebs-monitoring.yaml](../manifests/monitoring/openebs-monitoring.yaml)
joins three sources into usable series.

The exporter knows PV names but nothing about PVCs. kube-state-metrics knows the
mapping. Joining them requires one trick — reconciling label names:

```promql
label_replace(
  label_replace(
    openebs_hostpath_volume_used_bytes
      * on (persistentvolume) group_left (claim_namespace, name)
        kube_persistentvolume_claim_ref,
    "namespace", "$1", "claim_namespace", "(.*)"
  ),
  "persistentvolumeclaim", "$1", "name", "(.*)"
)
```

`kube_persistentvolume_claim_ref` is an *info* metric: its value is always `1`
and its only job is carrying labels. Multiplying by it leaves the left-hand value
untouched while `group_left` grafts `claim_namespace` and `name` on. The two
`label_replace` calls then rename those to `namespace` and
`persistentvolumeclaim` — the spelling every other Kubernetes metric uses — so
the result joins cleanly with the rest of the metric namespace.

| Rule | Meaning |
|---|---|
| `openebs:volume_used_bytes` | Real bytes on disk, labelled by namespace + PVC |
| `openebs:volume_requested_bytes` | What the PVC asked for (from kube-state-metrics) |
| `openebs:volume_used_ratio` | used ÷ requested |
| `openebs:node_backing_fs_used_ratio` | `/var` utilisation per node |

**`volume_used_ratio` can legitimately exceed 1.0.** Hostpath enforces no quota,
so a volume may grow past its own request. That is not a bug in the metric — it
is the condition worth alerting on.

## Alerts

| Alert | Condition | Why it matters |
|---|---|---|
| `OpenEBSVolumeExceededRequest` | ratio > 1 for 5m | A volume is consuming more than it reserved; nothing will stop it |
| `OpenEBSVolumeNearRequest` | ratio > 0.85 | Early warning |
| `OpenEBSNodeBackingFilesystemFilling` | `/var` > 85% | **The real risk.** When this fills, every volume on the node fails together and the kubelet evicts for disk pressure |
| `OpenEBSPVCPending` | Pending for 10m | Brief Pending is normal under `WaitForFirstConsumer`; ten minutes means no pod ever scheduled |
| `OpenEBSHostpathExporterDown` | `up == 0` for 5m | The measurement is stale, not the storage |

## Three layers of metrics

It is worth being precise about what each layer can and cannot tell you, because
they are easy to conflate and the gaps only show up when you need them.

| Layer | Source | Answers | Cannot answer |
|---|---|---|---|
| **Host / VM** | node-exporter | Is the VM out of CPU, RAM or disk? | Which workload caused it |
| **Infrastructure** | cAdvisor, kube-state-metrics, our hostpath exporter | How much CPU/memory/disk is this pod or volume using? | Whether the application is actually working |
| **Application** | the workload's own `/metrics` | Write latency, error counts, business counters | Anything outside the process |

The middle layer describes a black box from outside. It will happily report a
healthy pod at 2% CPU that has silently failed every write for an hour. Only the
application can say that, and only if instrumented — which is why the demo
workload publishes `ledger_*` metrics ([docs/06](06-test-workload.md)).

## The dashboards

Three dashboards ship with this lab, all delivered as labelled ConfigMaps and
imported by the Grafana sidecar. They sit alongside the ~29 stock
kube-prometheus-stack dashboards.

| Dashboard | UID | Layer |
|---|---|---|
| Infrastructure - Colima VM and kind nodes | `vm-infra` | Host / VM |
| OpenEBS - LocalPV Hostpath Storage | `openebs-storage` | Infrastructure |
| Application - ledger | `app-ledger` | Application |

### Infrastructure — the VM

[vm-infra-dashboard.yaml](../manifests/monitoring/dashboards/vm-infra-dashboard.yaml)

The important thing this gets right: **kind nodes share one kernel.** All three
node-exporter instances report the same Colima VM — identical `MemTotal`
(7.74 GiB), CPU count (4), and `node_boot_time_seconds`. So
`sum(node_memory_MemTotal_bytes)` returns ~23 GiB for a 7.74 GiB VM, and the
stock upstream node dashboards do exactly that.

Every VM-level panel here aggregates with `max()` or `avg()`:

```promql
max(node_memory_MemTotal_bytes)                          # not sum()
1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))   # avg over cpu+instance
```

A second row covers what genuinely *is* per-node — pods scheduled, network
throughput (each kind node has its own network namespace), and top pods by
CPU/memory from cAdvisor.

### Application — the ledger workload

[app-ledger-dashboard.yaml](../manifests/monitoring/dashboards/app-ledger-dashboard.yaml)

Built entirely from `ledger_*` series: write rate, byte throughput, latency
percentiles from the application's histogram, error counts, and file sizes as
the application sees them.

The last panel is the interesting one — it plots the application's own byte
accounting against the hostpath exporter's on-disk measurement of the same
volume. They should track together. A widening gap means the two layers disagree
about reality, which is exactly the kind of thing neither layer can detect alone.

Latency percentiles come from the histogram:

```promql
histogram_quantile(0.99, sum by (le) (rate(ledger_write_duration_seconds_bucket[5m])))
```

`sum by (le)` before `histogram_quantile` is mandatory — quantiles must be
computed over the aggregated bucket counts, not per-series and then averaged.

### Storage — OpenEBS

[openebs-storage-dashboard.yaml](../manifests/monitoring/dashboards/openebs-storage-dashboard.yaml),
imported by the Grafana sidecar from a labelled ConfigMap.

| Panel | Form | Why that form |
|---|---|---|
| Volumes / Requested / Consumed | stat | Single headline numbers — a chart would add nothing |
| Closest to its request | stat, thresholded | One number where the *state* is the message |
| Volume consumption over time | time series | Change over time, one line per volume |
| Consumed vs requested | bar gauge | Comparing a bounded ratio across a handful of items |
| Volume inventory | table | Enumerable facts, three measures joined per row |
| Node backing filesystem | time series | The real ceiling, per node |
| Volumes per node | time series | Uneven distribution concentrates risk |

Conventions worth keeping if you extend it:

- **Status colours are reserved.** Green/amber/red appear only where they encode
  state (fill ratio), never as series identity. Per-volume series use a
  categorical palette.
- **The ratio scale runs to 1.2, not 1.0**, so an overrun is visible rather than
  clipped at the top of the bar.
- **No dual axes.** Bytes and ratios are separate panels.
- **Units are declared** (`bytes`, `percentunit`) so Grafana formats them and the
  numbers stay readable.

## Validating that it works

A dashboard that loads is not a dashboard that works. Panels referencing a
metric that doesn't exist render as an empty chart, not an error — nothing
notices.

[test_06_grafana.py](../validation/test_06_grafana.py) closes that gap by
extracting **every panel's PromQL** from the imported dashboard and running each
one through Grafana's own datasource proxy:

```python
resolved = expr.replace("$pvc", ".*")      # expand the template variable
resp = requests.get(f"{GRAFANA_URL}/api/datasources/proxy/uid/{uid}/api/v1/query",
                    params={"query": resolved}, auth=GRAFANA_AUTH)
...
elif not body["data"]["result"]:
    empty.append(...)
```

Going through the proxy rather than querying Prometheus directly also tests
Grafana's *own* connection to the datasource — the in-cluster Service path, which
can be broken while the NodePort path we use for testing works fine.

On the Prometheus side, [test_05](../validation/test_05_prometheus.py) asserts
the recording rules produce series at all (a bad join yields silence), that every
series carries PVC identity, that the demo volume is growing, and — directly
guarding the trap this document is about:

```python
assert len(set(values.values())) > 1, (
    "every volume reports an identical size, which means the metric is "
    "measuring the node filesystem rather than the volumes"
)
```

Next: [08 — Troubleshooting](08-troubleshooting.md)
