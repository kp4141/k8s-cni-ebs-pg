#!/usr/bin/env bash
# Create the kind cluster.
#
# The cluster deliberately comes up broken: with disableDefaultCNI the nodes
# report NotReady and CoreDNS stays Pending until a CNI is installed. That is
# the expected state at the end of this script, not a failure.

source "$(dirname "$0")/lib.sh"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  warn "cluster '${CLUSTER}' already exists -- skipping create"
else
  step "Creating kind cluster '${CLUSTER}' (1 control-plane + 2 workers)"
  # No --wait: waiting for Ready would hang for the full timeout, because
  # nothing can go Ready until Calico is installed in the next step.
  kind create cluster --config cluster/kind-config.yaml
fi

step "Cluster reachability"
retry 30 2 "API server to respond" kc cluster-info

step "Node status (NotReady is expected at this point)"
kc get nodes -o wide

step "Confirming the CNI really is absent"
notready=$(kc get nodes --no-headers | grep -c 'NotReady' || true)
if (( notready > 0 )); then
  ok "${notready}/3 nodes NotReady -- no CNI installed, as intended"
else
  warn "all nodes already Ready; a CNI is present. Was disableDefaultCNI honoured?"
fi

echo
kc get pods -A
cat <<EOF

${C_YEL}Expected right now:${C_RST}
  - all nodes NotReady               (kubelet reports no network plugin)
  - coredns pods Pending             (no pod network to schedule onto)
  - kube-proxy / etcd / apiserver Running (hostNetwork, so unaffected)

Next: make cni
EOF
