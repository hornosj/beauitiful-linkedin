import { defineConfig } from 'electron-vite'
import { loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

export default defineConfig(({ mode }) => {
  // Lê VITE_* do `.env` da RAIZ do projeto (a mesma pasta do sidecar Python),
  // para existir um único arquivo de configuração. Esses valores alimentam o
  // renderer (tela de login) E o processo principal — que repassa SUPABASE_URL
  // ao sidecar empacotado em runtime (em produção o exe não enxerga o .env por
  // conta própria). Só variáveis com prefixo VITE_ chegam ao bundle do app;
  // segredos sem prefixo (APOLLO_API_KEY, li_at, OPENAI_API_KEY...) ficam de
  // fora — é a proteção embutida do Vite.
  const repoRoot = resolve(__dirname, '..')
  const env = loadEnv(mode, repoRoot, 'VITE_')
  const supabaseDefines = {
    'import.meta.env.VITE_SUPABASE_URL': JSON.stringify(env.VITE_SUPABASE_URL ?? ''),
    'import.meta.env.VITE_SUPABASE_AUTH_REQUIRED': JSON.stringify(
      env.VITE_SUPABASE_AUTH_REQUIRED ?? ''
    )
  }

  return {
    main: {
      // Injeta os valores no bundle do main para repassar ao sidecar.
      define: supabaseDefines,
      build: {
        outDir: 'out/main',
        lib: {
          entry: resolve(__dirname, 'src/main/index.ts')
        }
      }
    },
    preload: {
      build: {
        outDir: 'out/preload',
        lib: {
          entry: resolve(__dirname, 'src/preload/index.ts')
        },
        rollupOptions: {
          output: {
            format: 'cjs',
            entryFileNames: '[name].js'
          }
        }
      }
    },
    renderer: {
      root: resolve(__dirname, 'src/renderer'),
      // Lê variáveis VITE_* do `.env` da raiz do projeto (e não de src/renderer/),
      // onde ficam VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY do login por conta.
      envDir: repoRoot,
      build: {
        outDir: 'out/renderer',
        rollupOptions: {
          input: resolve(__dirname, 'src/renderer/index.html')
        }
      },
      plugins: [react()]
    }
  }
})
