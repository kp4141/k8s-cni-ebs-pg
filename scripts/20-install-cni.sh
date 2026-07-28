#!/usr/bin/env bash
# Install Calico as the cluster CNI.
#
# Two phases, and the split matters:
#   1. the Tigera operator -- a controller that knows how to build Calico
#   2. the Installation CR  -- our declaration of what Calico should look like
# The operator does nothing until phase 2 is applied.

source "$(dirname "$0")/lib.sh"
require_cluster

CALICO_MANIFESTS="https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests"

step "Installing Calico operator CRDs"
# As of Calico v3.30 the CRDs were split out of tigera-operator.yaml into their
# own manifest. Applying only tigera-operator.yaml -- which is what every
# pre-3.30 guide tells you to do -- leaves the operator running but the
# Installation CR unappliable:
#   no matches for kind "Installation" in version "operator.tigera.io/v1"
# See docs/08 §"no matches for kind Installation".
#
# --server-side because these CRDs blow past the 262144-byte ceiling on the
# last-applied-configuration annotation that client-side apply writes.
kc apply --server-side --force-conflicts -f "${CALICO_MANIFESTS}/operator-crds.yaml"

retry 30 2 "Installation CRD to register" \
  kc get crd installations.operator.tigera.io

step "Installing Tigera operator ${CALICO_VERSION}"
kc apply --server-side --force-conflicts -f "${CALICO_MANIFESTS}/tigera-operator.yaml"

step "Waiting for the operator to be ready"
retry 60 5 "tigera-operator deployment" \
  kc -n "$NS_CALICO" rollout status deploy/tigera-operator --timeout=10s

step "Applying the Calico Installation (pod CIDR 10.244.0.0/16)"
kc apply -f cluster/calico-installation.yaml

step "Waiting for calico-system to converge"
# The operator creates the namespace itself, so wait for it before querying.
retry 60 5 "calico-system namespace" kc get ns "$NS_CALICO_SYS"
retry 60 5 "calico-node daemonset" \
  kc -n "$NS_CALICO_SYS" rollout status ds/calico-node --timeout=10s
retry 60 5 "calico-kube-controllers" \
  kc -n "$NS_CALICO_SYS" rollout status deploy/calico-kube-controllers --timeout=10s

step "Waiting for all nodes to report Ready"
retry 60 5 "all 3 nodes Ready" bash -c \
  'test "$(kubectl --context '"$KCTX"' get nodes --no-headers | grep -c " Ready ")" -eq 3'

step "Waiting for CoreDNS (it was Pending until pods had a network)"
retry 60 5 "coredns rollout" \
  kc -n kube-system rollout status deploy/coredns --timeout=10s

step "Calico is up"
kc get nodes -o wide
echo
kc -n "$NS_CALICO_SYS" get pods -o wide
echo
step "IP pool in use"
kc get ippools.crd.projectcalico.org -o custom-columns=\
'NAME:.metadata.name,CIDR:.spec.cidr,ENCAP:.spec.vxlanMode,NAT:.spec.natOutgoing' 2>/dev/null \
  || warn "ippools CRD not queryable yet (the Calico APIServer may still be starting)"

cat <<EOF

${C_GRN}Nodes are Ready and pods have a network.${C_RST}
Next: make openebs   (or: make validate-net to test networking now)
EOF
