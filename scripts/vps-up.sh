#!/usr/bin/env bash
# Put the whole system on a fresh Ubuntu VPS (amd64 or arm64) with the production values.
#
#   git clone https://github.com/roybishal362/Urumi.git && cd Urumi
#   EMAIL=you@example.com GROQ_API_KEY=gsk_... ./scripts/vps-up.sh
#
# What it does, in order:
#   1. installs k3s (Kubernetes, with Traefik and local-path storage), Helm and Docker
#   2. opens ports 80/443 in the host firewall (Oracle's Ubuntu images block them)
#   3. builds the four images on this machine and loads them into k3s, so they
#      match its CPU (Oracle's free VMs are ARM) and no registry is needed
#   4. installs cert-manager and a Let's Encrypt issuer
#   5. installs the store, then the gateway, with values-prod.yaml
#
# Hostnames default to <name>.<public-ip>.nip.io, which resolve to this machine
# with no DNS setup and can get Let's Encrypt certificates. Set STORE_HOST and
# GATEWAY_HOST to use your own domain instead (point its A records here first).
#
# Safe to run again: every step installs-or-upgrades.
set -euo pipefail

: "${EMAIL:?Set EMAIL to your address, for certificate expiry notices}"
TAG="${TAG:-0.1.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf '\n==> %s\n' "$1"; }
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

step "k3s, Helm and Docker"
if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | $SUDO sh -s - --write-kubeconfig-mode 644
fi
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
if ! command -v helm >/dev/null; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | $SUDO bash
fi
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | $SUDO sh
fi
until kubectl get nodes >/dev/null 2>&1; do sleep 3; done
kubectl wait --for=condition=Ready node --all --timeout=300s

step "Host firewall"
# Oracle's Ubuntu images ship iptables rules that reject everything except SSH,
# including traffic k3s forwards to pods. Allow web traffic, and stop the
# FORWARD reject from breaking pod networking. (Other providers: no-op.)
if $SUDO iptables -C INPUT -j REJECT --reject-with icmp-host-prohibited 2>/dev/null; then
  for port in 80 443; do
    $SUDO iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null ||
      $SUDO iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT
  done
  $SUDO iptables -D FORWARD -j REJECT --reject-with icmp-host-prohibited 2>/dev/null || true
  command -v netfilter-persistent >/dev/null && $SUDO netfilter-persistent save || true
  echo "Opened 80/443. Also allow them in your cloud firewall (Oracle: the VCN security list)."
fi

step "Hostnames"
IP="$(curl -fsS https://api.ipify.org)"
DASHED="${IP//./-}"
STORE_HOST="${STORE_HOST:-store.$DASHED.nip.io}"
GATEWAY_HOST="${GATEWAY_HOST:-gateway.$DASHED.nip.io}"
echo "Public IP $IP -> https://$STORE_HOST and https://$GATEWAY_HOST"

step "Build images and load them into k3s"
IMAGES=(woo-mcp:services/woo-mcp gateway:services/gateway demo-mcp:services/demo-mcp dashboard:dashboard)
for entry in "${IMAGES[@]}"; do
  name="urumi/${entry%%:*}:$TAG"
  echo "building $name"
  $SUDO docker build -q -t "$name" "${entry#*:}"
  $SUDO docker save "$name" | $SUDO k3s ctr images import - >/dev/null
done

step "cert-manager and the Let's Encrypt issuer"
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml >/dev/null
for d in cert-manager cert-manager-webhook cert-manager-cainjector; do
  kubectl -n cert-manager rollout status "deploy/$d" --timeout=300s
done
# The webhook can take a few seconds more before it accepts issuers.
for _ in 1 2 3 4 5 6; do
  sed "s/you@example.com/$EMAIL/" deploy/cluster-issuer.yaml | kubectl apply -f - && break
  sleep 10
done

step "Store"
helm upgrade --install store charts/store -n store --create-namespace \
  -f charts/store/values-prod.yaml \
  --set store.host="$STORE_HOST" \
  --set store.adminEmail="$EMAIL" \
  --set wooMcp.image.repository=urumi/woo-mcp --set wooMcp.image.tag="$TAG"
kubectl -n store rollout status deployment/store-wordpress --timeout=900s
kubectl -n store rollout status deployment/store-woo-mcp --timeout=300s

step "Gateway"
kubectl create namespace gateway --dry-run=client -o yaml | kubectl apply -f - >/dev/null
LLM_ARGS=()
if [ -n "${GROQ_API_KEY:-}" ]; then
  # Created here rather than passed to Helm, so the key is not stored in the release history.
  kubectl -n gateway create secret generic llm-api-key --from-literal=api-key="$GROQ_API_KEY" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  LLM_ARGS=(--set llm.existingSecret=llm-api-key)
fi
helm upgrade --install gateway charts/gateway -n gateway \
  -f charts/gateway/values-prod.yaml \
  --set gateway.host="$GATEWAY_HOST" \
  --set gateway.adminEmail="$EMAIL" \
  --set gateway.image.repository=urumi/gateway --set gateway.image.tag="$TAG" \
  --set dashboard.image.repository=urumi/dashboard --set dashboard.image.tag="$TAG" \
  --set demoServers.image.repository=urumi/demo-mcp --set demoServers.image.tag="$TAG" \
  "${LLM_ARGS[@]}"
kubectl -n gateway rollout status deployment/gateway --timeout=300s
kubectl -n gateway rollout status deployment/gateway-dashboard --timeout=300s

step "Certificates"
# Let's Encrypt has to reach this machine on port 80 to issue them.
kubectl wait --for=condition=Ready certificate --all -n store --timeout=300s || echo "store certificate not ready yet: kubectl describe certificate -n store"
kubectl wait --for=condition=Ready certificate --all -n gateway --timeout=300s || echo "gateway certificate not ready yet: kubectl describe certificate -n gateway"

secret() { kubectl -n "$1" get secret "$2" -o "jsonpath={.data.$3}" | base64 -d; }
step "Ready"
echo "Storefront     https://$STORE_HOST"
echo "Store admin    https://$STORE_HOST/wp-admin     admin / $(secret store store-wp-admin password)"
echo "Dashboard      https://$GATEWAY_HOST            $EMAIL / $(secret gateway gateway-app admin-password)"
echo "MCP endpoint   https://$GATEWAY_HOST/mcp"
[ -n "${GROQ_API_KEY:-}" ] || echo "No GROQ_API_KEY given: the chat agent is off. Run again with GROQ_API_KEY=... to turn it on."
