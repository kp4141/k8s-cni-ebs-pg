# k8s-cni-prometheus lab driver.
#
# Targets are numbered in dependency order. `make all` runs the whole lab from
# an empty machine to validated dashboards.

SHELL       := /bin/bash
CLUSTER     := k8s-cni-lab
KCTX        := kind-$(CLUSTER)
VENV        := .venv
PY          := $(VENV)/bin/python
PYTEST      := $(VENV)/bin/pytest

export KCTX

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: all
all: preflight venv cluster cni openebs monitoring workload validate ## Run the entire lab end to end

.PHONY: preflight
preflight: ## Check host tooling and the container runtime
	@scripts/00-preflight.sh

.PHONY: venv
venv: $(VENV)/.installed ## Create the Python virtualenv and install deps

$(VENV)/.installed: requirements.txt
	@command -v python3.12 >/dev/null 2>&1 \
	  && PY312=python3.12 \
	  || PY312=/opt/homebrew/bin/python3.12; \
	  $$PY312 -m venv $(VENV)
	@$(VENV)/bin/pip install --quiet --upgrade pip
	@$(VENV)/bin/pip install --quiet -r requirements.txt
	@touch $@
	@echo "venv ready: $$($(PY) --version)"

.PHONY: cluster
cluster: ## Create the 3-node kind cluster (no CNI yet, nodes stay NotReady)
	@scripts/10-cluster-up.sh

.PHONY: cni
cni: ## Install Calico and wait for nodes to go Ready
	@scripts/20-install-cni.sh

.PHONY: openebs
openebs: ## Install OpenEBS LocalPV Hostpath and its StorageClass
	@scripts/30-install-openebs.sh

.PHONY: monitoring
monitoring: ## Install kube-prometheus-stack backed by OpenEBS storage
	@scripts/40-install-monitoring.sh

.PHONY: workload
workload: ## Deploy the persistence demo workload onto an OpenEBS PVC
	@scripts/50-deploy-workload.sh

.PHONY: validate
validate: venv ## Run the full pytest validation suite
	@$(PYTEST) validation -v

.PHONY: validate-net
validate-net: venv ## Run only the networking validations
	@$(PYTEST) validation/test_02_networking.py -v

.PHONY: validate-storage
validate-storage: venv ## Run only the storage validations
	@$(PYTEST) validation/test_03_storage.py validation/test_04_workload.py -v

.PHONY: validate-metrics
validate-metrics: venv ## Run only the Prometheus and Grafana validations
	@$(PYTEST) validation/test_05_prometheus.py validation/test_06_grafana.py -v

.PHONY: urls
urls: ## Print the Prometheus and Grafana access URLs and credentials
	@scripts/urls.sh

.PHONY: teardown
teardown: ## Delete the kind cluster (leaves Colima and host tools alone)
	@scripts/99-teardown.sh

.PHONY: clean
clean: teardown ## Delete the cluster and the virtualenv
	@rm -rf $(VENV) artifacts
	@echo "removed venv and artifacts"
