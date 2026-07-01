# HPA Demo — CPU-based Autoscaler

Teaching lab on HorizontalPodAutoscaler + Minikube for the Kubernetes Lab course (graduate students).

Demonstrates CPU-based autoscaling with the built-in Kubernetes HPA: HTTP load saturates the php-apache pod CPU, the HPA detects the spike and scales from 1 to N replicas. When load stops, it scales back down after the cooldown period.

## Architecture

```
Minikube
├── php-apache (deployment, 1→N replicas) — CPU-intensive HTTP server
├── HPA — target 50% CPU, min 1, max 10
├── metrics-server — collects CPU metrics
└── load-generator — busybox pod with wget loop

Local (outside cluster)
└── dashboard.py — Rich-based TUI: CPU, pods, HPA, load control
```

## Usage

```bash
./01-setup-minikube.sh         # start Minikube
./02-setup-metrics-server.sh   # enable metrics-server addon
./03-setup-app.sh              # deploy php-apache + HPA
python3 dashboard.py           # launch the TUI
```

Keys: `1` start load, `2` stop load, `q` quit.

## Requirements

- Minikube (or any Kubernetes cluster with metrics-server)
- kubectl
- Python 3.9+ (single dependency: `rich`, auto-installed)

## References

- [Kubernetes HPA](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/)
- [HPA Walkthrough](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale-walkthrough/)
