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
