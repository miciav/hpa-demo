# HPA Demo — Design

Laboratorio didattico su HorizontalPodAutoscaler + Minikube per studenti magistrali (Kubernetes Lab).

## Obiettivo

Mostrare l'autoscaling basato su CPU con il built-in HPA di Kubernetes: un carico HTTP satura la CPU del pod php-apache, l'HPA rileva l'utilizzo e scala da 1 a N repliche. Quando il carico cessa, scala giù dopo il cooldown.

## Architettura

```
Minikube
├── php-apache (deployment, 1→N repliche) — serve richieste HTTP, CPU-intensive
├── HPA — target 50% CPU media, min 1, max 10
├── metrics-server — raccoglie metriche CPU dai pod
└── load-generator (pod effimero) — busybox con wget in loop

Locale (fuori cluster)
└── dashboard.py — TUI con Rich: CPU, pod, HPA, load control
```

## Componenti

| File | Ruolo | Note |
|---|---|---|
| `01-setup-minikube.sh` | Avvia Minikube se spento | Skip se già Running |
| `02-setup-metrics-server.sh` | Abilita metrics-server addon | Necessario per le metriche CPU |
| `03-setup-app.sh` | Deploya php-apache + HPA | Idempotente (`kubectl apply`) |
| `04-cleanup.sh` | Rimuove tutte le risorse | HPA, deployment, service, load-generator |
| `dashboard.py` | TUI real-time | Auto-installa `rich` se manca |
| `k8s/php-apache-deployment.yaml` | Deployment + Service php-apache | `requests.cpu: 200m`, `limits.cpu: 500m` |
| `k8s/hpa.yaml` | HPA autoscaling/v2 | Target 50% CPU, min 1, max 10 |

## TUI Layout

```
┌──────────────────────────────────────────────────────────┐
│  ● connected  cpu=45%  pods=3/10  load=on  ns=default    │
├──────────────────────┬───────────────────────────────────┤
│  CPU Utilization     │  Pods (3)                         │
│  [████████░░░░] 45%  │  Name          CPU    Ready  Age  │
│  Target: 50%         │  php-apache-xx 45m    1/1    2m   │
│                      │  php-apache-yy 52m    1/1    1m   │
│  HPA: 3 → 5          │  php-apache-zz 38m    1/1    30s  │
│  Min: 1  Max: 10     │                                   │
│                      │                                   │
│  1 start load        │                                   │
│  2 stop load         │                                   │
│  q quit              │                                   │
├──────────────────────┴───────────────────────────────────┤
│  Activity (last 6)                                       │
│  [21:30:45] Load generator started                       │
│  [21:30:44] Scaling up: 1 → 3 pods                       │
│  [21:30:30] Dashboard started                            │
└──────────────────────────────────────────────────────────┘
```

- **Refresh**: 1s via `rich.live.Live`
- **Metriche**: `kubectl get hpa`, `kubectl top pods`, `kubectl get pods`
- **Input**: tasti 1/2/q mappati a funzioni
- **Dipendenze**: solo `rich` (auto-install)

## HPA

```yaml
apiVersion: autoscaling/v2
metrics:
- type: Resource
  resource:
    name: cpu
    target:
      type: Utilization
      averageUtilization: 50
```

Formula: `desiredReplicas = ceil(currentReplicas * (currentCPU% / targetCPU%))`. Con CPU al 250% e target 50%: `ceil(1 * 5) = 5` pod.
