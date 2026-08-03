import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type MouseEvent } from 'react'
import { flushSync } from 'react-dom'
import './App.css'

type Role = 'user' | 'assistant'

type Source = {
  id: string
  note_path: string
  note_title: string
  heading_path: string
  source: string
}

type ChatImagePayload = {
  mime: string
  data_base64: string
}

type ChatMessage = {
  role: Role
  content: string
  sources?: Source[]
  routing?: RoutingInfo
  error?: string
  phase?: 'clarify' | 'advise' | 'out_of_scope'
  imagePreviews?: string[]
  imageCount?: number
}

type RoutingInfo = {
  tag_routing?: boolean
  tag_routing_ready?: boolean
  applied_tags?: string[]
  tag_scores?: Record<string, number>
  scoped?: boolean
  scoped_chunk_count?: number
  fallback_reason?: string | null
  phase?: string
  rag_used?: boolean
  skipped_retrieval?: boolean
}

type ChatThread = {
  id: string
  title: string
  messages: ChatMessage[]
  updatedAt: number
  contextSummary?: string
  expertId?: string
  profileSynced?: boolean
}

type ExpertInfo = {
  id: string
  display_name: string
  avatar_label: string
  short_bio: string
}

/** Separate from Digital Twin so both apps can save threads side-by-side. */
const LS_KEY = 'romance-expert-chat-threads-v1'
const MAX_STORED_THREADS = 50

function newId(): string {
  return crypto.randomUUID()
}

function titleFromMessages(messages: ChatMessage[], fallback: string): string {
  const first = messages.find((m) => m.role === 'user' && m.content.trim())
  if (!first) return fallback
  const line = first.content.trim().split('\n')[0] ?? ''
  if (!line) return fallback
  return line.length > 48 ? `${line.slice(0, 45)}…` : line
}

/** Strip common Markdown markers so plain chat bubbles stay readable. */
function formatChatText(text: string): string {
  return text
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/(^|[\s，。；：、（）()【】])\*([^*\n]+)\*(?=[\s，。；：、（）()【】]|$)/g, '$1$2')
    .replace(/^#{1,6}\s+/gm, '')
}

function loadPersisted(): { threads: ChatThread[]; activeId: string } | null {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return null
    const o = JSON.parse(raw) as { threads?: ChatThread[]; activeId?: string }
    if (!Array.isArray(o.threads) || o.threads.length === 0) return null
    const threads = o.threads.filter(
      (t) => t && typeof t.id === 'string' && Array.isArray(t.messages),
    ) as ChatThread[]
    if (!threads.length) return null
    const activeId = o.activeId && threads.some((t) => t.id === o.activeId) ? o.activeId : threads[0].id
    return { threads, activeId }
  } catch {
    return null
  }
}

function capThreads(list: ChatThread[]): ChatThread[] {
  if (list.length <= MAX_STORED_THREADS) return list
  return [...list].sort((a, b) => b.updatedAt - a.updatedAt).slice(0, MAX_STORED_THREADS)
}

/** Drop base64 previews before local/cloud persistence (keep imageCount). */
function sanitizeThreadsForStorage(threads: ChatThread[]): ChatThread[] {
  return threads.map((t) => ({
    ...t,
    messages: t.messages.map((m) => {
      const count = m.imageCount ?? m.imagePreviews?.length
      const { imagePreviews: _drop, ...rest } = m
      return count ? { ...rest, imageCount: count } : rest
    }),
  }))
}

function useIndexStatus() {
  const [status, setStatus] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const r = await fetch('/api/index/status', { credentials: 'include' })
      setStatus(await r.json())
    } catch {
      setStatus({ error: 'Cannot reach backend' })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { status, loading, refresh }
}

type StreamMeta = {
  sources?: Source[]
  routing?: RoutingInfo
  phase?: 'clarify' | 'advise' | 'out_of_scope'
  context_summary?: string
}

type AppConfig = {
  public_deploy: boolean
  show_sources: boolean
  show_routing: boolean
  allow_index: boolean
  auth_required?: boolean
  auth_mode?: 'none' | 'shared_password' | 'accounts'
  server_chat?: boolean
  username?: string | null
  is_admin?: boolean
  default_expert_id?: string
  experts?: ExpertInfo[]
}

const DEFAULT_CONFIG: AppConfig = {
  public_deploy: false,
  show_sources: false,
  show_routing: false,
  allow_index: false,
  server_chat: false,
  is_admin: false,
  default_expert_id: 'afu',
  experts: [{ id: 'afu', display_name: '阿FU', avatar_label: '阿FU', short_bio: '' }],
}

const MAX_IMAGES = 3
const MAX_IMAGE_EDGE = 1280

async function compressImageFile(file: File): Promise<ChatImagePayload> {
  const bitmap = await createImageBitmap(file)
  const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(bitmap.width, bitmap.height))
  const w = Math.max(1, Math.round(bitmap.width * scale))
  const h = Math.max(1, Math.round(bitmap.height * scale))
  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('无法处理图片')
  ctx.drawImage(bitmap, 0, 0, w, h)
  bitmap.close()
  const dataUrl = canvas.toDataURL('image/jpeg', 0.72)
  const comma = dataUrl.indexOf(',')
  return {
    mime: 'image/jpeg',
    data_base64: comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl,
  }
}

function useAppConfig() {
  const [config, setConfig] = useState<AppConfig>(DEFAULT_CONFIG)
  useEffect(() => {
    fetch('/api/config', { credentials: 'include' })
      .then((r) => r.json())
      .then((o) => setConfig({ ...DEFAULT_CONFIG, ...(o as AppConfig) }))
      .catch(() => setConfig(DEFAULT_CONFIG))
  }, [])
  return config
}

async function streamChat(
  payload: {
    message: string
    history: { role: Role; content: string }[]
    context_summary: string
    images: ChatImagePayload[]
    expert_id?: string
    profile_synced?: boolean
  },
  onMeta: (m: StreamMeta) => void,
  onToken: (t: string) => void,
  onError: (e: string) => void,
  signal?: AbortSignal,
): Promise<'ok' | 'aborted' | 'error'> {
  const waitAbort = (): Promise<'aborted'> =>
    new Promise((resolve) => {
      if (!signal) return
      if (signal.aborted) {
        resolve('aborted')
        return
      }
      signal.addEventListener('abort', () => resolve('aborted'), { once: true })
    })

  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
  try {
    if (signal?.aborted) return 'aborted'

    const fetchPromise = fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(payload),
      signal,
    })

    const fetchRace = await Promise.race([
      fetchPromise.then((res) => ({ kind: 'res' as const, res })),
      waitAbort().then(() => ({ kind: 'aborted' as const })),
    ])
    if (fetchRace.kind === 'aborted') {
      try {
        await fetchPromise
      } catch {
        /* aborted */
      }
      return 'aborted'
    }

    const res = fetchRace.res
    if (res.status === 401) {
      onError('请先登录后再提问。')
      return 'error'
    }
    if (!res.ok || !res.body) {
      let detail = ''
      try {
        detail = (await res.text()).trim().slice(0, 200)
      } catch {
        detail = ''
      }
      if (res.status === 503) {
        onError(
          detail ||
            '服务暂时不可用（503）。常见于部署重启或网关超时，请稍等 1–2 分钟后刷新重试。',
        )
        return 'error'
      }
      onError(detail ? `请求失败（${res.status}）：${detail}` : `请求失败（${res.status}）`)
      return 'error'
    }

    reader = res.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    const abortP = waitAbort()

    while (true) {
      if (signal?.aborted) {
        try {
          await reader.cancel()
        } catch {
          /* ignore */
        }
        return 'aborted'
      }

      const readP = reader.read().then((r) => ({ kind: 'read' as const, ...r }))
      const step = await Promise.race([readP, abortP.then(() => ({ kind: 'aborted' as const }))])
      if (step.kind === 'aborted') {
        try {
          await reader.cancel()
        } catch {
          /* ignore */
        }
        return 'aborted'
      }
      if (step.done) break
      if (signal?.aborted) {
        try {
          await reader.cancel()
        } catch {
          /* ignore */
        }
        return 'aborted'
      }

      buf += dec.decode(step.value, { stream: true })
      for (;;) {
        const nl = buf.indexOf('\n')
        if (nl < 0) break
        const line = buf.slice(0, nl).trim()
        buf = buf.slice(nl + 1)
        if (!line) continue
        if (signal?.aborted) return 'aborted'
        let obj: Record<string, unknown>
        try {
          obj = JSON.parse(line) as Record<string, unknown>
        } catch {
          continue
        }
        if (Array.isArray(obj.sources)) {
          onMeta({
            sources: obj.sources as Source[],
            routing: obj.routing as RoutingInfo | undefined,
            phase: (obj.routing as RoutingInfo | undefined)?.phase as
              | 'clarify'
              | 'advise'
              | undefined,
          })
          continue
        }
        if (obj.meta && typeof obj.meta === 'object') {
          const meta = obj.meta as Record<string, unknown>
          onMeta({
            phase:
              meta.phase === 'clarify' ||
              meta.phase === 'advise' ||
              meta.phase === 'out_of_scope'
                ? meta.phase
                : undefined,
            context_summary:
              typeof meta.context_summary === 'string' ? meta.context_summary : undefined,
            routing:
              typeof meta.rag_used === 'boolean' || typeof meta.phase === 'string'
                ? {
                    rag_used: Boolean(meta.rag_used),
                    phase: typeof meta.phase === 'string' ? meta.phase : undefined,
                  }
                : undefined,
          })
          continue
        }
        if (typeof obj.error === 'string') {
          onError(obj.error)
          return 'error'
        }
        if (typeof obj.text === 'string') {
          if (signal?.aborted) return 'aborted'
          onToken(obj.text)
        }
      }
    }
    return signal?.aborted ? 'aborted' : 'ok'
  } catch (e) {
    if (
      signal?.aborted ||
      (e instanceof DOMException && e.name === 'AbortError') ||
      (e instanceof Error && e.name === 'AbortError')
    ) {
      return 'aborted'
    }
    onError(e instanceof Error ? e.message : String(e))
    return 'error'
  } finally {
    if (reader && signal?.aborted) {
      try {
        await reader.cancel()
      } catch {
        /* ignore */
      }
    }
  }
}

function emptyThread(expertId = 'afu'): ChatThread {
  const id = newId()
  return {
    id,
    title: '新对话',
    messages: [],
    updatedAt: Date.now(),
    expertId,
    profileSynced: false,
  }
}

function normalizeThread(t: ChatThread, defaultExpertId = 'afu'): ChatThread {
  return {
    ...t,
    expertId: (t.expertId || defaultExpertId).trim() || defaultExpertId,
    profileSynced: Boolean(t.profileSynced),
  }
}

function normalizeThreads(list: ChatThread[], defaultExpertId = 'afu'): ChatThread[] {
  return list.map((t) => normalizeThread(t, defaultExpertId))
}

function initialChatState(): { threads: ChatThread[]; activeId: string } {
  const saved = loadPersisted()
  if (saved?.threads.length) {
    const threads = capThreads(normalizeThreads(saved.threads))
    const activeId =
      saved.activeId && threads.some((t) => t.id === saved.activeId)
        ? saved.activeId
        : threads[0]!.id
    return { threads, activeId }
  }
  const t = emptyThread()
  return { threads: [t], activeId: t.id }
}

export default function App() {
  const appConfig = useAppConfig()
  const experts =
    appConfig.experts && appConfig.experts.length > 0
      ? appConfig.experts
      : (DEFAULT_CONFIG.experts as ExpertInfo[])
  const defaultExpertId = appConfig.default_expert_id || 'afu'
  const { status } = useIndexStatus()
  const [threadsLoaded, setThreadsLoaded] = useState(!DEFAULT_CONFIG.server_chat)
  const [pickerExpertId, setPickerExpertId] = useState(defaultExpertId)
  const [boot] = useState(initialChatState)
  const [threads, setThreads] = useState(boot.threads)
  const [activeId, setActiveId] = useState(boot.activeId)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [pendingImages, setPendingImages] = useState<ChatImagePayload[]>([])
  const [pendingPreviews, setPendingPreviews] = useState<string[]>([])
  const [memoryOpen, setMemoryOpen] = useState(false)
  const [memoryText, setMemoryText] = useState('')
  const [memoryLoading, setMemoryLoading] = useState(false)
  const [accountMenuOpen, setAccountMenuOpen] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const accountMenuRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const runIdRef = useRef(0)
  const generatingRef = useRef(false)
  const lastPromptRef = useRef('')
  const suppressSubmitUntilRef = useRef(0)
  const threadsRef = useRef(threads)
  const activeIdRef = useRef(activeId)

  useEffect(() => {
    threadsRef.current = threads
  }, [threads])
  useEffect(() => {
    activeIdRef.current = activeId
  }, [activeId])

  useEffect(() => {
    if (!accountMenuOpen) return
    const onDoc = (ev: Event) => {
      const el = accountMenuRef.current
      if (el && !el.contains(ev.target as Node)) {
        setAccountMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [accountMenuOpen])

  useEffect(() => {
    const ids = new Set(experts.map((e) => e.id))
    setPickerExpertId((prev) => (ids.has(prev) ? prev : defaultExpertId))
  }, [experts, defaultExpertId])

  const serverChat = Boolean(appConfig.server_chat)
  const activeThread = threads.find((t) => t.id === activeId)
  const contextSummary = activeThread?.contextSummary ?? ''
  const activeExpertId = activeThread?.expertId || defaultExpertId
  const activeExpert =
    experts.find((e) => e.id === activeExpertId) ??
    experts[0] ??
    ({
      id: 'afu',
      display_name: '阿FU',
      avatar_label: '阿FU',
      short_bio: '',
    } satisfies ExpertInfo)
  const multiExperts = experts.length > 1

  useEffect(() => {
    if (!serverChat) {
      const saved = loadPersisted()
      if (saved?.threads.length) {
        setThreads(capThreads(normalizeThreads(saved.threads, defaultExpertId)))
        setActiveId(saved.activeId)
      } else {
        const t = emptyThread(defaultExpertId)
        setThreads([t])
        setActiveId(t.id)
      }
      setThreadsLoaded(true)
      return
    }

    let cancelled = false
    fetch('/api/chat/state', { credentials: 'include' })
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status))
        return r.json()
      })
      .then((state: { threads?: ChatThread[]; active_id?: string | null }) => {
        if (cancelled) return
        const list =
          Array.isArray(state.threads) && state.threads.length
            ? capThreads(normalizeThreads(state.threads, defaultExpertId))
            : [emptyThread(defaultExpertId)]
        const aid =
          state.active_id && list.some((t) => t.id === state.active_id)
            ? state.active_id
            : list[0]!.id
        setThreads(list)
        setActiveId(aid)
        const active = list.find((t) => t.id === aid)
        if (active?.expertId) setPickerExpertId(active.expertId)
      })
      .catch(() => {
        if (!cancelled) {
          const t = emptyThread(defaultExpertId)
          setThreads([t])
          setActiveId(t.id)
        }
      })
      .finally(() => {
        if (!cancelled) setThreadsLoaded(true)
      })
    return () => {
      cancelled = true
    }
  }, [serverChat, defaultExpertId])

  useEffect(() => {
    if (!threadsLoaded) return
    const safeThreads = sanitizeThreadsForStorage(threads)
    if (serverChat) {
      const timer = window.setTimeout(() => {
        void fetch('/api/chat/state', {
          method: 'PUT',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ threads: safeThreads, active_id: activeId }),
        })
      }, 700)
      return () => window.clearTimeout(timer)
    }
    localStorage.setItem(LS_KEY, JSON.stringify({ threads: safeThreads, activeId }))
  }, [threads, activeId, serverChat, threadsLoaded])

  const messages = activeThread?.messages ?? []

  const recentsSorted = useMemo(
    () => [...threads].sort((a, b) => b.updatedAt - a.updatedAt),
    [threads],
  )

  const ready = useMemo(() => {
    if (!status) return false
    const map = status.experts as Record<string, { ready?: boolean }> | undefined
    if (map && activeExpertId && typeof map[activeExpertId]?.ready === 'boolean') {
      return Boolean(map[activeExpertId]?.ready)
    }
    if (activeExpertId === 'afu' || activeExpertId === defaultExpertId) {
      return Boolean(status.ready)
    }
    return Boolean(status.ready)
  }, [status, activeExpertId, defaultExpertId])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, sending, activeId])

  const updateActiveMessages = useCallback(
    (fn: (prev: ChatMessage[]) => ChatMessage[]) => {
      setThreads((ts) =>
        capThreads(
          ts.map((t) => {
            if (t.id !== activeId) return t
            const nextMsgs = fn(t.messages)
            const nextTitle =
              t.title === '新对话' ? titleFromMessages(nextMsgs, '新对话') : t.title
            return { ...t, messages: nextMsgs, title: nextTitle, updatedAt: Date.now() }
          }),
        ),
      )
    },
    [activeId],
  )

  function handleNewChat() {
    if (generatingRef.current || sending) stopGeneration()
    const thread = emptyThread(pickerExpertId || defaultExpertId)
    setThreads((ts) => capThreads([thread, ...ts]))
    setActiveId(thread.id)
    setInput('')
    setPendingImages([])
    setPendingPreviews([])
  }

  function handleSelectExpert(nextExpertId: string) {
    const next = (nextExpertId || defaultExpertId).trim() || defaultExpertId
    setPickerExpertId(next)
    const active = threads.find((t) => t.id === activeId)
    if (!active) {
      const thread = emptyThread(next)
      setThreads([thread])
      setActiveId(thread.id)
      return
    }
    // Empty thread: switch expert in place so the next message uses it.
    if (active.messages.length === 0) {
      setThreads((ts) =>
        capThreads(
          ts.map((t) =>
            t.id === activeId ? { ...t, expertId: next, updatedAt: Date.now() } : t,
          ),
        ),
      )
      return
    }
    // Thread already has messages for another expert → open a fresh chat.
    if ((active.expertId || defaultExpertId) === next) return
    if (generatingRef.current || sending) stopGeneration()
    const thread = emptyThread(next)
    setThreads((ts) => capThreads([thread, ...ts]))
    setActiveId(thread.id)
    setInput('')
    setPendingImages([])
    setPendingPreviews([])
  }

  function handleSelectThread(id: string) {
    if (id === activeId) return
    if (generatingRef.current || sending) stopGeneration()
    setActiveId(id)
    const t = threads.find((x) => x.id === id)
    if (t?.expertId) setPickerExpertId(t.expertId)
    setInput('')
    setPendingImages([])
    setPendingPreviews([])
  }

  function handleDeleteThread(idToDelete: string, e: MouseEvent) {
    e.stopPropagation()
    if (sending) return
    const filtered = threads.filter((t) => t.id !== idToDelete)
    const nextList: ChatThread[] =
      filtered.length > 0
        ? capThreads(filtered)
        : [emptyThread(pickerExpertId || defaultExpertId)]
    setThreads(nextList)
    if (activeId === idToDelete) {
      const sorted = [...nextList].sort((a, b) => b.updatedAt - a.updatedAt)
      setActiveId(sorted[0]!.id)
      if (sorted[0]?.expertId) setPickerExpertId(sorted[0].expertId)
    }
    setInput('')
  }

  function toggleProfileSync() {
    if (sending) return
    setThreads((ts) =>
      capThreads(
        ts.map((t) =>
          t.id === activeId
            ? { ...t, profileSynced: !Boolean(t.profileSynced), updatedAt: Date.now() }
            : t,
        ),
      ),
    )
  }

  function stopGeneration() {
    // Idempotent: second call no-ops after cancel.
    if (!generatingRef.current && !abortRef.current) {
      flushSync(() => setSending(false))
      return
    }
    generatingRef.current = false
    runIdRef.current += 1
    // Prevent the same click/pointer sequence from immediately re-submitting
    // after the button swaps from「停止」to「发送」.
    suppressSubmitUntilRef.current = Date.now() + 800
    const controller = abortRef.current
    abortRef.current = null
    try {
      controller?.abort()
    } catch {
      /* ignore */
    }

    const aid = activeIdRef.current
    const msgs = [...(threadsRef.current.find((t) => t.id === aid)?.messages ?? [])]
    let restore = lastPromptRef.current
    if (msgs.length > 0 && msgs[msgs.length - 1]?.role === 'assistant') {
      msgs.pop()
    }
    if (msgs.length > 0 && msgs[msgs.length - 1]?.role === 'user') {
      const userMsg = msgs.pop()!
      restore =
        userMsg.content
          .replace(/\n\[已上传\d+张截图\]$/, '')
          .replace(/^（上传了 \d+ 张截图）$/, '')
          .trim() || restore
    }

    flushSync(() => {
      setSending(false)
      setInput(restore)
      setThreads((ts) =>
        capThreads(
          ts.map((t) => (t.id === aid ? { ...t, messages: msgs, updatedAt: Date.now() } : t)),
        ),
      )
    })
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (Date.now() < suppressSubmitUntilRef.current) return
    const q = input.trim()
    if ((!q && pendingImages.length === 0) || generatingRef.current || sending) return
    const imagesToSend = [...pendingImages]
    const previews = [...pendingPreviews]
    lastPromptRef.current =
      q || (imagesToSend.length ? `（上传了 ${imagesToSend.length} 张截图）` : '')
    setInput('')
    setPendingImages([])
    setPendingPreviews([])

    const history = (activeThread?.messages ?? [])
      .filter((m) => !m.error && m.content.trim())
      .slice(-10)
      .map((m) => ({ role: m.role, content: m.content }))

    const displayContent =
      q || (imagesToSend.length ? `（上传了 ${imagesToSend.length} 张截图）` : '')
    const persistContent =
      imagesToSend.length > 0
        ? `${displayContent}\n[已上传${imagesToSend.length}张截图]`
        : displayContent

    updateActiveMessages((m) => [
      ...m,
      {
        role: 'user',
        content: persistContent,
        imagePreviews: previews,
        imageCount: imagesToSend.length || undefined,
      },
    ])

    const runId = ++runIdRef.current
    generatingRef.current = true
    setSending(true)

    const controller = new AbortController()
    abortRef.current = controller

    let acc = ''
    let sources: Source[] | undefined
    let routing: RoutingInfo | undefined
    let phase: 'clarify' | 'advise' | 'out_of_scope' | undefined
    let hadError = false
    let nextSummary = contextSummary

    const stillActive = () => generatingRef.current && runId === runIdRef.current

    const result = await streamChat(
      {
        message: q,
        history,
        context_summary: contextSummary,
        images: imagesToSend,
        expert_id: activeThread?.expertId || defaultExpertId,
        profile_synced: Boolean(activeThread?.profileSynced),
      },
      (meta) => {
        if (!stillActive()) return
        if (meta.sources) sources = meta.sources
        if (meta.routing) routing = { ...routing, ...meta.routing }
        if (meta.phase) phase = meta.phase
        if (meta.context_summary) nextSummary = meta.context_summary
      },
      (t) => {
        if (!stillActive()) return
        acc += t
        updateActiveMessages((m) => {
          const copy = [...m]
          const last = copy[copy.length - 1]
          if (last?.role === 'assistant' && !last.error) {
            copy[copy.length - 1] = { ...last, content: acc, sources, routing, phase }
          } else {
            copy.push({ role: 'assistant', content: acc, sources, routing, phase })
          }
          return copy
        })
      },
      (err) => {
        if (!stillActive()) return
        hadError = true
        updateActiveMessages((m) => [
          ...m,
          { role: 'assistant', content: '', error: err, sources, routing, phase },
        ])
      },
      controller.signal,
    )

    if (abortRef.current === controller) abortRef.current = null

    // If user already stopped, UI was restored — do not touch state again.
    if (!stillActive() || result === 'aborted') {
      generatingRef.current = false
      flushSync(() => setSending(false))
      return
    }

    generatingRef.current = false

    if (nextSummary && nextSummary !== contextSummary) {
      setThreads((ts) =>
        ts.map((t) => (t.id === activeId ? { ...t, contextSummary: nextSummary } : t)),
      )
    }

    setSending(false)

    if (!hadError && acc.trim() === '') {
      updateActiveMessages((m) => [
        ...m,
        { role: 'assistant', content: '', error: '模型返回为空。', sources, routing, phase },
      ])
    }
  }

  async function openMemoryPanel() {
    setMemoryOpen(true)
    setMemoryLoading(true)
    try {
      const eid = encodeURIComponent(activeExpertId)
      const [profileRes, adviceRes] = await Promise.all([
        fetch('/api/user/memory?scope=profile', { credentials: 'include' }),
        fetch(`/api/user/memory?scope=advice&expert_id=${eid}`, { credentials: 'include' }),
      ])
      if (!profileRes.ok) {
        setMemoryText(
          profileRes.status === 400 ? '登录账户后才会保存跨对话记忆。' : '无法加载记忆。',
        )
        return
      }
      const profile = (await profileRes.json()) as { memory?: string }
      const advice = adviceRes.ok
        ? ((await adviceRes.json()) as { memory?: string })
        : { memory: '' }
      const profileText =
        (profile.memory || '').trim() || '（暂无共享用户画像。有效咨询后会自动积累。）'
      const adviceText =
        (advice.memory || '').trim() ||
        `（暂无「${activeExpert.display_name}」的建议记忆。）`
      setMemoryText(
        `【共享用户画像】\n${profileText}\n\n【${activeExpert.display_name} · 建议记忆（仅本专家）】\n${adviceText}`,
      )
    } catch {
      setMemoryText('无法加载记忆。')
    } finally {
      setMemoryLoading(false)
    }
  }

  async function clearMemory() {
    if (!confirm('确定清空共享用户画像与各专家建议记忆？之后新对话将不再引用旧信息。')) return
    setMemoryLoading(true)
    try {
      const r = await fetch('/api/user/memory', { method: 'DELETE', credentials: 'include' })
      if (!r.ok) {
        alert('清空失败')
        return
      }
      setMemoryText('（暂无长时记忆。有效咨询后会自动积累。）')
    } catch {
      alert('清空失败')
    } finally {
      setMemoryLoading(false)
    }
  }

  async function onPickImages(files: FileList | null) {
    if (!files || files.length === 0) return
    const room = MAX_IMAGES - pendingImages.length
    if (room <= 0) {
      alert(`最多上传 ${MAX_IMAGES} 张截图`)
      return
    }
    const picked = Array.from(files).slice(0, room)
    try {
      const compressed = await Promise.all(picked.map((f) => compressImageFile(f)))
      const urls = compressed.map(
        (c) => `data:${c.mime};base64,${c.data_base64}`,
      )
      setPendingImages((prev) => [...prev, ...compressed].slice(0, MAX_IMAGES))
      setPendingPreviews((prev) => [...prev, ...urls].slice(0, MAX_IMAGES))
    } catch (err) {
      alert(err instanceof Error ? err.message : '图片处理失败')
    }
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo">♥</span>
          <span>Romance Expert</span>
        </div>
        <p className="muted small">你的脱单与亲密关系顾问团</p>

        <div className="expert-picker">
          <div className="status-title">咨询专家</div>
          {multiExperts ? (
            <select
              className="expert-select"
              value={activeExpertId}
              disabled={sending}
              aria-label="选择咨询专家"
              onChange={(e) => handleSelectExpert(e.target.value)}
            >
              {experts.map((ex) => (
                <option key={ex.id} value={ex.id}>
                  {ex.display_name}
                </option>
              ))}
            </select>
          ) : (
            <p className="expert-name">{activeExpert.display_name}</p>
          )}
          {activeExpert.short_bio ? (
            <p className="muted small">{activeExpert.short_bio}</p>
          ) : null}
          <p className="muted small">
            当前对话：{activeExpert.display_name}
            {multiExperts ? '（切换专家会开启对应对话）' : ''}
          </p>
        </div>

        <button
          type="button"
          className="btn new-chat"
          disabled={sending}
          onClick={handleNewChat}
        >
          + 新对话
        </button>

        <div className="recents-section">
          <div className="recents-heading">最近</div>
          <div className="recents-list" role="list">
            {recentsSorted.map((t) => (
              <div
                key={t.id}
                className={`recents-row ${t.id === activeId ? 'active' : ''}`}
                role="listitem"
              >
                <button
                  type="button"
                  className="recents-select"
                  disabled={sending}
                  onClick={() => handleSelectThread(t.id)}
                >
                  <span className="recents-title">{t.title}</span>
                  <span className="recents-meta muted small">
                    {multiExperts
                      ? `${experts.find((e) => e.id === (t.expertId || defaultExpertId))?.display_name ?? t.expertId ?? defaultExpertId} · `
                      : ''}
                    {new Date(t.updatedAt).toLocaleString(undefined, {
                      month: 'short',
                      day: 'numeric',
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </span>
                </button>
                <button
                  type="button"
                  className="recents-delete"
                  title="删除对话"
                  aria-label={`删除对话：${t.title}`}
                  disabled={sending}
                  onClick={(e) => handleDeleteThread(t.id, e)}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        </div>

        <footer className="sidebar-foot">
          <div className="account-dock" ref={accountMenuRef}>
            {accountMenuOpen && appConfig.username ? (
              <div className="account-menu" role="menu">
                <button
                  type="button"
                  className="account-menu-item"
                  role="menuitem"
                  onClick={() => {
                    setAccountMenuOpen(false)
                    void openMemoryPanel()
                  }}
                >
                  <span className="account-menu-icon" aria-hidden>
                    M
                  </span>
                  <span>我的记忆</span>
                  <span className="account-menu-chevron">&gt;</span>
                </button>
                <button
                  type="button"
                  className="account-menu-item"
                  role="menuitem"
                  onClick={() => {
                    window.location.href = '/settings'
                  }}
                >
                  <span className="account-menu-icon" aria-hidden>
                    S
                  </span>
                  <span>设置</span>
                  <span className="account-menu-chevron">&gt;</span>
                </button>
              </div>
            ) : null}
            <button
              type="button"
              className="account-bar"
              onClick={() => {
                if (!appConfig.username) {
                  window.location.href = '/'
                  return
                }
                setAccountMenuOpen((v) => !v)
              }}
            >
              <span className="account-avatar" aria-hidden>
                {(appConfig.username || '登').slice(0, 1)}
              </span>
              <span className="account-name">
                {appConfig.username || '登录'}
              </span>
            </button>
          </div>
          {serverChat && appConfig.username ? (
            <p className="muted small account-cloud-hint">对话已云端保存</p>
          ) : null}
        </footer>
      </aside>

      {memoryOpen ? (
        <div
          className="memory-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="我的记忆"
          onClick={() => setMemoryOpen(false)}
        >
          <div className="memory-panel" onClick={(e) => e.stopPropagation()}>
            <div className="memory-head">
              <h2>我的记忆</h2>
              <button type="button" className="btn secondary" onClick={() => setMemoryOpen(false)}>
                关闭
              </button>
            </div>
            <p className="muted small">
              共享用户画像可跨专家；各专家的建议记忆相互隔离。点「同步用户信息」后，当前专家才会读取画像。
            </p>
            <pre className="memory-body">{memoryLoading ? '加载中…' : memoryText}</pre>
            <div className="memory-actions">
              <button
                type="button"
                className="btn secondary"
                disabled={memoryLoading}
                onClick={() => void clearMemory()}
              >
                清空记忆
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <main className="chat-panel">
        <header className="topbar">
          <div className="topbar-left">
            <button
              type="button"
              className="btn new-chat mobile-only"
              disabled={sending}
              onClick={handleNewChat}
            >
              + 新对话
            </button>
            <h1>
              {activeThread?.title ?? '对话'}
              {activeThread?.title && activeThread.title !== '新对话' ? (
                <span className="topbar-expert muted"> · {activeExpert.display_name}</span>
              ) : null}
            </h1>
          </div>
          {appConfig.is_admin ? (
            <button
              type="button"
              className="btn secondary topbar-admin"
              onClick={() => {
                window.location.href = '/admin'
              }}
            >
              运营后台
            </button>
          ) : null}
        </header>

        <div className="thread" ref={scrollRef}>
          {messages.length === 0 ? (
            <div className="empty">
              <h2>{activeExpert.display_name}</h2>
              <p className="muted">
                {ready
                  ? activeExpert.short_bio || '亲密关系咨询'
                  : '知识库尚未就绪，请稍后再试。'}
              </p>
            </div>
          ) : (
            messages.map((msg, i) => (
              <div key={`${activeId}-${i}-${msg.role}`} className={`bubble-row ${msg.role}`}>
                <div className="avatar">
                  {msg.role === 'user' ? '我' : activeExpert.avatar_label}
                </div>
                <div className={`bubble ${msg.role}`}>
                  {msg.phase === 'clarify' ? (
                    <div className="phase-tag">追问中</div>
                  ) : msg.phase === 'advise' ? (
                    <div className="phase-tag advise">建议</div>
                  ) : msg.phase === 'out_of_scope' ? (
                    <div className="phase-tag">仅限亲密关系话题</div>
                  ) : null}
                  {msg.imagePreviews && msg.imagePreviews.length > 0 ? (
                    <div className="shot-row">
                      {msg.imagePreviews.map((src) => (
                        <img key={src.slice(0, 48)} src={src} alt="截图" className="shot-thumb" />
                      ))}
                    </div>
                  ) : msg.imageCount ? (
                    <p className="muted small">含 {msg.imageCount} 张截图</p>
                  ) : null}
                  {msg.error ? (
                    <p className="err">{msg.error}</p>
                  ) : (
                    <div className="md">
                      {formatChatText(
                        msg.content || (sending && msg.role === 'assistant' ? '…' : ''),
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))
          )}
        </div>

        <form className="composer" onSubmit={(e) => void handleSubmit(e)}>
          {pendingPreviews.length > 0 ? (
            <div className="composer-shots">
              {pendingPreviews.map((src, idx) => (
                <button
                  key={src.slice(0, 40) + idx}
                  type="button"
                  className="shot-chip"
                  title="移除"
                  onClick={() => {
                    setPendingImages((p) => p.filter((_, i) => i !== idx))
                    setPendingPreviews((p) => p.filter((_, i) => i !== idx))
                  }}
                >
                  <img src={src} alt="" />
                  <span>×</span>
                </button>
              ))}
            </div>
          ) : null}
          <div className="composer-row">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif"
              multiple
              hidden
              onChange={(e) => {
                void onPickImages(e.target.files)
                e.target.value = ''
              }}
            />
            <div className="composer-tools">
              <button
                type="button"
                className="btn secondary attach-btn"
                disabled={!ready || sending || !threadsLoaded || pendingImages.length >= MAX_IMAGES}
                onClick={() => fileInputRef.current?.click()}
                title="上传聊天截图"
              >
                截图
              </button>
              <button
                type="button"
                className={`btn secondary sync-btn${activeThread?.profileSynced ? ' on' : ''}`}
                disabled={!threadsLoaded || sending}
                onClick={toggleProfileSync}
                title="同步后，专家可了解你已在其他专家处提供的个人信息和经历，但不会得知其他专家的建议"
              >
                {activeThread?.profileSynced ? '已同步' : '同步信息'}
              </button>
            </div>
            <textarea
              className="input"
              rows={2}
              placeholder={
                sending
                  ? '生成中…可点「停止」后修改问题'
                  : ready
                    ? `向${activeExpert.display_name}描述你的情况…（可先上传截图）信息不够我会追问`
                    : '知识库尚未就绪'
              }
              value={input}
              disabled={!ready || !threadsLoaded}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  if (Date.now() < suppressSubmitUntilRef.current) return
                  if (!sending && !generatingRef.current) void handleSubmit(e)
                }
              }}
            />
            <div className="composer-actions">
              {/* Both mounted so swapping「停止」/「发送」won't re-fire submit on the same click. */}
              <button
                type="button"
                className="btn send stop"
                hidden={!sending}
                tabIndex={sending ? 0 : -1}
                onClick={(ev) => {
                  ev.preventDefault()
                  ev.stopPropagation()
                  stopGeneration()
                }}
              >
                停止
              </button>
              <button
                type="submit"
                className="btn send"
                hidden={sending}
                tabIndex={sending ? -1 : 0}
                disabled={
                  !ready ||
                  !threadsLoaded ||
                  sending ||
                  (!input.trim() && pendingImages.length === 0)
                }
                onClick={(ev) => {
                  if (Date.now() < suppressSubmitUntilRef.current) {
                    ev.preventDefault()
                    ev.stopPropagation()
                  }
                }}
              >
                发送
              </button>
            </div>
          </div>
          {!activeThread?.profileSynced ? (
            <p className="composer-hint muted small">
              未同步你的信息。同步后，专家可了解你已在其他专家处提供的个人信息和经历，但不会得知其他专家的建议
            </p>
          ) : (
            <p className="composer-hint muted small">
              已同步你的信息。专家可了解你已在其他专家处提供的个人信息和经历，但不会得知其他专家的建议
            </p>
          )}
        </form>
      </main>
    </div>
  )
}
