#!/usr/bin/env bash
# Shared helpers. Sourced by every numbered script.

set -euo pipefail

CLUSTER="${CLUSTER:-k8s-cni-lab}"
KCTX="${KCTX:-kind-${CLUSTER}}"

# Namespaces
NS_CALICO="tigera-operator"
NS_CALICO_SYS="calico-system"
NS_OPENEBS="openebs"
NS_MONITORING="monitoring"
NS_DEMO="storage-demo"
NS_NETTEST="net-test"

# Pinned chart / component versions. Bump deliberately, not incidentally --
# these are the versions the docs and troubleshooting notes were written
# against.
CALICO_VERSION="${CALICO_VERSION:-v3.32.1}"
OPENEBS_CHART_VERSION="${OPENEBS_CHART_VERSION:-4.5.1}"
KPS_CHART_VERSION="${KPS_CHART_VERSION:-87.20.0}"

# Colours, suppressed when not attached to a terminal.
if [[ -t 1 ]]; then
  C_BLU=$'\033[1;34m'; C_GRN=$'\033[1;32m'; C_YEL=$'\033[1;33m'
  C_RED=$'\033[1;31m'; C_RST=$'\033[0m'
else
  C_BLU=""; C_GRN=""; C_YEL=""; C_RED=""; C_RST=""
fi

step() { echo -e "\n${C_BLU}==> $*${C_RST}"; }
ok()   { echo -e "${C_GRN}  ok${C_RST} $*"; }
warn() { echo -e "${C_YEL}  !!${C_RST} $*"; }
die()  { echo -e "${C_RED}  xx${C_RST} $*" >&2; exit 1; }

# Every kubectl call is context-pinned. Without this a stray
# `kubectl config use-context` elsewhere on the machine can silently point the
# lab at the wrong cluster.
kc() { kubectl --context "$KCTX" "$@"; }

# Wait until a command succeeds, or give up. Used instead of `sleep` guesses so
# failures report what was actually being waited on.
#   retry <attempts> <delay-seconds> <description> <command...>
retry() {
  local attempts="$1" delay="$2" desc="$3"; shift 3
  local i=1
  while (( i <= attempts )); do
    if "$@" >/dev/null 2>&1; then
      ok "$desc"
      return 0
    fi
    printf '  .. waiting for %s (%d/%d)\r' "$desc" "$i" "$attempts"
    sleep "$delay"
    (( i++ ))
  done
  echo
  die "timed out waiting for ${desc} after $(( attempts * delay ))s"
}

require_cluster() {
  kc cluster-info >/dev/null 2>&1 \
    || die "cluster '${KCTX}' unreachable. Run: make cluster"
}
