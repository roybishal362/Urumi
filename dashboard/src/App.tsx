import { useCallback, useEffect, useState } from 'react'

import { api } from './api'
import type { GatewayInfo, User } from './api'
import { AuditPage } from './pages/Audit'
import { ChatPage } from './pages/Chat'
import { KeysPage } from './pages/Keys'
import { AcceptInvitePage, LoginPage } from './pages/Login'
import { MembersPage } from './pages/Members'
import { ServersPage } from './pages/Servers'

export interface Session {
  user: User
  gateway: GatewayInfo
}

const PAGES = [
  { path: '/chat', label: 'Chat', adminOnly: false },
  { path: '/servers', label: 'MCP servers', adminOnly: false },
  { path: '/keys', label: 'API keys', adminOnly: false },
  { path: '/members', label: 'Members', adminOnly: true },
  { path: '/audit', label: 'Audit log', adminOnly: false },
]

/** A tiny router: the current path, and a function to change it without a page load. */
function usePath(): [string, (path: string) => void] {
  const [path, setPath] = useState(window.location.pathname)
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname)
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  const navigate = useCallback((next: string) => {
    window.history.pushState(null, '', next)
    setPath(next)
  }, [])
  return [path, navigate]
}

export default function App() {
  const [path, navigate] = usePath()
  // undefined: still asking the gateway. null: not signed in.
  const [session, setSession] = useState<Session | null | undefined>(undefined)

  const loadSession = useCallback(async () => {
    try {
      setSession(await api.me())
    } catch {
      setSession(null)
    }
  }, [])

  // Ask the gateway once, on first render, whether there is a session.
  useEffect(() => {
    let cancelled = false
    api.me().then(
      (me) => !cancelled && setSession(me),
      () => !cancelled && setSession(null),
    )
    return () => {
      cancelled = true
    }
  }, [])

  if (path === '/accept-invite') {
    return (
      <AcceptInvitePage
        onDone={async () => {
          await loadSession()
          navigate('/keys')
        }}
      />
    )
  }
  if (session === undefined) return <div className="center muted">Loading…</div>
  if (session === null) {
    return (
      <LoginPage
        onDone={async () => {
          await loadSession()
          navigate('/servers')
        }}
      />
    )
  }

  const isAdmin = session.user.role === 'admin'
  const pages = PAGES.filter((page) => isAdmin || !page.adminOnly)
  const current = pages.find((page) => page.path === path)?.path ?? '/servers'

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">U</span>
          <div>
            <div className="brand-name">Urumi Gateway</div>
            <div className="brand-sub">Team MCP hub</div>
          </div>
        </div>
        <nav>
          {pages.map((page) => (
            <a
              key={page.path}
              href={page.path}
              className={page.path === current ? 'nav-link active' : 'nav-link'}
              onClick={(event) => {
                event.preventDefault()
                navigate(page.path)
              }}
            >
              {page.label}
            </a>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="who">
            <div className="who-email" title={session.user.email}>
              {session.user.email}
            </div>
            <div className="muted small">{session.user.role}</div>
          </div>
          <button
            type="button"
            className="btn btn-small"
            onClick={async () => {
              await api.logout()
              setSession(null)
              navigate('/login')
            }}
          >
            Sign out
          </button>
        </div>
      </aside>
      <main className="main">
        {current === '/chat' && <ChatPage session={session} />}
        {current === '/servers' && <ServersPage session={session} />}
        {current === '/keys' && <KeysPage session={session} />}
        {current === '/members' && isAdmin && <MembersPage session={session} />}
        {current === '/audit' && <AuditPage session={session} />}
      </main>
    </div>
  )
}
