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
}

const DEFAULT_CONFIG: AppConfig = {
  public_deploy: false,
  show_sources: true,
  show_routing: true,
  allow_index: true,
  server_chat: false,
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

function emptyThread(): ChatThread {
  const id = newId()
  return { id, title: '新对话', messages: [], updatedAt: Date.now() }
}

export default function App() {
  const appConfig = useAppConfig()
  const { status, loading: statusLoading, refresh } = useIndexStatus()
  const [indexing, setIndexing] = useState(false)
  const [threadsLoaded, setThreadsLoaded] = useState(!DEFAULT_CONFIG.server_chat)
  const [threads, setThreads] = useState<ChatThread[]>(() => {
    const saved = loadPersisted()
    if (saved?.threads.length) return capThreads(saved.threads)
    return [emptyThread()]
  })
  const [activeId, setActiveId] = useState(() => {
    const saved = loadPersisted()
    if (saved?.activeId) return saved.activeId
    return newId()
  })
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [pendingImages, setPendingImages] = useState<ChatImagePayload[]>([])
  const [pendingPreviews, setPendingPreviews] = useState<string[]>([])
  const [memoryOpen, setMemoryOpen] = useState(false)
  const [memoryText, setMemoryText] = useState('')
  const [memoryLoading, setMemoryLoading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
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

  const serverChat = Boolean(appConfig.server_chat)
  const activeThread = threads.find((t) => t.id === activeId)
  const contextSummary = activeThread?.contextSummary ?? ''

  useEffect(() => {
    if (!serverChat) {
      const saved = loadPersisted()
      if (saved?.threads.length) {
        setThreads(capThreads(saved.threads))
        setActiveId(saved.activeId)
      } else {
        const t = emptyThread()
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
            ? capThreads(state.threads)
            : [emptyThread()]
        const aid =
          state.active_id && list.some((t) => t.id === state.active_id)
            ? state.active_id
            : list[0]!.id
        setThreads(list)
        setActiveId(aid)
      })
      .catch(() => {
        if (!cancelled) {
          const t = emptyThread()
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
  }, [serverChat])

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

  const ready = Boolean(status?.ready)
  const metaLine = useMemo(() => {
    if (!status) return '正在连接…'
    const chunks = status.chunk_count as number | undefined
    const vec = status.vector_enabled ? '向量 + BM25' : '仅 BM25'
    const at = status.last_indexed_at as string | undefined
    if (appConfig.public_deploy) {
      return `${ready ? '已就绪' : '服务暂不可用'} · ${chunks ?? 0} 个知识片段${at ? ` · ${at}` : ''}`
    }
    const tagN = Number(status.tag_count ?? 0)
    const tagsLine =
      Boolean(status.tag_routing_ready) && tagN > 0
        ? ` · ${tagN} 个标签（路由已启用）`
        : tagN > 0
          ? ` · ${tagN} 个标签（重建后可路由）`
          : ''
    return `${ready ? '已就绪' : '未索引'} · ${chunks ?? 0} 个切片 · ${vec}${tagsLine}${at ? ` · ${at}` : ''}`
  }, [status, ready, appConfig.public_deploy])

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
    const id = newId()
    const thread: ChatThread = { id, title: '新对话', messages: [], updatedAt: Date.now() }
    setThreads((ts) => capThreads([thread, ...ts]))
    setActiveId(id)
    setInput('')
    setPendingImages([])
    setPendingPreviews([])
  }

  function handleSelectThread(id: string) {
    if (id === activeId) return
    if (generatingRef.current || sending) stopGeneration()
    setActiveId(id)
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
        : [{ id: newId(), title: '新对话', messages: [], updatedAt: Date.now() }]
    setThreads(nextList)
    if (activeId === idToDelete) {
      const sorted = [...nextList].sort((a, b) => b.updatedAt - a.updatedAt)
      setActiveId(sorted[0]!.id)
    }
    setInput('')
  }

  async function handleIndex() {
    setIndexing(true)
    try {
      const r = await fetch('/api/index', { method: 'POST', credentials: 'include' })
      if (!r.ok) {
        const d = (await r.json().catch(() => null)) as { detail?: unknown } | null
        const detail =
          typeof d?.detail === 'string' ? d.detail : JSON.stringify(d?.detail ?? {})
        throw new Error(detail || r.statusText)
      }
      await refresh()
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e))
    } finally {
      setIndexing(false)
    }
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
      const r = await fetch('/api/user/memory', { credentials: 'include' })
      if (!r.ok) {
        setMemoryText(r.status === 400 ? '登录账户后才会保存跨对话记忆。' : '无法加载记忆。')
        return
      }
      const data = (await r.json()) as { memory?: string }
      setMemoryText((data.memory || '').trim() || '（暂无长时记忆。有效咨询后会自动积累。）')
    } catch {
      setMemoryText('无法加载记忆。')
    } finally {
      setMemoryLoading(false)
    }
  }

  async function clearMemory() {
    if (!confirm('确定清空跨对话长时记忆？之后新对话将不再引用旧对象信息。')) return
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
        <p className="muted small">
          {appConfig.public_deploy
            ? '亲密关系顾问 · 在线版'
            : '亲密关系 RAG · 仅索引「关于亲密关系」文件夹'}
        </p>

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

        {appConfig.allow_index ? (
          <div className="status-card">
            <div className="status-title">索引</div>
            <p className="status-body">{statusLoading ? '加载中…' : metaLine}</p>
            {(status?.error as string | undefined)?.length ? (
              <p className="warn small">{String(status?.error)}</p>
            ) : null}
            <button
              type="button"
              className="btn secondary"
              disabled={indexing}
              onClick={() => void handleIndex()}
            >
              {indexing ? '正在构建…' : '构建索引'}
            </button>
          </div>
        ) : (
          <div className="status-card">
            <div className="status-title">状态</div>
            <p className="status-body">{statusLoading ? '加载中…' : metaLine}</p>
          </div>
        )}

        <footer className="sidebar-foot muted small">
          {appConfig.username ? (
            <span>
              已登录：<strong>{appConfig.username}</strong>
              {serverChat ? ' · 对话已云端保存' : ''}
            </span>
          ) : (
            <span>聊天：Kimi · 知识库检索仅在建议阶段</span>
          )}
          {appConfig.username ? (
            <button
              type="button"
              className="btn secondary logout-btn"
              onClick={() => void openMemoryPanel()}
            >
              我的记忆
            </button>
          ) : null}
          {appConfig.auth_required ? (
            <button
              type="button"
              className="btn secondary logout-btn"
              onClick={() => {
                void fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).then(
                  () => window.location.reload(),
                )
              }}
            >
              退出登录
            </button>
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
              跨对话自动积累的用户画像与对象档案，仅你的账户可见。
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
            <h1>{activeThread?.title ?? '对话'}</h1>
          </div>
        </header>

        <div className="thread" ref={scrollRef}>
          {messages.length === 0 ? (
            <div className="empty">
              <h2>亲密关系咨询</h2>
              <p className="muted">
                {ready
                  ? '可以直接提问。信息不够时我会先追问；也可上传聊天截图。'
                  : appConfig.allow_index
                    ? '请先构建索引，然后开始对话。'
                    : '知识库尚未就绪，请稍后再试。'}
              </p>
            </div>
          ) : (
            messages.map((msg, i) => (
              <div key={`${activeId}-${i}-${msg.role}`} className={`bubble-row ${msg.role}`}>
                <div className="avatar">{msg.role === 'user' ? '我' : '阿FU'}</div>
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
                  {appConfig.show_routing && msg.role === 'assistant' && msg.routing?.tag_routing ? (
                    <div className="routing-hint muted small">
                      {(msg.routing.applied_tags?.length ?? 0) > 0 ? (
                        <>
                          <strong>标签收窄</strong>：{msg.routing.applied_tags!.join('、')}
                          {msg.routing.scoped ? (
                            <span> （仅限带上述标签的笔记检索）</span>
                          ) : (
                            <span>
                              {msg.routing.fallback_reason
                                ? ` （未收窄：${msg.routing.fallback_reason}）`
                                : ' （未收窄）'}
                            </span>
                          )}
                        </>
                      ) : msg.routing.tag_routing_ready === false &&
                        msg.routing.fallback_reason === 'rebuild_index_for_tag_router' ? (
                        <span>
                          点击 <strong>构建索引</strong> 后可启用标签语义路由。
                        </span>
                      ) : msg.routing.fallback_reason ? (
                        <span>标签路由：{msg.routing.fallback_reason}</span>
                      ) : null}
                    </div>
                  ) : null}
                  {appConfig.show_sources && msg.sources?.length ? (
                    <div className="sources">
                      <div className="src-title">出处</div>
                      <ul>
                        {msg.sources.map((s) => (
                          <li key={s.id}>
                            <span className="pill">{s.source}</span>
                            <strong>{s.note_title}</strong>
                            <span className="muted"> · {s.heading_path}</span>
                            <div className="path muted small">{s.note_path}</div>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
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
            <button
              type="button"
              className="btn secondary attach-btn"
              disabled={!ready || sending || !threadsLoaded || pendingImages.length >= MAX_IMAGES}
              onClick={() => fileInputRef.current?.click()}
              title="上传聊天截图"
            >
              截图
            </button>
            <textarea
              className="input"
              rows={2}
              placeholder={
                sending
                  ? '生成中…可点「停止」后修改问题'
                  : ready
                    ? '描述你的情况…（可先上传截图）信息不够我会追问'
                    : '请先构建索引再发送'
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
        </form>
      </main>
    </div>
  )
}
