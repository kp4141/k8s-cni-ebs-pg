# 01 — Python environment

## What the virtualenv is for

The venv does not run Kubernetes. It runs the *validation* of Kubernetes.

The cluster is built by `kind`, `helm` and `kubectl`; those are Go binaries
installed by Homebrew. What the venv provides is a place to assert that the
result is correct — talking to the Kubernetes API, Prometheus and Grafana
directly, and failing loudly with a diagnosis when something is off.

That distinction matters because it decides where a check belongs. "Did helm
exit 0" is a shell concern. "Does every Grafana panel query actually return
samples" is a program, and it lives here.

## Creating it

```bash
make venv
```

which is:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

macOS ships Python 3.9, old enough that several dependencies no longer publish
wheels for it. Homebrew's `python@3.12` is used explicitly rather than whatever
`python3` resolves to.

The `Makefile` guards the build with a `.venv/.installed` stamp file, so `make
venv` is a no-op until `requirements.txt` changes.

## Dependencies

| Package | Why |
|---|---|
| `kubernetes` | Official API client. Also provides pod `exec`, which is how network tests run commands inside pods. |
| `pytest` | Test runner. Each assertion is one verifiable claim about the cluster. |
| `requests` | Prometheus and Grafana HTTP APIs. |
| `PyYAML` | Parsing helm chart metadata and manifests. |
| `tenacity`, `pytest-order` | Retry and ordering helpers. |

Resolved versions on this build: `kubernetes==33.1.0`, `pytest==8.4.2`.

## Harness design

[validation/conftest.py](../validation/conftest.py) holds the shared machinery.

**Everything is context-pinned.** Each client is bound to
`kind-k8s-cni-lab`. A `kubectl config use-context` elsewhere on the machine
cannot silently redirect the suite at a different cluster.

**`pod_exec` returns a real exit code.** The Kubernetes exec API delivers the
exit status out-of-band on a separate channel rather than as a return value, so
the helper parses it out of the status object:

```python
status = json.loads(resp.read_channel(ERROR_CHANNEL))
if status.get("status") != "Success":
    for cause in status.get("details", {}).get("causes", []):
        if cause.get("reason") == "ExitCode":
            rc = int(cause.get("message", 1))
```

Without that, a failed command inside a pod looks identical to a successful one,
and the NetworkPolicy tests — which depend entirely on distinguishing "connection
refused" from "connected" — would pass no matter what.

**`wait_until` instead of `sleep`.** Polls a predicate and reports *what* it was
waiting for on timeout. Fixed sleeps are either too short (flaky) or too long
(slow), and tell you nothing when they expire.

**Failures carry the next command.** When Prometheus is unreachable the test
prints the port-forward to run, not just a connection error.

## Running

```bash
make validate            # everything, 40 tests
make validate-net        # CNI only
make validate-storage    # OpenEBS and the workload
make validate-metrics    # Prometheus and Grafana
```

Direct invocation works too, and is better for iterating:

```bash
.venv/bin/pytest validation -v
.venv/bin/pytest validation/test_02_networking.py::test_networkpolicy_is_actually_enforced -v
```

Override endpoints with `KCTX`, `PROM_URL`, `GRAFANA_URL`, `GRAFANA_PASSWORD` if
you are port-forwarding instead of using NodePorts.

## Notes on the tests

They run against live infrastructure, which has two consequences.

*They are not hermetic.* `test_02_networking` creates a namespace, deployments
and a DaemonSet, then deletes them. `test_04_workload` deliberately deletes a
running pod. Do not point this suite at a cluster you care about.

*They are order-dependent by design.* Files are numbered so `pytest` collects
them in dependency order: no point testing volume metrics before a volume
exists. Individual files still run standalone — each re-applies the manifests it
needs.

Next: [02 — Kubernetes install](02-kubernetes-install.md)
