import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode
} from 'react'
import type { Session } from '@supabase/supabase-js'
import { authEnabled, supabase } from './supabaseClient'
import LoginScreen from './LoginScreen'

interface AuthContextValue {
  /** True quando o controle de acesso por conta está ligado (Supabase). */
  enabled: boolean
  /** E-mail do usuário logado (ou null quando a auth está desligada). */
  email: string | null
  /** Encerra a sessão e volta para a tela de login. */
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue>({
  enabled: false,
  email: null,
  signOut: async () => {}
})

/** Hook para o resto do app ler o usuário logado e disparar logout. */
export function useAuth(): AuthContextValue {
  return useContext(AuthContext)
}

const THEME_STORAGE_KEY = 'beautiful-linkedin.theme'

/** Aplica o tema salvo já na tela de login, para não piscar claro/escuro. */
function applyStoredTheme(): void {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches
    const dark = stored === 'dark' || (!stored && prefersDark)
    document.documentElement.classList.toggle('dark', dark)
  } catch {
    // localStorage indisponível — segue no tema padrão.
  }
}

function AuthSplash(): JSX.Element {
  return (
    <div className="login-screen">
      <div className="login-splash">
        <span className="login-splash-dot" />
        Carregando…
      </div>
    </div>
  )
}

/**
 * Portão de autenticação. Envolve o app inteiro:
 *  - auth desligada (sem Supabase configurado) → renderiza o app direto (dev);
 *  - carregando a sessão → splash;
 *  - sem sessão → tela de login;
 *  - sessão válida → app, com contexto de usuário/logout disponível.
 *
 * Revogação: o supabase-js renova o token de ~1h sozinho. Se o usuário for
 * banido/deletado no painel, o refresh falha, o onAuthStateChange dispara
 * SIGNED_OUT e o portão volta para a tela de login.
 */
export default function AuthGate({ children }: { children: ReactNode }): JSX.Element {
  const [session, setSession] = useState<Session | null>(null)
  const [loading, setLoading] = useState(authEnabled)

  useEffect(() => {
    applyStoredTheme()
  }, [])

  useEffect(() => {
    if (!authEnabled || !supabase) {
      setLoading(false)
      return
    }
    let active = true
    void supabase.auth.getSession().then(({ data }) => {
      if (!active) return
      setSession(data.session)
      setLoading(false)
    })
    const { data: sub } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next)
      setLoading(false)
    })
    return () => {
      active = false
      sub.subscription.unsubscribe()
    }
  }, [])

  const signOut = async (): Promise<void> => {
    if (supabase) await supabase.auth.signOut()
  }

  // Auth desligada: app abre direto (modo dev/local sem Supabase).
  if (!authEnabled) {
    return (
      <AuthContext.Provider value={{ enabled: false, email: null, signOut }}>
        {children}
      </AuthContext.Provider>
    )
  }

  if (loading) return <AuthSplash />

  if (!session) return <LoginScreen />

  return (
    <AuthContext.Provider
      value={{ enabled: true, email: session.user?.email ?? null, signOut }}
    >
      {children}
    </AuthContext.Provider>
  )
}
