#!/usr/bin/env bash
# Shows that the NetworkPolicies do what the docs say, by trying connections
# that should work and connections that should not.
#
#   ./scripts/check_isolation.sh
#
# "open" means a TCP connection was established; "blocked" means it timed out.
set -u

STORE_NS="${STORE_NS:-store}"
GATEWAY_NS="${GATEWAY_NS:-gateway}"
failures=0

# try <namespace> <deploy-or-pod> <container> <host> <port>: prints open or blocked.
# The project's images have Python but no shell tools, so Python makes the connection.
try() {
  kubectl -n "$1" exec "$2" -c "$3" -- python -c "
import socket, sys
try:
    socket.create_connection(('$4', $5), timeout=4).close()
    print('open')
except OSError:
    print('blocked')
" 2>/dev/null
}

expect() {
  local want="$1" label="$2" got="$3"
  if [ "$got" = "$want" ]; then
    printf '  [PASS] %-62s %s\n' "$label" "$got"
  else
    printf '  [FAIL] %-62s %s (expected %s)\n' "$label" "${got:-no answer}" "$want"
    failures=$((failures + 1))
  fi
}

WOO="store-woo-mcp.$STORE_NS.svc.cluster.local"
WP="store-wordpress.$STORE_NS.svc.cluster.local"
DB="store-mariadb.$STORE_NS.svc.cluster.local"
PG="gateway-postgres.$GATEWAY_NS.svc.cluster.local"
FX="gateway-fx.$GATEWAY_NS.svc.cluster.local"

echo "The gateway reaches what it needs:"
expect open    "gateway -> WooCommerce MCP server"            "$(try "$GATEWAY_NS" deploy/gateway gateway "$WOO" 8000)"
expect open    "gateway -> its database"                      "$(try "$GATEWAY_NS" deploy/gateway gateway "$PG" 5432)"
expect open    "gateway -> demo MCP server (fx)"              "$(try "$GATEWAY_NS" deploy/gateway gateway "$FX" 8000)"
expect blocked "gateway -> cloud metadata address"            "$(try "$GATEWAY_NS" deploy/gateway gateway 169.254.169.254 80)"

echo "The WooCommerce MCP server reaches only its own WordPress:"
expect open    "woo-mcp -> WordPress"                         "$(try "$STORE_NS" deploy/store-woo-mcp woo-mcp "$WP" 80)"
expect blocked "woo-mcp -> the store's database"              "$(try "$STORE_NS" deploy/store-woo-mcp woo-mcp "$DB" 3306)"
expect blocked "woo-mcp -> the gateway's database"            "$(try "$STORE_NS" deploy/store-woo-mcp woo-mcp "$PG" 5432)"
expect blocked "woo-mcp -> the internet"                      "$(try "$STORE_NS" deploy/store-woo-mcp woo-mcp 1.1.1.1 443)"

echo "Only the gateway reaches upstream MCP servers:"
expect blocked "demo server (fx) -> WooCommerce MCP server"   "$(try "$GATEWAY_NS" deploy/gateway-fx mcp "$WOO" 8000)"
expect blocked "demo server (fx) -> the gateway's database"   "$(try "$GATEWAY_NS" deploy/gateway-fx mcp "$PG" 5432)"
expect blocked "demo server (fx) -> the internet"             "$(try "$GATEWAY_NS" deploy/gateway-fx mcp 1.1.1.1 443)"
expect blocked "demo server (fx) -> another demo server"      "$(try "$GATEWAY_NS" deploy/gateway-shipping mcp "$FX" 8000)"

echo "Nothing is published to the outside except the storefront and the dashboard:"
kubectl get ingress -A -o custom-columns='NAMESPACE:.metadata.namespace,HOST:.spec.rules[*].host,BACKENDS:.spec.rules[*].http.paths[*].backend.service.name' --no-headers | sed 's/^/  /'
exposed=""
for ns in "$STORE_NS" "$GATEWAY_NS"; do
  exposed="$exposed$(kubectl get svc -n "$ns" -o jsonpath='{range .items[?(@.spec.type!="ClusterIP")]}{.metadata.name}{" "}{end}' 2>/dev/null)"
done
expect "" "Services that are not ClusterIP (should be none)" "$exposed"

echo
if [ "$failures" -eq 0 ]; then echo "All isolation checks passed."; else echo "$failures isolation check(s) failed."; fi
exit "$failures"
