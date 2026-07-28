"""Prometheus scrape-target and metric-content validation.

Distinguishes two failure modes that look alike from a dashboard: a target that
is down, and a target that is up but producing nothing useful.
"""

from __future__ import annotations

import time

import pytest
import requests

from conftest import PROM_URL, prom_query, prom_query_scalar, wait_until

# Every one of these must be scrapeable. The four control-plane jobs are only
# reachable because cluster/kind-config.yaml rebound them off 127.0.0.1 at
# cluster creation; on a stock kind cluster they are all down.
REQUIRED_JOBS = {
    "apiserver",
    "kubelet",
    "kube-controller-manager",
    "kube-scheduler",
    "kube-proxy",
    "kube-etcd",
    "node-exporter",
    "kube-state-metrics",
    "openebs-hostpath-exporter",
    "ledger",
}


def _fetch_targets():
    resp = requests.get(f"{PROM_URL}/api/v1/targets", timeout=20)
    resp.raise_for_status()
    return resp.json()["data"]["activeTargets"]


@pytest.fixture(scope="module")
def targets(prometheus_ready):
    """Scrape targets, once they have settled.

    Polled rather than sampled once. test_04 deliberately deletes the ledger
    pod, so when this module runs immediately afterwards that target is
    legitimately down for a few seconds while the replacement starts and
    Prometheus rediscovers it. A single snapshot would flake on that and report
    a real-looking failure that is nothing of the sort.
    """
    settled = _fetch_targets()
    deadline = time.time() + 180
    while time.time() < deadline:
        settled = _fetch_targets()
        if all(t["health"] == "up" for t in settled):
            break
        time.sleep(10)
    return settled


def test_every_expected_job_has_at_least_one_target(targets):
    found = {t["labels"].get("job") for t in targets}
    missing = REQUIRED_JOBS - found
    assert not missing, (
        f"no scrape targets for: {sorted(missing)}\n"
        f"jobs present: {sorted(j for j in found if j)}"
    )


def test_no_target_is_down(targets):
    down = [
        f"{t['labels'].get('job')} @ {t.get('scrapeUrl')}: {t.get('lastError')}"
        for t in targets
        if t["health"] != "up"
    ]
    assert not down, (
        "unhealthy scrape targets after waiting for them to settle:\n"
        + "\n".join(down)
    )


def test_control_plane_components_are_actually_scraped(targets):
    """Guards the kubeadm bind-address patch specifically."""
    for job in ("kube-controller-manager", "kube-scheduler", "kube-proxy", "kube-etcd"):
        up = [t for t in targets if t["labels"].get("job") == job and t["health"] == "up"]
        assert up, (
            f"{job} has no healthy target. Its metrics listener is probably still "
            "bound to 127.0.0.1 -- see cluster/kind-config.yaml kubeadmConfigPatches."
        )


def test_hostpath_exporter_reports_from_every_node(prometheus_ready, core):
    node_count = len(core.list_node().items)
    series = prom_query("openebs_hostpath_volumes")
    assert len(series) == node_count, (
        f"exporter reporting from {len(series)} of {node_count} nodes"
    )


# --------------------------------------------------------------- metric content
def test_kube_state_metrics_sees_our_pvcs(prometheus_ready):
    series = prom_query('kube_persistentvolumeclaim_info{storageclass=~"openebs-.*"}')
    assert len(series) >= 4, (
        f"expected at least 4 OpenEBS PVCs (3 monitoring + 1 demo), got {len(series)}"
    )


def test_recording_rules_are_producing_series(prometheus_ready):
    """Recording rules fail silently: a bad join yields an empty series and the
    dashboard simply shows 'No data'."""
    for rule in (
        "openebs:volume_used_bytes",
        "openebs:volume_requested_bytes",
        "openebs:volume_used_ratio",
        "openebs:node_backing_fs_used_ratio",
    ):
        series = prom_query(rule)
        assert series, (
            f"recording rule {rule} produced no series. Check the join labels "
            f"with: {rule.split(':')[1]} in the Prometheus expression browser."
        )


def test_volume_metrics_carry_pvc_identity(prometheus_ready):
    """The join must survive into the recorded series, or the dashboard cannot
    label anything."""
    series = prom_query("openebs:volume_used_bytes")
    for s in series:
        labels = s["metric"]
        assert labels.get("namespace"), f"series without namespace: {labels}"
        assert labels.get("persistentvolumeclaim"), f"series without PVC: {labels}"


def test_demo_volume_usage_is_nonzero_and_distinct(prometheus_ready):
    """The bug this catches: kubelet_volume_stats_* reports identical node-level
    figures for every hostpath volume. Distinct values prove we are measuring
    the directories, not the filesystem."""
    series = prom_query("openebs:volume_used_bytes")
    values = {
        s["metric"]["persistentvolumeclaim"]: float(s["value"][1]) for s in series
    }
    assert "data-ledger-0" in values, f"demo volume missing from {list(values)}"
    assert values["data-ledger-0"] > 0, "demo volume reports zero bytes used"
    assert len(set(values.values())) > 1, (
        "every volume reports an identical size, which means the metric is "
        f"measuring the node filesystem rather than the volumes: {values}"
    )


def test_used_ratio_is_a_sane_fraction(prometheus_ready):
    ratios = {
        s["metric"]["persistentvolumeclaim"]: float(s["value"][1])
        for s in prom_query("openebs:volume_used_ratio")
    }
    assert ratios, "no ratio series"
    for name, value in ratios.items():
        # May legitimately exceed 1 (hostpath enforces no quota) but never
        # negative, and 100x over request means the join matched wrong pairs.
        assert 0 <= value < 100, f"implausible used/requested ratio for {name}: {value}"


def test_demo_volume_is_growing(prometheus_ready):
    """The workload appends every 5s, so a 5-minute delta must be positive.

    A flat series usually means the exporter is caching, or the workload died.
    """
    growth = prom_query(
        'delta(openebs_hostpath_volume_used_bytes[5m]) > 0'
    )
    assert growth, (
        "no hostpath volume grew in the last 5 minutes; is the ledger workload "
        "still writing? kubectl -n storage-demo logs ledger-0"
    )


def test_alert_rules_are_loaded(prometheus_ready):
    resp = requests.get(f"{PROM_URL}/api/v1/rules", timeout=20)
    resp.raise_for_status()
    names = {
        rule["name"]
        for group in resp.json()["data"]["groups"]
        for rule in group["rules"]
        if rule["type"] == "alerting"
    }
    for expected in (
        "OpenEBSVolumeExceededRequest",
        "OpenEBSNodeBackingFilesystemFilling",
        "OpenEBSPVCPending",
    ):
        assert expected in names, f"alert {expected} not loaded; found {sorted(names)}"


# ------------------------------------------------------- application metrics
@pytest.fixture(scope="module")
def app_metrics_ready(prometheus_ready):
    """Wait for the instrumented workload to be scraped.

    Counters live in the process, so deleting the pod (which test_04 does)
    resets them to zero and leaves a gap until the new pod is discovered and
    scraped at the 15s interval.
    """
    wait_until(
        lambda: prom_query("ledger_writes_total"),
        timeout=180,
        interval=5,
        desc="ledger application metrics to be scraped",
    )
    return True


def test_application_publishes_its_own_metrics(app_metrics_ready):
    """Distinct from cAdvisor/kube-state-metrics, which only observe the pod.

    These series exist only because the workload is instrumented.
    """
    for metric in (
        "ledger_writes_total",
        "ledger_bytes_written_total",
        "ledger_write_errors_total",
        "ledger_boots_total",
        "ledger_file_size_bytes",
        "ledger_write_duration_seconds_count",
    ):
        assert prom_query(metric), (
            f"application metric {metric} missing. Is the ledger ServiceMonitor "
            "adopted? kubectl -n storage-demo get servicemonitor ledger"
        )


def test_application_write_counter_is_advancing(app_metrics_ready):
    rate = prom_query("rate(ledger_writes_total[5m]) > 0")
    assert rate, (
        "the application reports no writes in the last 5 minutes; the writer "
        "thread has stalled. kubectl -n storage-demo logs ledger-0"
    )


def test_application_reports_no_write_errors(app_metrics_ready):
    errors = prom_query_scalar("sum(ledger_write_errors_total)")
    assert errors == 0, (
        f"{errors:.0f} write errors reported by the application. On hostpath the "
        "usual cause is the node's filesystem filling (ENOSPC)."
    )


def test_application_histogram_is_well_formed(app_metrics_ready):
    """Prometheus histogram buckets are cumulative, and nothing enforces it.

    A malformed histogram produces no error anywhere -- histogram_quantile()
    just returns wrong numbers. This caught a real bug here: buckets were being
    cumulated twice (once when recorded, once when rendered), which reported a
    p99 of 2.3s for writes whose true mean was 1.3ms.

    Two invariants, both needed. The +Inf check alone passed while the histogram
    was badly broken.
    """
    buckets = []
    for s in prom_query("ledger_write_duration_seconds_bucket"):
        le = s["metric"]["le"]
        edge = float("inf") if le == "+Inf" else float(le)
        buckets.append((edge, le, float(s["value"][1])))
    buckets.sort()
    assert buckets, "no histogram buckets exposed"

    # 1. Counts must never decrease as the bucket boundary widens.
    previous = -1.0
    for _edge, le, value in buckets:
        assert value >= previous, (
            f"bucket le={le} ({value}) is lower than the preceding bucket "
            f"({previous}); buckets must be cumulative.\n"
            + "\n".join(f"  le={l:<8} {v:.0f}" for _e, l, v in buckets)
        )
        previous = value

    # 2. The +Inf bucket is by definition every observation.
    inf = buckets[-1][2]
    count = prom_query_scalar("ledger_write_duration_seconds_count")
    assert inf == count, f"+Inf bucket ({inf}) != _count ({count})"


def test_application_latency_quantile_is_plausible(app_metrics_ready):
    """p99 must be within the range the histogram can actually express, and in
    the same ballpark as the arithmetic mean derived from _sum/_count."""
    p99 = prom_query_scalar(
        "histogram_quantile(0.99, sum by (le) "
        "(rate(ledger_write_duration_seconds_bucket[5m])))"
    )
    mean = prom_query_scalar(
        "sum(rate(ledger_write_duration_seconds_sum[5m])) / "
        "sum(rate(ledger_write_duration_seconds_count[5m]))"
    )
    assert p99 == p99, "p99 is NaN -- the histogram is malformed"
    assert 0 < p99 < 2.5, f"p99 of {p99:.3f}s is outside the bucket range"
    assert p99 < mean * 500, (
        f"p99 ({p99:.3f}s) is wildly detached from the mean ({mean:.5f}s), "
        "which is what double-cumulated buckets look like"
    )


def test_application_boot_metric_matches_persistence_claim(app_metrics_ready):
    """The persistence proof, expressed as a metric: more than one boot on the
    same volume means the volume outlived at least one pod."""
    boots = prom_query_scalar("max(ledger_boots_total)")
    assert boots >= 1, "application reports no recorded boots"


def test_prometheus_is_storing_on_openebs(core):
    """Prometheus' own TSDB is a real OpenEBS consumer, which is the point."""
    pvc = core.read_namespaced_persistent_volume_claim(
        "prometheus-kps-prometheus-db-prometheus-kps-prometheus-0", "monitoring"
    )
    assert pvc.status.phase == "Bound"
    assert pvc.spec.storage_class_name == "openebs-hostpath"
