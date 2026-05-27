import { useState, type FormEvent } from 'react'
import './Login.css'

type Props = {
  onSuccess: () => void
}

export default function Login({ onSuccess }: Props) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ password }),
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

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={(e) => void handleSubmit(e)}>
        <div className="login-brand">
          <span className="logo">♥</span>
          <span>Romance Expert</span>
        </div>
        <h1>登录</h1>
        <p>请输入访问密码以使用亲密关系顾问。</p>
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
        <button type="submit" className="login-submit" disabled={loading || !password.trim()}>
          {loading ? '验证中…' : '进入'}
        </button>
      </form>
    </div>
  )
}
