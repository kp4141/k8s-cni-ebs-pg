"""OpenEBS StorageClass and PVC lifecycle validation."""

from __future__ import annotations

import pytest
from kubernetes import client as k8s

from conftest import kubectl, wait_until

SC_NAME = "openebs-hostpath"
PROBE_NS = "storage-probe"
PROBE_PVC = "probe-pvc"
PROBE_POD = "probe-pod"


def test_storageclass_exists_with_the_expected_provisioner(storage):
    sc = storage.read_storage_class(SC_NAME)
    assert sc.provisioner == "openebs.io/local", (
        f"unexpected provisioner: {sc.provisioner}"
    )
    assert sc.volume_binding_mode == "WaitForFirstConsumer", (
        "hostpath volumes are node-local, so binding must be deferred until the "
        f"consuming pod is scheduled; found {sc.volume_binding_mode}"
    )


def test_openebs_is_the_only_default_storageclass(storage):
    """Two defaults is an error state, and zero silently breaks any PVC that
    omits storageClassName."""
    defaults = [
        sc.metadata.name
        for sc in storage.list_storage_class().items
        if (sc.metadata.annotations or {}).get(
            "storageclass.kubernetes.io/is-default-class"
        )
        == "true"
    ]
    assert defaults == [SC_NAME], f"expected only {SC_NAME} to be default, got {defaults}"


def test_provisioner_is_running(apps):
    dep = apps.read_namespaced_deployment("openebs-localpv-provisioner", "openebs")
    assert (dep.status.ready_replicas or 0) >= 1, "localpv provisioner is not ready"


@pytest.fixture(scope="module")
def probe_namespace(core):
    kubectl("create", "namespace", PROBE_NS, check=False)
    yield PROBE_NS
    kubectl(
        "delete", "namespace", PROBE_NS, "--ignore-not-found", "--wait=false",
        check=False,
    )


def test_pvc_stays_pending_until_a_pod_consumes_it(core, probe_namespace):
    """WaitForFirstConsumer, demonstrated rather than asserted from the spec.

    This is the single most-reported OpenEBS "bug" that is not a bug: a fresh
    PVC sits Pending indefinitely and looks broken. It is waiting for a pod.
    """
    pvc = k8s.V1PersistentVolumeClaim(
        metadata=k8s.V1ObjectMeta(name=PROBE_PVC),
        spec=k8s.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name=SC_NAME,
            resources=k8s.V1VolumeResourceRequirements(requests={"storage": "512Mi"}),
        ),
    )
    core.create_namespaced_persistent_volume_claim(probe_namespace, pvc)

    # Give the provisioner ample opportunity to bind it early. It must not.
    import time

    time.sleep(15)
    current = core.read_namespaced_persistent_volume_claim(PROBE_PVC, probe_namespace)
    assert current.status.phase == "Pending", (
        f"PVC bound without a consumer (phase={current.status.phase}); "
        "WaitForFirstConsumer is not in effect"
    )

    # Now attach a pod and the same PVC must bind.
    pod = k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(name=PROBE_POD),
        spec=k8s.V1PodSpec(
            restart_policy="Never",
            containers=[
                k8s.V1Container(
                    name="writer",
                    image="busybox:1.36",
                    command=[
                        "sh", "-c",
                        "echo provisioned > /data/probe.txt && cat /data/probe.txt && sleep 3600",
                    ],
                    volume_mounts=[k8s.V1VolumeMount(name="data", mount_path="/data")],
                )
            ],
            volumes=[
                k8s.V1Volume(
                    name="data",
                    persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
                        claim_name=PROBE_PVC
                    ),
                )
            ],
        ),
    )
    core.create_namespaced_pod(probe_namespace, pod)

    def bound():
        c = core.read_namespaced_persistent_volume_claim(PROBE_PVC, probe_namespace)
        return c.status.phase == "Bound"

    wait_until(bound, timeout=180, desc="PVC to bind once a pod consumes it")


def test_provisioned_pv_is_pinned_to_one_node(core, probe_namespace):
    """A hostpath PV is a directory on a specific node.

    Its node affinity is what stops the scheduler from moving the pod somewhere
    the data does not exist. Losing that affinity would mean silent data loss on
    reschedule.
    """
    pvc = core.read_namespaced_persistent_volume_claim(PROBE_PVC, probe_namespace)
    assert pvc.spec.volume_name, "PVC has no bound volume"

    pv = core.read_persistent_volume(pvc.spec.volume_name)
    assert pv.spec.storage_class_name == SC_NAME
    assert pv.spec.node_affinity is not None, "hostpath PV must carry node affinity"

    terms = pv.spec.node_affinity.required.node_selector_terms
    values = [
        v
        for term in terms
        for expr in term.match_expressions
        if expr.key == "kubernetes.io/hostname"
        for v in expr.values
    ]
    assert len(values) == 1, f"expected affinity to exactly one node, got {values}"

    pod = core.read_namespaced_pod(PROBE_POD, probe_namespace)
    assert pod.spec.node_name == values[0], (
        f"pod is on {pod.spec.node_name} but its volume lives on {values[0]}"
    )
