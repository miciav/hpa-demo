#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="default"

echo "=== Removing HPA ==="
kubectl delete hpa php-apache --namespace "$NAMESPACE" --ignore-not-found

echo "=== Removing load-generator pod ==="
kubectl delete pod load-generator --namespace "$NAMESPACE" --ignore-not-found

echo "=== Removing php-apache ==="
kubectl delete deployment php-apache --namespace "$NAMESPACE" --ignore-not-found
kubectl delete service php-apache --namespace "$NAMESPACE" --ignore-not-found

echo "=== Cleanup done ==="
