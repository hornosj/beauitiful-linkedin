import { createClient, type SupabaseClient } from '@supabase/supabase-js'

/**
 * Cliente Supabase do renderer (controle de acesso por conta).
 *
 * A URL e a anon key são PÚBLICAS por design (a anon key só permite operações
 * gateadas por Row Level Security / Auth), então podem ser embarcadas no app.
 * Elas vêm do build via Vite: defina-as em `electron/.env`:
 *
 *   VITE_SUPABASE_URL=https://SEU-PROJETO.supabase.co
 *   VITE_SUPABASE_ANON_KEY=eyJhbGci...
 *
 * Sem essas variáveis o app abre SEM login (modo dev/local) e o sidecar também
 * roda aberto — os dois lados ligam a auth juntos.
 */
const url = ((import.meta.env.VITE_SUPABASE_URL as string | undefined) ?? '').trim()
const anonKey = ((import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined) ?? '').trim()

/** True quando o Supabase está configurado — liga a tela de login. */
export const authEnabled = Boolean(url && anonKey)

export const supabase: SupabaseClient | null = authEnabled
  ? createClient(url, anonKey, {
      auth: {
        // Mantém a sessão entre aberturas do app e renova o token de ~1h
        // automaticamente. Se o usuário for banido/deletado no painel, o
        // refresh falha e o onAuthStateChange dispara SIGNED_OUT.
        persistSession: true,
        autoRefreshToken: true,
        // App desktop não usa redirect de OAuth na URL.
        detectSessionInUrl: false
      }
    })
  : null

/**
 * Devolve o access token atual (ou null se a auth estiver desligada / sem
 * sessão). Usado pelo ApiClient para anexar `Authorization: Bearer`.
 */
export async function getAccessToken(): Promise<string | null> {
  if (!supabase) return null
  const { data } = await supabase.auth.getSession()
  return data.session?.access_token ?? null
}
