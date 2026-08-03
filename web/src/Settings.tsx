import { useEffect, useState, type FormEvent } from 'react'
import './Settings.css'

type UsageSummary = {
  username: string
  timezone?: string
  daily_7d: { day: string; tokens: number }[]
  tokens_30d: number
  tokens_total: number
  tokens_month: number
  monthly_allowance: number
  month_progress: number
  is_admin?: boolean
}

type Panel = 'menu' | 'password' | 'usage'

function fmtTokens(n: number): string {
  return Number(n || 0).toLocaleString()
}

function fmtDay(day: string): string {
  const parts = day.split('-')
  if (parts.length !== 3) return day
  return `${Number(parts[1])}/${Number(parts[2])}`
}

export default function Settings() {
  const [username, setUsername] = useState('')
  const [authMode, setAuthMode] = useState<string>('none')
  const [loading, setLoading] = useState(true)
  const [panel, setPanel] = useState<Panel>('menu')
  const [error, setError] = useState('')
  const [okMsg, setOkMsg] = useState('')

  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [savingPassword, setSavingPassword] = useState(false)

  const [usage, setUsage] = useState<UsageSummary | null>(null)
  const [usageLoading, setUsageLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    fetch('/api/auth/status', { credentials: 'include' })
      .then((r) => r.json())
      .then(
        (o: {
          username?: string | null
          auth_mode?: string
          authenticated?: boolean
          auth_required?: boolean
        }) => {
          if (cancelled) return
          if (o.auth_required && !o.authenticated) {
            window.location.href = '/'
            return
          }
          setUsername(o.username || '')
          setAuthMode(o.auth_mode || 'none')
          setLoading(false)
        },
      )
      .catch(() => {
        if (!cancelled) {
          setError('无法加载账户信息')
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (panel !== 'usage') return
    let cancelled = false
    setUsageLoading(true)
    setError('')
    fetch('/api/user/usage', { credentials: 'include' })
      .then(async (r) => {
        if (!r.ok) {
          const d = (await r.json().catch(() => null)) as { detail?: string } | null
          throw new Error(d?.detail || '加载用量失败')
        }
        return (await r.json()) as UsageSummary
      })
      .then((d) => {
        if (!cancelled) {
          setUsage(d)
          setUsageLoading(false)
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : '加载用量失败')
          setUsageLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [panel])

  async function onChangePassword(e: FormEvent) {
    e.preventDefault()
    setError('')
    setOkMsg('')
    if (newPassword !== confirmPassword) {
      setError('两次输入的新密码不一致。')
      return
    }
    setSavingPassword(true)
    try {
      const r = await fetch('/api/auth/change-password', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_password: oldPassword,
          new_password: newPassword,
          confirm_password: confirmPassword,
        }),
      })
      const d = (await r.json().catch(() => null)) as { detail?: string; message?: string } | null
      if (!r.ok) {
        throw new Error(typeof d?.detail === 'string' ? d.detail : '重设密码失败')
      }
      setOkMsg(d?.message || '密码已更新。')
      setOldPassword('')
      setNewPassword('')
      setConfirmPassword('')
      setPanel('menu')
    } catch (err) {
      setError(err instanceof Error ? err.message : '重设密码失败')
    } finally {
      setSavingPassword(false)
    }
  }

  async function onLogout() {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' })
    window.location.href = '/'
  }

  if (loading) {
    return (
      <div className="settings-page">
        <p className="muted">加载中…</p>
      </div>
    )
  }

  const canChangePassword = Boolean(username) && authMode === 'accounts'
  const maxDaily = Math.max(1, ...(usage?.daily_7d.map((d) => d.tokens) || [1]))

  return (
    <div className="settings-page">
      <header className="settings-top">
        <button
          type="button"
          className="settings-back"
          onClick={() => {
            window.location.href = '/'
          }}
        >
          返回聊天
        </button>
        <h1>设置</h1>
      </header>

      {error ? <p className="settings-err">{error}</p> : null}
      {okMsg ? <p className="settings-ok">{okMsg}</p> : null}

      <section className="settings-card settings-profile">
        <div className="settings-avatar" aria-hidden>
          {(username || '?').slice(0, 1).toUpperCase()}
        </div>
        <div>
          <div className="settings-username">{username || '未登录'}</div>
          <div className="muted small">账户设置与用量</div>
        </div>
      </section>

      {panel === 'menu' ? (
        <>
          <section className="settings-card settings-list">
            {canChangePassword ? (
              <button
                type="button"
                className="settings-row"
                onClick={() => {
                  setError('')
                  setPanel('password')
                }}
              >
                <span className="settings-row-icon" aria-hidden>
                  *
                </span>
                <span className="settings-row-label">重设密码</span>
                <span className="settings-chevron">&gt;</span>
              </button>
            ) : null}
            <button
              type="button"
              className="settings-row"
              onClick={() => {
                setError('')
                setPanel('usage')
              }}
            >
              <span className="settings-row-icon" aria-hidden>
                #
              </span>
              <span className="settings-row-label">用量统计</span>
              <span className="settings-chevron">&gt;</span>
            </button>
          </section>

          {username ? (
            <button type="button" className="settings-logout" onClick={() => void onLogout()}>
              退出登录
            </button>
          ) : null}
        </>
      ) : null}

      {panel === 'password' ? (
        <section className="settings-card settings-panel">
          <button type="button" className="settings-subback" onClick={() => setPanel('menu')}>
            返回
          </button>
          <h2>重设密码</h2>
          <form className="settings-form" onSubmit={(e) => void onChangePassword(e)}>
            <label>
              旧密码
              <input
                type="password"
                autoComplete="current-password"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                required
              />
            </label>
            <label>
              新密码
              <input
                type="password"
                autoComplete="new-password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                minLength={4}
                required
              />
            </label>
            <label>
              再次输入新密码
              <input
                type="password"
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                minLength={4}
                required
              />
            </label>
            <button type="submit" className="settings-primary" disabled={savingPassword}>
              {savingPassword ? '保存中…' : '确认修改'}
            </button>
          </form>
        </section>
      ) : null}

      {panel === 'usage' ? (
        <section className="settings-card settings-panel">
          <button type="button" className="settings-subback" onClick={() => setPanel('menu')}>
            返回
          </button>
          <h2>用量统计</h2>
          {usageLoading ? (
            <p className="muted">加载中…</p>
          ) : usage ? (
            <>
              <div className="usage-progress-block">
                <div className="usage-progress-head">
                  <span>本月已用</span>
                  <span>
                    {fmtTokens(usage.tokens_month)} / {fmtTokens(usage.monthly_allowance)}
                  </span>
                </div>
                <div
                  className="usage-bar"
                  role="progressbar"
                  aria-valuenow={Math.round(usage.month_progress * 100)}
                  aria-valuemin={0}
                  aria-valuemax={100}
                >
                  <div
                    className="usage-bar-fill"
                    style={{ width: `${Math.round(usage.month_progress * 100)}%` }}
                  />
                </div>
                {usage.is_admin ? (
                  <p className="muted small">管理员账号不限额度；上图仅展示本月消耗对照。</p>
                ) : null}
              </div>

              <div className="usage-stats-row">
                <div>
                  <div className="label">近 30 日</div>
                  <div className="value">{fmtTokens(usage.tokens_30d)}</div>
                </div>
                <div>
                  <div className="label">累计总用量</div>
                  <div className="value">{fmtTokens(usage.tokens_total)}</div>
                </div>
              </div>

              <h3>近 7 天每日 Token</h3>
              <div className="usage-daily">
                {usage.daily_7d.map((d) => (
                  <div key={d.day} className="usage-day">
                    <div className="usage-day-bar-wrap">
                      <div
                        className="usage-day-bar"
                        style={{
                          height: `${Math.max(4, Math.round((d.tokens / maxDaily) * 72))}px`,
                        }}
                        title={`${d.day}: ${fmtTokens(d.tokens)}`}
                      />
                    </div>
                    <div className="usage-day-label">{fmtDay(d.day)}</div>
                    <div className="usage-day-tokens muted small">{fmtTokens(d.tokens)}</div>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <p className="muted">暂无用量数据</p>
          )}
        </section>
      ) : null}
    </div>
  )
}
