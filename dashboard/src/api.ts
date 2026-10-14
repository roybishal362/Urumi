// Everything the dashboard knows about the gateway's REST API.
//
// The browser authenticates with an HttpOnly session cookie that the gateway
// sets at sign-in, so there is no token for this code to hold or leak. No
// upstream credential ever appears in these types: the API does not return them.

export type Role = 'admin' | 'member'

export interface User {
  id: string
  email: string
  name: string
  role: Role
}

export interface GatewayInfo {
  mcp_url: string
  chat_ready: boolean
  chat_model: string
}

export interface ToolInfo {
  name: string // as exposed by the gateway, e.g. woo__list_orders
  tool: string // the upstream's own name
  description: string
  admin_only: boolean
  // The server changed this tool after it was approved; hidden until an admin reviews it.
  changed: boolean
}

export interface ToolDefinition {
  title: string | null
  description: string
  input_schema: unknown
  output_schema: unknown
  annotations: unknown
}

export type ServerStatus = 'connected' | 'unreachable' | 'disabled' | 'connecting'
export type AuthType = 'none' | 'bearer' | 'header'

export interface Server {
  id: string
  slug: string
  name: string
  enabled: boolean
  status: ServerStatus
  error: string | null
  since: string | null
  last_ok: string | null
  protocol_version: string | null
  // The most recent stretch of unreachability, kept after the server recovers.
  last_outage: { started: string; ended: string; seconds: number; reason: string } | null
  outage_count: number
  tools: ToolInfo[]
  // Present for admins only.
  url?: string
  auth_type?: AuthType
  auth_header?: string | null
  has_credential?: boolean
}

export interface ServerInput {
  name: string
  slug?: string
  url: string
  auth_type: AuthType
  auth_header?: string | null
  credential?: string
  enabled?: boolean
}

export interface Member {
  id: string
  email: string
  name: string
  role: Role
  state: 'active' | 'invited' | 'deactivated'
  active_keys: number
  created_at: string
}

export interface ApiKey {
  id: string
  name: string
  prefix: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
  user_id: string
  user_email?: string
  // Set when an admin created the key for someone else.
  issued_by: string | null
}

export interface AuditEntry {
  id: number
  ts: string
  user_email: string
  key_prefix: string | null
  source: 'mcp' | 'chat'
  server: string
  tool: string
  duration_ms: number
  outcome: 'success' | 'error' | 'timeout' | 'unreachable' | 'denied' | 'rejected'
  error: string | null
}

export interface ServerStats {
  server: string
  calls: number
  refused: number
  errors: number
  error_rate: number
  p50_ms: number | null
  p95_ms: number | null
}

export interface AvailableTool {
  name: string
  server: string
  description: string
  admin_only: boolean
}

export type ChatEvent =
  | { type: 'tool_start'; id: string; name: string; server: string | null; arguments: Record<string, unknown> | null }
  | { type: 'tool_end'; id: string; name: string; ok: boolean; outcome: string; duration_ms: number; preview: string }
  | { type: 'status'; message: string }
  | { type: 'answer'; content: string }
  | { type: 'error'; message: string }

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function describe(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail
  // FastAPI reports validation problems as a list of {loc, msg}.
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const field = Array.isArray(d?.loc) ? d.loc[d.loc.length - 1] : ''
        return field ? `${field}: ${d?.msg}` : String(d?.msg ?? '')
      })
      .join('; ')
  }
  return fallback
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    let detail: unknown
    try {
      detail = (await response.json()).detail
    } catch {
      detail = undefined
    }
    throw new ApiError(response.status, describe(detail, `Request failed (${response.status})`))
  }
  return (await response.json()) as T
}

export const api = {
  me: () => request<{ user: User; gateway: GatewayInfo }>('GET', '/api/auth/me'),
  login: (email: string, password: string) => request<{ user: User }>('POST', '/api/auth/login', { email, password }),
  logout: () => request<{ ok: boolean }>('POST', '/api/auth/logout'),
  inviteDetails: (token: string) =>
    request<{ email: string; name: string; role: Role }>('GET', `/api/auth/invite/${encodeURIComponent(token)}`),
  acceptInvite: (token: string, name: string, password: string) =>
    request<{ user: User }>('POST', '/api/auth/accept-invite', { token, name, password }),

  servers: () => request<{ servers: Server[] }>('GET', '/api/servers'),
  addServer: (input: ServerInput) => request<{ server: Server }>('POST', '/api/servers', input),
  updateServer: (id: string, input: Partial<ServerInput>) => request<{ server: Server }>('PATCH', `/api/servers/${id}`, input),
  removeServer: (id: string) => request<{ ok: boolean }>('DELETE', `/api/servers/${id}`),
  refreshServer: (id: string) => request<{ server: Server }>('POST', `/api/servers/${id}/refresh`),
  setToolPolicy: (id: string, tool: string, adminOnly: boolean) =>
    request<{ server: Server }>('PUT', `/api/servers/${id}/tool-policy`, { tool, admin_only: adminOnly }),
  reviewTool: (id: string, tool: string) =>
    request<{ approved: ToolDefinition | null; current: ToolDefinition }>('POST', `/api/servers/${id}/tool-review`, { tool }),
  approveTool: (id: string, tool: string) => request<{ server: Server }>('POST', `/api/servers/${id}/tool-approve`, { tool }),

  members: () => request<{ members: Member[] }>('GET', '/api/members'),
  invite: (email: string, name: string, role: Role) =>
    request<{ member: Member; invite_url: string }>('POST', '/api/members/invite', { email, name, role }),
  renewInvite: (id: string) => request<{ member: Member; invite_url: string }>('POST', `/api/members/${id}/invite`),
  updateMember: (id: string, change: { role?: Role; is_active?: boolean }) =>
    request<{ member: Member }>('PATCH', `/api/members/${id}`, change),
  removeMember: (id: string) => request<{ ok: boolean }>('DELETE', `/api/members/${id}`),

  keys: (everyone: boolean) => request<{ keys: ApiKey[] }>('GET', `/api/keys${everyone ? '?everyone=true' : ''}`),
  createKey: (name: string, userId?: string) =>
    request<{ key: ApiKey; secret: string }>('POST', '/api/keys', { name, user_id: userId }),
  revokeKey: (id: string) => request<{ key: ApiKey }>('DELETE', `/api/keys/${id}`),

  audit: (params: { limit?: number; before_id?: number; outcome?: string; server?: string; user_email?: string }) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') query.set(key, String(value))
    }
    return request<{ entries: AuditEntry[] }>('GET', `/api/audit?${query}`)
  },
  tools: () => request<{ tools: AvailableTool[] }>('GET', '/api/tools'),
  stats: (hours = 24) => request<{ hours: number; servers: ServerStats[] }>('GET', `/api/stats?hours=${hours}`),
}

/**
 * Send the conversation to the chat agent and call `onEvent` for each step as
 * it happens. The gateway streams one JSON object per line.
 */
export async function streamChat(
  messages: { role: 'user' | 'assistant'; content: string }[],
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch('/api/chat', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
    signal,
  })
  if (!response.ok || !response.body) {
    throw new ApiError(response.status, `The chat request failed (${response.status})`)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let newline = buffer.indexOf('\n')
    while (newline >= 0) {
      const line = buffer.slice(0, newline).trim()
      buffer = buffer.slice(newline + 1)
      if (line) onEvent(JSON.parse(line) as ChatEvent)
      newline = buffer.indexOf('\n')
    }
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer) as ChatEvent)
}
