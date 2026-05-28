import { useEffect, useState, type FormEvent } from 'react'
import './Login.css'

type AuthMode = 'none' | 'shared_password' | 'accounts'
type View = 'login' | 'register'

type Props = {
  onSuccess: () => void
}

type AuthStatus = {
  auth_mode?: AuthMode
  registration_enabled?: boolean
}

export default function Login({ onSuccess }: Props) {
  const [authMode, setAuthMode] = useState<AuthMode>('accounts')
  const [registrationOpen, setRegistrationOpen] = useState(false)
  const [view, setView] = useState<View>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [passwordConfirm, setPasswordConfirm] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    fetch('/api/auth/status', { credentials: 'include' })
      .then((r) => r.json())
      .then((o: AuthStatus) => {
        if (o.auth_mode) setAuthMode(o.auth_mode)
        if (o.registration_enabled) {
          setRegistrationOpen(true)
          setView('register')
        }
      })
      .catch(() => undefined)
  }, [])

  async function handleLogin(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          username: accountsMode ? username.trim() : '',
          password,
        }),
      })
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as { detail?: string } | null
        setError(typeof data?.detail === 'string' ? data.detail : '登录失败')
        return
      }
      onSuccess()
    } catch {
      setError('无法连接服务器')
    } finally {
      setLoading(false)
    }
  }

  async function handleRegister(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (password !== passwordConfirm) {
      setError('两次输入的密码不一致')
      return
    }
    setLoading(true)
    try {
      const res = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          username: username.trim(),
          password,
          invite_code: inviteCode.trim(),
        }),
      })
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as { detail?: string } | null
        setError(typeof data?.detail === 'string' ? data.detail : '注册失败')
        return
      }
      onSuccess()
    } catch {
      setError('无法连接服务器')
    } finally {
      setLoading(false)
    }
  }

  const accountsMode = authMode === 'accounts'
  const isRegister = view === 'register' && registrationOpen

  function switchView(next: View) {
    setView(next)
    setError('')
    setPassword('')
    setPasswordConfirm('')
  }

  return (
    <div className="login-screen">
      <form
        className="login-card"
        onSubmit={(e) => void (isRegister ? handleRegister(e) : handleLogin(e))}
      >
        <div className="login-brand">
          <span className="logo">♥</span>
          <span>Romance Expert</span>
        </div>

        {registrationOpen ? (
          <div className="login-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={view === 'login'}
              className={view === 'login' ? 'active' : ''}
              onClick={() => switchView('login')}
            >
              登录
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={view === 'register'}
              className={view === 'register' ? 'active' : ''}
              onClick={() => switchView('register')}
            >
              注册
            </button>
          </div>
        ) : (
          <h1>登录</h1>
        )}

        <p>
          {isRegister
            ? '填写邀请码创建账户，对话记录仅自己可见。'
            : accountsMode
              ? '使用账户名和密码登录。'
              : '请输入访问密码以使用亲密关系顾问。'}
        </p>

        {(accountsMode || isRegister) ? (
          <div className="login-field">
            <label htmlFor="username">账户名</label>
            <input
              id="username"
              type="text"
              autoComplete="username"
              value={username}
              disabled={loading}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
        ) : null}

        <div className="login-field">
          <label htmlFor="password">密码</label>
          <input
            id="password"
            type="password"
            autoComplete={isRegister ? 'new-password' : 'current-password'}
            value={password}
            disabled={loading}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        {isRegister ? (
          <>
            <div className="login-field">
              <label htmlFor="password-confirm">确认密码</label>
              <input
                id="password-confirm"
                type="password"
                autoComplete="new-password"
                value={passwordConfirm}
                disabled={loading}
                onChange={(e) => setPasswordConfirm(e.target.value)}
              />
            </div>
            <div className="login-field">
              <label htmlFor="invite-code">邀请码</label>
              <input
                id="invite-code"
                type="text"
                autoComplete="off"
                value={inviteCode}
                disabled={loading}
                onChange={(e) => setInviteCode(e.target.value)}
              />
            </div>
          </>
        ) : null}

        {error ? <p className="login-error">{error}</p> : null}

        <button
          type="submit"
          className="login-submit"
          disabled={
            loading ||
            !password.trim() ||
            ((accountsMode || isRegister) && !username.trim()) ||
            (isRegister && (!inviteCode.trim() || !passwordConfirm.trim()))
          }
        >
          {loading ? '请稍候…' : isRegister ? '创建账户' : '进入'}
        </button>
      </form>
    </div>
  )
}
