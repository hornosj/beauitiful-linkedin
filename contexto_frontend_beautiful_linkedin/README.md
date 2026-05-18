# contexto_frontend_beautiful_linkedin

Mock HTML completo da interface do **Beautiful LinkedIn** (app Electron).

## Como usar

Abra `index.html` diretamente no browser — **sem servidor necessário**. O arquivo
é 100% self-contained (fontes via CDN, sem dependências locais).

## O que está mockado

| Área | Detalhe |
|------|---------|
| Shell principal | Titlebar glassmorphic, sidebar com nav, toolbar sticky |
| Nova busca | Formulário completo, filtros avançados, tabela de resultados com score bars e quick-actions |
| Leads salvos | Lista de tabelas salvas, tabela de leads com checkbox de seleção, filtro de texto |
| Preferências | Sheet com abas: Geral, Conta, Chaves de API, Provedores, Exportação, Atalhos, Sobre |
| Diagnóstico | Sheet com stats por provider e chaves carregadas no sidecar |
| Dialogs | Modo arriscado (browser), Chrome CDP, Empresa pequena |
| Enriquecimento interno | Modal animado com progress bars e feed de leads em tempo real |
| Enriquecimento pago | Painel com seleção de providers, modo cascata/paralelo, estimativa de custo |
| Pill minimizado | Estado minimizado do enriquecimento com track de progresso |
| Toast | Notificações flutuantes de sucesso/erro |
| Tema escuro | Toggle funcional via botão na titlebar ou painel de preferências |

## Interações funcionais

- **Ctrl+Enter / botão Buscar** → simula busca com toasts em etapas
- **Ctrl+,** → abre Preferências
- **Ctrl+N** → volta para Nova busca
- **Esc** → fecha qualquer modal/sheet aberto
- **Chips de senioridade** → toggle on/off
- **Segmented controls** → modo de coleta, intensidade, cascata/paralelo
- **Filtro de leads** → filtra resultados em tempo real
- **Hover em linhas de leads** → mostra quick-actions (Visitar / Copiar)
- **Enriquecer interno** → abre modal com animação de progresso
- **Minimizar enriquecimento** → cria pill flutuante que reabre o modal
- **Selecionar tabela salva** → carrega nome e meta no painel direito

## Stack do app real

```
Electron 29 + React 18 + TypeScript + Tailwind CSS
Fonte: Inter (sans) + Roboto Mono (mono)
Backend sidecar: FastAPI (Python 3.11+)
```

## Tokens de design

| Token | Valor (light) |
|-------|---------------|
| `--bg` | `#f5f5f7` |
| `--surface` | `#ffffff` |
| `--accent` | `#007aff` |
| `--ink` | `#1d1d1f` |
| `--line` | `#e5e5ea` |
| `--success` | `#34c759` |
| `--risky` | `#ff3b30` |
