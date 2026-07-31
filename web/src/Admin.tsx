import { useEffect, useState } from 'react'
import './Admin.css'

type Overview = {
  timezone?: string
  user_count: number
  active_users_yesterday: number
  active_users_7d: number
  active_users_30d: number
  tokens_yesterday: number
  cost_cny_yesterday: number
  tokens_30d: number
  cost_cny_30d: number
  invite_use_count: number
  invite_max_uses: number | null
  registration_slots_remaining: number | null
  price_input_cny_per_1m: number
  price_output_cny_per_1m: number
  cost_note: string
}

type UserRow = {
  id: string
  username: string
  created_at: string
  is_admin: boolean
  thread_count: number
  message_count: number
  last_active_at: string | null
  tokens_total: number
  cost_cny_total: number
  usage_events: number
}

type ChatMessage = {
  role?: string
  content?: string
  phase?: string
  imageCount?: number
}

type ChatThread = {
  id?: string
  title?: string
  updatedAt?: number
  contextSummary?: string
  messages?: ChatMessage[]
}

type UsageSummary = {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_cny_total: number
  events: number
  by_kind?: { kind: string; tokens: number; cost_cny: number; events: number }[]
}

type UserDetail = UserRow & {
  chat_state: { threads?: ChatThread[]; active_id?: string | null }
  memory_text?: string
  usage: {
    timezone?: string
    today: UsageSummary
    month: UsageSummary
    total: UsageSummary
  }
  cost_note?: string
}

function fmtMoney(n: number): string {
  return `¥${Number(n || 0).toFixed(4)}`
}

function fmtTokens(n: number): string {
  return Number(n || 0).toLocaleString()
}

function fmtTime(raw: string | null | undefined): string {
  if (!raw) return '—'
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return raw
  return d.toLocaleString()
}

function UsageBlock({
  title,
  summary,
  showKind,
}: {
  title: string
  summary: UsageSummary
  showKind?: boolean
}) {
  return (
    <div className="admin-usage-block">
      <h3>{title}</h3>
      <div className="admin-detail-stats">
        <span>Token {fmtTokens(summary.total_tokens)}</span>
        <span>
          输入/输出 {fmtTokens(summary.prompt_tokens)} / {fmtTokens(summary.completion_tokens)}
        </span>
        <span>费用 {fmtMoney(summary.cost_cny_total)}</span>
      </div>
      {showKind ? (
        summary.by_kind && summary.by_kind.length > 0 ? (
          <ul className="admin-kind-list">
            {summary.by_kind.map((k) => (
              <li key={k.kind}>
                {k.kind}：{fmtTokens(k.tokens)} · {fmtMoney(k.cost_cny)}（{k.events} 次）
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted small">尚无 token 记录</p>
        )
      ) : null}
    </div>
  )
}

export default function Admin() {
  const [denied, setDenied] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [overview, setOverview] = useState<Overview | null>(null)
  const [users, setUsers] = useState<UserRow[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<UserDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [openThreadId, setOpenThreadId] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const status = await fetch('/api/auth/status', { credentials: 'include' }).then((r) =>
          r.json(),
        )
        if (!status.authenticated) {
          window.location.href = '/'
          return
        }
        if (!status.is_admin) {
          if (!cancelled) {
            setDenied(true)
            setLoading(false)
          }
          return
        }
        const [ov, us] = await Promise.all([
          fetch('/api/admin/overview', { credentials: 'include' }),
          fetch('/api/admin/users', { credentials: 'include' }),
        ])
        if (ov.status === 403 || us.status === 403) {
          if (!cancelled) {
            setDenied(true)
            setLoading(false)
          }
          return
        }
        if (!ov.ok || !us.ok) {
          throw new Error('加载后台数据失败')
        }
        const ovj = (await ov.json()) as Overview
        const usj = (await us.json()) as { users: UserRow[] }
        if (!cancelled) {
          setOverview(ovj)
          setUsers(usj.users || [])
          setLoading(false)
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : '加载失败')
          setLoading(false)
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetailLoading(true)
    setOpenThreadId(null)
    fetch(`/api/admin/users/${selectedId}`, { credentials: 'include' })
      .then(async (r) => {
        if (!r.ok) throw new Error('加载用户详情失败')
        return (await r.json()) as UserDetail
      })
      .then((d) => {
        if (!cancelled) {
          setDetail(d)
          setDetailLoading(false)
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : '加载详情失败')
          setDetailLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [selectedId])

  if (loading) {
    return (
      <div className="admin-page">
        <p className="muted">加载运营后台…</p>
      </div>
    )
  }

  if (denied) {
    return (
      <div className="admin-page">
        <h1>运营后台</h1>
        <p>当前账号没有管理员权限。</p>
        <a className="admin-link" href="/">
          返回聊天
        </a>
      </div>
    )
  }

  return (
    <div className="admin-page">
      <header className="admin-header">
        <div>
          <h1>运营后台</h1>
          <p className="muted small">粉丝试用数据：使用频率、聊天内容、token 与费用估算</p>
        </div>
        <a className="admin-link" href="/">
          返回聊天
        </a>
      </header>

      {error ? <p className="admin-err">{error}</p> : null}

      {overview ? (
        <section className="admin-cards">
          <div className="admin-card">
            <div className="label">注册用户</div>
            <div className="value">{overview.user_count}</div>
          </div>
          <div className="admin-card">
            <div className="label">昨日活跃用户</div>
            <div className="value">{overview.active_users_yesterday}</div>
          </div>
          <div className="admin-card">
            <div className="label">近 7 日活跃用户</div>
            <div className="value">{overview.active_users_7d}</div>
          </div>
          <div className="admin-card">
            <div className="label">近 30 日活跃用户</div>
            <div className="value">{overview.active_users_30d}</div>
          </div>
          <div className="admin-card">
            <div className="label">昨日 Token</div>
            <div className="value">{fmtTokens(overview.tokens_yesterday)}</div>
          </div>
          <div className="admin-card">
            <div className="label">昨日费用估算</div>
            <div className="value">{fmtMoney(overview.cost_cny_yesterday)}</div>
          </div>
          <div className="admin-card">
            <div className="label">近 30 日 Token</div>
            <div className="value">{fmtTokens(overview.tokens_30d)}</div>
          </div>
          <div className="admin-card">
            <div className="label">近 30 日费用估算</div>
            <div className="value">{fmtMoney(overview.cost_cny_30d)}</div>
          </div>
          <div className="admin-card">
            <div className="label">邀请码剩余</div>
            <div className="value">
              {overview.registration_slots_remaining == null
                ? '不限'
                : overview.registration_slots_remaining}
            </div>
          </div>
        </section>
      ) : null}

      {overview ? (
        <p className="muted small admin-note">
          {overview.cost_note}；单价 输入 ¥{overview.price_input_cny_per_1m}/百万 · 输出 ¥
          {overview.price_output_cny_per_1m}/百万
        </p>
      ) : null}

      <div className="admin-layout">
        <section className="admin-panel">
          <h2>用户列表</h2>
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>用户</th>
                  <th>消息</th>
                  <th>会话</th>
                  <th>Token</th>
                  <th>费用估算</th>
                  <th>最近活跃</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr
                    key={u.id}
                    className={selectedId === u.id ? 'active' : ''}
                    onClick={() => setSelectedId(u.id)}
                  >
                    <td>
                      {u.username}
                      {u.is_admin ? <span className="admin-badge">管理员</span> : null}
                    </td>
                    <td>{u.message_count}</td>
                    <td>{u.thread_count}</td>
                    <td>{fmtTokens(u.tokens_total)}</td>
                    <td>{fmtMoney(u.cost_cny_total)}</td>
                    <td>{fmtTime(u.last_active_at)}</td>
                  </tr>
                ))}
                {users.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="muted">
                      暂无用户
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </section>

        <section className="admin-panel">
          <h2>用户详情</h2>
          {!selectedId ? (
            <p className="muted">点击左侧用户查看聊天与用量</p>
          ) : detailLoading ? (
            <p className="muted">加载中…</p>
          ) : detail ? (
            <div className="admin-detail">
              <div className="admin-detail-head">
                <strong>{detail.username}</strong>
                <span className="muted small">注册 {fmtTime(detail.created_at)}</span>
              </div>
              <div className="admin-usage-sections">
                <UsageBlock title="今日消费" summary={detail.usage.today} />
                <UsageBlock title="本月消费" summary={detail.usage.month} />
                <UsageBlock title="总消费" summary={detail.usage.total} showKind />
              </div>
              {detail.cost_note ? (
                <p className="muted small admin-note">{detail.cost_note}</p>
              ) : null}

              <h3>对话线程</h3>
              {(detail.chat_state.threads || []).length === 0 ? (
                <p className="muted">暂无聊天记录</p>
              ) : (
                <div className="admin-threads">
                  {(detail.chat_state.threads || []).map((t) => {
                    const tid = t.id || t.title || 'thread'
                    const open = openThreadId === tid
                    return (
                      <div key={tid} className="admin-thread">
                        <button
                          type="button"
                          className="admin-thread-toggle"
                          onClick={() => setOpenThreadId(open ? null : tid)}
                        >
                          <span>{t.title || '未命名对话'}</span>
                          <span className="muted small">
                            {(t.messages || []).length} 条 · {open ? '收起' : '展开'}
                          </span>
                        </button>
                        {open ? (
                          <div className="admin-msgs">
                            {(t.messages || []).map((m, i) => (
                              <div key={`${tid}-${i}`} className={`admin-msg ${m.role || ''}`}>
                                <div className="admin-msg-meta">
                                  {m.role === 'user' ? '用户' : '阿FU'}
                                  {m.phase ? ` · ${m.phase}` : ''}
                                  {m.imageCount ? ` · ${m.imageCount} 图` : ''}
                                </div>
                                <pre>{m.content || '（空）'}</pre>
                              </div>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    )
                  })}
                </div>
              )}

              {detail.memory_text ? (
                <>
                  <h3>长时记忆</h3>
                  <pre className="admin-memory">{detail.memory_text}</pre>
                </>
              ) : null}
            </div>
          ) : (
            <p className="muted">未找到用户</p>
          )}
        </section>
      </div>
    </div>
  )
}
