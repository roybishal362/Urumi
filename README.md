# WooCommerce hosting with a team MCP gateway on Kubernetes

A WooCommerce store, an MCP server that exposes its products and orders, and a gateway that
puts that server and any others behind one MCP endpoint for a whole team. An admin connects
MCP servers once in a dashboard; every team member then uses all of them with a single URL and
their own API key, or through a chat agent in the dashboard.

Everything runs in Kubernetes and is installed with Helm. Local and production use the same
charts with different values files.

- **Design and tradeoffs:** [docs/DESIGN.md](docs/DESIGN.md)
- **Threat model:** [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)
- **Demo walkthrough:** [docs/DEMO.md](docs/DEMO.md)

## The four layers

| Layer | What it is | Where |
|---|---|---|
| Store | WordPress + WooCommerce + MariaDB, cash on delivery enabled | [charts/store](charts/store) |
| WooCommerce MCP server | Written for this project. Streamable HTTP, six tools, cluster-internal only | [services/woo-mcp](services/woo-mcp) |
| Gateway and dashboard | Backend that is an MCP client to every registered server and an MCP server to the team; React dashboard | [services/gateway](services/gateway), [dashboard](dashboard), [charts/gateway](charts/gateway) |
| Chat agent | A tool loop in the gateway that acts as the signed-in user | [services/gateway/gateway/chat.py](services/gateway/gateway/chat.py) |

Three upstream servers are connected out of the box: WooCommerce (bearer token), a currency
server (no credential) and a shipping server (custom header credential). The last two live in
[services/demo-mcp](services/demo-mcp) and both expose a tool called `get_rates`, to show how
same-named tools are handled.

## Local setup

### Prerequisites

Docker (Docker Desktop on Windows/macOS), `kubectl`, `helm` and `k3d`.

```
# Windows
winget install Helm.Helm k3d.k3d
# macOS
brew install helm k3d
```

About 6 GB of free memory for Docker and 6 GB of disk for images.

### One command

```
# Windows PowerShell
powershell -ExecutionPolicy Bypass -File scripts\local-up.ps1

# Linux, macOS, WSL, Git Bash
./scripts/local-up.sh
```

It creates a k3d cluster, builds four images, loads them into the cluster and installs both
charts. The first run takes 5 to 10 minutes, mostly image pulls and the WordPress and
WooCommerce install. It ends by printing the URLs and the generated admin passwords:

| | URL | Sign in |
|---|---|---|
| Storefront | http://store.localtest.me | |
| Store admin | http://store.localtest.me/wp-admin | `admin` / printed by the script |
| Dashboard | http://gateway.localtest.me | `admin@example.com` / printed by the script |
| MCP endpoint | http://gateway.localtest.me/mcp | an API key from the dashboard |

**Chat agent key.** The chat agent needs an LLM API key (a free [Groq](https://console.groq.com/keys)
key works). Copy `.env.example` to `.env`, set `GROQ_API_KEY`, and run the script again. It is
the only secret a person supplies; it goes into a Kubernetes Secret and nowhere else. Every
other secret is generated inside the cluster.

### The same thing by hand

The script is a convenience. These are the commands it runs:

```
k3d cluster create --config deploy/k3d-cluster.yaml

docker build -t urumi/woo-mcp:0.1.0   services/woo-mcp
docker build -t urumi/gateway:0.1.0   services/gateway
docker build -t urumi/demo-mcp:0.1.0  services/demo-mcp
docker build -t urumi/dashboard:0.1.0 dashboard
k3d image import urumi/woo-mcp:0.1.0 urumi/gateway:0.1.0 urumi/demo-mcp:0.1.0 urumi/dashboard:0.1.0 -c urumi

helm upgrade --install store charts/store -n store --create-namespace -f charts/store/values-local.yaml
kubectl -n store rollout status deployment/store-wordpress --timeout=900s

helm upgrade --install gateway charts/gateway -n gateway --create-namespace \
  -f charts/gateway/values-local.yaml --set llm.apiKey=$GROQ_API_KEY
```

Install the store first. The gateway chart reads the WooCommerce MCP server's token from the
store's Secret (Helm `lookup`) so it can register that server automatically. If the gateway
was installed before the store, it starts without WooCommerce; run the gateway's
`helm upgrade` again and `kubectl -n gateway rollout restart deploy/gateway`, or add the server
by hand on the MCP servers page.

### Local domains

`*.localtest.me` is a public DNS name that resolves to `127.0.0.1`, and k3d publishes the
cluster's ingress on host ports 80 and 443. So `store.localtest.me` and `gateway.localtest.me`
reach Traefik with no hosts-file edits. Without internet DNS, add this to your hosts file instead:

```
127.0.0.1 store.localtest.me gateway.localtest.me
```

### Tear down

```
k3d cluster delete urumi
```

## Using it

### Place an order

1. Open http://store.localtest.me/shop/, pick a product, **Add to cart**.
2. Go to **Checkout**, fill in an address, choose **Cash on delivery**, **Place order**.
3. See it in WooCommerce admin: http://store.localtest.me/wp-admin → WooCommerce → Orders.

### Connect an external MCP client

1. Sign in to the dashboard, open **API keys**, **Generate key**. The key is shown once, with
   ready-to-paste snippets.
2. Give the client the gateway URL and the key. Nothing else.

```
# Claude Code
claude mcp add --transport http urumi http://gateway.localtest.me/mcp --header "Authorization: Bearer urm_..."
```

```json
// Cursor: .cursor/mcp.json
{ "mcpServers": { "urumi": { "url": "http://gateway.localtest.me/mcp",
                             "headers": { "Authorization": "Bearer urm_..." } } } }
```

```
# MCP Inspector
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP. URL: http://gateway.localtest.me/mcp
# Header: Authorization = Bearer urm_...
```

The client lists tools from every enabled server, named `<server>__<tool>`: `woo__list_orders`,
`fx__convert`, `shipping__get_rates` and so on.

### Use the chat agent

Dashboard → **Chat**. Ask "What orders came in today?" or "Create a product called Monsoon
Umbrella priced at 24.99". Each tool call the agent makes is shown with its server, arguments,
duration and result. It acts as you: same tools, same audit trail.

Tested with Groq's free tier and `openai/gpt-oss-120b`: creating products, finding today's
orders, changing an order's status, follow-up questions, and one question answered with three
servers (`woo__list_orders`, `fx__convert`, `shipping__get_rates`). The free tier allows 8,000
tokens a minute and a multi-step question uses about 5,000, so it manages roughly one such
question a minute. Ask faster and the agent waits for the limit to clear (it says so in the
chat) instead of failing.

### Invite a team member

Dashboard → **Members** → enter an email → **Invite**. There is no mail server, so the
dashboard shows a one-time link for you to pass on. The member opens it, sets a password and
can then generate their own key.

## Walking the definition of done

With two members, A and B (invite both from the Members page):

| Step | How |
|---|---|
| Storefront checkout with COD | "Place an order" above |
| Order visible in WooCommerce admin | wp-admin → WooCommerce → Orders |
| A asks the agent to create a product | Chat, as A: `Create a product called "Monsoon Umbrella" priced at 24.99`. It appears at http://store.localtest.me/shop/ |
| A asks what orders came in today | Order it on the storefront, then in chat: `What orders came in today?` |
| B connects an external client | B's key + gateway URL in Claude Code, Cursor or Inspector; ask the same orders question |
| One prompt, two upstream servers | `Take today's order total from the store and convert it to INR` (uses `woo__list_orders` and `fx__convert`) |
| Revoke B's key | API keys → Revoke (as B, or as admin with "Show the whole team's keys"). B's next call gets HTTP 401 |
| Disable an upstream | MCP servers → Shipping → Disable. `shipping__*` tools are gone from chat's tool list at once and from the external client the next time it lists tools (in Claude Code: `/mcp` → reconnect) |
| Kill an upstream pod | `kubectl -n gateway delete pod -l app.kubernetes.io/component=mcp-fx`. The gateway checks each server every 2 seconds locally, so the dashboard shows Currency as Unreachable with the reason, and after Kubernetes replaces the pod, "Recovered N s ago after being unreachable for N s". For a longer outage use `kubectl -n gateway scale deploy/gateway-fx --replicas=0` (then `=1`). WooCommerce and Shipping keep working throughout. |
| Audit log | Dashboard → Audit log: who, via chat or MCP client and which key, server, tool, when, how long, outcome; plus calls, error rate and latency per server for the last 24 hours |

To see the timeout handling: call `fx__debug_sleep` with `seconds: 60`. It returns a timeout
error after 30 seconds while calls to other tools keep answering.

## Checking it works

```
# 27 checks covering the definition of done, against the running cluster.
# Behaves as an admin, two members, a shopper and an external MCP client.
python -m venv .venv && .venv/Scripts/pip install mcp httpx2      # .venv/bin/pip on Linux/macOS
.venv/Scripts/python scripts/smoke_test.py

# Tries the network paths that should be open and the ones that should be blocked.
./scripts/check_isolation.sh

# Checks each release from inside the cluster.
helm test store -n store --logs
helm test gateway -n gateway --logs

# Unit and end-to-end tests. The gateway tests start real MCP servers on localhost.
pip install -r services/gateway/requirements-dev.txt -r services/woo-mcp/requirements-dev.txt
cd services/gateway && python -m pytest       # 46 tests
cd services/woo-mcp && python -m pytest       # 10 tests
```

[CI](.github/workflows/ci.yml) runs the tests, the dashboard build and lint, Helm lint with
every values file, and the image builds on every push. Run it by hand ("Run workflow") to also
deploy everything to a throwaway k3d cluster and run the smoke test, the isolation checks and
`helm test` there.

Besides the smoke test's own client, the MCP endpoint was exercised with MCP Inspector's
command-line client, which is built on the official TypeScript SDK (the one Claude Code and
Cursor use): it lists all tools and calls them.

## Production-like setup on a VPS (k3s)

The same charts, with `values-prod.yaml`. What changes is listed in
[docs/DESIGN.md](docs/DESIGN.md#what-changes-for-production): hostnames, TLS, HTTP→HTTPS redirect,
storage class and size, image registry, replica counts, nightly backups.

> I have not run this on a real VPS. I did rehearse it: both charts installed with
> `values-prod.yaml` on the local cluster, behind real TLS from cert-manager, with the full smoke
> test passing over HTTPS (see "Production rehearsal" below). What the rehearsal cannot cover is
> Let's Encrypt itself and a public domain.

```bash
# 1. On the VPS: k3s (ships Traefik and the local-path storage class)
curl -sfL https://get.k3s.io | sh -
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# 2. DNS: point store.example.com and gateway.example.com at the VPS (A records).

# 3. TLS: cert-manager and a Let's Encrypt issuer
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
# edit the email address in deploy/cluster-issuer.yaml first
kubectl apply -f deploy/cluster-issuer.yaml

# 4. Images: push to a registry and set the repositories in the values files ...
for s in woo-mcp gateway demo-mcp; do
  docker build -t ghcr.io/YOU/urumi-$s:0.1.0 services/$s && docker push ghcr.io/YOU/urumi-$s:0.1.0
done
docker build -t ghcr.io/YOU/urumi-dashboard:0.1.0 dashboard && docker push ghcr.io/YOU/urumi-dashboard:0.1.0
# ... or, with no registry, build on the VPS and load straight into k3s:
#   docker save urumi/gateway:0.1.0 | sudo k3s ctr images import -
#   and pass --set gateway.image.repository=urumi/gateway (same for the other three)

# 5. The LLM key as a Secret created out of band, so it is not in shell history or Helm values
kubectl create namespace gateway
kubectl -n gateway create secret generic llm-api-key --from-file=api-key=./groq-key.txt

# 6. Install: store first, then gateway
helm upgrade --install store charts/store -n store --create-namespace \
  -f charts/store/values-prod.yaml --set store.host=store.example.com
kubectl -n store rollout status deployment/store-wordpress --timeout=900s

helm upgrade --install gateway charts/gateway -n gateway \
  -f charts/gateway/values-prod.yaml --set gateway.host=gateway.example.com \
  --set llm.existingSecret=llm-api-key

# 7. Passwords
kubectl -n gateway get secret gateway-app -o jsonpath='{.data.admin-password}' | base64 -d
kubectl -n store get secret store-wp-admin -o jsonpath='{.data.password}' | base64 -d
```

### Production rehearsal

Let's Encrypt cannot issue certificates for `*.localtest.me`, so the rehearsal uses cert-manager
with a local certificate authority. Everything else is `values-prod.yaml` unchanged, apart from a
small overlay for hostnames, the issuer name and locally built images
([deploy/prod-rehearsal](deploy/prod-rehearsal)).

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
kubectl apply -f deploy/prod-rehearsal/local-ca.yaml

helm upgrade --install store charts/store -n store-prod --create-namespace \
  -f charts/store/values-prod.yaml -f deploy/prod-rehearsal/store-values.yaml
helm upgrade --install gateway charts/gateway -n gateway-prod --create-namespace \
  -f charts/gateway/values-prod.yaml -f deploy/prod-rehearsal/gateway-values.yaml

# Trust the rehearsal CA for the smoke test, then run it over HTTPS
kubectl -n cert-manager get secret rehearsal-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > rehearsal-ca.crt
SSL_CERT_FILE=rehearsal-ca.crt python scripts/smoke_test.py --gateway https://gateway-prod.localtest.me \
  --store https://store-prod.localtest.me --gateway-namespace gateway-prod --admin-email owner@example.com
```

What it showed: certificates issued and verified; `http://` answered with a permanent redirect
to `https://`; WordPress serving only `https://` links (no mixed content); a browser checkout
over HTTPS; the session cookie marked `Secure`; two gateway replicas starting cleanly on an empty
database; the nightly backup jobs producing real dumps; all 27 smoke checks passing. It also
caught two bugs that only appear with production settings (replicas racing to create tables on
first start, and a backup job that reported success after a failed dump); both are fixed.

### Upgrade and rollback

```bash
# Gateway: new image tag, rolling update, readiness-gated
helm upgrade gateway charts/gateway -n gateway --reuse-values --set gateway.image.tag=0.2.0
helm history gateway -n gateway
helm rollback gateway 1 -n gateway

# Store: versions are values; the setup container runs the WordPress/WooCommerce updaters
helm upgrade store charts/store -n store --reuse-values \
  --set wordpress.image.tag=7.1.4-php8.3-apache --set wordpress.coreVersion=7.1.4 \
  --set wordpress.woocommerceVersion=11.3.0
```

Store upgrades migrate the database forward only, so roll a store back by restoring a backup.
With production values a CronJob dumps each database nightly and keeps the last seven; take an
extra one before an upgrade with
`kubectl -n store create job --from=cronjob/store-db-backup before-upgrade`. Restore commands are
at the top of each chart's `templates/backup.yaml`. Details in
[docs/DESIGN.md](docs/DESIGN.md#upgrades-and-rollback).

## Kubernetes at a glance

| | |
|---|---|
| Namespaces | `store` (one store) and `gateway` |
| Public | Two Ingress hosts: the storefront, and the dashboard with `/api` and `/mcp`. Nothing else has a route in. |
| Internal only | The WooCommerce MCP server, the demo MCP servers, MariaDB, Postgres: ClusterIP Services, no Ingress, and NetworkPolicies that admit only their one legitimate caller |
| NetworkPolicies | Default deny, ingress and egress, in both namespaces. See [charts/store/templates/networkpolicy.yaml](charts/store/templates/networkpolicy.yaml) and [charts/gateway/templates/networkpolicy.yaml](charts/gateway/templates/networkpolicy.yaml) |
| Persistent volumes | WordPress files, MariaDB data, Postgres data, and (production) a volume per namespace for nightly database dumps |
| Secrets | Generated by the charts on first install and kept across upgrades. Split per consumer, so the MCP server never holds a database password. No secret is in the repository. |
| Probes | Readiness and liveness on every container; startup probes on the slow starters |
| Resources | Requests and limits on every container, set in the values files |
| Pod security | Non-root, read-only root filesystem, all capabilities dropped, no Kubernetes API token |
| Availability | Rolling updates gated by readiness, a short `preStop` drain, and PodDisruptionBudgets when a component runs two or more replicas |
| Chart safety | `values.schema.json` rejects mistyped values before anything is applied; `helm test` checks a running release |

```
kubectl get pods,svc,ingress,pvc,networkpolicy -n store
kubectl get pods,svc,ingress,pvc,networkpolicy -n gateway
```

## Bonus and stand-out items

The multi-store bonus is **not** attempted. Of the stand-out items:

| Item | Status | Where |
|---|---|---|
| 2. Tool permissions | Done: per-tool "admins only". Hidden from members' tool lists and refused if called directly. | [aggregator.py](services/gateway/gateway/aggregator.py), MCP servers page, test `test_admin_only_tools_are_hidden_from_members_and_refused_if_called` |
| 6. Security hardening | Done: SSRF checks on upstream URLs, default-deny NetworkPolicies, non-root containers, upstream credentials encrypted at rest. Beyond the list: tool definitions are pinned, and a tool a server changes after approval is withheld until an admin reviews it. No component talks to the Kubernetes API, so there is no RBAC to grant. | [ssrf.py](services/gateway/gateway/ssrf.py), [security.py](services/gateway/gateway/security.py), [upstreams.py](services/gateway/gateway/upstreams.py) (pinning), the `networkpolicy.yaml` files, [scripts/check_isolation.sh](scripts/check_isolation.sh) |
| 7. Scaling | Done: several gateway replicas (production values run two; the smoke test passes against two, over HTTPS). The MCP endpoint is stateless, registry changes made through one replica apply on the others on their next request, and replicas starting together take turns creating the schema. Rate limits per API key and per user for chat (counted per replica). | [upstreams.py](services/gateway/gateway/upstreams.py) `ensure_current`, [ratelimit.py](services/gateway/gateway/ratelimit.py), tests `test_a_change_made_through_one_replica...`, `test_requests_beyond_the_rate_limit...` |
| 8. Observability | Done, without Prometheus: the dashboard says why an upstream is unreachable (refused, DNS, timeout, HTTP 401, ...) and keeps the last outage after recovery; the audit page shows calls, error rate and median/95th-percentile latency per server. | [upstreams.py](services/gateway/gateway/upstreams.py) `describe_error`, `Outage`; `/api/stats` in [activity.py](services/gateway/gateway/api/activity.py) |
| 9. Upgrades and rollback | Done: rolling, readiness-gated upgrades with `helm rollback` tested; store versions driven by values; nightly database backups with restore commands; schema additions applied automatically to existing databases. | [docs/DESIGN.md](docs/DESIGN.md#upgrades-and-rollback), [setup.sh](charts/store/files/setup.sh), `templates/backup.yaml` |
| 1. VPS deployment | Rehearsed, not deployed: production values installed locally behind cert-manager TLS, smoke test passing over HTTPS. No real VPS. | "Production rehearsal" above |
| 3, 4, 5 | Not attempted | |

## Repository layout

```
charts/store/        Helm chart: WordPress, MariaDB, WooCommerce MCP server
charts/gateway/      Helm chart: gateway, dashboard, Postgres, demo MCP servers
services/woo-mcp/    The WooCommerce MCP server (Python)
services/gateway/    The gateway backend: MCP client + MCP server + REST API + chat agent (Python)
services/demo-mcp/   Two small MCP servers used as extra upstreams (Python)
dashboard/           The React dashboard
deploy/              k3d cluster config, cert-manager issuer for the VPS, production rehearsal overlays
scripts/             local-up, smoke test, isolation check
docs/                Design note, threat model, demo walkthrough
.github/workflows/   CI
```
