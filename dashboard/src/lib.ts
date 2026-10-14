// Hooks and formatting helpers shared by the pages.

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from './api'

/**
 * Load data, optionally re-loading on an interval, and again whenever `key`
 * changes (pass the filters the request depends on). Keeps the last good data
 * when a refresh fails.
 */
export function useLoader<T>(load: () => Promise<T>, intervalMs?: number, key = '') {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Always call the newest `load`, without restarting the interval on every render.
  const loadRef = useRef(load)
  useEffect(() => {
    loadRef.current = load
  })

  const reload = useCallback(async () => {
    try {
      setData(await loadRef.current())
      setError(null)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        // The session ended (expired, or the account was deactivated).
        window.location.assign('/login')
        return
      }
      setError(errorText(err))
    }
  }, [])

  useEffect(() => {
    void reload()
    if (!intervalMs) return
    const timer = window.setInterval(() => void reload(), intervalMs)
    return () => window.clearInterval(timer)
  }, [reload, intervalMs, key])

  return { data, error, reload }
}

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : 'Something went wrong.'
}

export function timeAgo(iso: string | null): string {
  if (!iso) return 'never'
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export function clock(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}
