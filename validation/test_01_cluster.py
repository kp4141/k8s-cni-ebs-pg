"""Cluster-level sanity checks.

If these fail, nothing downstream is worth debugging.
"""

from __future__ import annotations

EXPECTED_NODES = 3


def test_all_nodes_ready(core):
    nodes = core.list_node().items
    assert len(nodes) == EXPECTED_NODES, (
        f"expected {EXPECTED_NODES} nodes, found {len(nodes)}: "
        f"{[n.metadata.name for n in nodes]}"
    )

    not_ready = []
    for node in nodes:
        ready = next(
            (c for c in node.status.conditions if c.type == "Ready"), None
        )
        if ready is None or ready.status != "True":
            not_ready.append(
                f"{node.metadata.name}: {ready.message if ready else 'no Ready condition'}"
            )

    assert not not_ready, "nodes not Ready:\n" + "\n".join(not_ready)


def test_topology_is_one_control_plane_two_workers(core):
    nodes = core.list_node().items
    cp = [
        n
        for n in nodes
        if "node-role.kubernetes.io/control-plane" in (n.metadata.labels or {})
    ]
    assert len(cp) == 1, f"expected 1 control-plane, found {len(cp)}"
    assert len(nodes) - len(cp) == 2, "expected 2 worker nodes"


def test_no_node_reports_missing_cni(core):
    """The pre-CNI failure mode, asserted as an explicit regression guard."""
    for node in core.list_node().items:
        ready = next(c for c in node.status.conditions if c.type == "Ready")
        assert "cni plugin not initialized" not in (ready.message or ""), (
            f"{node.metadata.name} still reports no CNI -- did Calico roll out? "
            f"message: {ready.message}"
        )


def test_calico_daemonset_runs_on_every_node(apps, core):
    ds = apps.read_namespaced_daemon_set("calico-node", "calico-system")
    desired = ds.status.desired_number_scheduled
    ready = ds.status.number_ready or 0
    assert desired == EXPECTED_NODES, f"calico-node desired={desired}"
    assert ready == desired, f"calico-node ready={ready}/{desired}"


def test_coredns_is_running(apps):
    dep = apps.read_namespaced_deployment("coredns", "kube-system")
    assert (dep.status.ready_replicas or 0) >= 1, (
        "CoreDNS has no ready replicas; it stays Pending until the CNI works"
    )


def test_pods_have_addresses_from_the_configured_pod_cidr(core):
    """Pod IPs must come from 10.244.0.0/16, matching the Calico IPPool.

    A pod holding an address outside the pool means kind and Calico disagree
    about the pod CIDR, which produces intermittent routing failures rather
    than a clean error.
    """
    import ipaddress

    pool = ipaddress.ip_network("10.244.0.0/16")
    offenders = []
    for pod in core.list_pod_for_all_namespaces().items:
        ip = pod.status.pod_ip
        if not ip or pod.spec.host_network:
            continue  # host-network pods legitimately use the node address
        if ipaddress.ip_address(ip) not in pool:
            offenders.append(f"{pod.metadata.namespace}/{pod.metadata.name}={ip}")

    assert not offenders, (
        "pods outside the 10.244.0.0/16 pod CIDR:\n" + "\n".join(offenders)
    )
