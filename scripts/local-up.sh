#!/usr/bin/env bash
# Bring the whole system up on a local k3d cluster (Linux, macOS, WSL, Git Bash).
#
# Creates the cluster if needed, builds the four images, loads them into the
# cluster, and installs the two Helm releases with the local values files.
# Safe to run again: every step is an upgrade-or-skip.
#
# The chat agent needs an LLM API key. Put GROQ_API_KEY=... in a .env file at
# the repository root (it is git-ignored) or export it before running this.
# Without it everything else still works.
#
#   ./scripts/local-up.sh
#   SKIP_BUILD=1 ./scripts/local-up.sh     # reuse the images already built
set -euo pipefail

CLUSTER="${CLUSTER:-urumi}"
TAG="${TAG:-0.1.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf '\n==> %s\n' "$1"; }

for tool in docker kubectl helm k3d; do
  command -v "$tool" >/dev/null || { echo "$tool is not on PATH. See README.md, 'Prerequisites'." >&2; exit 1; }
done

# --- LLM key: from the environment, or from .env ---
LLM_KEY="${GROQ_API_KEY:-${LLM_API_KEY:-}}"
if [ -z "$LLM_KEY" ] && [ -f .env ]; then
  LLM_KEY="$(grep -E '^[[:space:]]*(GROQ_API_KEY|LLM_API_KEY)[[:space:]]*=' .env | tail -n 1 | cut -d= -f2- | tr -d '"'"'"' \r' || true)"
fi

# --- cluster ---
step "Cluster"
if k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  echo "Cluster '$CLUSTER' already exists."
else
  k3d cluster create --config deploy/k3d-cluster.yaml
fi
kubectl config use-context "k3d-$CLUSTER"

# --- images ---
IMAGES=(
  "urumi/woo-mcp:services/woo-mcp"
  "urumi/gateway:services/gateway"
  "urumi/demo-mcp:services/demo-mcp"
  "urumi/dashboard:dashboard"
)
REFS=()
for entry in "${IMAGES[@]}"; do REFS+=("${entry%%:*}:$TAG"); done

if [ -z "${SKIP_BUILD:-}" ]; then
  step "Build images"
  for entry in "${IMAGES[@]}"; do
    echo "building ${entry%%:*}:$TAG"
    docker build -q -t "${entry%%:*}:$TAG" "${entry#*:}"
  done
fi
step "Load images into the cluster"
k3d image import "${REFS[@]}" -c "$CLUSTER"

# --- store ---
step "Install the store (WordPress + WooCommerce + MCP server)"
helm upgrade --install store charts/store --namespace store --create-namespace -f charts/store/values-local.yaml
echo "Waiting for the store. The first start installs WordPress and WooCommerce and takes a few minutes."
kubectl -n store rollout status statefulset/store-mariadb --timeout=300s
kubectl -n store rollout status deployment/store-wordpress --timeout=900s
kubectl -n store rollout status deployment/store-woo-mcp --timeout=180s

# --- gateway ---
# Installed after the store on purpose: the chart reads the MCP server's token
# from the store's Secret so the gateway can register it automatically.
step "Install the gateway (backend + dashboard + database + demo MCP servers)"
HELM_ARGS=(upgrade --install gateway charts/gateway --namespace gateway --create-namespace -f charts/gateway/values-local.yaml)
if [ -n "$LLM_KEY" ]; then HELM_ARGS+=(--set "llm.apiKey=$LLM_KEY"); fi
helm "${HELM_ARGS[@]}"
kubectl -n gateway rollout status statefulset/gateway-postgres --timeout=300s
kubectl -n gateway rollout status deployment/gateway --timeout=300s
kubectl -n gateway rollout status deployment/gateway-dashboard --timeout=180s

if [ -z "${SKIP_BUILD:-}" ]; then
  # Image tags do not change between local builds, so restart the pods that
  # run locally built images to pick up what was just loaded.
  step "Restart workloads to pick up rebuilt images"
  kubectl -n store rollout restart deployment/store-woo-mcp
  kubectl -n gateway rollout restart deployment/gateway deployment/gateway-dashboard deployment/gateway-fx deployment/gateway-shipping
  kubectl -n store rollout status deployment/store-woo-mcp --timeout=180s
  kubectl -n gateway rollout status deployment/gateway --timeout=300s
  kubectl -n gateway rollout status deployment/gateway-dashboard --timeout=180s
fi

secret() { kubectl -n "$1" get secret "$2" -o "jsonpath={.data.$3}" | base64 -d; }

step "Ready"
echo "Storefront      http://store.localtest.me"
echo "Store admin     http://store.localtest.me/wp-admin    admin / $(secret store store-wp-admin password)"
echo "Dashboard       http://gateway.localtest.me           admin@example.com / $(secret gateway gateway-app admin-password)"
echo "MCP endpoint    http://gateway.localtest.me/mcp"
if [ -z "$LLM_KEY" ]; then
  printf '\nNo LLM API key was found, so the chat agent is off. Add GROQ_API_KEY=... to .env and run this script again.\n'
fi
