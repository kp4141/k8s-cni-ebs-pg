# Kubernetes Assignment Validation Results

This document contains validation evidence collected from my local Kubernetes environment.

## Environment

- Platform: macOS
- Container runtime: Colima/Docker
- Kubernetes distribution: Kind
- CNI: Calico
- Storage: OpenEBS
- Monitoring: Prometheus, Grafana and Alertmanager

## Kind Cluster Creation

I created a three-node Kind cluster containing one control-plane node and two worker nodes.

The default CNI was intentionally disabled. As expected, all nodes initially reported NotReady and the node condition included NetworkPluginNotReady.

The controller-manager, scheduler, kube-proxy and etcd metrics ports were verified inside the control-plane container and were listening beyond loopback so that Prometheus could scrape them later.

## Calico CNI Validation

I installed the Calico CRDs and Tigera Operator, then applied an Installation custom resource using the same 10.244.0.0/16 Pod CIDR configured in Kind.

The calico-node DaemonSet successfully deployed on all three nodes. All nodes changed from NotReady to Ready, and CoreDNS became available.

The Calico IPPool reported the expected CIDR and CrossSubnet VXLAN mode.

## Networking, DNS and NetworkPolicy Validation

The two NGINX replicas were scheduled on different nodes through required Pod anti-affinity.

The client Pod successfully resolved the Kubernetes Service DNS name and directly reached a web Pod running on another node, validating CoreDNS and the Calico cross-node data path.

The web Service also responded through its fully qualified DNS name, validating DNS, Service endpoints, kube-proxy forwarding and Calico networking.

After applying default-deny ingress, the request timed out. After applying the allow-client policy, the authorized client Pod could access TCP port 80 again.

## OpenEBS Storage Validation

I installed only the OpenEBS LocalPV Hostpath engine because the Kind environment does not provide the raw storage devices required by LVM, ZFS or Mayastor.

I made openebs-hostpath the only default StorageClass.

A PVC initially remained Pending because the StorageClass uses WaitForFirstConsumer. After deploying a consuming Pod, the PVC became Bound, the Pod wrote data to the mounted volume, and the generated PV showed node affinity for the node containing the Hostpath directory.

## Monitoring and Stateful Workload Validation

I installed kube-prometheus-stack with OpenEBS-backed persistence for Prometheus, Grafana and Alertmanager.

The Prometheus readiness endpoint returned successfully, Grafana reported a healthy database, and the expected Kubernetes targets were reviewed for healthy scrape status.

I deployed the OpenEBS Hostpath exporter and verified the custom per-volume usage metric.

The ledger workload was deployed as a StatefulSet. After deleting ledger-0, Kubernetes recreated the Pod with the same stable identity and reattached its PVC. The previous boot history remained available, proving that the data outlived the Pod.

## Final Result

The complete assignment was deployed and validated on my local macOS environment.

The final environment included:

- One Kind control-plane node and two Kind worker nodes
- Calico Pod networking and NetworkPolicy enforcement
- Kubernetes DNS and Service discovery
- Direct cross-node Pod communication
- OpenEBS LocalPV Hostpath dynamic storage
- WaitForFirstConsumer and PV node-affinity validation
- Prometheus, Grafana and Alertmanager with persistent storage
- Kubernetes control-plane and node monitoring
- OpenEBS per-volume usage monitoring
- A persistent ledger StatefulSet
- Ledger application-specific Prometheus metrics
- Repository validation test results

The detailed terminal outputs are stored under docs/evidence, and the main visual evidence is stored under docs/screenshots.
