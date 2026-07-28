#!/usr/bin/env bash
# Print access details and check that each endpoint actually answers.

source "$(dirname "$0")/lib.sh"

probe() {
  local name="$1" url="$2" extra="${3:-}"
  if curl -s -m 5 -o /dev/null -w '%{http_code}' "$url" | grep -qE '^(200|30[0-9])$'; then
    ok "$(printf '%-11s %-32s %s' "$name" "$url" "$extra")"
  else
    warn "$(printf '%-11s %-32s UNREACHABLE' "$name" "$url")"
  fi
}

step "Lab endpoints"
probe "Prometheus" "http://localhost:30090" ""
probe "Grafana"    "http://localhost:30030" "admin / admin"
probe "Alertmgr"   "http://localhost:30090/-/ready" ""

cat <<EOF

If a NodePort is unreachable (Colima port forwarding can drop after a VM
restart), fall back to a port-forward:

  kubectl --context ${KCTX} -n ${NS_MONITORING} port-forward svc/kps-prometheus 30090:9090
  kubectl --context ${KCTX} -n ${NS_MONITORING} port-forward svc/kps-grafana    30030:80

The OpenEBS dashboard: http://localhost:30030/d/openebs-storage
EOF
