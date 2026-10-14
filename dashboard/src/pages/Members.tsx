import { useState } from 'react'
import type { FormEvent } from 'react'

import type { Session } from '../App'
import { api } from '../api'
import type { Member, Role } from '../api'
import { errorText, timeAgo, useLoader } from '../lib'
import { CopyButton, ErrorNote, Modal, PageHeader, Pill } from '../ui'
import { NewKeyModal } from './Keys'

const STATE_TONE = { active: 'good', invited: 'warn', deactivated: 'muted' } as const

export function MembersPage({ session }: { session: Session }) {
  const { data, error, reload } = useLoader(() => api.members(), 10000)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<Role>('member')
  const [invite, setInvite] = useState<{ email: string; url: string } | null>(null)
  const [issued, setIssued] = useState<{ email: string; secret: string } | null>(null)
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

  async function sendInvite(event: FormEvent) {
    event.preventDefault()
    await run(async () => {
      const result = await api.invite(email, '', role)
      setInvite({ email: result.member.email, url: result.invite_url })
      setEmail('')
    })
  }

  return (
    <>
      <PageHeader title="Members" subtitle="Admins manage servers and members. Members use the tools." />
      <form className="card inline-form" onSubmit={sendInvite}>
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="teammate@example.com" required />
        <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="member">Member</option>
          <option value="admin">Admin</option>
        </select>
        <button className="btn btn-primary">Invite</button>
      </form>
      <ErrorNote message={error ?? actionError} />

      <div className="card table-card">
        <table>
          <thead>
            <tr>
              <th>Member</th>
              <th>Role</th>
              <th>Status</th>
              <th>Active keys</th>
              <th>Joined</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data?.members.map((member) => (
              <MemberRow
                key={member.id}
                member={member}
                isSelf={member.id === session.user.id}
                run={run}
                onInvite={(url) => setInvite({ email: member.email, url })}
                onIssued={(secret) => setIssued({ email: member.email, secret })}
              />
            ))}
          </tbody>
        </table>
      </div>

      {invite && (
        <Modal title={`Invite link for ${invite.email}`} onClose={() => setInvite(null)}>
          <div className="form">
            <p className="muted">
              Send this link to {invite.email}. It lets them choose a password, works once, and expires in a few days. There
              is no mail server here, so the gateway does not send it for you.
            </p>
            <div className="snippet">
              <code>{invite.url}</code>
              <CopyButton text={invite.url} />
            </div>
            <div className="form-actions">
              <button type="button" className="btn btn-primary" onClick={() => setInvite(null)}>
                Done
              </button>
            </div>
          </div>
        </Modal>
      )}
      {issued && (
        <NewKeyModal secret={issued.secret} mcpUrl={session.gateway.mcp_url} owner={issued.email} onClose={() => setIssued(null)} />
      )}
    </>
  )
}

function MemberRow({
  member,
  isSelf,
  run,
  onInvite,
  onIssued,
}: {
  member: Member
  isSelf: boolean
  run: (action: () => Promise<unknown>) => Promise<void>
  onInvite: (url: string) => void
  onIssued: (secret: string) => void
}) {
  return (
    <tr className={member.state === 'deactivated' ? 'faded' : undefined}>
      <td>
        <div>{member.email}</div>
        {member.name && <div className="muted small">{member.name}</div>}
      </td>
      <td>
        <select
          value={member.role}
          disabled={isSelf}
          onChange={(e) => run(() => api.updateMember(member.id, { role: e.target.value as Role }))}
        >
          <option value="member">Member</option>
          <option value="admin">Admin</option>
        </select>
      </td>
      <td>
        <Pill tone={STATE_TONE[member.state]}>{member.state}</Pill>
      </td>
      <td>{member.active_keys}</td>
      <td>{timeAgo(member.created_at)}</td>
      <td className="right actions">
        {member.state !== 'deactivated' && (
          <button
            type="button"
            className="btn btn-small"
            onClick={() => run(async () => onIssued((await api.createKey('Issued by admin', member.id)).secret))}
          >
            Issue key
          </button>
        )}
        <button
          type="button"
          className="btn btn-small"
          title="A fresh one-time link. Also works as a password reset."
          onClick={() => run(async () => onInvite((await api.renewInvite(member.id)).invite_url))}
        >
          Invite link
        </button>
        {!isSelf && (
          <>
            <button
              type="button"
              className="btn btn-small"
              onClick={() => run(() => api.updateMember(member.id, { is_active: member.state === 'deactivated' }))}
            >
              {member.state === 'deactivated' ? 'Reactivate' : 'Deactivate'}
            </button>
            <button
              type="button"
              className="btn btn-small btn-danger"
              onClick={() => {
                if (window.confirm(`Remove ${member.email}? Their API keys stop working immediately.`)) {
                  void run(() => api.removeMember(member.id))
                }
              }}
            >
              Remove
            </button>
          </>
        )}
      </td>
    </tr>
  )
}
