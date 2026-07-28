# 08 — Troubleshooting

Every failure in this document was hit while building this lab, except where
marked *(anticipated)*. Symptoms are quoted verbatim so they match what you will
search for.

---

## Cluster and CNI

### `no matches for kind "Installation"`

```
resource mapping not found for name: "default" namespace: "" from
"cluster/calico-installation.yaml": no matches for kind "Installation" in
version "operator.tigera.io/v1"
ensure CRDs are installed first
```

**Cause.** Calico v3.30 split the CRDs out of `tigera-operator.yaml` into a
separate `operator-crds.yaml`. Guides written before that release tell you to
apply only the operator manifest, leaving a running operator with no CRDs to
configure it.

**Fix.** Apply the CRDs first:

```bash
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/projectcalico/calico/v3.32.1/manifests/operator-crds.yaml
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/projectcalico/calico/v3.32.1/manifests/tigera-operator.yaml
```

Confirm with `kubectl get crd installations.operator.tigera.io`.

---

### `metadata.annotations: Too long`

**Cause.** Client-side `kubectl apply` records the whole manifest in a
`last-applied-configuration` annotation, which is capped at 262144 bytes. The
Calico CRD set is larger.

**Fix.** `kubectl apply --server-side --force-conflicts`. Server-side apply
tracks ownership in managed fields and never writes that annotation.

---

### `unknown field nodeAddressAutodetectionV4.kubernetesInternalIP`

```
strict decoding error: unknown field
"spec.calicoNetwork.nodeAddressAutodetectionV4.kubernetesInternalIP"
```

**Cause.** Wrong field name. Several older write-ups use
`kubernetesInternalIP`.

**Fix.**

```yaml
nodeAddressAutodetectionV4:
  kubernetes: NodeInternalIP
```

When unsure, read the schema instead of guessing:

```bash
kubectl get crd installations.operator.tigera.io -o json \
  | jq '.spec.versions[0].schema.openAPIV3Schema.properties.spec.properties
        .calicoNetwork.properties.nodeAddressAutodetectionV4.properties | keys'
```

---

### Nodes stay `NotReady` after installing Calico

Check in this order:

```bash
kubectl -n calico-system get pods
kubectl -n calico-system logs ds/calico-node -c calico-node --tail=50
kubectl get installation default -o yaml | grep -A5 status
kubectl -n tigera-operator logs deploy/tigera-operator --tail=50
```

Common causes: the Installation was never applied (see above); images still
pulling on a slow link; or the operator rejecting the Installation, which shows
up only in the operator's log.

Before the CNI is installed, `NotReady` with `cni plugin not initialized` is the
**expected** state — see [docs/02](02-kubernetes-install.md).

---

### Cross-node pod traffic times out *(anticipated)*

Same-node pod-to-pod works, cross-node hangs.

**Cause.** `encapsulation: VXLANCrossSubnet` routes natively when nodes share a
subnet, which relies on the Docker bridge forwarding pod-CIDR packets. Some
Docker configurations drop them.

**Fix.** Switch to unconditional encapsulation in
[cluster/calico-installation.yaml](../cluster/calico-installation.yaml):

```yaml
encapsulation: VXLAN
```

then `kubectl apply -f cluster/calico-installation.yaml` and wait for the
operator to reconcile. Costs a little throughput, removes the dependency.

---

### Pods get addresses outside the pod CIDR

**Cause.** kind's `networking.podSubnet` and Calico's IPPool `cidr` disagree.
Symptoms are intermittent rather than total: some traffic routes, some does not.

**Fix.** They must be identical — `10.244.0.0/16` in both
[cluster/kind-config.yaml](../cluster/kind-config.yaml) and
[cluster/calico-installation.yaml](../cluster/calico-installation.yaml). Changing
the pool after allocation is messy; rebuild the cluster.

`test_pods_have_addresses_from_the_configured_pod_cidr` catches this.

Related: Calico's stock `192.168.0.0/16` collides with common home router ranges.
Do not use it on a laptop.

---

## Storage

### PVC stuck in `Pending`

```
NAME        STATUS    VOLUME   CAPACITY   STORAGECLASS
probe-pvc   Pending                       openebs-hostpath
```

**Usually not a fault.** `openebs-hostpath` uses
`volumeBindingMode: WaitForFirstConsumer`; the volume is not provisioned until a
pod mounts it, because the provisioner cannot know which node to create the
directory on until the scheduler has placed the pod.

**Fix.** Create a pod that uses it. If it is *still* Pending after that, the pod
is unschedulable — look at the pod, not the PVC:

```bash
kubectl -n <ns> describe pod <pod> | tail -20
```

`OpenEBSPVCPending` fires after 10 minutes for exactly this reason.

---

### PVC bound, but not to OpenEBS

**Cause.** kind preinstalls `rancher.io/local-path` as the default
StorageClass. A PVC omitting `storageClassName` lands there and silently bypasses
OpenEBS.

**Fix.**

```bash
kubectl patch sc standard -p \
  '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}'
kubectl patch sc openebs-hostpath -p \
  '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
```

`make openebs` does this. Two defaults is also an error state — check with
`kubectl get sc` and look for more than one `(default)`.

---

### Pod won't schedule after being deleted

```
0/3 nodes are available: 1 node(s) had volume node affinity conflict...
```

**Cause.** Working as designed. A hostpath PV is pinned to the node holding its
directory. If that node is cordoned, drained or gone, the pod cannot run.

**Fix.** Uncordon the node. If the node is genuinely gone, the data is gone with
it — LocalPV has no replication. See
[docs/04](04-openebs-storage.md#the-limits-that-matter).

---

### A volume grew past its requested size

**Cause.** Hostpath enforces **no quota**. The `1Gi` in a PVC spec is advisory.

**Fix.** There is no storage-layer fix; cap it in the application, as the demo
workload does at 200 MiB. Watch `OpenEBSVolumeExceededRequest` and
`OpenEBSNodeBackingFilesystemFilling` — the second is the one that takes out
every volume on the node at once.

---

## Monitoring

### Control-plane targets down in Prometheus

`kube-controller-manager`, `kube-scheduler`, `kube-proxy` and `kube-etcd` all
show `connection refused`.

**Cause.** kubeadm binds their metrics to `127.0.0.1`. Prometheus is a pod on the
pod network and cannot reach loopback on the node.

**Fix.** `kubeadmConfigPatches` in
[cluster/kind-config.yaml](../cluster/kind-config.yaml) — see
[docs/02](02-kubernetes-install.md#exposing-control-plane-metrics). **This is a
cluster-creation-time fix**; applying it later requires editing static pod
manifests inside the node.

Two failure modes to know:

- **`extraArgs` written as a map silently does nothing.** kubeadm moved to
  `v1beta4` in Kubernetes 1.31 and it is now a list of `{name, value}`. The map
  form produces no error and no effect.
- Verify the result rather than trusting the config:

  ```bash
  docker exec k8s-cni-lab-control-plane ss -lntp | grep -E ':(10257|10259|10249|2381)'
  ```

  Every line should show `*:PORT`, not `127.0.0.1:PORT`.

---

### kubelet resource target 404

```
kubelet @ https://172.18.0.3:10250/metrics/resource/v1alpha1:
server returned HTTP status 404 Not Found
```

**Cause.** kube-prometheus-stack still defaults `resourcePath` to
`/metrics/resource/v1alpha1`, removed in Kubernetes 1.24.

**Fix.**

```yaml
kubelet:
  serviceMonitor:
    resourcePath: /metrics/resource
```

Easy to miss: the kubelet's other three endpoints scrape fine, so dashboards look
healthy while one target is permanently down.

---

### ServiceMonitor exists but no target appears

`kubectl get servicemonitor` shows your object. Prometheus never scrapes it. No
error anywhere.

**Cause.** The operator only adopts monitors carrying the chart's release label.
Hand-written ones are ignored.

**Fix.**

```yaml
prometheus:
  prometheusSpec:
    serviceMonitorSelectorNilUsesHelmValues: false
    podMonitorSelectorNilUsesHelmValues: false
    ruleSelectorNilUsesHelmValues: false
```

If it still does not appear, check the Service actually has endpoints
(`kubectl get endpoints <svc>`) and that the ServiceMonitor's `selector` and
`namespaceSelector` match it.

---

### Every volume reports the same size

`kubelet_volume_stats_*` returns identical capacity and usage for every PVC on a
node, equal to the node's filesystem.

**Cause.** Not a bug. Hostpath volumes are directories on a shared filesystem, so
`statfs()` returns the filesystem's numbers.

**Fix.** Do not use `kubelet_volume_stats_*` for hostpath. Use the exporter and
recording rules in [docs/07](07-metrics-and-dashboards.md).

---

### histogram_quantile returns an implausible value

A p99 of 2.3 s for writes whose `_sum / _count` mean is 1.3 ms.

**Cause.** Malformed histogram buckets. Prometheus buckets are **cumulative** —
`le="0.005"` must count every observation at or below 5 ms, and `le="+Inf"` must
equal `_count`. Nothing validates this. `histogram_quantile()` raises no error on
a broken histogram; it silently returns nonsense.

Hit here in a hand-written exporter that incremented every matching bucket when
recording *and* accumulated again when rendering, so counts were cumulated
twice:

```
le=1.0     288
le=2.5     318
le=+Inf     30     <-- lower than the bucket below it
```

**Fix.** Record into the narrowest matching bucket only (`break` after the first
match), then accumulate at render time. Or record cumulatively and do not
accumulate again — but not both.

**Diagnose** by listing raw buckets in `le` order and looking for any decrease:

```bash
curl -s -G --data-urlencode 'query=ledger_write_duration_seconds_bucket' \
  localhost:30090/api/v1/query | python3 -m json.tool | grep -E '"le"|"1"'
```

Beware one trap when verifying a fix: `rate(...[5m])` keeps using the previous
5 minutes of samples, so a corrected exporter still reports the old wrong
quantile until the bad samples age out of the window. Wait out the range before
concluding the fix failed.

---

### Recording rule produces no series

Dashboard panels show "No data" but the underlying metrics exist.

**Cause.** Almost always a join that matches nothing — a label name differing
between the two sides.

**Fix.** Take the rule apart in the Prometheus expression browser, running each
side separately and inspecting the labels:

```promql
openebs_hostpath_volume_used_bytes
kube_persistentvolume_claim_ref
```

Label names differ more than you expect: kube-state-metrics uses
`claim_namespace` and `name` where the kubelet uses `namespace` and
`persistentvolumeclaim`. That is exactly why the rules use `label_replace`.

`test_recording_rules_are_producing_series` catches this class of failure.

---

### Grafana looks empty — "there are no dashboards"

**Cause.** Almost always navigation, not a missing import. Grafana's landing page
is a welcome screen, not a dashboard list, and ~29 of the stock
kube-prometheus-stack dashboards are filed at the root (the old "General"
folder) rather than in a named folder.

**Check before debugging anything:**

```bash
curl -s -u admin:admin 'http://localhost:30030/api/search?type=dash-db&limit=200' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d),'dashboards'); \
[print(' ', x.get('folderTitle','General'), '|', x['title']) for x in d]"
```

32 dashboards is the expected count for this lab. If they are listed there, they
are installed — use the **Dashboards** item in the left nav, or go straight to a
known URL such as <http://localhost:30030/d/vm-infra>.

---

### Node dashboards show 3x the VM's real resources

"Node Exporter / Nodes" or "USE Method / Cluster" reports ~23 GiB of RAM and 12
CPUs on a VM that has 7.74 GiB and 4.

**Cause.** kind "nodes" are containers sharing one Linux kernel — the Colima
VM's. Every node-exporter instance reports the same underlying hardware, so
`sum()` across instances multiplies by the node count. Confirm they are one
kernel by comparing boot times:

```bash
curl -s -G --data-urlencode 'query=node_boot_time_seconds' \
  localhost:30090/api/v1/query | python3 -m json.tool | grep -A2 instance
```

Identical `node_boot_time_seconds` across all three instances means one kernel.

**Fix.** Nothing to repair — the metrics are correct, the aggregation is wrong.
For VM-level figures use `max()` or `avg()` across instances, never `sum()`:

```promql
max(node_memory_MemTotal_bytes)                      # VM RAM
1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))  # VM CPU in use
```

The **Infrastructure - Colima VM and kind nodes** dashboard shipped here does
this correctly. The stock upstream dashboards assume one kernel per node, which
is true on real clusters and false on kind. Treat their node-level totals as
unreliable in this lab and per-pod panels as fine.

---

### Grafana dashboard doesn't appear

**Cause.** The sidecar imports ConfigMaps labelled `grafana_dashboard: "1"`. A
missing label, or a namespace outside `searchNamespace`, means it is never seen.

**Fix.**

```bash
kubectl -n monitoring logs deploy/kps-grafana -c grafana-sc-dashboard --tail=30
```

Import takes up to ~60s. Also verify the embedded JSON parses — invalid JSON is
copied in and then rejected by Grafana:

```bash
python3 -c "import yaml,json,sys; \
  print(json.loads(yaml.safe_load(open(sys.argv[1]))['data']['openebs-storage.json'])['title'])" \
  manifests/monitoring/dashboards/openebs-storage-dashboard.yaml
```

---

## Environment

### Image pulls time out

```
failed to resolve reference "quay.io/prometheus/node-exporter:...":
dial tcp 44.217.187.181:443: i/o timeout
```

**Cause.** Slow or flaky connectivity to the registry, not a wrong image. Note
`i/o timeout` rather than `not found` or `unauthorized`.

**Fix.** Usually self-heals — Kubernetes retries with backoff, and containerd's
pull deadline is what expired. Watch rather than intervene:

```bash
kubectl -n monitoring get pods -w
```

If it persists, pre-pull into the VM and load into the nodes:

```bash
docker pull quay.io/prometheus/node-exporter:v1.12.1-distroless
kind load docker-image quay.io/prometheus/node-exporter:v1.12.1-distroless --name k8s-cni-lab
```

---

### `curl`/`wget` to `localhost` inside a pod refuses, but the pod is healthy

```
wget: can't connect to remote host: Connection refused
```

while the same server answers on its pod IP.

**Cause.** `localhost` resolves to `::1` first. A server bound to IPv4
`0.0.0.0` is not listening there.

**Fix.** Use `127.0.0.1` explicitly. This bit the hostpath exporter during
development: the container was healthy and Prometheus was scraping it
successfully the whole time.

---

### NodePort unreachable from macOS

**Cause.** Colima's port forwarding can drop after a VM restart.

**Fix.** `make urls` probes and reports. Fall back to:

```bash
kubectl -n monitoring port-forward svc/kps-prometheus 30090:9090
kubectl -n monitoring port-forward svc/kps-grafana    30030:80
```

---

### `exec format error` in a pod *(anticipated)*

**Cause.** An `amd64`-only image on Apple Silicon. Nothing to do with the
cluster.

**Fix.** Use a multi-arch tag. Every image in this lab publishes `arm64`.

---

### Pods OOMKilled or evicted during install

**Cause.** Undersized VM. This does not present as a clear resource error — it
looks like unrelated components failing.

**Fix.**

```bash
colima stop
colima start --cpu 4 --memory 8 --disk 60 --runtime docker
```

`make preflight` warns below 4 CPU or 7 GB.

---

### Shell redirect errors in a container's logs

```
/bin/sh: can't open /data/bulk.dat: no such file
```

**Cause.** In `wc -c < "$FILE" 2>/dev/null`, redirections are evaluated left to
right *before* the command runs, so the shell reports the failed input
redirection on the still-unredirected stderr. The `2>/dev/null` never applies.

**Fix.** Create the file first (`touch "$FILE"`), or group the redirect:
`{ wc -c < "$FILE"; } 2>/dev/null`.

---

## General technique

When something in this stack breaks, work bottom-up — a failure at a lower layer
presents as a mystery at every layer above it:

```bash
kubectl get nodes                       # 1. nodes Ready?
kubectl -n calico-system get pods       # 2. CNI healthy?
kubectl get sc && kubectl get pvc -A    # 3. storage bound?
kubectl -n monitoring get pods          # 4. monitoring running?
curl -s localhost:30090/api/v1/targets  # 5. targets up?
```

Then the two commands that explain most of the rest:

```bash
kubectl -n <ns> describe pod <pod> | tail -30      # events, not spec
kubectl -n <ns> get events --sort-by=.lastTimestamp | tail -20
```

And when a resource rejects your YAML, read its schema rather than searching for
an example — the API is the authority and it is queryable:

```bash
kubectl explain installation.spec.calicoNetwork --recursive
kubectl get crd <name> -o json | jq '.spec.versions[0].schema'
```
