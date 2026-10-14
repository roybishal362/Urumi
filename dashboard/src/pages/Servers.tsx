import { useState } from 'react'
import type { FormEvent } from 'react'

import type { Session } from '../App'
import { api } from '../api'
import type { AuthType, Server, ServerInput, ServerStatus, ToolDefinition } from '../api'
import { errorText, timeAgo, useLoader } from '../lib'
import { CopyButton, ErrorNote, Field, Modal, PageHeader, Pill } from '../ui'

const STATUS: Record<ServerStatus, { label: string; tone: 'good' | 'bad' | 'warn' | 'muted' }> = {
  connected: { label: 'Connected', tone: 'good' },
  unreachable: { label: 'Unreachable', tone: 'bad' },
  disabled: { label: 'Disabled', tone: 'muted' },
  connecting: { label: 'Connecting…', tone: 'warn' },
}

export function ServersPage({ session }: { session: Session }) {
  const isAdmin = session.user.role === 'admin'
  // Polled, so a server going down shows up here without a page reload.
  const { data, error, reload } = useLoader(() => api.servers(), 4000)
  const [editing, setEditing] = useState<Server | 'new' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  async function run(action: () => Promise<unknown>) {
    setActionError(null)
    try {
      await action()
      await reload()
    } catch (err) {
      setActionError(errorText(err))
    }
  }

  return (
    <>
      <PageHeader
        title="MCP servers"
        subtitle="Servers connected here are available to the whole team through one endpoint."
        action={
          isAdmin && (
            <button type="button" className="btn btn-primary" onClick={() => setEditing('new')}>
              Add server
            </button>
          )
        }
      />
      <div className="card endpoint">
        <div>
          <div className="small muted">Your team's MCP endpoint</div>
          <code className="endpoint-url">{session.gateway.mcp_url}</code>
        </div>
        <CopyButton text={session.gateway.mcp_url} />
      </div>
      <ErrorNote message={error ?? actionError} />
      {data?.servers.length === 0 && <div className="card muted">No servers yet.</div>}
      {data?.servers.map((server) => (
        <ServerCard key={server.id} server={server} isAdmin={isAdmin} run={run} onEdit={() => setEditing(server)} />
      ))}
      {editing && (
        <ServerForm
          server={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            await reload()
          }}
        />
      )}
    </>
  )
}

function ServerCard({
  server,
  isAdmin,
  run,
  onEdit,
}: {
  server: Server
  isAdmin: boolean
  run: (action: () => Promise<unknown>) => Promise<void>
  onEdit: () => void
}) {
  const [open, setOpen] = useState(false)
  const status = STATUS[server.status]
  const [reviewing, setReviewing] = useState<string | null>(null)
  const changedCount = server.tools.filter((tool) => tool.changed).length

  return (
    <section className="card server">
      <div className="server-head">
        <div className="server-title">
          <h2>{server.name}</h2>
          <Pill tone={status.tone}>{status.label}</Pill>
          <code className="prefix" title="Every tool from this server starts with this prefix">
            {server.slug}__
          </code>
        </div>
        {isAdmin && (
          <div className="actions">
            {server.enabled && (
              <button type="button" className="btn btn-small" onClick={() => run(() => api.refreshServer(server.id))}>
                Reconnect
              </button>
            )}
            <button type="button" className="btn btn-small" onClick={onEdit}>
              Edit
            </button>
            <button
              type="button"
              className="btn btn-small"
              onClick={() => run(() => api.updateServer(server.id, { enabled: !server.enabled }))}
            >
              {server.enabled ? 'Disable' : 'Enable'}
            </button>
            <button
              type="button"
              className="btn btn-small btn-danger"
              onClick={() => {
                if (window.confirm(`Remove "${server.name}"? Its tools will no longer be available to the team.`)) {
                  void run(() => api.removeServer(server.id))
                }
              }}
            >
              Remove
            </button>
          </div>
        )}
      </div>

      {isAdmin && (
        <div className="server-meta">
          <span className="mono">{server.url}</span>
          <span>
            {server.auth_type === 'none' && 'No credential'}
            {server.auth_type === 'bearer' && 'Bearer token ••••••'}
            {server.auth_type === 'header' && `Header ${server.auth_header} ••••••`}
          </span>
          {server.protocol_version && <span>MCP {server.protocol_version}</span>}
        </div>
      )}

      {server.status === 'unreachable' && (
        <div className="note note-bad">
          <strong>Why:</strong> {server.error ?? 'Unknown error.'}
          <span className="muted small"> Unreachable since {timeAgo(server.since)}. Retrying automatically.</span>
        </div>
      )}
      {server.status === 'disabled' && <div className="note">Disabled. Its tools are hidden from every client and from chat.</div>}
      {changedCount > 0 && (
        <div className="note note-bad">
          {changedCount} tool{changedCount === 1 ? '' : 's'} changed since approval. Changed tools are hidden from everyone,
          including chat, until an admin reviews and approves the new version below.
        </div>
      )}
      {server.status === 'connected' && server.last_outage && isRecent(server.last_outage.ended) && (
        <div className="note note-warn">
          Recovered {timeAgo(server.last_outage.ended)} after being unreachable for {server.last_outage.seconds} s.
          <span className="small"> Cause: {server.last_outage.reason || 'unknown'}</span>
        </div>
      )}

      {server.status !== 'disabled' && (
        <>
          <button type="button" className="btn btn-ghost tools-toggle" onClick={() => setOpen(!open)}>
            {open ? '▾' : '▸'} {server.tools.length} tool{server.tools.length === 1 ? '' : 's'}
            {server.status === 'unreachable' && server.tools.length > 0 && <span className="muted"> (last known; currently unavailable)</span>}
          </button>
          {open && (
            <ul className="tools">
              {server.tools.map((tool) => (
                <li key={tool.name}>
                  <div>
                    <code>{tool.name}</code>
                    {tool.admin_only && <Pill tone="info">Admins only</Pill>}
                    {tool.changed && <Pill tone="bad">Changed since approval</Pill>}
                    <div className="muted small">{tool.description}</div>
                  </div>
                  {isAdmin && tool.changed && (
                    <button type="button" className="btn btn-small btn-danger" onClick={() => setReviewing(tool.tool)}>
                      Review change
                    </button>
                  )}
                  {isAdmin && (
                    <label className="check" title="Hide this tool from members and refuse it if they call it">
                      <input
                        type="checkbox"
                        checked={tool.admin_only}
                        onChange={(event) => run(() => api.setToolPolicy(server.id, tool.tool, event.target.checked))}
                      />
                      Admins only
                    </label>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {reviewing && (
        <ReviewModal
          serverId={server.id}
          tool={reviewing}
          onClose={() => setReviewing(null)}
          onApproved={async () => {
            setReviewing(null)
            await run(async () => undefined)
          }}
        />
      )}
    </section>
  )
}

/** Side by side: the definition an admin approved, and what the server says now. */
function ReviewModal({
  serverId,
  tool,
  onClose,
  onApproved,
}: {
  serverId: string
  tool: string
  onClose: () => void
  onApproved: () => Promise<void>
}) {
  const { data, error } = useLoader(() => api.reviewTool(serverId, tool))
  const [busy, setBusy] = useState(false)
  const [approveError, setApproveError] = useState<string | null>(null)
  const show = (definition: ToolDefinition | null) =>
    definition ? JSON.stringify({ description: definition.description, input_schema: definition.input_schema }, null, 2) : '(none)'

  return (
    <Modal title={`Review change to "${tool}"`} onClose={onClose}>
      <div className="form">
        <p className="muted">
          The server now describes this tool differently from the version that was approved. Tool descriptions are read by
          AI models, so a changed description can be an attempt to steer them. Approve only if the change is expected.
        </p>
        <ErrorNote message={error ?? approveError} />
        {data && (
          <div className="diff">
            <div>
              <h3>Approved</h3>
              <pre>{show(data.approved)}</pre>
            </div>
            <div>
              <h3>Now</h3>
              <pre>{show(data.current)}</pre>
            </div>
          </div>
        )}
        <div className="form-actions">
          <button type="button" className="btn" onClick={onClose}>
            Keep it hidden
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || !data}
            onClick={async () => {
              setBusy(true)
              try {
                await api.approveTool(serverId, tool)
                await onApproved()
              } catch (err) {
                setApproveError(errorText(err))
              } finally {
                setBusy(false)
              }
            }}
          >
            Approve new version
          </button>
        </div>
      </div>
    </Modal>
  )
}

/** Outages from the last hour are worth showing; older ones are noise. */
function isRecent(iso: string): boolean {
  return Date.now() - new Date(iso).getTime() < 60 * 60 * 1000
}

function ServerForm({ server, onClose, onSaved }: { server: Server | null; onClose: () => void; onSaved: () => Promise<void> }) {
  const [name, setName] = useState(server?.name ?? '')
  const [slug, setSlug] = useState(server?.slug ?? '')
  const [url, setUrl] = useState(server?.url ?? '')
  const [authType, setAuthType] = useState<AuthType>(server?.auth_type ?? 'none')
  const [authHeader, setAuthHeader] = useState(server?.auth_header ?? 'X-API-Key')
  const [credential, setCredential] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const input: ServerInput = { name, url, auth_type: authType }
    if (authType === 'header') input.auth_header = authHeader
    // An empty credential on edit means "keep the one already stored".
    if (credential) input.credential = credential
    try {
      if (server) await api.updateServer(server.id, input)
      else await api.addServer({ ...input, slug })
      await onSaved()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={server ? `Edit ${server.name}` : 'Add an MCP server'} onClose={onClose}>
      <form onSubmit={submit} className="form">
        <ErrorNote message={error} />
        <Field label="Name">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Analytics" required autoFocus />
        </Field>
        {!server && (
          <Field label="Tool prefix" hint="Lowercase letters and digits. Tools appear as prefix__toolname. Cannot be changed later.">
            <input
              value={slug}
              onChange={(e) => setSlug(e.target.value.toLowerCase())}
              placeholder="analytics"
              pattern="[a-z][a-z0-9]{1,15}"
              required
            />
          </Field>
        )}
        <Field label="URL" hint="The server's Streamable HTTP endpoint.">
          <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://mcp.example.com/mcp" required />
        </Field>
        <Field label="Credential">
          <select value={authType} onChange={(e) => setAuthType(e.target.value as AuthType)}>
            <option value="none">None</option>
            <option value="bearer">Bearer token (Authorization header)</option>
            <option value="header">Custom header</option>
          </select>
        </Field>
        {authType === 'header' && (
          <Field label="Header name">
            <input value={authHeader} onChange={(e) => setAuthHeader(e.target.value)} required />
          </Field>
        )}
        {authType !== 'none' && (
          <Field
            label={authType === 'bearer' ? 'Token' : 'Header value'}
            hint="Stored encrypted on the gateway. It is never shown again, to anyone."
          >
            <input
              type="password"
              value={credential}
              onChange={(e) => setCredential(e.target.value)}
              placeholder={server?.has_credential ? 'Leave empty to keep the current one' : ''}
              required={!server?.has_credential}
              autoComplete="off"
            />
          </Field>
        )}
        <div className="form-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={busy}>
            {busy ? 'Saving…' : server ? 'Save' : 'Add server'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
