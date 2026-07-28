#!/usr/bin/env bash
# Verify the host has everything the lab needs before anything is created.
# Cheap to run, and it turns a confusing mid-install failure into a clear
# message up front.

source "$(dirname "$0")/lib.sh"

step "Checking host tooling"
missing=0
for tool in colima docker kubectl kind helm; do
  if command -v "$tool" >/dev/null 2>&1; then
    ok "$tool -> $(command -v "$tool")"
  else
    warn "$tool NOT FOUND"
    missing=1
  fi
done

if (( missing )); then
  die "install the missing tools first:
    brew install colima docker kubectl kind helm python@3.12"
fi

step "Checking Python 3.12"
if command -v python3.12 >/dev/null 2>&1; then
  ok "$(python3.12 --version)"
elif [[ -x /opt/homebrew/bin/python3.12 ]]; then
  ok "$(/opt/homebrew/bin/python3.12 --version) (via /opt/homebrew)"
else
  die "python3.12 not found. Run: brew install python@3.12"
fi

step "Checking container runtime"
if ! docker info >/dev/null 2>&1; then
  warn "docker daemon not reachable"
  die "start the VM first:
    colima start --cpu 4 --memory 8 --disk 60 --runtime docker"
fi

read -r server arch ncpu mem < <(
  docker info --format '{{.ServerVersion}} {{.Architecture}} {{.NCPU}} {{.MemTotal}}'
)
# Reported as GiB with a decimal: a VM started with `--memory 8` reports about
# 7.7 GiB once the hypervisor takes its share, and a bare truncated "7 GB" reads
# like the flag did not take effect.
mem_gib=$(awk -v b="$mem" 'BEGIN { printf "%.1f", b / 1073741824 }')
ok "docker ${server} (${arch}), ${ncpu} CPU, ${mem_gib} GiB"

# The full stack -- Calico, kube-prometheus-stack, OpenEBS and a 3-node
# cluster -- does not fit comfortably below this. Undersized VMs fail later as
# OOMKills and evictions, which are far harder to read than this warning.
if (( ncpu < 4 )); then
  warn "only ${ncpu} CPUs; 4+ recommended. Resize: colima stop && colima start --cpu 4"
fi
if (( mem < 7 * 1024 * 1024 * 1024 )); then
  warn "only ${mem_gib} GiB RAM; start the VM with --memory 8. Resize: colima stop && colima start --memory 8"
fi

step "Preflight complete"
ok "host is ready -- next: make cluster"
