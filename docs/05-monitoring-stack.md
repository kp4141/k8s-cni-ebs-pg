# 05 — Monitoring stack

```bash
make monitoring
```

Installs kube-prometheus-stack 87.20.0: Prometheus Operator, Prometheus,
Grafana 13.1.1, Alertmanager, node-exporter and kube-state-metrics.

Storage must exist first. Installing this before a working StorageClass leaves
Prometheus `Pending` on an unbindable PVC, which is a slow and confusing way to
discover the ordering — so the script checks and fails fast with the fix.

## The stack stores itself on OpenEBS

```
NAME                                        STATUS   CAPACITY   STORAGECLASS
alertmanager-kps-alertmanager-db-...-0      Bound    1Gi        openebs-hostpath
kps-grafana                                 Bound    2Gi        openebs-hostpath
prometheus-kps-prometheus-db-...-0          Bound    8Gi        openebs-hostpath
```

This is deliberate. Prometheus' TSDB is a demanding stateful workload with
continuous writes — a far better exercise of the storage layer than any purpose-
built demo, and it means storage failures surface as monitoring failures rather
than staying hidden until something needs them.

## Configuration worth knowing

Full file: [manifests/monitoring/kube-prometheus-values.yaml](../manifests/monitoring/kube-prometheus-values.yaml).

### Adopting hand-written monitors

```yaml
serviceMonitorSelectorNilUsesHelmValues: false
podMonitorSelectorNilUsesHelmValues: false
ruleSelectorNilUsesHelmValues: false
```

By default the operator only adopts ServiceMonitors and PrometheusRules carrying
the chart's own release label. Anything you write by hand is *silently ignored*:
`kubectl get servicemonitor` shows your object, everything looks correct, and it
simply never appears in Prometheus. Setting these to `false` means "select
everything".

This lab's hostpath exporter ServiceMonitor and OpenEBS PrometheusRule both
depend on it.

### Retention

```yaml
retention: 6h
retentionSize: 5GB
```

`retentionSize` is the real guard. Time-based retention alone can still fill an
8Gi volume if scrape volume grows, and on hostpath a full volume threatens the
whole node's disk rather than just Prometheus.

### The kubelet resource path

```yaml
kubelet:
  serviceMonitor:
    resourcePath: /metrics/resource
```

The chart still defaults this to `/metrics/resource/v1alpha1`, an endpoint
Kubernetes **removed in 1.24**. On anything newer the kubelet returns 404 and
that target sits permanently down.

It is easy to miss because the kubelet's other three endpoints (main, cAdvisor,
probes) scrape fine, so metrics keep flowing and dashboards look healthy. This
lab hit it directly — `test_no_target_is_down` caught three 404ing targets that
nothing else noticed.

### Grafana dashboard sidecar

```yaml
sidecar:
  dashboards:
    enabled: true
    label: grafana_dashboard
    searchNamespace: ALL
    folderAnnotation: grafana_folder
```

Any ConfigMap labelled `grafana_dashboard: "1"` in any namespace is imported
automatically. Dashboards stay in git, survive Grafana restarts, and need no API
calls to install.

## Scrape targets

All 34 targets across 14 jobs are healthy:

```
apiserver                    up  x1
coredns                      up  x2
kps-alertmanager             up  x2
kps-grafana                  up  x1
kps-operator                 up  x1
kps-prometheus               up  x2
kube-controller-manager      up  x1     ← needs the kubeadm bind-address patch
kube-etcd                    up  x1     ← needs the kubeadm bind-address patch
kube-proxy                   up  x3     ← needs the kubeadm bind-address patch
kube-scheduler               up  x1     ← needs the kubeadm bind-address patch
kube-state-metrics           up  x1
kubelet                      up  x12
node-exporter                up  x3
openebs-hostpath-exporter    up  x3     ← this lab's own exporter
```

The four marked jobs are the ones that are down on a stock kind cluster. See
[docs/02](02-kubernetes-install.md#exposing-control-plane-metrics).

`kubelet up x12` is 4 endpoints × 3 nodes: main metrics, cAdvisor, probes and
resource.

## Access

```bash
make urls
```

| Service | URL | Credentials |
|---|---|---|
| Prometheus | <http://localhost:30090> | none |
| Grafana | <http://localhost:30030> | `admin` / `admin` |
| OpenEBS dashboard | <http://localhost:30030/d/openebs-storage> | |

Lab-only credentials — fine on a local kind cluster, not a pattern to carry into
anything network-reachable.

If a NodePort stops answering after a Colima restart:

```bash
kubectl -n monitoring port-forward svc/kps-prometheus 30090:9090
kubectl -n monitoring port-forward svc/kps-grafana    30030:80
```

## Validation

```bash
make validate-metrics
```

The checks that matter most:

- **Every expected job has a target**, and **no target is down** — these
  distinguish "not configured" from "configured but broken".
- **Control-plane components are specifically asserted**, so a regression in the
  kubeadm patch produces a named failure rather than a vague one.
- **Prometheus' own PVC is on `openebs-hostpath`**, tying the monitoring layer
  back to the storage layer.

Next: [06 — Test workload](06-test-workload.md)
