import { useState } from 'react'

import type { Session } from '../App'
import { api } from '../api'
import type { AuditEntry } from '../api'
import { clock, useLoader } from '../lib'
import { ErrorNote, PageHeader, Pill } from '../ui'

const OUTCOME_TONE: Record<AuditEntry['outcome'], 'good' | 'bad' | 'warn' | 'muted'> = {
  success: 'good',
  error: 'bad',
  timeout: 'warn',
  unreachable: 'bad',
  denied: 'warn',
  rejected: 'bad',
}

const OUTCOMES = ['success', 'error', 'timeout', 'unreachable', 'denied', 'rejected']

export function AuditPage({ session }: { session: Session }) {
  const isAdmin = session.user.role === 'admin'
  const [outcome, setOutcome] = useState('')
  const [server, setServer] = useState('')
  const [userEmail, setUserEmail] = useState('')
  const [limit, setLimit] = useState(100)
  const [live, setLive] = useState(true)
  const { data, error } = useLoader(
    () => api.audit({ limit, outcome, server, user_email: userEmail }),
    live ? 3000 : undefined,
    [limit, outcome, server, userEmail].join('|'),
  )
  const { data: stats } = useLoader(() => api.stats(24), live ? 10000 : undefined)
  // Filter choices come from what is in the log, so they need no extra API.
  const servers = [...new Set(data?.entries.map((entry) => entry.server).filter(Boolean))].sort()

  return (
    <>
      <PageHeader
        title="Audit log"
        subtitle={
          isAdmin
            ? 'Every tool call made through the gateway, by anyone, from chat or an MCP client.'
            : 'Every tool call you have made through the gateway.'
        }
      />
      {stats && stats.servers.length > 0 && (
        <div className="card table-card">
          <table>
            <thead>
              <tr>
                <th>Last 24 hours, by server</th>
                <th className="right">Calls</th>
                <th className="right">Errors</th>
                <th className="right">Error rate</th>
                <th className="right">Median</th>
                <th className="right">95th percentile</th>
                <th className="right" title="Calls refused before reaching the server: revoked keys, admin-only tools">
                  Refused
                </th>
              </tr>
            </thead>
            <tbody>
              {stats.servers.map((s) => (
                <tr key={s.server}>
                  <td className="mono">{s.server}</td>
                  <td className="right">{s.calls}</td>
                  <td className="right">{s.errors}</td>
                  <td className="right">
                    <Pill tone={s.error_rate === 0 ? 'good' : s.error_rate < 0.1 ? 'warn' : 'bad'}>
                      {(s.error_rate * 100).toFixed(0)}%
                    </Pill>
                  </td>
                  <td className="right">{s.p50_ms === null ? '—' : `${s.p50_ms} ms`}</td>
                  <td className="right">{s.p95_ms === null ? '—' : `${s.p95_ms} ms`}</td>
                  <td className="right">{s.refused}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="card inline-form">
        <select value={outcome} onChange={(e) => setOutcome(e.target.value)} aria-label="Outcome">
          <option value="">Any outcome</option>
          {OUTCOMES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
        <select value={server} onChange={(e) => setServer(e.target.value)} aria-label="Server">
          <option value="">Any server</option>
          {server && !servers.includes(server) && <option value={server}>{server}</option>}
          {servers.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
        {isAdmin && (
          <input value={userEmail} onChange={(e) => setUserEmail(e.target.value)} placeholder="Filter by user email" />
        )}
        <label className="check push-right">
          <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
          Live
        </label>
      </div>
      <ErrorNote message={error} />
      <div className="card table-card">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Who</th>
              <th>Via</th>
              <th>Server</th>
              <th>Tool</th>
              <th className="right">Took</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {data?.entries.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  Nothing here yet. Tool calls appear as they happen.
                </td>
              </tr>
            )}
            {data?.entries.map((entry) => (
              <tr key={entry.id}>
                <td className="nowrap">{clock(entry.ts)}</td>
                <td>{entry.user_email}</td>
                <td className="nowrap">
                  {entry.source === 'chat' ? 'Chat agent' : 'MCP client'}
                  {entry.key_prefix && <span className="muted small mono"> {entry.key_prefix}…</span>}
                </td>
                <td className="mono">{entry.server || '—'}</td>
                <td className="mono">{entry.tool}</td>
                <td className="right nowrap">{entry.duration_ms} ms</td>
                <td>
                  <Pill tone={OUTCOME_TONE[entry.outcome] ?? 'muted'}>{entry.outcome}</Pill>
                  {entry.error && <div className="muted small audit-error">{entry.error}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data && data.entries.length >= limit && (
        <button type="button" className="btn" onClick={() => setLimit(limit + 200)}>
          Show older entries
        </button>
      )}
    </>
  )
}
