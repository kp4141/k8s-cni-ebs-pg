# 03 — CNI install and network validation

```bash
make cni
```

Installs Calico v3.32.1 and takes the cluster from `NotReady` to `Ready`.

## Why Calico

Any CNI would give pods addresses. Calico was chosen because it also gives the
lab something to *validate*:

- **NetworkPolicy enforcement.** Many CNIs accept `NetworkPolicy` objects and
  ignore them. Calico enforces them, so the policy test can prove enforcement
  rather than acceptance.
- **An operator-driven install.** Configuration is a declarative `Installation`
  resource rather than a patched YAML blob, which makes the pod CIDR and
  encapsulation choices explicit and reviewable.
- **Real components to observe.** `calico-node` on every node, `calico-typha`
  for datastore fan-out, `calico-kube-controllers`, plus its own API server.

## Install order

Three steps, and the order is not optional:

```bash
kubectl apply --server-side -f .../manifests/operator-crds.yaml   # 1
kubectl apply --server-side -f .../manifests/tigera-operator.yaml # 2
kubectl apply -f cluster/calico-installation.yaml                 # 3
```

**Step 1 is new.** Calico v3.30 split the CRDs out of `tigera-operator.yaml` into
their own `operator-crds.yaml`. Every guide written before that release tells you
to apply only the operator manifest, which now leaves you with a running operator
and no way to configure it:

```
no matches for kind "Installation" in version "operator.tigera.io/v1"
ensure CRDs are installed first
```

**`--server-side` is also not optional.** The Calico CRD set exceeds the
262144-byte ceiling on the `last-applied-configuration` annotation that
client-side apply writes. Plain `kubectl apply` fails with
`metadata.annotations: Too long`. Server-side apply does not use that annotation.

Step 2 installs a controller that does nothing on its own. Step 3 is the
declaration of intent that makes it act.

## Configuration

[cluster/calico-installation.yaml](../cluster/calico-installation.yaml):

```yaml
calicoNetwork:
  ipPools:
    - cidr: 10.244.0.0/16          # must equal kind's podSubnet
      blockSize: 26
      encapsulation: VXLANCrossSubnet
      natOutgoing: Enabled
  nodeAddressAutodetectionV4:
    kubernetes: NodeInternalIP
```

**`blockSize: 26`** gives each node a /26 (64 addresses) carved from the pool.
Nodes claim blocks as they need them, which keeps one node from monopolising the
range.

**`VXLANCrossSubnet`** encapsulates only between nodes on different subnets. In
kind every node sits on one Docker bridge, so traffic is routed natively with no
encapsulation overhead. If cross-node traffic ever fails, plain `VXLAN`
encapsulates unconditionally and stops depending on the bridge forwarding
pod-CIDR packets — see [docs/08](08-troubleshooting.md).

**`kubernetes: NodeInternalIP`** — note the spelling. It is *not*
`kubernetesInternalIP`, which older write-ups use and which the API rejects:

```
strict decoding error: unknown field
"spec.calicoNetwork.nodeAddressAutodetectionV4.kubernetesInternalIP"
```

Default autodetection is "first found", which on a kind node can latch onto a
Docker-managed interface rather than the one carrying node traffic. Pinning to
the address Kubernetes already recorded removes the guesswork.

## Result

```
NAME                        STATUS   ROLES           VERSION
k8s-cni-lab-control-plane   Ready    control-plane   v1.36.1
k8s-cni-lab-worker          Ready    <none>          v1.36.1
k8s-cni-lab-worker2         Ready    <none>          v1.36.1
```

```
NAME                                       READY   IP
calico-node-846f7                          1/1     172.18.0.3      ← host-networked
calico-typha-87ddd6d84-fdfkk               1/1     172.18.0.2
calico-kube-controllers-7c455cb9d6-lhqm8   1/1     10.244.40.130   ← pod network
csi-node-driver-p7f8g                      2/2     10.244.247.193
csi-node-driver-zdh6p                      2/2     10.244.67.1
```

Two things worth reading off that output. `calico-node` runs host-networked —
it has to, since it is what *creates* the pod network. And the per-node block
allocation is visible: `10.244.40.x`, `10.244.247.x`, `10.244.67.x` are three
different /26 blocks on three different nodes.

CoreDNS, stuck `Pending` since cluster creation, schedules on its own once pods
have a network.

## Validation

```bash
make validate-net
```

Seven tests in [test_02_networking.py](../validation/test_02_networking.py).

### The fixtures are shaped to make the tests meaningful

`web` is a 2-replica Deployment with **required** pod anti-affinity on
`kubernetes.io/hostname`, forcing the replicas onto different nodes. `client` is
a DaemonSet with a control-plane toleration, so there is a client on all three
nodes.

The first two tests assert this actually happened. If anti-affinity silently
failed and both web pods shared a node, every later "cross-node" result would be
a same-node result and the suite would pass while proving nothing.

### What is checked

**DNS** — `nslookup kubernetes.default.svc.cluster.local` must return
`10.96.0.1`, and the user Service must resolve too. This exercises CoreDNS, the
pod network, and Service DNS together.

**Pod-to-pod, full mesh** — every client curls every web pod *by IP*, bypassing
Services entirely. That isolates the data plane: if a Service test failed you
would not know whether kube-proxy or the CNI was at fault. The test records which
paths were cross-node and fails if none were.

**Service ClusterIP** — the same request through the Service name, which adds
kube-proxy and DNS on top of the working data plane.

**NetworkPolicy enforcement** — the important one:

1. Confirm the client can reach `web`.
2. Apply `default-deny-ingress`. Poll until traffic **stops**.
3. Apply `allow-client-to-web`. Poll until traffic **resumes**.
4. Remove both policies.

Step 2 is the whole point. Kubernetes accepts a `NetworkPolicy` on a cluster
with no policy engine at all and traffic keeps flowing — "the policy applied
cleanly" proves nothing. Only observing traffic stop proves enforcement.

The polling matters too: programming policy on every node is not instantaneous,
so the test waits for the transition rather than checking once and flaking.

Next: [04 — OpenEBS storage](04-openebs-storage.md)
