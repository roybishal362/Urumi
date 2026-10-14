# Demo video script

A run-through that covers everything the assignment asks the video to show, in an order that
flows. About 20 minutes. Commands assume the local setup from the README is running.

**Before recording**

- `scripts\local-up.ps1` (or `./scripts/local-up.sh`) has run, with `GROQ_API_KEY` in `.env`.
- Leave about a minute between chat questions. Groq's free tier allows 8,000 tokens a minute and a
  multi-step question uses about 5,000; ask faster and the chat shows "waiting for the rate limit"
  and carries on, which is fine to show but slows the video down.
- Two browser windows: one signed in as the admin, one private window for member A.
- A terminal with `kubectl` pointed at the cluster.
- Invite two members on the Members page: `alice@example.com` (A) and `bob@example.com` (B).
  Open each invite link in a private window and set a password.
- Have the "MCP servers" page open, so a dying server is visible the moment it happens.

---

## 1. What it is (2 min)

Show the diagram in [DESIGN.md](DESIGN.md#what-runs-where). Say:

- Four layers: a WooCommerce store, an MCP server I wrote for it, a gateway with a dashboard, and
  a chat agent.
- Two Helm charts, two namespaces: `store` for one shop, `gateway` for the team's hub.
- Everything goes through the gateway: the chat agent and external MCP clients never talk to an
  upstream server directly.

```
kubectl get pods,svc,ingress,pvc -n store
kubectl get pods,svc,ingress,pvc -n gateway
```

## 2. The store (2 min)

1. http://store.localtest.me/shop/: add a product to the cart, check out with **Cash on delivery**.
2. http://store.localtest.me/wp-admin → WooCommerce → Orders: the order is there.

Say: WordPress and MariaDB are official images; a WP-CLI container in the same pod installs and
configures WooCommerce on first start; the pod only turns Ready when that is done; data lives on
two persistent volumes.

## 3. Chat agent as user A (3 min)

In A's window, open **Chat**.

1. `Create a product called "Monsoon Umbrella" priced at 24.99`. Expand the `woo__create_product`
   step to show its arguments and result. Open the product link: it is on the storefront.
2. Order the umbrella on the storefront by hand.
3. `What orders came in today?` It calls `woo__list_orders` with `period: today`.

Say: the agent is a loop. Send the question and the tools to the model, run the tools it asks
for through the gateway's own routing code, feed the results back, repeat until it answers.
It runs as A, so it sees A's tools and every call is audited under A.

## 4. External MCP client as user B (3 min)

1. In B's window: **API keys → Generate key**. Copy the Claude Code command from the dialog.
2. Terminal: paste it (`claude mcp add --transport http urumi http://gateway.localtest.me/mcp --header "Authorization: Bearer urm_..."`), start `claude`, run `/mcp`: all tools from three servers.
   (Or MCP Inspector: `npx @modelcontextprotocol/inspector`, Streamable HTTP, the URL, header `Authorization: Bearer ...`.)
3. Ask: `What orders came in today?`
4. Two servers in one prompt: `Take today's order total from the store and convert it to INR,
   then quote shipping a 1 kg parcel to the US.` (uses `woo__list_orders`, `fx__convert`,
   `shipping__get_rates`).

Point out `fx__get_rates` and `shipping__get_rates`: two servers with the same tool name,
told apart by the server prefix.

## 5. Team and control (4 min)

1. **Revoke B's key.** Admin window → API keys → "Show the whole team's keys" → Revoke B's key.
   In Claude Code, ask again: refused (HTTP 401).
2. **Disable a server.** MCP servers → Shipping → Disable. A's chat: the tools list no longer
   has `shipping__*`. External client: list tools again (Claude Code `/mcp` → reconnect, or
   Inspector "List Tools"): gone. Calling one directly is refused. Enable it again.
3. **Kill a pod.**
   ```
   kubectl -n gateway delete pod -l app.kubernetes.io/component=mcp-fx
   ```
   Kubernetes replaces it within seconds. The dashboard either shows Currency as Unreachable
   with the reason, or, if the replacement was faster than the 2-second health check, a
   "Recovered N s ago after being unreachable" note. For a longer outage:
   ```
   kubectl -n gateway scale deploy/gateway-fx --replicas=0     # Unreachable: Connection refused
   kubectl -n gateway scale deploy/gateway-fx --replicas=1     # reconnects by itself
   ```
   Meanwhile ask the chat about orders: WooCommerce keeps working.
4. **Audit log.** Every call above: who, chat or MCP client (and which key), server, tool, time,
   duration, outcome. Point at B's rejected call after the revoke, and the per-server panel
   (calls, error rate, latency).

## 6. Kubernetes (2 min)

```
kubectl -n store get deploy store-wordpress -o yaml | less    # probes, resources, securityContext
kubectl get networkpolicy -A
./scripts/check_isolation.sh
```

Say:

- Public: two Ingress hosts only. Everything else is ClusterIP.
- Every namespace starts from default-deny NetworkPolicies (in and out). The WooCommerce MCP
  server accepts only the gateway's pods and can only reach its own WordPress. The script
  proves it by trying connections that should and should not work.
- Secrets are generated by the charts, split per consumer, never in git. Containers run as
  non-root with read-only file systems and no Kubernetes API token.
- `helm test store -n store` and `helm test gateway -n gateway` check a release from inside the cluster.

## 7. MCP internals (2 min)

Show [DESIGN.md](DESIGN.md#how-one-tool-call-travels): the path of one call.

- The gateway holds one long-lived MCP connection per upstream, supervised by a background
  task that re-lists tools every few seconds (the health check) and reconnects with backoff.
- Tool lists are cached, so listing never waits on a slow server.
- Calls have a 30 s timeout. Show it: call `fx__debug_sleep` with 60 seconds; it times out while
  other calls answer immediately.
- Three credentials, none forwarded: the user's key stops at the gateway, the gateway's token
  stops at the MCP server, the WooCommerce key stops at WordPress.

## 8. Security (2 min)

- API keys: random, shown once, stored as SHA-256. Upstream credentials: encrypted in the
  database with a key from a Kubernetes Secret, never returned by any API.
- A member can use tools; cannot see server URLs or credentials, manage servers or members,
  see others' keys or activity, or call admin-only tools.
- **A malicious server.** Show tool pinning: tools are pinned when first seen; if a server later
  rewrites a description (for example to slip instructions to the model), that tool is hidden
  from everyone until an admin reviews the before/after and approves it. Also: descriptions are
  labelled with their server and length-capped, the server never sees user keys, and the
  network policy boxes it in.
- Adding a server by URL refuses cloud metadata and private addresses (try
  `http://169.254.169.254/` in the Add server dialog).

## 9. Local to production (2 min)

```
diff charts/gateway/values-local.yaml charts/gateway/values-prod.yaml
```

- Same templates; values change the domain, TLS (cert-manager), HTTP→HTTPS redirect, storage
  class and size, image registry, replicas, and switch on nightly database backups.
- Rehearsed locally: both charts installed with `values-prod.yaml` behind real TLS from
  cert-manager, two gateway replicas, and the full smoke test passing over HTTPS
  (README, "Production rehearsal").
- Upgrade: `helm upgrade ... --set gateway.image.tag=0.2.0` rolls pods one by one; `helm
  rollback gateway <revision>` goes back. Store versions are values; the setup container runs
  the WordPress/WooCommerce updaters. Store rollbacks restore a backup, because those updates
  migrate the database forward only.
