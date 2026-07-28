"""Shared fixtures and helpers for the lab validation suite.

Everything here talks to the live cluster. There are no mocks: a passing run
means the cluster genuinely did the thing.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest
import requests
from kubernetes import client, config
from kubernetes.stream import stream
from kubernetes.stream.ws_client import ERROR_CHANNEL

KCTX = os.environ.get("KCTX", "kind-k8s-cni-lab")
PROM_URL = os.environ.get("PROM_URL", "http://localhost:30090")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:30030")
GRAFANA_AUTH = (
    os.environ.get("GRAFANA_USER", "admin"),
    os.environ.get("GRAFANA_PASSWORD", "admin"),
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NS_NETTEST = "net-test"
NS_DEMO = "storage-demo"
NS_MONITORING = "monitoring"
NS_OPENEBS = "openebs"


# --------------------------------------------------------------- kubectl shim
def kubectl(*args: str, check: bool = True, timeout: int = 120) -> str:
    """Run kubectl against the lab context and return stdout.

    Used for apply/delete where hand-rolling the Python client object graph
    would be far more code than the YAML it replaces.
    """
    cmd = ["kubectl", "--context", KCTX, *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO_ROOT
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"kubectl {' '.join(args)} failed (rc={proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc.stdout


# ------------------------------------------------------------ client fixtures
@pytest.fixture(scope="session", autouse=True)
def _kube_config():
    try:
        config.load_kube_config(context=KCTX)
    except Exception as exc:  # pragma: no cover - environment problem
        pytest.exit(
            f"cannot load kube context '{KCTX}': {exc}\n"
            "Is the cluster up? Try: make cluster",
            returncode=2,
        )


@pytest.fixture(scope="session")
def core(_kube_config) -> client.CoreV1Api:
    return client.CoreV1Api()


@pytest.fixture(scope="session")
def apps(_kube_config) -> client.AppsV1Api:
    return client.AppsV1Api()


@pytest.fixture(scope="session")
def storage(_kube_config) -> client.StorageV1Api:
    return client.StorageV1Api()


# ------------------------------------------------------------------ exec help
def pod_exec(
    core: client.CoreV1Api,
    namespace: str,
    pod: str,
    argv: list[str],
    container: str | None = None,
    timeout: int = 60,
) -> tuple[int, str, str]:
    """Exec argv inside a pod. Returns (returncode, stdout, stderr).

    The exit code arrives out-of-band on the error channel rather than as a
    normal return value, so it has to be parsed out of a status object.
    """
    resp = stream(
        core.connect_get_namespaced_pod_exec,
        pod,
        namespace,
        command=argv,
        container=container,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=False,
    )

    out: list[str] = []
    err: list[str] = []
    deadline = time.time() + timeout
    while resp.is_open():
        if time.time() > deadline:
            resp.close()
            raise TimeoutError(f"exec timed out after {timeout}s: {argv}")
        resp.update(timeout=1)
        if resp.peek_stdout():
            out.append(resp.read_stdout())
        if resp.peek_stderr():
            err.append(resp.read_stderr())

    status_raw = resp.read_channel(ERROR_CHANNEL)
    resp.close()

    rc = 0
    if status_raw:
        status = json.loads(status_raw)
        if status.get("status") != "Success":
            rc = 1
            for cause in status.get("details", {}).get("causes", []):
                if cause.get("reason") == "ExitCode":
                    rc = int(cause.get("message", 1))
    return rc, "".join(out), "".join(err)


def wait_until(predicate, timeout: int = 120, interval: float = 3.0, desc: str = ""):
    """Poll predicate until it returns truthy. Returns its value, or raises."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {desc or predicate}")


# --------------------------------------------------------------- prometheus
def prom_query(expr: str, timeout: int = 20) -> list[dict]:
    """Run an instant PromQL query and return the result vector."""
    resp = requests.get(
        f"{PROM_URL}/api/v1/query", params={"query": expr}, timeout=timeout
    )
    resp.raise_for_status()
    body = resp.json()
    assert body.get("status") == "success", f"query failed: {expr} -> {body}"
    return body["data"]["result"]


def prom_query_scalar(expr: str) -> float:
    """Run a PromQL query expected to yield exactly one sample."""
    result = prom_query(expr)
    assert result, f"query returned no series: {expr}"
    return float(result[0]["value"][1])


@pytest.fixture(scope="session")
def prometheus_ready():
    """Fail fast, and usefully, if Prometheus is not reachable."""
    try:
        resp = requests.get(f"{PROM_URL}/-/ready", timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        pytest.fail(
            f"Prometheus unreachable at {PROM_URL}: {exc}\n"
            "Check the NodePort is published: kubectl -n monitoring get svc kps-prometheus\n"
            "Or port-forward instead: "
            "kubectl -n monitoring port-forward svc/kps-prometheus 30090:9090"
        )
    return PROM_URL


@pytest.fixture(scope="session")
def grafana_ready():
    try:
        resp = requests.get(f"{GRAFANA_URL}/api/health", timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        pytest.fail(
            f"Grafana unreachable at {GRAFANA_URL}: {exc}\n"
            "Or port-forward instead: "
            "kubectl -n monitoring port-forward svc/kps-grafana 30030:80"
        )
    return GRAFANA_URL


def grafana_get(path: str, timeout: int = 20):
    resp = requests.get(f"{GRAFANA_URL}{path}", auth=GRAFANA_AUTH, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
