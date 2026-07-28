#!/usr/bin/env bash
# Install OpenEBS with only the LocalPV Hostpath engine enabled.

source "$(dirname "$0")/lib.sh"
require_cluster

step "Adding the OpenEBS helm repo"
helm repo add openebs https://openebs.github.io/openebs >/dev/null 2>&1 || true
helm repo update openebs >/dev/null
ok "repo ready"

step "Installing openebs chart ${OPENEBS_CHART_VERSION}"
helm upgrade --install openebs openebs/openebs \
  --kube-context "$KCTX" \
  --namespace "$NS_OPENEBS" --create-namespace \
  --version "$OPENEBS_CHART_VERSION" \
  --values manifests/storage/openebs-values.yaml \
  --wait --timeout 10m

step "Provisioner status"
kc -n "$NS_OPENEBS" get pods

step "StorageClasses"
kc get sc

# The chart ships openebs-hostpath. Confirm it landed rather than assuming.
if kc get sc openebs-hostpath >/dev/null 2>&1; then
  ok "openebs-hostpath StorageClass present"
else
  die "openebs-hostpath StorageClass missing -- check: helm get values openebs -n ${NS_OPENEBS}"
fi

step "Making openebs-hostpath the cluster default"
# kind preinstalls rancher.io/local-path and marks it default. Two defaults is
# an error state, and a PVC with no storageClassName would otherwise land on
# local-path and quietly bypass OpenEBS entirely -- which would make the whole
# storage validation meaningless. Demote one, promote the other.
if kc get sc standard >/dev/null 2>&1; then
  kc patch sc standard -p \
    '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}' >/dev/null
  ok "demoted kind's 'standard' (rancher.io/local-path) from default"
fi
kc patch sc openebs-hostpath -p \
  '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' >/dev/null
ok "openebs-hostpath is now the default StorageClass"

echo
kc get sc
cat <<EOF

${C_GRN}OpenEBS ready.${C_RST}
Note: openebs-hostpath uses volumeBindingMode: WaitForFirstConsumer, so a new
PVC sits in Pending until a pod actually mounts it. That is correct behaviour,
not a fault -- see docs/04.

Next: make monitoring
EOF
