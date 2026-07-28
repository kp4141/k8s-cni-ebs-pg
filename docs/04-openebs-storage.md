# 04 — OpenEBS storage

```bash
make openebs
```

Installs OpenEBS 4.5.1 with a single engine: LocalPV Hostpath.

## Choosing an engine

The OpenEBS umbrella chart enables five storage engines plus a bundled
Loki/Alloy logging stack by default. Most of them cannot work inside a kind node
on macOS, and leaving them on fills the namespace with `CrashLoopBackOff` pods
that look like a broken install.

| Engine | Enabled | Why |
|---|---|---|
| **LocalPV Hostpath** | **yes** | A directory on the node. No kernel modules, no spare block devices, no special hardware. |
| LocalPV LVM | no | Needs a real LVM volume group. A kind node is a container with no VG and no spare device to build one from. |
| LocalPV ZFS | no | Needs a zpool and the ZFS kernel module, absent from the Colima VM's kernel. |
| LocalPV Rawfile | no | Upstream still marks it pre-stable. |
| Replicated (Mayastor) | no | See below. |
| Loki + Alloy | no | We run our own observability stack; two collectors on an 8 GB VM fight over memory. |

The toggle names come from the chart's dependency conditions, which is the
reliable way to find them:

```bash
helm show chart openebs/openebs --version 4.5.1   # read `dependencies[].condition`
```

### Why not Mayastor

Replicated PV Mayastor is the interesting part of OpenEBS — genuine synchronous
replication across nodes with NVMe-oF. It needs hugepages configured on the host,
an NVMe-capable kernel, and dedicated block devices to build DiskPools from.

None of that survives `macOS → Colima VM → Docker → kind node`. Attempting it
produces `io-engine` pods that crash-loop on hugepage allocation. On real
hardware, or a cloud VM with spare disks, it is the engine worth using; this lab
is honest about not being that.

## StorageClass

```
NAME                         PROVISIONER        RECLAIMPOLICY   VOLUMEBINDINGMODE
openebs-hostpath (default)   openebs.io/local   Delete          WaitForFirstConsumer
standard                     rancher.io/local-path
```

### Demoting kind's default

kind preinstalls `rancher.io/local-path` and marks it default. Two defaults is an
error state, and — worse — a PVC that omits `storageClassName` would land on
local-path and quietly bypass OpenEBS entirely, making the whole storage
validation meaningless.

`make openebs` demotes `standard` and promotes `openebs-hostpath`.
[test_03_storage.py](../validation/test_03_storage.py) asserts exactly one
default exists and that it is the OpenEBS one.

### WaitForFirstConsumer

The single most-reported OpenEBS "bug" that is not a bug: a fresh PVC sits
`Pending` and looks broken.

```
NAME        STATUS    VOLUME   CAPACITY   STORAGECLASS
probe-pvc   Pending                       openebs-hostpath
```

A hostpath volume is a directory on *one specific node*. The provisioner cannot
know which node until the scheduler has placed the pod that will use it — binding
early would risk creating the directory on a node the pod never lands on. So the
PVC waits.

The test demonstrates this rather than asserting it from the spec: create a PVC
with no consumer, confirm it stays `Pending`, then attach a pod and confirm it
binds.

## How a volume is actually made

Once a pod is scheduled, the provisioner creates a directory on that node:

```
/var/openebs/local/pvc-cbd3ec04-a9c8-42a2-b479-085cdab252be/
├── boots.txt
├── bulk.dat
└── ledger.log
```

and creates a PV pointing at it with **node affinity** to the node holding the
directory:

```yaml
nodeAffinity:
  required:
    nodeSelectorTerms:
      - matchExpressions:
          - key: kubernetes.io/hostname
            operator: In
            values: [k8s-cni-lab-worker]
```

That affinity is load-bearing. It is what stops the scheduler from moving the pod
to a node where the data does not exist. Losing it would mean silent data loss on
reschedule — the pod would start happily with an empty directory.

Inspect it directly:

```bash
kubectl -n storage-demo get pvc data-ledger-0
docker exec k8s-cni-lab-worker ls -la /var/openebs/local/<pv-name>
```

## The limits that matter

**No quota enforcement.** The `1Gi` in a PVC spec is advisory metadata. Nothing
stops a hostpath volume growing past it — it will keep consuming the node's disk
until `/var` is full, at which point *every* volume on that node fails at once
and the kubelet starts evicting for disk pressure. This is why
[docs/07](07-metrics-and-dashboards.md) alerts on both per-volume overrun and
node filesystem pressure.

**No replication.** One node holds the data. If it goes away, the data goes with
it. LocalPV is for workloads that replicate at the application layer, or for data
you can rebuild.

**Not portable.** The pod is pinned to one node for the life of the volume.

**Teardown destroys it.** `kind delete cluster` deletes the node containers and
their filesystems, and hostpath data lives on those filesystems.

## Validation

```bash
make validate-storage
```

Five tests: provisioner identity and binding mode, exactly one default
StorageClass, provisioner running, `WaitForFirstConsumer` demonstrated
end-to-end, and PV node affinity pinned to exactly the node running the pod.

Next: [05 — Monitoring stack](05-monitoring-stack.md)
