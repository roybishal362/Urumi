import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'

import { api } from '../api'
import { errorText } from '../lib'
import { ErrorNote, Field } from '../ui'

export function LoginPage({ onDone }: { onDone: () => Promise<void> }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.login(email, password)
      await onDone()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="center">
      <form className="card auth-card" onSubmit={submit}>
        <div className="brand">
          <span className="brand-mark">U</span>
          <div>
            <div className="brand-name">Urumi Gateway</div>
            <div className="brand-sub">Sign in to your team's MCP hub</div>
          </div>
        </div>
        <ErrorNote message={error} />
        <Field label="Email">
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus required autoComplete="username" />
        </Field>
        <Field label="Password">
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            autoComplete="current-password"
          />
        </Field>
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}

export function AcceptInvitePage({ onDone }: { onDone: () => Promise<void> }) {
  const token = new URLSearchParams(window.location.search).get('token') ?? ''
  const [email, setEmail] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api
      .inviteDetails(token)
      .then((invite) => {
        setEmail(invite.email)
        setName(invite.name)
      })
      .catch((err) => setError(errorText(err)))
  }, [token])

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.acceptInvite(token, name, password)
      await onDone()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="center">
      <form className="card auth-card" onSubmit={submit}>
        <h1>Join the team</h1>
        <ErrorNote message={error} />
        {email && (
          <>
            <p className="muted">
              You were invited as <strong>{email}</strong>. Choose a password to finish.
            </p>
            <Field label="Your name">
              <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
            </Field>
            <Field label="Password" hint="At least 10 characters.">
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={10}
                required
                autoComplete="new-password"
              />
            </Field>
            <button className="btn btn-primary" disabled={busy}>
              {busy ? 'Saving…' : 'Create account'}
            </button>
          </>
        )}
      </form>
    </div>
  )
}
