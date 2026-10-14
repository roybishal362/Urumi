# System design and tradeoffs

## What runs where

```
                         Internet / your laptop
                                  |
                      Traefik ingress controller          (kube-system)
                     /                          \
   store.<domain>   /                            \   gateway.<domain>
                   v                              v
 +------------------- namespace: store ---+   +----------------- namespace: gateway ------------------+
 |                                        |   |   /            /api, /mcp                             |
 |  WordPress + WooCommerce  <----------+ |   |  dashboard     gateway backend ------> Postgres       |
 |   (Deployment, PVC)                  | |   |  (nginx,       (FastAPI)               (StatefulSet,  |
 |        |                             | |   |   static)        |   |   |              PVC)          |
 |        v                             | |   |                  |   |   +--> fx MCP server           |
 |  MariaDB (StatefulSet, PVC)          | |   |                  |   +------> shipping MCP server     |
 |                                      | |   |                  |                                    |
 |  WooCommerce MCP server -------------+ |   |                  +--> LLM API (Groq, outside)         |
 |   (Deployment, no Ingress)  <----------+---+------------------+                                    |
 +----------------------------------------+   +-------------------------------------------------------+
```

Two Helm charts, two releases, two namespaces:

| Chart | Namespace | Workloads | Persistent | Public |
|---|---|---|---|---|
| `charts/store` | `store` | WordPress (+ WP-CLI setup container), MariaDB, WooCommerce MCP server | WordPress files (PVC), MariaDB data (PVC) | Storefront only |
| `charts/gateway` | `gateway` | Gateway backend, dashboard, Postgres, two demo MCP servers | Postgres data (PVC) | Dashboard, `/api`, `/mcp` |

Everything else is a ClusterIP Service with no Ingress rule. On top of that, every namespace
starts from a default-deny NetworkPolicy (ingress and egress) and opens only the arrows in the
diagram. `scripts/check_isolation.sh` proves it by attempting the connections.

**Why two charts.** A store is a unit you might have many of; the gateway is one per team.
Keeping the store as its own chart means "one more store" is one more release in its own
namespace. It also puts the store's secrets in a different namespace from the gateway's, which
is a boundary Kubernetes enforces by itself.

## The four layers

### 1. Store

Official `wordpress` and `mariadb` images, no custom image. A second container in the
WordPress pod runs WP-CLI and executes `charts/store/files/setup.sh` on every start: install
WordPress, install WooCommerce at the pinned version, enable cash on delivery, add a free
shipping method, create the shop pages, seed two products, and register the REST API key.
Every step checks before acting, so restarts are cheap and re-running is safe. The pod only
becomes Ready when the script finishes, so the storefront is never served half-configured.

Tradeoff: WooCommerce is downloaded on first start instead of being baked into an image. That
keeps the repository free of a WordPress image build, at the cost of needing internet access on
first install. A production setup would bake the plugin into an image.

### 2. WooCommerce MCP server (`services/woo-mcp`)

Written with the official MCP Python SDK (`MCPServer`). Six tools: `list_products`,
`get_product`, `create_product`, `list_orders`, `get_order`, `update_order_status`.

**How it talks to the store: the WooCommerce REST API (`wc/v3`), over the cluster network.**

- *Why REST and not WP-CLI or the database.* REST is WooCommerce's supported contract: it runs
  the same validation, hooks and stock logic as the admin UI. WP-CLI would need the MCP server
  to share WordPress's filesystem and database credentials, which is exactly the access I want
  it not to have. Writing to the database directly would bypass WooCommerce entirely.
- *Authentication.* WooCommerce accepts its consumer key and secret as Basic credentials only
  over TLS. Inside the cluster the hop is plain HTTP, so the server uses WooCommerce's other
  documented scheme, one-legged OAuth 1.0a: every request is signed with HMAC-SHA256 and the
  consumer secret never crosses the network (`woo_mcp/oauth1.py`).
- *A detail worth knowing.* WooCommerce checks the signature against the URL it builds from
  the site's own public address, not from the Host header. The server therefore sends requests
  to the in-cluster Service but signs them for `http://<public host>/...`.
- *Tool output is trimmed.* A raw WooCommerce order is several kilobytes. Tools return the
  fields a person would ask about, so a model can read twenty orders without filling its
  context. `list_orders` takes a `period` ("today", "yesterday", ...) and works out the date
  range in the store's own timezone, because models are unreliable at date arithmetic.

It is stateless (no MCP sessions, JSON responses), so it scales by adding replicas.

### 3. Gateway (`services/gateway`)

One FastAPI process with two faces: a REST API for the dashboard, and an MCP endpoint at `/mcp`.

#### Gateway as MCP client: one supervised connection per upstream

`gateway/upstreams.py`. Each enabled server gets an `UpstreamConnection`, which owns one
background task:

```
connect -> list tools -> Connected -> re-list every 5 s (this is the health check)
   ^                                        |
   +---- wait 1 s, 2 s, 4 s ... 20 s <------+  any failure: Unreachable, with the reason
```

- The connection is long-lived and shared. Tool calls from any number of users are multiplexed
  over it; MCP requests carry ids, so one slow call does not queue the others.
- The tool list is cached on the connection. Listing tools for a client reads memory only and
  never touches an upstream.
- The client negotiates the protocol with each upstream (`mode="auto"`): the stateless
  2026-07-28 revision if the server offers it, otherwise the older `initialize` handshake.

`UpstreamManager` keeps the running connections equal to the enabled rows in the database. That
is the Kubernetes controller pattern in miniature: desired state in a store, a loop that
converges on it. It is also what lets several gateway replicas agree without talking to each
other. Every registry change (add, edit, disable, remove, tool policy) bumps a version counter
in the same transaction. Each replica compares that counter with the one it last acted on at
the start of every request, so a server disabled through one replica is gone from the others on
their very next request. A timer re-reads the registry as a fallback and restarts any
supervisor that has died.

#### Gateway as MCP server: aggregation and routing

`gateway/aggregator.py` and `gateway/mcp_endpoint.py`.

- `tools/list` returns the cached tools of every *connected* server the caller may use.
- `tools/call` looks the name up in a routing table, checks the caller's role, calls the owning
  connection with a timeout, writes an audit row, and returns the result.

**Tool name collisions.** Every tool is exposed as `<server prefix>__<tool name>`, for example
`woo__list_orders`. Prefixes are unique (a database constraint), so two servers can both expose
`get_rates` and they become `fx__get_rates` and `shipping__get_rates`. The demo servers do
exactly that on purpose. The prefix is always added, not only on a clash, for two reasons: a
tool's name never changes because another server was added later (which would silently break
saved prompts and client configs), and the name tells a model and an audit reader which system
a tool acts on. Routing is by table lookup, not by parsing the name. Names that contain
characters LLM APIs reject, or exceed 64 characters, are rewritten with a short hash so they
stay valid and unique.

**The endpoint is stateless.** No session ids, one JSON response per POST. Any replica can
serve any request and a gateway restart strands nobody. It serves both protocol eras: clients
that open with `initialize` (2025 revisions) and clients using the 2026-07-28 per-request form.
The cost: the gateway cannot push a "tool list changed" notification, so a client sees a
disabled server's tools disappear the next time it lists tools (in Claude Code, on reconnect).
A stale client that calls a removed tool gets a clear "Unknown tool" error.

#### How one tool call travels

```
Claude Code                    gateway                      woo-mcp                 WordPress
    |  POST /mcp                   |                            |                        |
    |  Authorization: Bearer urm_… |                            |                        |
    |  tools/call woo__list_orders |                            |                        |
    |----------------------------->|                            |                        |
    |        hash key -> user (not revoked, active)             |                        |
    |        woo__list_orders -> server "woo", tool "list_orders"                        |
    |        role check                                         |                        |
    |                              |  POST /mcp                 |                        |
    |                              |  Authorization: Bearer <woo token, decrypted>       |
    |                              |  tools/call list_orders    |                        |
    |                              |--------------------------->|  GET /wp-json/wc/v3/orders
    |                              |                            |  ?after=…&oauth_signature=…
    |                              |                            |----------------------->|
    |                              |                            |<------- JSON ----------|
    |                              |<--- trimmed result --------|                        |
    |        audit row: user, server, tool, ms, outcome         |                        |
    |<------ result ---------------|                            |                        |
```

Three separate credentials are in play and none is forwarded: the user's gateway key stops at
the gateway, the gateway's token stops at the MCP server, and the WooCommerce key stops at
WordPress.

#### Failure handling

| What happens | What the gateway does | What the caller sees |
|---|---|---|
| Upstream pod dies | Supervisor's next check (every 5 s; 2 s locally) fails; status becomes Unreachable with the reason; retries with backoff. After recovery the outage stays on record (when, how long, why), because Kubernetes often replaces a pod before anyone looks | Its tools leave `tools/list`. Other servers are untouched. |
| Call to a dead upstream (stale client) | Fails immediately from cached state, no network wait | `isError` result: "The 'X' server is unreachable. …" |
| Upstream is slow | The call is cancelled at the timeout (30 s by default); the connection stays up | `isError` result: "… timed out." Other calls, even to the same server, were never blocked. |
| Upstream rejects the credential | Connection attempt fails; an extra HTTP probe reads the status code | Dashboard: "The server rejected the gateway's credential (HTTP 401)." |
| Upstream returns a tool error | Passed through unchanged | The upstream's own `isError` result; audited as `error` |
| Gateway pod restarts | New pod reconnects to every upstream before reporting Ready | Nothing: the endpoint is stateless, and a `preStop` delay drains the old pod |
| Several gateway replicas start at once on an empty database | A PostgreSQL advisory lock lets one at a time create tables, the first admin and the seed servers | Nothing (found and fixed during the production rehearsal) |
| A server changes a tool after it was approved | The tool's fingerprint no longer matches its pin, so it is withheld | The tool disappears for everyone, chat included, until an admin approves the new version |
| One key floods the endpoint | Token bucket per key (240/min by default) | HTTP 429 with `Retry-After` for that key only |
| Postgres is down | Readiness fails, so the pod leaves the Service; liveness stays green, so it is not restarted | 503 until the database returns |

Failures in a call come back as `isError` tool results with a readable message rather than as
protocol errors, so a model can read what went wrong and continue with the other servers.

#### Team, keys, audit

- Two roles. Admins manage servers and members; members use tools. An admin can also mark any
  tool "admins only": it is then hidden from members' tool lists and refused if called, with
  the same error as a tool that does not exist.
- No mail server, so inviting a member produces a one-time link the admin passes on.
- API keys are `urm_` plus 256 random bits. Only the SHA-256 is stored. Keys are checked
  against the database on every request with no cache, so revocation is immediate.
- A key an admin issues for someone else records who issued it, so calls made with it can be
  traced to the admin as well as to the owner.
- Every tool call writes an audit row: user, key prefix, source (MCP client or chat), server,
  tool, duration, outcome, error. Rejected attempts with a revoked key are recorded against the
  key's owner. Arguments and results are not stored, because they can hold customer data. The
  audit page also summarises the last 24 hours per server: calls, error rate, median and
  95th-percentile latency.
- Rate limits: requests per minute per API key on `/mcp`, and chat messages per minute per user.
  Counted in memory per replica, so with two replicas the effective limit is up to twice as high.

#### Tool pinning

Tool descriptions are instructions to a model, written by the upstream. A server that was
harmless when an admin added it can later rewrite a description to steer models (a "rug pull").
So each tool's definition (description, input and output schema, annotations) is fingerprinted
with SHA-256. The first definition seen is pinned in the database. From then on, if the
fingerprint changes, the tool is withheld from every tool list and every call, chat included,
and flagged on the MCP servers page. An admin opens a side-by-side view of the approved and the
new definition and either approves it (which re-pins it) or leaves it withheld. Only the changed
tool is affected; the server's other tools keep working.

### 4. Chat agent (`gateway/chat.py`)

A plain loop: send the conversation and the user's tools to the model; run any tool calls it
asks for; feed the results back; stop when it answers in text (or after 8 rounds).

It runs inside the gateway and calls the same `Aggregator.call_tool` function the MCP endpoint
calls, with a principal built from the signed-in user's session. So it sees exactly the tools
that user would get with their own API key, obeys the same role rules, and its calls are
audited under that user with source `chat`. It has no credentials of its own and no separate
path to the upstreams.

The model is reached over the OpenAI-compatible chat completions API, so the provider is three
settings (`llm.baseUrl`, `llm.model`, `llm.apiKey`). Groq's free tier is the default.

Free tiers limit tokens per minute (8,000 for `openai/gpt-oss-120b` on Groq), and every step of
the loop resends the tool definitions and the conversation. Measured against Groq: one step costs
about 1,400 prompt tokens (most of it the 11 tool definitions) and a three-step question about
5,000-6,000, so the free tier sustains roughly one multi-step question a minute. The agent keeps within it where it
can: schema titles are stripped from tool definitions, replies and reasoning are capped
(`llm.maxTokens`, `llm.reasoningEffort: low`), history is trimmed to the last 12 messages and tool
results to 5,000 characters. When the provider still answers "rate limited, retry in N seconds",
the agent waits (up to 30 s, three times) and carries on, showing "waiting for the rate limit" in
the chat instead of failing.

## Architecture choices and their tradeoffs

| Choice | Why | Cost |
|---|---|---|
| k3d locally | It runs real k3s: same Traefik, same local-path storage, same NetworkPolicy controller as the VPS. Local-to-VPS differences shrink to values. | Tied to Docker. |
| Python, official MCP SDK 2.x | First-party client and server with both protocol eras handled for me. | SDK 2.x is recent; its transport is async task groups, which shaped the supervisor design. |
| Persistent upstream connections | Fast calls, instant knowledge of health, cached tool lists. | Needs a supervisor and careful failure handling. A connect-per-call design would be simpler and slower. |
| Stateless gateway MCP endpoint | Horizontal scaling and painless restarts. | No push notifications for tool list changes. |
| Registry in Postgres, with a version counter each replica checks per request | Replicas converge with no messaging layer, and changes apply everywhere on the next request. | One extra primary-key read per request. |
| Connection status kept in memory per replica | Simple and always current. | Each replica runs its own health checks, so after a server dies the replicas notice up to one check interval (5 s) apart, and the dashboard shows the view of whichever replica answered. |
| Tables created at startup, new nullable columns added automatically | No migration tool to explain, and an upgraded gateway starts against its old database. | Renames or type changes would need a real migration tool (Alembic). |
| Chat history kept in the browser | The gateway stays stateless. | A page reload clears the conversation. |
| Helm-generated secrets (kept across upgrades with `lookup`) | Zero secrets to invent or commit; one command installs. | `helm template` cannot see the cluster, so it renders different random values each time. |

## What changes for production

All of it is in the values files. The templates are identical.

| Concern | Local (`values-local.yaml`) | VPS (`values-prod.yaml`) |
|---|---|---|
| DNS | `*.localtest.me`, which resolves to 127.0.0.1 in public DNS | Real A records pointing at the VPS |
| Ingress | Traefik, plain HTTP on port 80 | Traefik, TLS; certificates from cert-manager (`ingress.tls.clusterIssuer`) |
| URL scheme | `http` | `https` (also switches the session cookie to Secure and WordPress to https URLs) |
| Storage class | Cluster default (local-path), 2 Gi | Named explicitly; 10-20 Gi. Swap in a replicated class such as Longhorn if node-local disk is not acceptable |
| Images | Built locally, loaded with `k3d image import` | Pulled from a registry (`*.image.repository`) |
| Replicas | 1 of each | 2 gateway, 2 dashboard, 2 WooCommerce MCP server |
| Secrets | Generated by the chart | Generated by the chart, or supplied: `secrets.*` values at deploy time, or `llm.existingSecret` for a Secret created out of band (SealedSecrets, External Secrets) |
| Resources | Small requests and limits | Larger |
| HTTP | Served as is | Permanently redirected to HTTPS (`ingress.tls.redirectHttp`, a Traefik middleware) |
| Backups | Off | Nightly `mariadb-dump` / `pg_dump` CronJobs to their own volumes, last 7 kept (`backup.*`) |
| Disruption budgets | None (one replica each) | Created automatically for anything with two or more replicas |

WordPress stays at one replica in both: its volume is ReadWriteOnce. Scaling it means shared
storage (RWX) or moving uploads to object storage, which is outside this assignment.

### Upgrades and rollback

- **Gateway, dashboard, MCP servers.** Build and push a new tag, then
  `helm upgrade gateway charts/gateway -n gateway --reuse-values --set gateway.image.tag=0.2.0`.
  Deployments roll one pod at a time; readiness gates traffic and the `preStop` delay drains the
  old pod. `helm rollback gateway <revision>` returns to the previous manifests. Because the
  gateway creates tables but never alters them, an older image runs against a newer database.
- **Store.** WordPress core and WooCommerce versions are Helm values
  (`wordpress.coreVersion`, `wordpress.woocommerceVersion`). On `helm upgrade` the setup
  container sees the installed version is behind and runs the WordPress and WooCommerce
  updaters, including their database migrations. In-dashboard auto-updates are switched off so
  the cluster follows the chart.
- **Rolling a store back** is different: WordPress and WooCommerce migrate their database
  forward only. `helm rollback` restores the manifests, but the data needs the backup taken
  before the upgrade: run the backup job by hand first
  (`kubectl create job --from=cronjob/store-db-backup before-upgrade`) and restore that dump
  if the upgrade goes wrong. Uploaded images live on the WordPress volume and need a volume
  snapshot. The setup script deliberately never downgrades.
- **Backups fail loudly.** A dump is written to a file, checked for the dump tool's
  "completed" marker, and only then compressed and rotated, so a failed dump fails the job and
  never replaces a good backup. (The first version piped the dump into gzip, which hid a failed
  dump behind gzip's success; the production rehearsal caught it.)
- `helm test store` and `helm test gateway` check a release from inside the cluster after an
  upgrade or a rollback.
- Secrets survive upgrades and rollbacks: the charts read the existing Secret before
  generating a value.

## Known limits

- Not deployed to a real VPS. The production values were rehearsed locally (cert-manager with
  a local CA, both charts with `values-prod.yaml`, the smoke test passing over HTTPS), but
  Let's Encrypt and a public domain are untested.
- On Groq's free tier the chat agent manages about one multi-step question a minute; quicker
  questions wait for the rate limit (visibly) rather than fail. A paid tier or another provider
  removes the wait; it is a setting.
- Rate limits and the sign-in throttle are counted per replica, in memory. A shared limit
  would need Redis or similar.
- No Prometheus metrics. Per-server calls, error rates and latency are computed from the audit
  log for the dashboard instead.
- Tools are pinned on first sight (trust on first use): a server that is hostile from the
  start is caught only by the admin reading its tools when adding it.
- The URL safety check resolves a hostname when a server is saved and again on each connect.
  A hostname that changes its answer in between (DNS rebinding) is not caught by the check;
  the egress NetworkPolicy blocking the link-local range is the backstop.
- The multi-store bonus is not built. The store chart is shaped for it (one release, one
  namespace, its own secrets and policies), but nothing provisions stores from the dashboard.
