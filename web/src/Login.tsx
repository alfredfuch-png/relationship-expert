import { useEffect, useState, type FormEvent } from 'react'
import './Login.css'

type AuthMode = 'none' | 'shared_password' | 'accounts'

type Props = {
  onSuccess: () => void
}

export default function Login({ onSuccess }: Props) {
  const [authMode, setAuthMode] = useState<AuthMode>('accounts')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    fetch('/api/auth/status', { credentials: 'include' })
      .then((r) => r.json())
      .then((o: { auth_mode?: AuthMode }) => {
        if (o.auth_mode) setAuthMode(o.auth_mode)
      })
      .catch(() => undefined)
  }, [])

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          username: authMode === 'accounts' ? username.trim() : '',
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

  const accountsMode = authMode === 'accounts'

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={(e) => void handleSubmit(e)}>
        <div className="login-brand">
          <span className="logo">♥</span>
          <span>Romance Expert</span>
        </div>
        <h1>登录</h1>
        <p>
          {accountsMode
            ? '使用你的账户名和密码登录，对话记录仅自己可见。'
            : '请输入访问密码以使用亲密关系顾问。'}
        </p>
        {accountsMode ? (
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
            autoComplete="current-password"
            value={password}
            disabled={loading}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {error ? <p className="login-error">{error}</p> : null}
        <button
          type="submit"
          className="login-submit"
          disabled={
            loading ||
            !password.trim() ||
            (accountsMode && !username.trim())
          }
        >
          {loading ? '验证中…' : '进入'}
        </button>
      </form>
    </div>
  )
}
