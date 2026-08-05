"""Grafana validation.

A dashboard that loads is not a dashboard that works. These tests follow the
whole path: Grafana is healthy, its datasource resolves, the dashboard was
imported, its panel queries are ones Prometheus recognises, and running one of
them *through Grafana* returns real samples.
"""

from __future__ import annotations

import json

import pytest
import requests

from conftest import GRAFANA_AUTH, GRAFANA_URL, grafana_get

# Every dashboard this repo ships. The stock kube-prometheus-stack dashboards
# are not listed: they are upstream's to keep working, and several of them
# aggregate node metrics with sum(), which is wrong on kind (see docs/08
# §"Node dashboards show 3x the VM's real resources").
DASHBOARD_UIDS = ["openebs-storage", "vm-infra", "app-ledger"]


def test_grafana_reports_healthy(grafana_ready):
    health = grafana_get("/api/health")
    assert health.get("database") == "ok", f"unhealthy: {health}"


@pytest.fixture(scope="module")
def prometheus_datasource(grafana_ready):
    sources = grafana_get("/api/datasources")
    prom = [d for d in sources if d["type"] == "prometheus"]
    assert prom, f"no Prometheus datasource provisioned; found {sources}"
    return prom[0]


def test_prometheus_datasource_is_reachable_from_grafana(prometheus_datasource):
    """Exercises Grafana's own connection, not ours.

    Grafana talks to the in-cluster Service; our tests talk via NodePort. One
    can work while the other is broken.
    """
    uid = prometheus_datasource["uid"]
    resp = requests.get(
        f"{GRAFANA_URL}/api/datasources/uid/{uid}/health",
        auth=GRAFANA_AUTH,
        timeout=30,
    )
    assert resp.status_code == 200, f"datasource health check failed: {resp.text}"
    body = resp.json()
    assert body.get("status", "").upper() == "OK", f"datasource unhealthy: {body}"


def _fetch_dashboard(uid: str):
    resp = requests.get(
        f"{GRAFANA_URL}/api/dashboards/uid/{uid}",
        auth=GRAFANA_AUTH,
        timeout=30,
    )
    assert resp.status_code == 200, (
        f"dashboard '{uid}' not found (HTTP {resp.status_code}). "
        "The sidecar imports it from the ConfigMap within ~60s; check "
        "kubectl -n monitoring logs deploy/kps-grafana -c grafana-sc-dashboard"
    )
    return resp.json()["dashboard"]


@pytest.fixture(scope="module", params=DASHBOARD_UIDS)
def dashboard(request, grafana_ready):
    return _fetch_dashboard(request.param)


def test_stock_kube_prometheus_dashboards_are_present(grafana_ready):
    """The chart ships ~29 infra dashboards. If the sidecar breaks, they vanish
    silently and Grafana just looks empty."""
    found = requests.get(
        f"{GRAFANA_URL}/api/search",
        params={"type": "dash-db", "limit": 500},
        auth=GRAFANA_AUTH,
        timeout=30,
    ).json()
    titles = {d["title"] for d in found}
    for expected in (
        "Kubernetes Monitoring Dashboard",
    ):
        assert expected in titles, (
            f"stock dashboard '{expected}' missing; {len(titles)} dashboards present"
        )


def test_dashboard_was_imported_by_the_sidecar(dashboard):
    assert dashboard["panels"], f"{dashboard['uid']} has no panels"


def test_dashboard_panels_all_carry_queries(dashboard):
    missing = [
        p["title"]
        for p in dashboard["panels"]
        if p["type"] != "row" and not p.get("targets")
    ]
    assert not missing, f"panels with no query: {missing}"


def test_every_panel_query_returns_data(prometheus_datasource, dashboard):
    """Run each panel's PromQL through Grafana's datasource proxy.

    This is the test that catches a dashboard referencing a metric that does not
    exist -- the panel renders as an empty chart rather than an error, so
    nothing else notices.
    """
    uid = prometheus_datasource["uid"]
    empty = []
    errors = []

    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            continue
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if not expr:
                continue
            # Panel queries use $pvc for the template variable; "All" expands to
            # a match-everything regex.
            resolved = expr.replace("$pvc", ".*")
            resp = requests.get(
                f"{GRAFANA_URL}/api/datasources/proxy/uid/{uid}/api/v1/query",
                params={"query": resolved},
                auth=GRAFANA_AUTH,
                timeout=30,
            )
            label = f"{panel['title']!r} [{target.get('refId')}]"
            if resp.status_code != 200:
                errors.append(f"{label}: HTTP {resp.status_code} {resp.text[:160]}")
                continue
            body = resp.json()
            if body.get("status") != "success":
                errors.append(f"{label}: {json.dumps(body)[:160]}")
            elif not body["data"]["result"]:
                empty.append(f"{label}: {resolved[:90]}")

    dash = dashboard["uid"]
    assert not errors, f"[{dash}] panel queries errored:\n" + "\n".join(errors)
    assert not empty, f"[{dash}] panel queries returned no data:\n" + "\n".join(empty)


def test_dashboard_variable_resolves_to_real_volumes(prometheus_datasource):
    """The 'Volume' dropdown must actually populate."""
    uid = prometheus_datasource["uid"]
    resp = requests.get(
        f"{GRAFANA_URL}/api/datasources/proxy/uid/{uid}/api/v1/label/"
        "persistentvolumeclaim/values",
        auth=GRAFANA_AUTH,
        timeout=30,
    )
    resp.raise_for_status()
    values = resp.json()["data"]
    assert "data-ledger-0" in values, (
        f"demo PVC missing from the variable's values: {values}"
    )
