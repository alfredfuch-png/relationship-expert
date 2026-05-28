import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type MouseEvent } from 'react'
import './App.css'

type Role = 'user' | 'assistant'

type Source = {
  id: string
  note_path: string
  note_title: string
  heading_path: string
  source: string
}

type ChatMessage = {
  role: Role
  content: string
  sources?: Source[]
  routing?: RoutingInfo
  error?: string
}

type RoutingInfo = {
  tag_routing?: boolean
  tag_routing_ready?: boolean
  applied_tags?: string[]
  tag_scores?: Record<string, number>
  scoped?: boolean
  scoped_chunk_count?: number
  fallback_reason?: string | null
}

type ChatThread = {
  id: string
  title: string
  messages: ChatMessage[]
  updatedAt: number
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

type StreamMeta = { sources?: Source[]; routing?: RoutingInfo }

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
  message: string,
  onMeta: (m: StreamMeta) => void,
  onToken: (t: string) => void,
  onError: (e: string) => void,
) {
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ message }),
  })
  if (res.status === 401) {
    onError('请先登录后再提问。')
    return
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
      return
    }
    if (res.status === 401) {
      onError('登录已过期，请刷新页面重新登录。')
      return
    }
    onError(detail ? `请求失败（${res.status}）：${detail}` : `请求失败（${res.status}）`)
    return
  }
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    for (;;) {
      const nl = buf.indexOf('\n')
      if (nl < 0) break
      const line = buf.slice(0, nl).trim()
      buf = buf.slice(nl + 1)
      if (!line) continue
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
        })
        continue
      }
      if (obj.meta && typeof obj.meta === 'object') {
        onMeta({})
        continue
      }
      if (typeof obj.error === 'string') {
        onError(obj.error)
        return
      }
      if (typeof obj.text === 'string') {
        onToken(obj.text)
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
  const scrollRef = useRef<HTMLDivElement>(null)

  const serverChat = Boolean(appConfig.server_chat)

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
    if (serverChat) {
      const timer = window.setTimeout(() => {
        void fetch('/api/chat/state', {
          method: 'PUT',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ threads, active_id: activeId }),
        })
      }, 700)
      return () => window.clearTimeout(timer)
    }
    localStorage.setItem(LS_KEY, JSON.stringify({ threads, activeId }))
  }, [threads, activeId, serverChat, threadsLoaded])

  const activeThread = threads.find((t) => t.id === activeId)
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
    if (sending) return
    const id = newId()
    const thread: ChatThread = { id, title: '新对话', messages: [], updatedAt: Date.now() }
    setThreads((ts) => capThreads([thread, ...ts]))
    setActiveId(id)
    setInput('')
  }

  function handleSelectThread(id: string) {
    if (sending || id === activeId) return
    setActiveId(id)
    setInput('')
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

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const q = input.trim()
    if (!q || sending) return
    setInput('')
    updateActiveMessages((m) => [...m, { role: 'user', content: q }])
    setSending(true)

    let acc = ''
    let sources: Source[] | undefined
    let routing: RoutingInfo | undefined
    let hadError = false

    await streamChat(
      q,
      ({ sources: src, routing: rv }) => {
        sources = src
        routing = rv
      },
      (t) => {
        acc += t
        updateActiveMessages((m) => {
          const copy = [...m]
          const last = copy[copy.length - 1]
          if (last?.role === 'assistant' && !last.error) {
            copy[copy.length - 1] = { ...last, content: acc, sources, routing }
          } else {
            copy.push({ role: 'assistant', content: acc, sources, routing })
          }
          return copy
        })
      },
      (err) => {
        hadError = true
        updateActiveMessages((m) => [
          ...m,
          { role: 'assistant', content: '', error: err, sources, routing },
        ])
      },
    )

    setSending(false)

    if (!hadError && acc.trim() === '') {
      updateActiveMessages((m) => [
        ...m,
        { role: 'assistant', content: '', error: '模型返回为空。', sources, routing },
      ])
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
            <span>对话模型：deepseek · 可先按标签收窄再检索上下文</span>
          )}
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
              <h2>问你的亲密关系笔记</h2>
              <p className="muted">
                {ready
                  ? appConfig.public_deploy
                    ? '直接提问即可，我会根据知识库给出建议。'
                    : '回答基于「关于亲密关系」目录下检索到的片段，并附带引用编号。'
                  : appConfig.allow_index
                    ? '请先构建索引，然后开始对话。'
                    : '知识库尚未就绪，请稍后再试。'}
              </p>
            </div>
          ) : (
            messages.map((msg, i) => (
              <div key={`${activeId}-${i}-${msg.role}`} className={`bubble-row ${msg.role}`}>
                <div className="avatar">{msg.role === 'user' ? '我' : '专家'}</div>
                <div className={`bubble ${msg.role}`}>
                  {msg.error ? (
                    <p className="err">{msg.error}</p>
                  ) : (
                    <div className="md">
                      {msg.content || (sending && msg.role === 'assistant' ? '…' : '')}
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
          <textarea
            className="input"
            rows={2}
            placeholder={
              ready ? '输入问题（恋爱、婚姻、沟通、边界……）' : '请先构建索引再发送'
            }
            value={input}
            disabled={!ready || sending || !threadsLoaded}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                void handleSubmit(e)
              }
            }}
          />
          <button
            type="submit"
            className="btn send"
            disabled={!ready || sending || !input.trim() || !threadsLoaded}
          >
            发送
          </button>
        </form>
      </main>
    </div>
  )
}
