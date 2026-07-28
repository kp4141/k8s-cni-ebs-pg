#!/usr/bin/env bash
# Deploy the persistence demo workload onto an OpenEBS volume.

source "$(dirname "$0")/lib.sh"
require_cluster

step "Deploying the ledger StatefulSet"
kc apply -f manifests/workload/ledger-statefulset.yaml

step "Waiting for the pod to be Ready"
retry 60 5 "ledger-0 to be ready" \
  kc -n "$NS_DEMO" wait --for=condition=Ready pod/ledger-0 --timeout=10s

step "PVC and PV"
kc -n "$NS_DEMO" get pvc
echo
kc get pv -o custom-columns=\
'NAME:.metadata.name,CAPACITY:.spec.capacity.storage,CLAIM:.spec.claimRef.name,SC:.spec.storageClassName,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]' \
  | grep -E 'NAME|ledger' || true

step "Where the data physically lives"
# LocalPV Hostpath is a directory on one specific node -- that node affinity is
# why the PV is not portable, and why the pod can only ever reschedule back to
# the same node.
node=$(kc -n "$NS_DEMO" get pod ledger-0 -o jsonpath='{.spec.nodeName}')
pvname=$(kc -n "$NS_DEMO" get pvc data-ledger-0 -o jsonpath='{.spec.volumeName}')
ok "pod on node: ${node}"
ok "backing dir : /var/openebs/local/${pvname} (on ${node})"
docker exec "$node" ls -la "/var/openebs/local/${pvname}" 2>/dev/null \
  || warn "could not list the host directory (is the docker CLI pointed at Colima?)"

step "Boot history so far"
kc -n "$NS_DEMO" logs ledger-0 | head -20

cat <<EOF

${C_GRN}Workload running.${C_RST}
Prove persistence by hand:
  kubectl --context ${KCTX} -n ${NS_DEMO} delete pod ledger-0
  kubectl --context ${KCTX} -n ${NS_DEMO} logs ledger-0 | head
The boot history should contain the earlier boot, not start over.

Or run the automated check: make validate-storage
EOF
