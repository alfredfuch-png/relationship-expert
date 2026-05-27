import { useEffect, useState } from 'react'
import App from './App.tsx'
import Login from './Login.tsx'
import './Login.css'

export default function Root() {
  const [checking, setChecking] = useState(true)
  const [authRequired, setAuthRequired] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)

  useEffect(() => {
    let cancelled = false
    fetch('/api/auth/status', { credentials: 'include' })
      .then((r) => r.json())
      .then((o: { auth_required?: boolean; authenticated?: boolean }) => {
        if (cancelled) return
        const required = Boolean(o.auth_required)
        setAuthRequired(required)
        setAuthenticated(required ? Boolean(o.authenticated) : true)
      })
      .catch(() => {
        if (!cancelled) {
          setAuthRequired(false)
          setAuthenticated(true)
        }
      })
      .finally(() => {
        if (!cancelled) setChecking(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (checking) {
    return (
      <div className="login-screen">
        <p className="muted">加载中…</p>
      </div>
    )
  }

  if (authRequired && !authenticated) {
    return <Login onSuccess={() => setAuthenticated(true)} />
  }

  return <App />
}
