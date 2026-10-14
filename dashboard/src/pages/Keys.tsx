import { useState } from 'react'
import type { FormEvent } from 'react'

import type { Session } from '../App'
import { api } from '../api'
import type { ApiKey } from '../api'
import { errorText, timeAgo, useLoader } from '../lib'
import { CopyButton, ErrorNote, Modal, PageHeader, Pill } from '../ui'

export function KeysPage({ session }: { session: Session }) {
  const isAdmin = session.user.role === 'admin'
  const [everyone, setEveryone] = useState(false)
  const { data, error, reload } = useLoader(() => api.keys(everyone && isAdmin), 10000, String(everyone))
  const [name, setName] = useState('')
  const [created, setCreated] = useState<{ key: ApiKey; secret: string } | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  async function create(event: FormEvent) {
    event.preventDefault()
    setActionError(null)
    try {
      setCreated(await api.createKey(name))
      setName('')
      await reload()
    } catch (err) {
      setActionError(errorText(err))
    }
  }

  async function revoke(key: ApiKey) {
    if (!window.confirm(`Revoke "${key.name}" (${key.prefix}…)? Clients using it will be refused on their next request.`)) return
    setActionError(null)
    try {
      await api.revokeKey(key.id)
      await reload()
    } catch (err) {
      setActionError(errorText(err))
    }
  }

  return (
    <>
      <PageHeader
        title="API keys"
        subtitle="A key lets an MCP client such as Claude Code or Cursor use every enabled server, as you."
      />
      <form className="card inline-form" onSubmit={create}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Key name, e.g. my laptop" maxLength={80} />
        <button className="btn btn-primary">Generate key</button>
        {isAdmin && (
          <label className="check push-right">
            <input
              type="checkbox"
              checked={everyone}
              onChange={(e) => setEveryone(e.target.checked)}
            />
            Show the whole team's keys
          </label>
        )}
      </form>
      <ErrorNote message={error ?? actionError} />

      <div className="card table-card">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Key</th>
              {everyone && <th>Owner</th>}
              <th>Created</th>
              <th>Last used</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data?.keys.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  No keys yet. Generate one to connect an MCP client.
                </td>
              </tr>
            )}
            {data?.keys.map((key) => (
              <tr key={key.id} className={key.revoked_at ? 'faded' : undefined}>
                <td>
                  {key.name}
                  {key.issued_by && <div className="muted small">issued by {key.issued_by}</div>}
                </td>
                <td className="mono">{key.prefix}…</td>
                {everyone && <td>{key.user_email}</td>}
                <td>{timeAgo(key.created_at)}</td>
                <td>{timeAgo(key.last_used_at)}</td>
                <td>{key.revoked_at ? <Pill tone="bad">Revoked</Pill> : <Pill tone="good">Active</Pill>}</td>
                <td className="right">
                  {!key.revoked_at && (
                    <button type="button" className="btn btn-small btn-danger" onClick={() => revoke(key)}>
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {created && <NewKeyModal secret={created.secret} mcpUrl={session.gateway.mcp_url} onClose={() => setCreated(null)} />}
    </>
  )
}

/** Shown once, right after a key is created. The gateway stores only the key's hash. */
export function NewKeyModal({ secret, mcpUrl, owner, onClose }: { secret: string; mcpUrl: string; owner?: string; onClose: () => void }) {
  const claude = `claude mcp add --transport http urumi ${mcpUrl} --header "Authorization: Bearer ${secret}"`
  const cursor = JSON.stringify(
    { mcpServers: { urumi: { url: mcpUrl, headers: { Authorization: `Bearer ${secret}` } } } },
    null,
    2,
  )
  return (
    <Modal title={owner ? `New key for ${owner}` : 'Your new API key'} onClose={onClose}>
      <div className="form">
        <div className="note note-warn">Copy it now. It is shown only this once; the gateway keeps only a hash of it.</div>
        <div className="snippet">
          <code>{secret}</code>
          <CopyButton text={secret} />
        </div>
        <h3>Claude Code</h3>
        <div className="snippet">
          <code>{claude}</code>
          <CopyButton text={claude} />
        </div>
        <h3>Cursor (.cursor/mcp.json)</h3>
        <div className="snippet">
          <pre>{cursor}</pre>
          <CopyButton text={cursor} />
        </div>
        <h3>MCP Inspector</h3>
        <p className="muted small">
          Transport: Streamable HTTP. URL: <code>{mcpUrl}</code>. Add a header <code>Authorization</code> with the value{' '}
          <code>Bearer &lt;your key&gt;</code>.
        </p>
        <div className="form-actions">
          <button type="button" className="btn btn-primary" onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </Modal>
  )
}
