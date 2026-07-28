"""Persistence proof for the demo workload.

The claim under test: data written to an OpenEBS volume outlives the pod that
wrote it. The only convincing way to show that is to destroy the pod and read
the data back from its replacement.
"""

from __future__ import annotations

import time

import pytest
from kubernetes.client.exceptions import ApiException

from conftest import NS_DEMO, kubectl, pod_exec, wait_until

POD = "ledger-0"
SENTINEL = "/data/sentinel.txt"


@pytest.fixture(scope="module", autouse=True)
def ledger(core, apps):
    kubectl("apply", "-f", "manifests/workload/ledger-statefulset.yaml")

    def ready():
        try:
            pod = core.read_namespaced_pod(POD, NS_DEMO)
        except ApiException:
            return False
        return pod.status.phase == "Running" and all(
            cs.ready for cs in (pod.status.container_statuses or [])
        )

    wait_until(ready, timeout=240, desc=f"{POD} to be ready")
    return POD


def _read(core, path, default=""):
    rc, out, _ = pod_exec(core, NS_DEMO, POD, ["cat", path])
    return out.strip() if rc == 0 else default


def _boot_count(core) -> int:
    rc, out, err = pod_exec(
        core, NS_DEMO, POD, ["sh", "-c", "wc -l < /data/boots.txt"]
    )
    assert rc == 0, f"could not read boot log: {err}"
    return int(out.strip())


def test_volume_is_mounted_from_openebs(core):
    pvc = core.read_namespaced_persistent_volume_claim("data-ledger-0", NS_DEMO)
    assert pvc.status.phase == "Bound"
    assert pvc.spec.storage_class_name == "openebs-hostpath"


def test_workload_is_writing_to_the_volume(core):
    rc, out, err = pod_exec(core, NS_DEMO, POD, ["ls", "-1", "/data"])
    assert rc == 0, f"cannot list /data: {err}"
    for expected in ("boots.txt", "ledger.log", "bulk.dat"):
        assert expected in out, f"{expected} missing from the volume:\n{out}"


def test_data_survives_pod_deletion(core):
    """Destroy the pod; the replacement must see the previous pod's writes."""
    marker = f"written-at-{int(time.time())}"
    rc, _, err = pod_exec(
        core, NS_DEMO, POD, ["sh", "-c", f"echo {marker} > {SENTINEL}"]
    )
    assert rc == 0, f"could not write sentinel: {err}"
    assert _read(core, SENTINEL) == marker, "sentinel did not read back before deletion"

    boots_before = _boot_count(core)
    old_uid = core.read_namespaced_pod(POD, NS_DEMO).metadata.uid

    core.delete_namespaced_pod(POD, NS_DEMO)

    def replaced_and_ready():
        try:
            pod = core.read_namespaced_pod(POD, NS_DEMO)
        except ApiException:
            return False  # still being recreated
        if pod.metadata.uid == old_uid:
            return False  # the old pod is still terminating
        return pod.status.phase == "Running" and all(
            cs.ready for cs in (pod.status.container_statuses or [])
        )

    wait_until(replaced_and_ready, timeout=240, interval=3, desc="a new ledger-0 pod")

    assert _read(core, SENTINEL) == marker, (
        "the file written by the previous pod is gone -- the volume did not "
        "persist, or the new pod bound a different volume"
    )

    boots_after = _boot_count(core)
    assert boots_after == boots_before + 1, (
        f"boot log should have grown by exactly one entry "
        f"({boots_before} -> {boots_after}); the volume was not reused"
    )


def test_pod_rescheduled_to_the_node_holding_its_data(core):
    """A hostpath volume cannot follow a pod, so the pod must follow the volume."""
    pod = core.read_namespaced_pod(POD, NS_DEMO)
    pvc = core.read_namespaced_persistent_volume_claim("data-ledger-0", NS_DEMO)
    pv = core.read_persistent_volume(pvc.spec.volume_name)

    pinned = [
        v
        for term in pv.spec.node_affinity.required.node_selector_terms
        for expr in term.match_expressions
        if expr.key == "kubernetes.io/hostname"
        for v in expr.values
    ]
    assert pod.spec.node_name in pinned, (
        f"pod landed on {pod.spec.node_name} but its data is on {pinned}"
    )
