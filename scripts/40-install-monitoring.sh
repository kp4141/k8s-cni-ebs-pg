#!/usr/bin/env bash
# Install kube-prometheus-stack, backed by OpenEBS storage.

source "$(dirname "$0")/lib.sh"
require_cluster

# Installing this before a working StorageClass leaves Prometheus Pending on an
# unbindable PVC, which is a slow and confusing way to discover the ordering.
kc get sc openebs-hostpath >/dev/null 2>&1 \
  || die "openebs-hostpath StorageClass not found. Run: make openebs"

step "Adding the prometheus-community helm repo"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null
ok "repo ready"

step "Installing kube-prometheus-stack ${KPS_CHART_VERSION}"
# 15m: the CRDs are large and the first run pulls Prometheus, Grafana,
# Alertmanager, kube-state-metrics and node-exporter images.
helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  --kube-context "$KCTX" \
  --namespace "$NS_MONITORING" --create-namespace \
  --version "$KPS_CHART_VERSION" \
  --values manifests/monitoring/kube-prometheus-values.yaml \
  --wait --timeout 15m

step "Monitoring workloads"
kc -n "$NS_MONITORING" get pods

step "PVCs served by OpenEBS"
# Prometheus, Grafana and Alertmanager all landed on openebs-hostpath. If these
# are Bound, the storage layer is already proven by the monitoring stack itself.
kc -n "$NS_MONITORING" get pvc

step "Applying OpenEBS recording rules and alerts"
# Not a ServiceMonitor: the hostpath engine exposes no metrics endpoint. These
# rules synthesise per-volume OpenEBS series out of kubelet + kube-state-metrics
# data. See the header of the file for why.
kc apply -f manifests/monitoring/openebs-monitoring.yaml

step "Installing the hostpath usage exporter"
# Provides genuine per-volume usage. Without it the storage dashboard would have
# to rely on kubelet_volume_stats_*, which reports node filesystem figures for
# every hostpath volume. See docs/07.
kc apply -f manifests/monitoring/openebs-hostpath-exporter.yaml
retry 40 5 "hostpath exporter daemonset" \
  kc -n "$NS_MONITORING" rollout status ds/openebs-hostpath-exporter --timeout=10s

step "Importing dashboards"
kc apply -f manifests/monitoring/dashboards/
ok "Grafana's sidecar imports it within ~60s (watch: kubectl -n monitoring logs deploy/kps-grafana -c grafana-sc-dashboard)"

cat <<EOF

${C_GRN}Monitoring stack ready.${C_RST}
  Prometheus  http://localhost:30090
  Grafana     http://localhost:30030   (admin / admin)

Next: make workload
EOF
