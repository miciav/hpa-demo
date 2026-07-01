#!/usr/bin/env bash
set -euo pipefail

# Check if minikube is already running
if minikube status 2>/dev/null | grep -q "Running"; then
    echo "Minikube is already running. Skipping."
    exit 0
fi

# Start minikube
echo "Starting minikube..."
minikube start --cpus=2 --memory=4096

# Wait for node to be Ready (timeout: 120s)
echo "Waiting for node to be Ready..."
for i in $(seq 1 120); do
    if kubectl get nodes 2>/dev/null | grep -q "Ready"; then
        echo "Node is ready."
        exit 0
    fi
    sleep 1
done

echo "Timed out waiting for node to become Ready." >&2
exit 1
