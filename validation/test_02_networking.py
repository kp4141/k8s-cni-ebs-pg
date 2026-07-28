"""CNI validation.

The shape that matters: traffic is forced across node boundaries. A pod-to-pod
test where both pods share a node passes even with a completely broken overlay,
because the packet never leaves the host.
"""

from __future__ import annotations

import pytest

from conftest import NS_NETTEST, kubectl, pod_exec, wait_until

WGET = ["wget", "-T", "4", "-q", "-O", "-"]


@pytest.fixture(scope="module", autouse=True)
def net_fixtures(core, apps):
    kubectl("apply", "-f", "manifests/networking/net-test.yaml")

    def web_ready():
        dep = apps.read_namespaced_deployment("web", NS_NETTEST)
        return (dep.status.ready_replicas or 0) == 2

    def clients_ready():
        ds = apps.read_namespaced_daemon_set("client", NS_NETTEST)
        return ds.status.number_ready == ds.status.desired_number_scheduled

    wait_until(web_ready, timeout=180, desc="both web replicas ready")
    wait_until(clients_ready, timeout=180, desc="client daemonset ready on all nodes")
    yield
    kubectl(
        "delete", "-f", "manifests/networking/net-test.yaml",
        "--ignore-not-found", check=False, timeout=180,
    )


def _pods(core, selector):
    return core.list_namespaced_pod(NS_NETTEST, label_selector=selector).items


@pytest.fixture(scope="module")
def web_pods(core, net_fixtures):
    return _pods(core, "app=web")


@pytest.fixture(scope="module")
def client_pods(core, net_fixtures):
    return _pods(core, "app=client")


def test_web_replicas_landed_on_different_nodes(web_pods):
    """Precondition for every cross-node assertion below."""
    nodes = {p.spec.node_name for p in web_pods}
    assert len(nodes) == 2, (
        f"anti-affinity did not spread the web pods; all on {nodes}. "
        "Cross-node routing would not actually be exercised."
    )


def test_client_runs_on_every_node(client_pods, core):
    node_count = len(core.list_node().items)
    assert len(client_pods) == node_count, (
        f"{len(client_pods)} client pods for {node_count} nodes -- "
        "the control-plane toleration may be missing"
    )


def test_dns_resolves_the_kubernetes_api_service(core, client_pods):
    pod = client_pods[0].metadata.name
    rc, out, err = pod_exec(
        core, NS_NETTEST, pod, ["nslookup", "kubernetes.default.svc.cluster.local"]
    )
    assert rc == 0, f"DNS lookup failed from {pod}: {out}{err}"
    assert "10.96.0.1" in out, f"unexpected ClusterIP for kubernetes service:\n{out}"


def test_dns_resolves_a_user_service(core, client_pods):
    pod = client_pods[0].metadata.name
    rc, out, err = pod_exec(
        core, NS_NETTEST, pod, ["nslookup", "web.net-test.svc.cluster.local"]
    )
    assert rc == 0, f"service DNS lookup failed from {pod}: {out}{err}"


def test_pod_to_pod_reaches_every_pod_from_every_node(core, client_pods, web_pods):
    """Full mesh: each client hits each web pod by IP, bypassing Services.

    This isolates the data plane. If a Service test failed you would not know
    whether kube-proxy or the CNI was at fault; this only exercises the CNI.
    """
    failures = []
    cross_node_ok = 0

    for c in client_pods:
        for w in web_pods:
            rc, out, err = pod_exec(
                core, NS_NETTEST, c.metadata.name, [*WGET, f"http://{w.status.pod_ip}/"]
            )
            same_node = c.spec.node_name == w.spec.node_name
            path = (
                f"{c.spec.node_name} -> {w.status.pod_ip}@{w.spec.node_name} "
                f"({'same-node' if same_node else 'CROSS-NODE'})"
            )
            if rc != 0 or "nginx" not in out.lower():
                failures.append(f"{path}: rc={rc} {err.strip()}")
            elif not same_node:
                cross_node_ok += 1

    assert not failures, "pod-to-pod failures:\n" + "\n".join(failures)
    assert cross_node_ok > 0, "no cross-node path was exercised"


def test_service_clusterip_is_reachable(core, client_pods):
    pod = client_pods[0].metadata.name
    rc, out, err = pod_exec(
        core, NS_NETTEST, pod, [*WGET, "http://web.net-test.svc.cluster.local/"]
    )
    assert rc == 0 and "nginx" in out.lower(), (
        f"Service ClusterIP unreachable from {pod}: rc={rc} {err}"
    )


def test_networkpolicy_is_actually_enforced(core, client_pods, web_pods):
    """Deny, confirm traffic stops, allow, confirm it resumes.

    Applying a NetworkPolicy always "succeeds" -- the API accepts the object on
    a cluster with no policy engine at all and traffic keeps flowing. The only
    proof of enforcement is observing traffic stop.
    """
    client = client_pods[0].metadata.name
    target = f"http://{web_pods[0].status.pod_ip}/"

    def reachable() -> bool:
        rc, _, _ = pod_exec(core, NS_NETTEST, client, [*WGET, target], timeout=20)
        return rc == 0

    assert reachable(), "precondition failed: web not reachable before any policy"

    try:
        kubectl("apply", "-f", "manifests/networking/netpol-default-deny.yaml")
        # Programming iptables/eBPF on every node is not instantaneous.
        wait_until(
            lambda: not reachable(),
            timeout=60,
            interval=2,
            desc="deny-all policy to block traffic",
        )

        kubectl("apply", "-f", "manifests/networking/netpol-allow-client.yaml")
        wait_until(
            reachable,
            timeout=60,
            interval=2,
            desc="allow policy to restore traffic",
        )
    finally:
        kubectl(
            "delete", "-f", "manifests/networking/netpol-allow-client.yaml",
            "--ignore-not-found", check=False,
        )
        kubectl(
            "delete", "-f", "manifests/networking/netpol-default-deny.yaml",
            "--ignore-not-found", check=False,
        )
