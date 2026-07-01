# HPA Demo — Implementation Plan

## Architecture

```
Minikube cluster
├── php-apache (deployment, 1→N) — CPU-intensive HTTP server, scaled by HPA
├── HPA — autoscaling/v2, CPU 50%, min 1, max 10
├── metrics-server — CPU metrics collection
└── load-generator — busybox pod with wget loop

Local
└── dashboard.py — Rich TUI: CPU bar, pod table, HPA info, log, load control
```

## File Structure

```
hpa-demo/
├── 01-setup-minikube.sh
├── 02-setup-metrics-server.sh
├── 03-setup-app.sh
├── 04-cleanup.sh
├── dashboard.py
└── k8s/
    ├── php-apache-deployment.yaml
    └── hpa.yaml
```

## Global Constraints

- Shell scripts: Bash 3.x+ compatible, `set -euo pipefail`, must handle already-running state gracefully
- All kubectl commands use `--namespace default` explicitly
- Python: 3.9+ compatible, single dependency `rich` (auto-install if missing)
- HPA: `autoscaling/v2`, CPU target 50% Utilization, min 1, max 10
- php-apache: image `registry.k8s.io/hpa-example`, requests 200m, limits 500m
- TUI: refresh 1s, keyboard shortcuts 1/2/q, layout left (CPU + HPA info) / right (pod table) + activity log

## Task 1: Setup Minikube script

Create `01-setup-minikube.sh`.

- Check `minikube status`, if "Running" print message and exit 0
- If not running: `minikube start --cpus=2 --memory=4096`
- Wait for node Ready, timeout 120s
- Exit 0 on success

## Task 2: Setup Metrics Server script

Create `02-setup-metrics-server.sh`.

- Check if metrics-server already enabled via `minikube addons list | grep metrics-server | grep enabled`
- If enabled, print message and exit 0
- `minikube addons enable metrics-server`
- Wait for metrics-server pod Ready in kube-system (`kubectl wait -n kube-system --for=condition=ready pod -l k8s-app=metrics-server --timeout=120s`)
- Exit 0 on success

## Task 3: App manifests and setup script

Create `k8s/php-apache-deployment.yaml`:
- Deployment, 1 replica, image `registry.k8s.io/hpa-example`, port 80
- Resources: requests.cpu=200m, limits.cpu=500m
- Service on port 80, selector `run: php-apache`

Create `k8s/hpa.yaml`:
- `autoscaling/v2`, target Deployment `php-apache`
- CPU averageUtilization 50, min 1, max 10

Create `03-setup-app.sh`:
- `kubectl apply -f k8s/php-apache-deployment.yaml`
- `kubectl apply -f k8s/hpa.yaml`
- Wait for php-apache pod Ready
- Print summary

## Task 4: Cleanup script

Create `04-cleanup.sh`:
- Delete HPA, deployment, service
- Delete load-generator pod if exists

## Task 5: TUI Dashboard

Create `dashboard.py`:
- Auto-install `rich` if missing
- CONFIG: namespace, deploy label, HPA name, target CPU, interval, etc.
- Data: `kubectl get hpa -o json`, `kubectl top pods`, `kubectl get pods`
- Actions: start/stop load generator via kubectl run/delete
- Layout: header (connection dot, CPU%, pods, load status), left (CPU bar + HPA info), right (pod table), bottom (log)
- Keyboard: 1=start load, 2=stop load, q=quit
- Robust error handling: kubectl failures surfaced in UI, not swallowed
- Color-coded log: green=info, yellow=scale, magenta=load actions, red=errors
