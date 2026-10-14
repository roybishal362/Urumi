import { useEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import type { Session } from '../App'
import { api, streamChat } from '../api'
import type { ChatEvent } from '../api'
import { errorText, useLoader } from '../lib'
import { PageHeader, Pill } from '../ui'

interface ToolStep {
  id: string
  name: string
  server: string | null
  arguments: Record<string, unknown> | null
  // Filled in when the call finishes.
  done: boolean
  ok?: boolean
  outcome?: string
  durationMs?: number
  preview?: string
}

interface Turn {
  role: 'user' | 'assistant'
  content: string
  steps: ToolStep[]
  error?: string
  // A passing note from the agent, such as "waiting for the rate limit".
  status?: string
  pending?: boolean
}

const SUGGESTIONS = [
  'What orders came in today?',
  'Create a product called "Monsoon Umbrella" priced at 24.99',
  "Convert today's order total to INR and quote shipping a 1 kg parcel to the US",
]

export function ChatPage({ session }: { session: Session }) {
  const [turns, setTurns] = useState<Turn[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [showTools, setShowTools] = useState(false)
  const { data: toolData } = useLoader(() => api.tools(), 8000)
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns])

  /** Apply one streamed event to the assistant turn that is being built. */
  function applyEvent(event: ChatEvent) {
    setTurns((current) => {
      const turns = [...current]
      const last = { ...turns[turns.length - 1], steps: [...turns[turns.length - 1].steps] }
      if (event.type === 'tool_start') {
        last.steps.push({ id: event.id, name: event.name, server: event.server, arguments: event.arguments, done: false })
      } else if (event.type === 'tool_end') {
        last.steps = last.steps.map((step) =>
          step.id === event.id && !step.done
            ? { ...step, done: true, ok: event.ok, outcome: event.outcome, durationMs: event.duration_ms, preview: event.preview }
            : step,
        )
      } else if (event.type === 'status') {
        last.status = event.message
      } else if (event.type === 'answer') {
        last.content = event.content
        last.status = undefined
        last.pending = false
      } else {
        last.error = event.message
        last.pending = false
      }
      turns[turns.length - 1] = last
      return turns
    })
  }

  async function send(text: string) {
    const question = text.trim()
    if (!question || busy) return
    // The gateway keeps no chat history; the browser sends the conversation each time.
    const history = [...turns.filter((turn) => turn.content), { role: 'user' as const, content: question, steps: [] }].map(
      ({ role, content }) => ({ role, content }),
    )
    setTurns([...turns, { role: 'user', content: question, steps: [] }, { role: 'assistant', content: '', steps: [], pending: true }])
    setDraft('')
    setBusy(true)
    try {
      await streamChat(history, applyEvent)
    } catch (err) {
      applyEvent({ type: 'error', message: errorText(err) })
    } finally {
      setBusy(false)
      setTurns((current) => current.map((turn) => (turn.pending ? { ...turn, pending: false } : turn)))
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    void send(draft)
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void send(draft)
    }
  }

  const tools = toolData?.tools ?? []

  return (
    <div className="chat">
      <PageHeader
        title="Chat"
        subtitle={
          <>
            Answers come from your team's tools, called as <strong>{session.user.email}</strong>. Model:{' '}
            <code>{session.gateway.chat_model}</code>
          </>
        }
        action={
          <button type="button" className="btn" onClick={() => setShowTools(!showTools)}>
            {tools.length} tools available {showTools ? '▾' : '▸'}
          </button>
        }
      />
      {!session.gateway.chat_ready && (
        <div className="note note-warn">
          The chat agent has no LLM API key yet. An admin sets it at deploy time (Helm value <code>llm.apiKey</code>).
        </div>
      )}
      {showTools && (
        <div className="card tool-list">
          {tools.length === 0 && <span className="muted">No tools right now. Check the MCP servers page.</span>}
          {tools.map((tool) => (
            <div key={tool.name} title={tool.description}>
              <code>{tool.name}</code> <span className="muted small">{tool.server}</span>
            </div>
          ))}
        </div>
      )}

      <div className="chat-log">
        {turns.length === 0 && (
          <div className="chat-empty">
            <p className="muted">Ask about your store, or try one of these:</p>
            {SUGGESTIONS.map((suggestion) => (
              <button key={suggestion} type="button" className="btn suggestion" onClick={() => void send(suggestion)}>
                {suggestion}
              </button>
            ))}
          </div>
        )}
        {turns.map((turn, index) => (
          <div key={index} className={`turn turn-${turn.role}`}>
            {turn.role === 'user' ? (
              <div className="bubble">{turn.content}</div>
            ) : (
              <div className="answer">
                {turn.steps.map((step) => (
                  <StepView key={step.id} step={step} />
                ))}
                {turn.pending && turn.status && <div className="note note-warn small">{turn.status}</div>}
                {turn.pending && <div className="muted small thinking">{turn.steps.length ? 'Working…' : 'Thinking…'}</div>}
                {turn.content && (
                  <div className="markdown">
                    <Markdown remarkPlugins={[remarkGfm]}>{turn.content}</Markdown>
                  </div>
                )}
                {turn.error && <div className="note note-bad">{turn.error}</div>}
              </div>
            )}
          </div>
        ))}
        <div ref={bottom} />
      </div>

      <form className="composer" onSubmit={onSubmit}>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask something. Enter to send, Shift+Enter for a new line."
          rows={2}
          disabled={busy}
        />
        <button className="btn btn-primary" disabled={busy || !draft.trim()}>
          {busy ? 'Working…' : 'Send'}
        </button>
        {turns.length > 0 && !busy && (
          <button type="button" className="btn" onClick={() => setTurns([])}>
            New chat
          </button>
        )}
      </form>
    </div>
  )
}

/** Show JSON results indented; anything else (or JSON cut short) as it came. */
function readable(text: string | undefined): string {
  if (!text) return ''
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

/** One tool call the agent made: which tool, on which server, how it went. Click to see details. */
function StepView({ step }: { step: ToolStep }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="step">
      <button type="button" className="step-head" onClick={() => setOpen(!open)}>
        <span className="step-icon">{!step.done ? '⏳' : step.ok ? '✓' : '✕'}</span>
        <code>{step.name}</code>
        {step.server && <span className="muted small">{step.server}</span>}
        {step.done && (
          <>
            <Pill tone={step.ok ? 'good' : 'bad'}>{step.outcome}</Pill>
            <span className="muted small">{step.durationMs} ms</span>
          </>
        )}
        <span className="muted small push-right">{open ? 'hide' : 'details'}</span>
      </button>
      {open && (
        <div className="step-body">
          <div className="small muted">Arguments</div>
          <pre>{JSON.stringify(step.arguments ?? {}, null, 2)}</pre>
          {step.done && (
            <>
              <div className="small muted">Result</div>
              <pre>{readable(step.preview)}</pre>
            </>
          )}
        </div>
      )}
    </div>
  )
}
