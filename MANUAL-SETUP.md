# Manual setup — the whole lab, by hand

Build the entire lab with nothing but a terminal. No `make`, no scripts from this
repo. Every manifest is inline, so this runbook works on a machine that has never
cloned the repository.

The point of doing it this way is that each step's output tells you something.
`make all` hides that; typing it out does not.

**Time:** 35–50 minutes, most of it image pulls.
**Target:** macOS on Apple Silicon. On Intel Macs everything is identical apart
from the reported architecture.

Conventions below: `$` marks a command you type, and the block under it is what
you should see. Only interesting output is shown.

---

## Phase 0 — Host tooling (~5 min)

```bash
brew install colima docker kubectl kind helm python@3.12
```

Verify each landed:

```bash
$ for t in colima docker kubectl kind helm; do printf '%-9s %s\n' "$t" "$(command -v $t)"; done
colima    /opt/homebrew/bin/colima
docker    /opt/homebrew/bin/docker
kubectl   /opt/homebrew/bin/kubectl
kind      /opt/homebrew/bin/kind
helm      /opt/homebrew/bin/helm

$ python3.12 --version
Python 3.12.13
```

`docker` here is the CLI only. The daemon comes from Colima in the next phase.

> **Already running Docker Desktop, OrbStack or Rancher Desktop?** Skip Phase 1
> entirely; just confirm `docker info` succeeds.

---

## Phase 1 — The Linux VM (~3 min)

```bash
colima start --cpu 4 --memory 8 --disk 60 --runtime docker
```

Sizing is not arbitrary. Below 4 CPU / 8 GB the stack does not fail cleanly — it
OOMKills and evicts pods several minutes into the install, which reads as
unrelated component failures.

```bash
$ docker info --format 'Server {{.ServerVersion}} · {{.Architecture}} · {{.NCPU}} CPU'
Server 29.5.2 · aarch64 · 4 CPU
```

`aarch64` is expected on Apple Silicon. Every image used here publishes `arm64`
builds; an `exec format error` later would mean an `amd64`-only image, not a
cluster problem.

---

## Phase 2 — Kubernetes, deliberately with no CNI (~3 min)

Two choices in this config drive everything after it. `disableDefaultCNI` makes
the CNI install an observable event rather than something kind does invisibly.
The `kubeadmConfigPatches` rebind control-plane metrics off `127.0.0.1` — **this
is only possible at cluster creation**; changing it later means editing static
pod manifests inside the node container.

```bash
cat > /tmp/kind-config.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: k8s-cni-lab

networking:
  disableDefaultCNI: true
  kubeProxyMode: iptables
  podSubnet: "10.244.0.0/16"
  serviceSubnet: "10.96.0.0/16"

kubeadmConfigPatches:
  - |
    apiVersion: kubeadm.k8s.io/v1beta4
    kind: ClusterConfiguration
    controllerManager:
      extraArgs:
        - name: bind-address
          value: "0.0.0.0"
    scheduler:
      extraArgs:
        - name: bind-address
          value: "0.0.0.0"
    etcd:
      local:
        extraArgs:
          - name: listen-metrics-urls
            value: "http://0.0.0.0:2381"
  - |
    apiVersion: kubeproxy.config.k8s.io/v1alpha1
    kind: KubeProxyConfiguration
    metricsBindAddress: "0.0.0.0"

nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30090
        hostPort: 30090
        protocol: TCP
      - containerPort: 30030
        hostPort: 30030
        protocol: TCP
  - role: worker
  - role: worker
EOF

kind create cluster --config /tmp/kind-config.yaml
```

> `extraArgs` is a **list** of `{name, value}` here. kubeadm moved to `v1beta4`
> in Kubernetes 1.31 and the older map form (`bind-address: "0.0.0.0"`) is
> silently ignored — no error, no effect, and the symptom is identical to never
> having written the patch.

Do not pass `--wait`. Nothing can become Ready yet, so it would simply hang.

### Confirm the cluster is broken in the expected way

```bash
$ kubectl get nodes
NAME                        STATUS     ROLES           AGE   VERSION
k8s-cni-lab-control-plane   NotReady   control-plane   71s   v1.36.1
k8s-cni-lab-worker          NotReady   <none>          58s   v1.36.1
k8s-cni-lab-worker2         NotReady   <none>          58s   v1.36.1

$ kubectl get nodes -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].message}'
container runtime network not ready: NetworkReady=false
reason:NetworkPluginNotReady message:Network plugin returns error:
cni plugin not initialized
```

```bash
$ kubectl get pods -A
kube-system   coredns-589f44dc88-42gvc                   0/1   Pending
kube-system   etcd-k8s-cni-lab-control-plane             1/1   Running
kube-system   kube-apiserver-…                           1/1   Running
kube-system   kube-proxy-6wqkb                           1/1   Running
```

Read that carefully — it is a map of what depends on the pod network. etcd,
apiserver, scheduler, controller-manager and kube-proxy are `hostNetwork: true`
and run fine. CoreDNS is an ordinary pod and cannot be scheduled at all.

### Confirm the metrics patch took effect

```bash
$ docker exec k8s-cni-lab-control-plane ss -lntp | grep -E ':(10257|10259|10249|2381)'
LISTEN 0 4096  *:2381    users:(("etcd",...))
LISTEN 0 4096  *:10249   users:(("kube-proxy",...))
LISTEN 0 4096  *:10259   users:(("kube-scheduler",...))
LISTEN 0 4096  *:10257   users:(("kube-controller",...))
```

Every line must show `*:PORT`. A `127.0.0.1:PORT` here means the patch did not
apply — recreate the cluster; it cannot be fixed in place.

---

## Phase 3 — Calico CNI (~4 min)

Three steps, and the order is not optional.

```bash
CALICO=v3.32.1
BASE=https://raw.githubusercontent.com/projectcalico/calico/${CALICO}/manifests

# 1. CRDs — split into their own manifest as of Calico v3.30
kubectl apply --server-side --force-conflicts -f ${BASE}/operator-crds.yaml

# 2. the operator
kubectl apply --server-side --force-conflicts -f ${BASE}/tigera-operator.yaml
kubectl -n tigera-operator rollout status deploy/tigera-operator --timeout=300s
```

> Two traps here.
> **`operator-crds.yaml` first.** Guides written before v3.30 apply only
> `tigera-operator.yaml`, which leaves a running operator and
> `no matches for kind "Installation"` when you try to configure it.
> **`--server-side` is required.** The CRDs exceed the 262144-byte cap on the
> `last-applied-configuration` annotation that client-side apply writes, failing
> with `metadata.annotations: Too long`.

Step 3 is the declaration of intent — the operator does nothing until now:

```bash
kubectl apply -f - <<'EOF'
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    ipPools:
      - name: default-ipv4-ippool
        cidr: 10.244.0.0/16
        blockSize: 26
        encapsulation: VXLANCrossSubnet
        natOutgoing: Enabled
        nodeSelector: all()
    nodeAddressAutodetectionV4:
      kubernetes: NodeInternalIP
---
apiVersion: operator.tigera.io/v1
kind: APIServer
metadata:
  name: default
spec: {}
EOF
```

> The field is `kubernetes: NodeInternalIP`. It is **not**
> `kubernetesInternalIP`, which several older write-ups use and which the API
> rejects with `strict decoding error: unknown field`.
>
> `cidr` must equal the `podSubnet` from Phase 2. Calico's stock
> `192.168.0.0/16` is avoided because it overlaps the range most home routers
> hand out, producing intermittent breakage that is genuinely unpleasant to
> diagnose.

Wait for it:

```bash
kubectl -n calico-system rollout status ds/calico-node --timeout=300s
kubectl wait --for=condition=Ready nodes --all --timeout=300s
kubectl -n kube-system rollout status deploy/coredns --timeout=300s
```

```bash
$ kubectl get nodes
NAME                        STATUS   ROLES           VERSION
k8s-cni-lab-control-plane   Ready    control-plane   v1.36.1
k8s-cni-lab-worker          Ready    <none>          v1.36.1
k8s-cni-lab-worker2         Ready    <none>          v1.36.1

$ kubectl get ippools.crd.projectcalico.org -o custom-columns=\
NAME:.metadata.name,CIDR:.spec.cidr,ENCAP:.spec.vxlanMode
NAME                  CIDR            ENCAP
default-ipv4-ippool   10.244.0.0/16   CrossSubnet
```

Note that `calico-node` runs host-networked while `calico-kube-controllers` gets
a pod IP — the data plane has to exist before anything can use it.

---

## Phase 4 — Verify networking by hand (~3 min)

Anti-affinity forces the two web pods onto different nodes. Without it a
"pod-to-pod" test could pass with a completely broken overlay, because the
packets never leave one host.

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata: {name: net-test}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, namespace: net-test}
spec:
  replicas: 2
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector: {matchLabels: {app: web}}
              topologyKey: kubernetes.io/hostname
      containers:
        - name: web
          image: nginx:1.27-alpine
          ports: [{containerPort: 80}]
---
apiVersion: v1
kind: Service
metadata: {name: web, namespace: net-test}
spec:
  selector: {app: web}
  ports: [{port: 80, targetPort: 80}]
---
apiVersion: v1
kind: Pod
metadata: {name: client, namespace: net-test, labels: {app: client}}
spec:
  containers:
    - name: client
      image: busybox:1.36
      command: ["sh","-c","sleep infinity"]
EOF

kubectl -n net-test wait --for=condition=Available deploy/web --timeout=180s
kubectl -n net-test wait --for=condition=Ready pod/client --timeout=180s
```

**Confirm the replicas really did split across nodes** — every check below is
meaningless otherwise:

```bash
$ kubectl -n net-test get pods -o wide
NAME                   READY   IP               NODE
client                 1/1     10.244.247.200   k8s-cni-lab-worker
web-6d4c8f9b7d-abcde   1/1     10.244.67.5      k8s-cni-lab-worker2
web-6d4c8f9b7d-fghij   1/1     10.244.247.199   k8s-cni-lab-worker
```

### DNS

```bash
$ kubectl -n net-test exec client -- nslookup kubernetes.default.svc.cluster.local
Address: 10.96.0.1
```

### Pod-to-pod across nodes, bypassing Services

Use the pod IP that is on a *different* node from `client`:

```bash
$ kubectl -n net-test exec client -- wget -T4 -qO- http://10.244.67.5/ | head -4
<!DOCTYPE html>
<html>
<head>
<title>Welcome to nginx!</title>
```

Hitting the IP directly isolates the CNI. If this works but the Service does not,
the problem is kube-proxy or DNS, not the network plane.

### Service ClusterIP

```bash
$ kubectl -n net-test exec client -- wget -T4 -qO- http://web.net-test.svc.cluster.local/ | grep title
<title>Welcome to nginx!</title>
```

### NetworkPolicy — is it actually enforced?

This is the one that matters. Kubernetes accepts a `NetworkPolicy` on a cluster
with **no policy engine at all** and traffic keeps flowing, so "it applied
cleanly" proves nothing. Only watching traffic stop proves enforcement.

```bash
kubectl apply -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: default-deny-ingress, namespace: net-test}
spec:
  podSelector: {matchLabels: {app: web}}
  policyTypes: [Ingress]
EOF

sleep 10
kubectl -n net-test exec client -- wget -T4 -qO- http://10.244.67.5/ ; echo "exit=$?"
```

Expected — the request must now **fail**:

```
wget: download timed out
exit=1
```

Now allow the client back in:

```bash
kubectl apply -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: allow-client-to-web, namespace: net-test}
spec:
  podSelector: {matchLabels: {app: web}}
  policyTypes: [Ingress]
  ingress:
    - from: [{podSelector: {matchLabels: {app: client}}}]
      ports: [{protocol: TCP, port: 80}]
EOF

sleep 10
$ kubectl -n net-test exec client -- wget -T4 -qO- http://10.244.67.5/ | grep title
<title>Welcome to nginx!</title>
```

Policies are additive — this carves an exception out of the deny, it does not
replace it. Clean up:

```bash
kubectl delete namespace net-test
```

---

## Phase 5 — OpenEBS storage (~4 min)

Only LocalPV Hostpath is enabled. The other engines need a real volume group, a
zpool, or hugepages and dedicated block devices — none of which exist inside a
kind node. Left on, they fill the namespace with `CrashLoopBackOff` pods that
look like a broken install.

```bash
helm repo add openebs https://openebs.github.io/openebs
helm repo update

helm install openebs openebs/openebs \
  --namespace openebs --create-namespace --version 4.5.1 \
  --set engines.local.hostpath.enabled=true \
  --set engines.local.lvm.enabled=false \
  --set engines.local.zfs.enabled=false \
  --set engines.local.rawfile.enabled=false \
  --set engines.replicated.mayastor.enabled=false \
  --set loki.enabled=false \
  --set alloy.enabled=false \
  --wait --timeout 10m
```

### Make OpenEBS the default StorageClass

kind preinstalls `rancher.io/local-path` as default. Leaving it means any PVC
that omits `storageClassName` silently bypasses OpenEBS entirely.

```bash
kubectl patch sc standard -p \
  '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}'
kubectl patch sc openebs-hostpath -p \
  '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
```

```bash
$ kubectl get sc
NAME                         PROVISIONER             VOLUMEBINDINGMODE
openebs-hostpath (default)   openebs.io/local        WaitForFirstConsumer
standard                     rancher.io/local-path   WaitForFirstConsumer
```

Exactly one `(default)`. Two is an error state; zero breaks any PVC without an
explicit class.

---

## Phase 6 — Prove the storage behaviour (~3 min)

### A PVC with no pod stays Pending — and that is correct

```bash
kubectl create namespace storage-probe
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: probe-pvc, namespace: storage-probe}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: openebs-hostpath
  resources: {requests: {storage: 512Mi}}
EOF

sleep 15
$ kubectl -n storage-probe get pvc
NAME        STATUS    VOLUME   CAPACITY   STORAGECLASS
probe-pvc   Pending                       openebs-hostpath
```

This is the most-reported OpenEBS "bug" that is not a bug. A hostpath volume is a
directory on *one specific node*; the provisioner cannot know which node until
the scheduler has placed the pod that will use it.

### Attach a pod and it binds

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: probe-pod, namespace: storage-probe}
spec:
  containers:
    - name: writer
      image: busybox:1.36
      command: ["sh","-c","echo provisioned > /data/probe.txt && sleep 3600"]
      volumeMounts: [{name: data, mountPath: /data}]
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: probe-pvc}
EOF

kubectl -n storage-probe wait --for=condition=Ready pod/probe-pod --timeout=180s

$ kubectl -n storage-probe get pvc
NAME        STATUS   VOLUME                                     CAPACITY
probe-pvc   Bound    pvc-8a3f…                                  512Mi
```

### The PV is pinned to one node

```bash
$ kubectl get pv -o custom-columns=\
NAME:.metadata.name,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]
NAME        NODE
pvc-8a3f…   k8s-cni-lab-worker
```

That affinity is load-bearing: it stops the scheduler moving the pod to a node
where the data does not exist. See the file on the node itself:

```bash
$ docker exec k8s-cni-lab-worker ls -la /var/openebs/local/pvc-8a3f…
-rw-r--r-- 1 root root  13 Jul 28 02:22 probe.txt
```

```bash
kubectl delete namespace storage-probe
```

---

## Phase 7 — Prometheus and Grafana (~8 min)

Storage must exist first, or Prometheus sits `Pending` on an unbindable PVC.

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

cat > /tmp/kps-values.yaml <<'EOF'
fullnameOverride: kps

prometheus:
  service: {type: NodePort, nodePort: 30090}
  prometheusSpec:
    retention: 6h
    retentionSize: 5GB
    scrapeInterval: 30s
    serviceMonitorSelectorNilUsesHelmValues: false
    podMonitorSelectorNilUsesHelmValues: false
    ruleSelectorNilUsesHelmValues: false
    probeSelectorNilUsesHelmValues: false
    scrapeConfigSelectorNilUsesHelmValues: false
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: openebs-hostpath
          accessModes: ["ReadWriteOnce"]
          resources: {requests: {storage: 8Gi}}
    resources:
      requests: {cpu: 200m, memory: 512Mi}
      limits: {memory: 1800Mi}

grafana:
  adminUser: admin
  adminPassword: admin
  service: {type: NodePort, nodePort: 30030}
  persistence:
    enabled: true
    type: pvc
    storageClassName: openebs-hostpath
    size: 2Gi
  sidecar:
    dashboards:
      enabled: true
      label: grafana_dashboard
      labelValue: "1"
      searchNamespace: ALL
      folderAnnotation: grafana_folder
      provider: {foldersFromFilesStructure: true}
    datasources: {enabled: true, searchNamespace: ALL}

alertmanager:
  alertmanagerSpec:
    storage:
      volumeClaimTemplate:
        spec:
          storageClassName: openebs-hostpath
          accessModes: ["ReadWriteOnce"]
          resources: {requests: {storage: 1Gi}}

kubeControllerManager:
  service: {port: 10257, targetPort: 10257}
  serviceMonitor: {https: true, insecureSkipVerify: true}
kubeScheduler:
  service: {port: 10259, targetPort: 10259}
  serviceMonitor: {https: true, insecureSkipVerify: true}
kubeProxy:
  service: {port: 10249, targetPort: 10249}
kubeEtcd:
  service: {port: 2381, targetPort: 2381}
  serviceMonitor: {scheme: http}

kubelet:
  serviceMonitor:
    cAdvisor: true
    probes: true
    resource: true
    resourcePath: /metrics/resource
EOF

helm install kps prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace --version 87.20.0 \
  --values /tmp/kps-values.yaml --wait --timeout 15m
```

> Two settings here are easy to get wrong and fail quietly.
>
> **`resourcePath: /metrics/resource`** — the chart still defaults to
> `/metrics/resource/v1alpha1`, an endpoint removed in Kubernetes 1.24. Without
> this the kubelet returns 404 on that target forever, while its other three
> endpoints scrape fine so nothing looks broken.
>
> **`…SelectorNilUsesHelmValues: false`** — otherwise the operator adopts only
> monitors carrying the chart's own release label, and every ServiceMonitor you
> write by hand is ignored silently.

Image pulls sometimes time out with `i/o timeout` on a slow link. Kubernetes
retries automatically; watch rather than intervene.

```bash
$ kubectl -n monitoring get pvc
NAME                            STATUS   CAPACITY   STORAGECLASS
alertmanager-…-0                Bound    1Gi        openebs-hostpath
kps-grafana                     Bound    2Gi        openebs-hostpath
prometheus-…-0                  Bound    8Gi        openebs-hostpath

$ curl -s http://localhost:30090/-/ready
Prometheus Server is Ready.

$ curl -s http://localhost:30030/api/health | grep database
  "database": "ok",
```

The monitoring stack storing itself on OpenEBS is deliberate — Prometheus' TSDB
is a far better exercise of the storage layer than any purpose-built demo.

### Every scrape target should be up

```bash
$ curl -s http://localhost:30090/api/v1/targets \
  | python3 -c "import json,sys,collections; \
c=collections.Counter((t['labels'].get('job'),t['health']) for t in json.load(sys.stdin)['data']['activeTargets']); \
[print(f'  {j:<26} {h}  x{n}') for (j,h),n in sorted(c.items())]"
  apiserver                  up  x1
  kube-controller-manager    up  x1
  kube-etcd                  up  x1
  kube-proxy                 up  x3
  kube-scheduler             up  x1
  kubelet                    up  x12
  kube-state-metrics         up  x1
  node-exporter              up  x3
```

The four control-plane jobs are up **only** because of the Phase 2 kubeadm
patches. On a stock kind cluster all four show `connection refused`.

---

## Phase 8 — Per-volume usage exporter (~2 min)

`kubelet_volume_stats_*` is the obvious source for PVC usage and it is wrong
here. Prove it to yourself first:

```bash
$ curl -s -G --data-urlencode 'query=kubelet_volume_stats_capacity_bytes' \
    http://localhost:30090/api/v1/query \
  | python3 -c "import json,sys; [print(f\"  {s['metric']['persistentvolumeclaim'][:40]:<40} {int(s['value'][1])/2**30:.2f} GiB\") for s in json.load(sys.stdin)['data']['result']]"
  alertmanager-kps-alertmanager-db-…       58.76 GiB
  kps-grafana                              58.76 GiB
  prometheus-kps-prometheus-db-…           58.76 GiB
```

A 1Gi, a 2Gi and an 8Gi PVC all report 58.76 GiB — the node's whole filesystem.
A hostpath volume is a directory on a shared filesystem, so `statfs()` returns
that filesystem's numbers for every volume on the node. A "% full per volume"
graph built on this shows identical, meaningless lines.

The exporter that fixes it measures each PV directory directly. Its ConfigMap is
long; apply it from the repo:

```bash
kubectl apply -f manifests/monitoring/openebs-hostpath-exporter.yaml
kubectl -n monitoring rollout status ds/openebs-hostpath-exporter --timeout=180s
```

Then the recording rules that join it to kube-state-metrics for PVC identity, and
the dashboards:

```bash
kubectl apply -f manifests/monitoring/openebs-monitoring.yaml
kubectl apply -f manifests/monitoring/dashboards/
```

> These four files are the only place this runbook depends on the repository.
> The dashboard JSON runs to several hundred lines each and inlining it would
> make this document unusable.

```bash
$ curl -s -G --data-urlencode 'query=openebs:volume_used_bytes' \
    http://localhost:30090/api/v1/query \
  | python3 -c "import json,sys; [print(f\"  {s['metric']['namespace']}/{s['metric']['persistentvolumeclaim'][:36]:<36} {int(s['value'][1])/2**20:.2f} MiB\") for s in json.load(sys.stdin)['data']['result']]"
  monitoring/prometheus-…                  11.36 MiB
  monitoring/kps-grafana                   49.58 MiB
  monitoring/alertmanager-…                 0.00 MiB
```

Different numbers per volume. That is the difference.

---

## Phase 9 — The demo workload (~2 min)

A StatefulSet that writes to an OpenEBS volume and publishes its own metrics. The
container script is long, so apply it from the repo:

```bash
kubectl apply -f manifests/workload/ledger-statefulset.yaml
kubectl -n storage-demo rollout status sts/ledger --timeout=240s
```

```bash
$ kubectl -n storage-demo logs ledger-0 | head -5
=== boot history on this volume ===
boot 2026-07-28T02:22:25+00:00 pod=ledger-0 node=k8s-cni-lab-worker
=== boots recorded: 1 ===
metrics on :8080/metrics
```

### Prove persistence

Delete the pod and read the boot history from its replacement:

```bash
kubectl -n storage-demo delete pod ledger-0
kubectl -n storage-demo rollout status sts/ledger --timeout=240s

$ kubectl -n storage-demo logs ledger-0 | head -5
=== boot history on this volume ===
boot 2026-07-28T02:22:25+00:00 pod=ledger-0 node=k8s-cni-lab-worker
boot 2026-07-28T02:23:23+00:00 pod=ledger-0 node=k8s-cni-lab-worker
=== boots recorded: 2 ===
```

Two entries from one pod name. The volume outlived the pod — that is the claim,
visible in `kubectl logs`.

### Application metrics

```bash
$ curl -s -G --data-urlencode 'query=sum(rate(ledger_writes_total[5m]))' \
    http://localhost:30090/api/v1/query \
  | python3 -c "import json,sys; print(f\"  write rate: {float(json.load(sys.stdin)['data']['result'][0]['value'][1]):.3f}/s\")"
  write rate: 0.181/s
```

These `ledger_*` series exist only because the app is instrumented. cAdvisor and
kube-state-metrics can tell you this pod's CPU and restart count; neither can
tell you a write took 4 ms or that three writes failed.

---

## Phase 10 — Final verification

```bash
$ kubectl get nodes                       # 3 × Ready
$ kubectl get pods -A | grep -v Running   # header + Completed only
$ kubectl get pvc -A                      # 4 × Bound on openebs-hostpath
```

All targets healthy:

```bash
$ curl -s http://localhost:30090/api/v1/targets \
  | python3 -c "import json,sys; t=json.load(sys.stdin)['data']['activeTargets']; \
print(f'  {sum(1 for x in t if x[\"health\"]==\"up\")}/{len(t)} targets up')"
  34/34 targets up
```

Dashboards — expect 32:

```bash
$ curl -s -u admin:admin 'http://localhost:30030/api/search?type=dash-db&limit=200' \
  | python3 -c "import json,sys; print(f'  {len(json.load(sys.stdin))} dashboards')"
  32 dashboards
```

Open them:

| | |
|---|---|
| Prometheus | http://localhost:30090 |
| Grafana | http://localhost:30030 — `admin` / `admin` |
| Infrastructure / VM | http://localhost:30030/d/vm-infra |
| OpenEBS storage | http://localhost:30030/d/openebs-storage |
| Application | http://localhost:30030/d/app-ledger |

Grafana's landing page is a welcome screen, not a dashboard list — use
**Dashboards** in the left nav.

> **Reading the stock node dashboards:** "Node Exporter / Nodes" and
> "USE Method / Cluster" `sum()` across nodes. kind nodes share one kernel, so
> they will report ~23 GiB of RAM for your 7.74 GiB VM. Confirm with
> `node_boot_time_seconds` — identical across all three instances means one
> kernel. The `vm-infra` dashboard uses `max()`/`avg()` and is correct.

If a NodePort stops answering (Colima port forwarding can drop after a VM
restart):

```bash
kubectl -n monitoring port-forward svc/kps-prometheus 30090:9090
kubectl -n monitoring port-forward svc/kps-grafana    30030:80
```

---

## Teardown

Narrowest first:

```bash
kind delete cluster --name k8s-cni-lab     # cluster + all volume data
colima stop                                # pause the VM
colima delete                              # delete the VM and its 60 GB disk
brew uninstall colima docker kubectl kind helm python@3.12
```

Deleting the cluster destroys the node containers and their filesystems, and
hostpath volume data lives on those filesystems. There is no separate step to
clean up PVs, and nothing survives.

---

## What each phase proved

| Phase | Established |
|---|---|
| 2 | Control plane runs without a CNI; only pod-network components block |
| 3 | Calico brings nodes Ready; pod CIDR matches the configured pool |
| 4 | Cross-node pod traffic works; NetworkPolicy is genuinely enforced |
| 5–6 | `WaitForFirstConsumer` is correct behaviour; PVs are node-pinned |
| 7 | All 34 targets scrape, including the four control-plane jobs |
| 8 | Standard PVC metrics are wrong for hostpath; directory measurement fixes it |
| 9 | Data outlives its pod; the app reports on itself |

For the design reasoning behind these choices see [ARCHITECTURE.md](ARCHITECTURE.md).
For failures not covered above, [docs/08-troubleshooting.md](docs/08-troubleshooting.md)
has verbatim error strings.
