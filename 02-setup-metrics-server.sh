#!/usr/bin/env bash
set -euo pipefail

# Check if metrics-server addon is already enabled
if minikube addons list 2>/dev/null | grep "metrics-server" | grep -q "enabled"; then
    echo "metrics-server addon is already enabled. Skipping."
    exit 0
fi

# Enable metrics-server addon
echo "Enabling metrics-server addon..."
minikube addons enable metrics-server

# Wait for metrics-server pod to be Ready
echo "Waiting for metrics-server pod..."
kubectl wait -n kube-system --for=condition=ready pod -l k8s-app=metrics-server --timeout=120s

echo "metrics-server is ready."
exit 0
