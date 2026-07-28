#!/usr/bin/env bash
# Delete the kind cluster.
#
# Scope is deliberately narrow: the cluster and everything in it. Colima and the
# Homebrew tools are left alone, because they are shared machine state this lab
# did not exclusively own.

source "$(dirname "$0")/lib.sh"

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  warn "cluster '${CLUSTER}' does not exist -- nothing to do"
  exit 0
fi

step "Deleting kind cluster '${CLUSTER}'"
# Deleting the nodes destroys their filesystems, and hostpath PV data lives on
# those filesystems. Everything written to an OpenEBS volume goes with them.
kind delete cluster --name "$CLUSTER"
ok "cluster deleted"

cat <<EOF

Still running / installed:
  Colima VM      colima stop        (or: colima delete, to reclaim the disk)
  Host tooling   brew uninstall colima docker kubectl kind helm python@3.12
  Virtualenv     rm -rf .venv

Rebuild the lab with: make all
EOF
