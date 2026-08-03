import { useEffect, useState } from 'react'
import './Admin.css'

type Overview = {
  timezone?: string
  user_count: number
  expert_count?: number
  expert_pack_count?: number
  active_users_yesterday: number
  active_users_7d: number
  active_users_30d: number
  tokens_yesterday: number
  cost_cny_yesterday: number
  tokens_30d: number
  cost_cny_30d: number
  open_risk_count?: number
  invite_use_count: number
  invite_max_uses: number | null
  registration_slots_remaining: number | null
  price_input_cny_per_1m: number
  price_output_cny_per_1m: number
  cost_note: string
}

type RiskAlert = {
  id: string
  user_id: string
  username?: string
  expert_id: string
  created_at: string
  categories: string[]
  snippet: string
  confidence: string
  status: string
  keyword_hits: string[]
  reason?: string
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
  has_open_risk?: boolean
  open_risk_count?: number
}

type ExpertRow = {
  id: string
  slug: string
  display_name: string
  avatar_label: string
  short_bio: string
  enabled: boolean
  chat_user_count: number
  thread_count: number
  tokens_7d: number
  cost_cny_7d: number
  tokens_30d: number
  cost_cny_30d: number
  index_ready: boolean
  index_chunk_count: number
  has_pack_knowledge: boolean
}

type ExpertDetail = ExpertRow & {
  scope?: string
  usage: {
    timezone?: string
    tokens_7d: number
    cost_cny_7d: number
    tokens_30d: number
    cost_cny_30d: number
  }
  index: {
    ready: boolean
    chunk_count: number
    vector_enabled: boolean
    tag_count: number
    tag_routing_ready: boolean
    last_indexed_at: string | null
    error?: string | null
    data_dir: string
    has_pack_knowledge: boolean
    knowledge_source: string
  }
  cost_note?: string
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
  expertId?: string
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
  recent_risk_alerts?: RiskAlert[]
  usage: {
    timezone?: string
    today: UsageSummary
    month: UsageSummary
    total: UsageSummary
  }
  token_quota?: {
    allowed: boolean
    unlimited: boolean
    monthly_allowance: number
    months_granted: number
    granted_tokens: number
    used_tokens: number
    remaining_tokens: number
    message: string
  }
  cost_note?: string
}

const RISK_CATEGORY_LABELS: Record<string, string> = {
  self_harm: '自伤/自杀',
  harm_others: '他伤/暴力威胁',
  domestic_violence: '家暴/人身危险',
  other_emergency: '其他紧急',
}

function riskCategoryLabel(cat: string): string {
  return RISK_CATEGORY_LABELS[cat] || cat
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
  const [experts, setExperts] = useState<ExpertRow[]>([])
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null)
  const [selectedExpertId, setSelectedExpertId] = useState<string | null>(null)
  const [userDetail, setUserDetail] = useState<UserDetail | null>(null)
  const [expertDetail, setExpertDetail] = useState<ExpertDetail | null>(null)
  const [userDetailLoading, setUserDetailLoading] = useState(false)
  const [expertDetailLoading, setExpertDetailLoading] = useState(false)
  const [openThreadId, setOpenThreadId] = useState<string | null>(null)
  const [indexingExpert, setIndexingExpert] = useState(false)
  const [riskAlerts, setRiskAlerts] = useState<RiskAlert[]>([])
  const [ackingId, setAckingId] = useState<string | null>(null)

  async function refreshRiskAndUsers() {
    const [ov, us, ra] = await Promise.all([
      fetch('/api/admin/overview', { credentials: 'include' }),
      fetch('/api/admin/users', { credentials: 'include' }),
      fetch('/api/admin/risk-alerts?status=open&limit=50', { credentials: 'include' }),
    ])
    if (ov.ok) {
      setOverview((await ov.json()) as Overview)
    }
    if (us.ok) {
      const usj = (await us.json()) as { users: UserRow[] }
      setUsers(usj.users || [])
    }
    if (ra.ok) {
      const raj = (await ra.json()) as { alerts: RiskAlert[] }
      setRiskAlerts(raj.alerts || [])
    }
  }

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
        const [ov, us, ex, ra] = await Promise.all([
          fetch('/api/admin/overview', { credentials: 'include' }),
          fetch('/api/admin/users', { credentials: 'include' }),
          fetch('/api/admin/experts', { credentials: 'include' }),
          fetch('/api/admin/risk-alerts?status=open&limit=50', { credentials: 'include' }),
        ])
        if (ov.status === 403 || us.status === 403 || ex.status === 403) {
          if (!cancelled) {
            setDenied(true)
            setLoading(false)
          }
          return
        }
        // Core panels must succeed; risk-alerts is additive (old backends may 404).
        if (!ov.ok || !us.ok || !ex.ok) {
          throw new Error('加载后台数据失败')
        }
        const ovj = (await ov.json()) as Overview
        const usj = (await us.json()) as { users: UserRow[] }
        const exj = (await ex.json()) as { experts: ExpertRow[] }
        const raj = ra.ok ? ((await ra.json()) as { alerts: RiskAlert[] }) : { alerts: [] }
        if (!cancelled) {
          setOverview(ovj)
          setUsers(usj.users || [])
          setExperts(exj.experts || [])
          setRiskAlerts(raj.alerts || [])
          if (!ra.ok) {
            setError('风险预警接口不可用，请重启后端后再试')
          }
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
    if (denied || loading) return
    const timer = window.setInterval(() => {
      void refreshRiskAndUsers().catch(() => {
        /* ignore poll errors */
      })
    }, 20000)
    return () => window.clearInterval(timer)
  }, [denied, loading])

  useEffect(() => {
    if (!selectedUserId) {
      setUserDetail(null)
      return
    }
    let cancelled = false
    setUserDetailLoading(true)
    setOpenThreadId(null)
    fetch(`/api/admin/users/${selectedUserId}`, { credentials: 'include' })
      .then(async (r) => {
        if (!r.ok) throw new Error('加载用户详情失败')
        return (await r.json()) as UserDetail
      })
      .then((d) => {
        if (!cancelled) {
          setUserDetail(d)
          setUserDetailLoading(false)
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : '加载详情失败')
          setUserDetailLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [selectedUserId])

  useEffect(() => {
    if (!selectedExpertId) {
      setExpertDetail(null)
      return
    }
    let cancelled = false
    setExpertDetailLoading(true)
    fetch(`/api/admin/experts/${encodeURIComponent(selectedExpertId)}`, {
      credentials: 'include',
    })
      .then(async (r) => {
        if (!r.ok) throw new Error('加载智能体详情失败')
        return (await r.json()) as ExpertDetail
      })
      .then((d) => {
        if (!cancelled) {
          setExpertDetail(d)
          setExpertDetailLoading(false)
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : '加载智能体详情失败')
          setExpertDetailLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [selectedExpertId])

  async function rebuildExpertIndex() {
    if (!selectedExpertId || indexingExpert) return
    setIndexingExpert(true)
    setError('')
    try {
      const r = await fetch(
        `/api/admin/experts/${encodeURIComponent(selectedExpertId)}/index`,
        { method: 'POST', credentials: 'include' },
      )
      if (!r.ok) {
        const d = (await r.json().catch(() => null)) as { detail?: unknown } | null
        const detail =
          typeof d?.detail === 'string' ? d.detail : JSON.stringify(d?.detail ?? {})
        throw new Error(detail || r.statusText)
      }
      const refreshed = await fetch(
        `/api/admin/experts/${encodeURIComponent(selectedExpertId)}`,
        { credentials: 'include' },
      )
      if (refreshed.ok) {
        setExpertDetail((await refreshed.json()) as ExpertDetail)
      }
      const list = await fetch('/api/admin/experts', { credentials: 'include' })
      if (list.ok) {
        const body = (await list.json()) as { experts: ExpertRow[] }
        setExperts(body.experts || [])
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '构建索引失败')
    } finally {
      setIndexingExpert(false)
    }
  }

  async function ackRiskAlert(alertId: string) {
    if (ackingId) return
    setAckingId(alertId)
    setError('')
    try {
      const r = await fetch(`/api/admin/risk-alerts/${encodeURIComponent(alertId)}/ack`, {
        method: 'POST',
        credentials: 'include',
      })
      if (!r.ok) {
        const d = (await r.json().catch(() => null)) as { detail?: unknown } | null
        const detail =
          typeof d?.detail === 'string' ? d.detail : JSON.stringify(d?.detail ?? {})
        throw new Error(detail || r.statusText)
      }
      await refreshRiskAndUsers()
      if (selectedUserId) {
        const detailRes = await fetch(`/api/admin/users/${selectedUserId}`, {
          credentials: 'include',
        })
        if (detailRes.ok) {
          setUserDetail((await detailRes.json()) as UserDetail)
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '标记已知悉失败')
    } finally {
      setAckingId(null)
    }
  }

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
            <div className="label">专家智能体数量</div>
            <div className="value">{overview.expert_count ?? experts.filter((e) => e.enabled).length}</div>
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
          <div className={`admin-card${(overview.open_risk_count || 0) > 0 ? ' admin-card-risk' : ''}`}>
            <div className="label">未处理风险预警</div>
            <div className="value">{overview.open_risk_count ?? riskAlerts.length}</div>
          </div>
        </section>
      ) : null}

      {overview ? (
        <p className="muted small admin-note">
          {overview.cost_note}；单价 输入 ¥{overview.price_input_cny_per_1m}/百万 · 输出 ¥
          {overview.price_output_cny_per_1m}/百万
        </p>
      ) : null}

      <section className={`admin-panel admin-risk-panel${riskAlerts.length > 0 ? ' has-open' : ''}`}>
        <div className="admin-risk-head">
          <h2>风险预警</h2>
          <span className={`admin-risk-count${riskAlerts.length > 0 ? ' hot' : ''}`}>
            未处理 {riskAlerts.length}
          </span>
        </div>
        {riskAlerts.length === 0 ? (
          <p className="muted">暂无未处理的紧急风险预警</p>
        ) : (
          <ul className="admin-risk-list">
            {riskAlerts.map((a) => (
              <li key={a.id} className="admin-risk-item">
                <div className="admin-risk-item-top">
                  <button
                    type="button"
                    className="admin-risk-user"
                    onClick={() => {
                      setSelectedUserId(a.user_id)
                      setSelectedExpertId(null)
                    }}
                  >
                    {a.username || a.user_id}
                  </button>
                  <span className="muted small">{fmtTime(a.created_at)}</span>
                  <span className="muted small">{a.expert_id || '—'}</span>
                  <span className="muted small">{a.confidence}</span>
                </div>
                <div className="admin-risk-cats">
                  {(a.categories || []).map((c) => (
                    <span key={c} className="admin-badge admin-badge-risk">
                      {riskCategoryLabel(c)}
                    </span>
                  ))}
                </div>
                <p className="admin-risk-snippet">{a.snippet || '（无摘要）'}</p>
                {a.reason ? <p className="muted small">判定：{a.reason}</p> : null}
                <button
                  type="button"
                  className="admin-ack-btn"
                  disabled={ackingId === a.id}
                  onClick={() => void ackRiskAlert(a.id)}
                >
                  {ackingId === a.id ? '处理中…' : '已知悉'}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

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
                    className={[
                      selectedUserId === u.id ? 'active' : '',
                      u.has_open_risk ? 'risk-row' : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    onClick={() => {
                      setSelectedUserId(u.id)
                      setSelectedExpertId(null)
                    }}
                  >
                    <td className={u.has_open_risk ? 'risk-username' : undefined}>
                      {u.username}
                      {u.is_admin ? <span className="admin-badge">管理员</span> : null}
                      {u.has_open_risk ? (
                        <span className="admin-badge admin-badge-risk">风险</span>
                      ) : null}
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
          {!selectedUserId ? (
            <p className="muted">点击左侧用户查看聊天与用量</p>
          ) : userDetailLoading ? (
            <p className="muted">加载中…</p>
          ) : userDetail ? (
            <div className="admin-detail">
              <div className="admin-detail-head">
                <strong className={userDetail.has_open_risk ? 'risk-username' : undefined}>
                  {userDetail.username}
                </strong>
                {userDetail.has_open_risk ? (
                  <span className="admin-badge admin-badge-risk">风险</span>
                ) : null}
                <span className="muted small">注册 {fmtTime(userDetail.created_at)}</span>
              </div>
              {(userDetail.recent_risk_alerts || []).length > 0 ? (
                <div className="admin-usage-block admin-user-risks">
                  <h3>近期风险预警</h3>
                  <ul className="admin-risk-list compact">
                    {(userDetail.recent_risk_alerts || []).slice(0, 5).map((a) => (
                      <li key={a.id} className="admin-risk-item">
                        <div className="admin-risk-item-top">
                          <span className={a.status === 'open' ? 'risk-username' : 'muted'}>
                            {a.status === 'open' ? '未处理' : '已悉'}
                          </span>
                          <span className="muted small">{fmtTime(a.created_at)}</span>
                          {(a.categories || []).map((c) => (
                            <span key={c} className="admin-badge admin-badge-risk">
                              {riskCategoryLabel(c)}
                            </span>
                          ))}
                        </div>
                        <p className="admin-risk-snippet">{a.snippet || '（无摘要）'}</p>
                        {a.status === 'open' ? (
                          <button
                            type="button"
                            className="admin-ack-btn"
                            disabled={ackingId === a.id}
                            onClick={() => void ackRiskAlert(a.id)}
                          >
                            {ackingId === a.id ? '处理中…' : '已知悉'}
                          </button>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {userDetail.token_quota ? (
                <div className="admin-usage-block">
                  <h3>对话额度</h3>
                  {userDetail.token_quota.unlimited ? (
                    <p className="muted small">管理员：不限额度</p>
                  ) : (
                    <div className="admin-detail-stats">
                      <span>剩余 {fmtTokens(userDetail.token_quota.remaining_tokens)}</span>
                      <span>已用 {fmtTokens(userDetail.token_quota.used_tokens)}</span>
                      <span>累计发放 {fmtTokens(userDetail.token_quota.granted_tokens)}</span>
                      <span>
                        月额度 {fmtTokens(userDetail.token_quota.monthly_allowance)} ×{' '}
                        {userDetail.token_quota.months_granted} 月
                      </span>
                    </div>
                  )}
                  {!userDetail.token_quota.allowed ? (
                    <p className="admin-err">{userDetail.token_quota.message || '额度已用完'}</p>
                  ) : null}
                </div>
              ) : null}
              <div className="admin-usage-sections">
                <UsageBlock title="今日消费" summary={userDetail.usage.today} />
                <UsageBlock title="本月消费" summary={userDetail.usage.month} />
                <UsageBlock title="总消费" summary={userDetail.usage.total} showKind />
              </div>
              {userDetail.cost_note ? (
                <p className="muted small admin-note">{userDetail.cost_note}</p>
              ) : null}

              <h3>对话线程</h3>
              {(userDetail.chat_state.threads || []).length === 0 ? (
                <p className="muted">暂无聊天记录</p>
              ) : (
                <div className="admin-threads">
                  {(userDetail.chat_state.threads || []).map((t) => {
                    const tid = t.id || t.title || 'thread'
                    const open = openThreadId === tid
                    const expertLabel = t.expertId || 'afu'
                    return (
                      <div key={tid} className="admin-thread">
                        <button
                          type="button"
                          className="admin-thread-toggle"
                          onClick={() => setOpenThreadId(open ? null : tid)}
                        >
                          <span>{t.title || '未命名对话'}</span>
                          <span className="muted small">
                            {expertLabel} · {(t.messages || []).length} 条 · {open ? '收起' : '展开'}
                          </span>
                        </button>
                        {open ? (
                          <div className="admin-msgs">
                            {(t.messages || []).map((m, i) => (
                              <div key={`${tid}-${i}`} className={`admin-msg ${m.role || ''}`}>
                                <div className="admin-msg-meta">
                                  {m.role === 'user' ? '用户' : expertLabel}
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

              {userDetail.memory_text ? (
                <>
                  <h3>长时记忆</h3>
                  <pre className="admin-memory">{userDetail.memory_text}</pre>
                </>
              ) : null}
            </div>
          ) : (
            <p className="muted">未找到用户</p>
          )}
        </section>
      </div>

      <div className="admin-layout admin-layout-experts">
        <section className="admin-panel">
          <h2>智能体列表</h2>
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>智能体</th>
                  <th>状态</th>
                  <th>对话用户</th>
                  <th>会话数</th>
                  <th>7 日 Token</th>
                  <th>索引</th>
                </tr>
              </thead>
              <tbody>
                {experts.map((e) => (
                  <tr
                    key={e.id}
                    className={selectedExpertId === e.id ? 'active' : ''}
                    onClick={() => {
                      setSelectedExpertId(e.id)
                      setSelectedUserId(null)
                    }}
                  >
                    <td>
                      {e.display_name}
                      <div className="muted small">{e.id}</div>
                    </td>
                    <td>{e.enabled ? '启用' : '停用'}</td>
                    <td>{e.chat_user_count}</td>
                    <td>{e.thread_count}</td>
                    <td>{fmtTokens(e.tokens_7d)}</td>
                    <td>
                      {e.index_ready ? `就绪 · ${e.index_chunk_count}` : '未就绪'}
                    </td>
                  </tr>
                ))}
                {experts.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="muted">
                      暂无智能体
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </section>

        <section className="admin-panel">
          <h2>智能体详情</h2>
          {!selectedExpertId ? (
            <p className="muted">点击左侧智能体查看用量与索引</p>
          ) : expertDetailLoading ? (
            <p className="muted">加载中…</p>
          ) : expertDetail ? (
            <div className="admin-detail">
              <div className="admin-detail-head">
                <strong>{expertDetail.display_name}</strong>
                <span className="muted small">
                  {expertDetail.id}
                  {expertDetail.enabled ? ' · 已启用' : ' · 已停用'}
                </span>
              </div>
              {expertDetail.short_bio ? (
                <p className="muted small">{expertDetail.short_bio}</p>
              ) : null}

              <div className="admin-detail-stats">
                <span>对话用户数 {expertDetail.chat_user_count}</span>
                <span>总对话数 {expertDetail.thread_count}</span>
              </div>

              <div className="admin-usage-sections">
                <div className="admin-usage-block">
                  <h3>近 7 日 Token</h3>
                  <div className="admin-detail-stats">
                    <span>{fmtTokens(expertDetail.usage.tokens_7d)}</span>
                    <span>费用 {fmtMoney(expertDetail.usage.cost_cny_7d)}</span>
                  </div>
                </div>
                <div className="admin-usage-block">
                  <h3>近 30 日 Token</h3>
                  <div className="admin-detail-stats">
                    <span>{fmtTokens(expertDetail.usage.tokens_30d)}</span>
                    <span>费用 {fmtMoney(expertDetail.usage.cost_cny_30d)}</span>
                  </div>
                </div>
              </div>
              {expertDetail.cost_note ? (
                <p className="muted small admin-note">{expertDetail.cost_note}</p>
              ) : null}

              <h3>知识索引</h3>
              <div className="admin-index-card">
                <p className="status-body">
                  {expertDetail.index.ready ? '已就绪' : '未就绪'} ·{' '}
                  {expertDetail.index.chunk_count} 个切片 ·{' '}
                  {expertDetail.index.vector_enabled ? '向量 + BM25' : '仅 BM25'}
                  {expertDetail.index.tag_routing_ready
                    ? ` · ${expertDetail.index.tag_count} 个标签（路由已启用）`
                    : expertDetail.index.tag_count > 0
                      ? ` · ${expertDetail.index.tag_count} 个标签`
                      : ''}
                </p>
                {expertDetail.index.last_indexed_at ? (
                  <p className="muted small">{expertDetail.index.last_indexed_at}</p>
                ) : null}
                <p className="muted small">
                  来源：{expertDetail.index.knowledge_source} · {expertDetail.index.data_dir}
                </p>
                {expertDetail.index.error ? (
                  <p className="admin-err">{String(expertDetail.index.error)}</p>
                ) : null}
                <button
                  type="button"
                  className="admin-index-btn"
                  disabled={indexingExpert}
                  onClick={() => void rebuildExpertIndex()}
                >
                  {indexingExpert ? '正在构建…' : '构建索引'}
                </button>
              </div>
            </div>
          ) : (
            <p className="muted">未找到智能体</p>
          )}
        </section>
      </div>
    </div>
  )
}
