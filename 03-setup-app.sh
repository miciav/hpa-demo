#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="default"

echo "=== Deploying php-apache ==="
kubectl apply -f k8s/php-apache-deployment.yaml --namespace "$NAMESPACE"

echo "=== Deploying HPA ==="
kubectl apply -f k8s/hpa.yaml --namespace "$NAMESPACE"

echo "=== Waiting for php-apache pod to be Ready ==="
kubectl wait --for=condition=ready pod -l run=php-apache --namespace "$NAMESPACE" --timeout=120s 2>/dev/null || echo "php-apache pod already running or timed out, continuing..."

echo ""
echo "=== Deployment Summary ==="
echo "php-apache: $(kubectl get pods -l run=php-apache --namespace "$NAMESPACE" -o name 2>/dev/null || echo 'not found')"
echo "Service:    $(kubectl get service php-apache --namespace "$NAMESPACE" -o name 2>/dev/null || echo 'not found')"
echo "HPA:        $(kubectl get hpa php-apache --namespace "$NAMESPACE" -o name 2>/dev/null || echo 'not found')"
echo "=== Done ==="
