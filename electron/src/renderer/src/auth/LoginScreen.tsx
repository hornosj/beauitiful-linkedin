import { useState, type FormEvent } from 'react'
import { supabase } from './supabaseClient'

/** Traduz as mensagens de erro do Supabase para pt-BR amigável. */
function humanizeAuthError(message: string): string {
  const m = message.toLowerCase()
  if (m.includes('invalid login credentials')) return 'E-mail ou senha incorretos.'
  if (m.includes('email not confirmed')) return 'E-mail ainda não confirmado. Fale com o administrador.'
  if (m.includes('rate limit') || m.includes('too many'))
    return 'Muitas tentativas. Aguarde alguns minutos e tente de novo.'
  if (m.includes('network') || m.includes('failed to fetch'))
    return 'Sem conexão com o servidor de login. Verifique sua internet.'
  if (m.includes('user is banned') || m.includes('banned'))
    return 'Acesso revogado. Fale com o administrador.'
  return message
}

/**
 * Tela de login mostrada quando o controle de acesso está ligado e não há
 * sessão válida. O cadastro é feito apenas pelo administrador no painel do
 * Supabase — por isso não há opção de "criar conta" aqui.
 */
export default function LoginScreen(): JSX.Element {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (event: FormEvent): Promise<void> => {
    event.preventDefault()
    if (!supabase || busy) return
    setBusy(true)
    setError(null)
    try {
      const { error: signInError } = await supabase.auth.signInWithPassword({
        email: email.trim(),
        password
      })
      if (signInError) {
        setError(humanizeAuthError(signInError.message))
        return
      }
      // Sucesso: o AuthGate detecta a nova sessão via onAuthStateChange e troca
      // a tela automaticamente. Mantemos `busy` ligado até a desmontagem.
    } catch (err) {
      setError(
        err instanceof Error
          ? humanizeAuthError(err.message)
          : 'Não foi possível entrar. Tente novamente.'
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="login-brand" aria-label="Beautiful LinkedIn">
          <img src="./beautiful-linkedin-icon.png" alt="" />
          <span>
            <span className="app-brand-beautiful">Beautiful</span>
            <span className="app-brand-linked">Linked</span>
            <span className="app-brand-in">in</span>
          </span>
        </div>
        <h1 className="login-title">Entrar na sua conta</h1>
        <p className="login-subtitle">Acesso restrito a colaboradores autorizados.</p>

        <label className="login-field">
          <span>E-mail</span>
          <input
            type="email"
            autoComplete="username"
            autoFocus
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="voce@empresa.com"
            disabled={busy}
          />
        </label>

        <label className="login-field">
          <span>Senha</span>
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            disabled={busy}
          />
        </label>

        {error && (
          <div className="login-error" role="alert">
            {error}
          </div>
        )}

        <button type="submit" className="login-submit" disabled={busy || !email || !password}>
          {busy ? 'Entrando…' : 'Entrar'}
        </button>

        <p className="login-hint">Esqueceu a senha? Fale com o administrador.</p>
      </form>
      <div className="login-footer" aria-label="jpAoH Software Solutions © 2026">
        jpAoH Software Solutions © 2026
      </div>
    </div>
  )
}
