# 02 — Kubernetes install

```bash
make cluster
```

Creates a 3-node cluster from [cluster/kind-config.yaml](../cluster/kind-config.yaml):
one control-plane, two workers, Kubernetes v1.36.1.

## Why three nodes

A single-node cluster cannot demonstrate a CNI. Pod-to-pod traffic that never
leaves the host works even with no overlay at all, because the packets stay on
one Linux bridge. Two workers force real cross-node routing, which is the thing
worth validating. The networking tests assert their fixtures actually landed on
different nodes before trusting any result.

## The cluster starts broken on purpose

```yaml
networking:
  disableDefaultCNI: true
  podSubnet: "10.244.0.0/16"
  serviceSubnet: "10.96.0.0/16"
```

kind normally installs `kindnetd` and the cluster comes up `Ready` with no
visible networking step. Turning that off means the CNI install in
[docs/03](03-cni-and-network-validation.md) has an observable before and after.

Immediately after `make cluster`:

```
NAME                        STATUS     ROLES           VERSION
k8s-cni-lab-control-plane   NotReady   control-plane   v1.36.1
k8s-cni-lab-worker          NotReady   <none>          v1.36.1
k8s-cni-lab-worker2         NotReady   <none>          v1.36.1
```

with every node reporting:

```
container runtime network not ready: NetworkReady=false
reason:NetworkPluginNotReady message:Network plugin returns error:
cni plugin not initialized
```

**This is the expected state, not a failure.** What is running and what is not
tells you exactly how Kubernetes layers:

| Component | State | Why |
|---|---|---|
| etcd, apiserver, scheduler, controller-manager | `Running` | `hostNetwork: true` — they use the node's own stack and need no CNI |
| kube-proxy | `Running` | Also host-networked |
| CoreDNS | `Pending` | A normal pod. Cannot be scheduled without a pod network |

CoreDNS pending is the clearest signal: the control plane is fine, and the *pod*
network is missing.

## Pod CIDR

`10.244.0.0/16`, matched exactly in the Calico IPPool.

Calico's stock manifest uses `192.168.0.0/16`, which overlaps the range most home
and office routers hand out. On a laptop that produces intermittent breakage that
is genuinely unpleasant to diagnose — some destinations work, some do not,
depending on whether the address collides with something on your LAN.

The two values must agree. [test_01_cluster.py](../validation/test_01_cluster.py)
asserts every non-host-network pod holds an address inside the pool, which
catches drift immediately.

## Exposing control-plane metrics

This is the part that cannot be fixed later.

kubeadm binds kube-controller-manager, kube-scheduler and etcd metrics to
`127.0.0.1`, and kube-proxy to `127.0.0.1:10249`. Prometheus runs in a pod on the
pod network, so it cannot reach any of them. kube-prometheus-stack ships
ServiceMonitors for all four, and they sit permanently down with
connection-refused.

```yaml
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
```

Two traps here:

**`extraArgs` is a list, not a map.** kubeadm moved to `v1beta4` in Kubernetes
1.31 and changed `extraArgs` from `{name: value}` to `[{name:, value:}]`. The old
map form is *silently ignored* — no error, the flag simply never applies, and the
symptom is identical to not having written the patch at all.

**It must happen at creation.** These are static pod manifests on the
control-plane node. Changing them afterwards means editing files in
`/etc/kubernetes/manifests` inside the node container, which does not survive a
cluster rebuild and will not be in your git history.

Verify after creation:

```bash
docker exec k8s-cni-lab-control-plane ss -lntp | grep -E ':(10257|10259|10249|2381)'
```

Bound correctly, every line shows `*:PORT` rather than `127.0.0.1:PORT`:

```
LISTEN 0 4096  *:2381    users:(("etcd",...))
LISTEN 0 4096  *:10249   users:(("kube-proxy",...))
LISTEN 0 4096  *:10259   users:(("kube-scheduler",...))
LISTEN 0 4096  *:10257   users:(("kube-controller",...))
```

In a real cluster you would not do this blindly — binding metrics to all
interfaces on an internet-reachable control plane is a genuine exposure. Here the
node is a container inside a VM on a laptop.

## NodePort access

```yaml
extraPortMappings:
  - containerPort: 30090   # Prometheus
    hostPort: 30090
  - containerPort: 30030   # Grafana
    hostPort: 30030
```

The path is kind node → Colima VM → macOS, and Colima forwards published
container ports to the host automatically. That makes
<http://localhost:30090> work from a browser without holding a `kubectl
port-forward` open.

Port forwarding sometimes drops after a Colima restart. `make urls` probes both
endpoints and prints the port-forward fallback if either fails.

## Verifying

```bash
make validate-net     # after the CNI is installed
.venv/bin/pytest validation/test_01_cluster.py -v
```

Next: [03 — CNI and network validation](03-cni-and-network-validation.md)
