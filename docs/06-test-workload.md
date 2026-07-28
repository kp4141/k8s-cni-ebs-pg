# 06 — Test workload

```bash
make workload
```

Deploys `ledger`, a StatefulSet in `storage-demo` whose only job is to make
persistence observable.

It has two jobs: prove persistence, and be a **real instrumented application** so
there is something to build an app-metrics dashboard from.

## Application metrics

The workload publishes its own metrics on `:8080/metrics`, scraped through a
ServiceMonitor in the same manifest.

This is a different category from what cAdvisor and kube-state-metrics already
provide. Those describe the pod from outside — CPU, memory, restart count. They
cannot tell you a write took 40 ms, that three writes failed with `ENOSPC`, or
how many bytes the application believes it has written. A pod sitting at 2% CPU
looks perfectly healthy while failing every write.

| Metric | Type | Meaning |
|---|---|---|
| `ledger_writes_total` | counter | Write cycles attempted |
| `ledger_write_errors_total` | counter | Write cycles that failed |
| `ledger_bytes_written_total` | counter | Bytes written |
| `ledger_write_duration_seconds` | histogram | Write latency distribution |
| `ledger_boots_total` | gauge | Container starts recorded on this volume |
| `ledger_file_size_bytes{file}` | gauge | Size of each file on the volume |
| `ledger_last_write_timestamp_seconds` | gauge | Unix time of the last success |
| `ledger_bulk_capped` | gauge | Whether `bulk.dat` hit its size cap |

Written against the Python standard library, with the exposition format emitted
by hand. That avoids a `pip install` at container start, so the pod has no
network dependency on the way up.

One detail worth copying if you write your own exporter: **Prometheus histogram
buckets are cumulative.** Each `le=` line must count every observation at or
below that edge, not just the ones falling between it and the previous edge, and
the `+Inf` bucket must equal `_count`. Getting this wrong makes
`histogram_quantile()` silently return nonsense — there is no error, just wrong
numbers. [test_application_histogram_is_well_formed](../validation/test_05_prometheus.py)
asserts the `+Inf`/`_count` invariant for exactly that reason.

The dashboard built on these is described in
[docs/07](07-metrics-and-dashboards.md#application--the-ledger-workload).

## Persistence

[manifests/workload/ledger-statefulset.yaml](../manifests/workload/ledger-statefulset.yaml)
mounts a 1Gi `openebs-hostpath` volume at `/data` and writes three files:

| File | Purpose |
|---|---|
| `boots.txt` | One line per container start. **The persistence proof.** |
| `ledger.log` | A timestamped line every 5s — evidence of continuous writes. |
| `bulk.dat` | Grows 256 KiB every 5s, capped at 200 MiB. Gives the usage metric a moving series to graph. |

`boots.txt` is the interesting one. Each start appends a line *and prints the
whole file*, so the container's own logs show the history of every previous pod
that used the volume:

```
=== boot history on this volume ===
boot 2026-07-28T02:22:25+00:00 pod=ledger-0 node=k8s-cni-lab-worker
boot 2026-07-28T02:23:23+00:00 pod=ledger-0 node=k8s-cni-lab-worker
=== boots recorded: 2 ===
```

Two entries from one pod name means the volume outlived a pod. That is the claim,
visible in `kubectl logs`.

### The 200 MiB cap

Without it `bulk.dat` grows forever. Since hostpath enforces no quota, it would
sail past the PVC's 1Gi request and keep going until the node's `/var` filled —
taking out every other volume on that node, including Prometheus. The pod would
start crash-looping on `ENOSPC`, which is a confusing way to learn about
[docs/04's](04-openebs-storage.md) quota caveat.

## Proving persistence by hand

```bash
kubectl -n storage-demo exec ledger-0 -- cat /data/boots.txt
kubectl -n storage-demo delete pod ledger-0
kubectl -n storage-demo logs ledger-0 | head
```

The StatefulSet recreates `ledger-0`, it reattaches the same PVC, and the boot
history contains the earlier entries rather than starting over.

## Proving it automatically

```bash
.venv/bin/pytest validation/test_04_workload.py -v
```

[test_data_survives_pod_deletion](../validation/test_04_workload.py) does the
strict version:

1. Write a unique sentinel (`written-at-<timestamp>`) to `/data/sentinel.txt`.
2. Read it back — confirms the write landed before anything is destroyed.
3. Record the boot count and the pod's **UID**.
4. Delete the pod.
5. Wait for a pod with the *same name but a different UID* to become Ready.
6. Assert the sentinel is still there, with the same content.
7. Assert the boot count went up by **exactly one**.

Steps 3 and 5 matter. Waiting on the name alone would race against the old pod
still terminating and could read the *original* pod's filesystem, which proves
nothing. The UID comparison guarantees we are talking to the replacement.

Step 7 is a tighter check than "the file exists": exactly one new boot means the
new pod reused the existing volume. A jump to 1 would mean a fresh empty volume;
a jump of 2+ would mean crash-looping.

A separate test asserts the pod was rescheduled onto the node its PV is pinned
to — the scheduler honouring the node affinity from
[docs/04](04-openebs-storage.md), which is what makes hostpath safe at all.

## Feeding the dashboard

The steady `bulk.dat` growth is what makes the Grafana time series show a real
climbing line instead of a flat one. Measured across this session, the demo
volume went from 12.26 MiB → 34.52 MiB while other volumes stayed roughly flat —
which is also how [test_demo_volume_is_growing](../validation/test_05_prometheus.py)
detects a stalled workload or a caching exporter.

Next: [07 — Metrics and dashboards](07-metrics-and-dashboards.md)
