# Arquitetura — Beautiful LinkedIn

Documento de referência arquitetural. Para uso do CLI, ver `README.md`. Para
mapa de arquivos e cuidados operacionais, ver `CLAUDE.md`. Para decisões
estruturais e seus *porquês*, ver `decisions.md`.

## 1. Visão geral

Beautiful LinkedIn é um agregador de descoberta pública de leads B2B. O sistema
existe em três camadas que se comunicam via processos:

```
┌─────────────────────────────────────────────────────────────┐
│ UI (opt-in)                                                 │
│ ┌──────────────┐        ┌──────────────────────────┐        │
│ │ CLI Typer    │        │ Electron desktop (React) │        │
│ └──────┬───────┘        └─────────────┬────────────┘        │
└────────┼──────────────────────────────┼─────────────────────┘
         │ in-process                   │ HTTP loopback
         │ (Python)                     │ (FastAPI sidecar)
         ▼                              ▼
┌─────────────────────────────────────────────────────────────┐
│ Núcleo Python — `beautiful_linkedin`                        │
│                                                             │
│   runner.run_prospecting()                                  │
│        │                                                    │
│        ├─► Providers (APIs pagas, cookie, public web)       │
│        ├─► Search engines (SearxNG, Serper, DDG, …)         │
│        ├─► Processing (extract, validate, dedup, score)     │
│        ├─► Storage (saved leads SQLite, enrichment)         │
│        └─► Export (CSV / XLSX)                              │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│ Persistência local                                          │
│   data/saved_leads.sqlite     leads salvos por tabela       │
│   data/cache.sqlite (opt-in)  cache HTTP versionado         │
│   ProgressStore (SQLite JSON) loop iterativo people search  │
└─────────────────────────────────────────────────────────────┘
```

Princípio condutor: **toda fonte é pública e opt-in**. APIs com chave faltando
são ignoradas com warning; providers de risco (cookie, navegador) só rodam
mediante seleção explícita; web scraping é o fallback gratuito.

## 2. Stack

- **Python 3.11+** — core, CLI, sidecar. Tipado com Pydantic. CLI via Typer +
  Rich + questionary.
- **HTTP** — `httpx.Client` síncrono em todo o projeto. Cache opcional em
  SQLite (`SqliteJsonCache`) com payload versionado.
- **FastAPI** — sidecar HTTP loopback consumido pelo Electron.
- **Electron + TypeScript + React** — app desktop. Empacotado pelo
  `electron-builder` com o sidecar Python compilado via PyInstaller.
- **pytest** — ~461 testes em ~52 arquivos. Padrão é offline com mocks
  injetáveis; testes não tocam DNS/HTTP/SMTP reais.

## 3. Núcleo Python

### 3.1 Fluxo principal

```
CLI/sidecar → scrape_modes.apply_scrape_mode()
            → runner.run_prospecting(settings, cache, engines, providers)
            → providers paralelos → List[Lead]
            → processing (normalize, validate, dedup, score)
            → export CSV/XLSX OR retorno tipado p/ sidecar
```

`scrape_modes.py` define três modos: `api` (somente APIs estruturadas), `serp`
(buscadores web públicos) e `cookie` (Voyager via `li_at`). Cada modo ajusta
limites, fontes habilitadas e timeouts antes de chamar o runner.

### 3.2 Providers de leads (`providers/`)

Cada provider implementa `LeadProvider`. São registrados em
`providers/factory.py` e selecionados via CLI (`--lead-providers`) ou payload do
sidecar. Categorias:

- **APIs pagas estruturadas:** `apollo.py`, `people_data_labs.py`,
  `coresignal.py`, `lusha.py`. Tentam resolver `company_id` antes de cair na
  estratégia by-search.
- **Cookie / Voyager:** `linkedin_cookie.py`, `linkedin_sales_navigator.py`.
  Puxam funcionários por empresa e filtram cargo localmente. Risco de bloqueio
  de conta — opt-in.
- **People Search da aba pública:** `linkedin_people_search.py` é o caminho
  preferido. Roda um **loop iterativo** `click → extract → validate → click`
  sobre `/company/<slug>/people/`. Três fetchers em ordem de preferência:
  1. **CDP** — anexa a um Chrome do usuário aberto com
     `--remote-debugging-port`. Não toca cookie, não desloga.
  2. **Scrapling** — fallback automatizado.
  3. **Playwright + li_at** — último recurso, pode deslogar.
  O loop persiste estado em `linkedin_people_search_progress.py`
  (`ProgressStore` sobre `SqliteJsonCache`), chaveado por `(company_slug,
  títulos normalizados)`. Re-execuções pulam URLs já classificadas.
- **LLM extractor / Playwright direto:** `linkedin_llm_extractor.py`,
  `linkedin_playwright.py`. Heurísticos auxiliares.
- **Públicos opcionais:** `public_directories.py` (TheOrg, RocketReach),
  `common_crawl.py` (CDX index, lento), `apify_linkedin.py` (Actor terceiro,
  opt-in explícito).

Auto-mode: `auto` resolve para APIs configuradas + `web`. Providers de risco
ou pesados (`apify_linkedin`, `common_crawl`, `public_directories`, cookie) só
entram quando selecionados nominalmente.

### 3.3 Motores de busca (`search/`)

`SearchEngine` é a interface comum. `smart_composite_search.py` consulta todos
os engines em paralelo e deduplica por URL. `factory.py` aplica a ordem `auto`:

1. SearxNG (`searxng_search.py`) quando `SEARXNG_BASE_URL` existe — meta-buscador
   self-hosted, gratuito.
2. Serper (`serper_search.py`) — simplifica query em HTTP 400.
3. Google Custom Search (`google_custom_search.py`).
4. DuckDuckGo API e HTML (`duckduckgo_search.py`, `duckduckgo_html_search.py`).
5. Brave API e HTML (`brave_search.py`, `brave_html_search.py`).
6. Bing HTML, Google HTML — fallbacks puros sem API key.

`query_builder.py` monta dorks balanceados por empresa e família de cargos
(ex: `build_balanced_queries_for_company`). Resultado é uma lista de queries
distintas usadas tanto pelos engines quanto pelo loop iterativo.

### 3.4 Processing (`processing/`)

Pipeline pós-fetch determinista:

- `lead_extractor.py` — parsing de páginas e SERPs em campos do `Lead`.
- `normalizer.py` — normalização de nome/cargo/empresa.
- `title_validator.py` — gate estrito pós-extração. Word-boundary regex sobre
  a união de `TITLE_ALIASES + SEARCH_TITLE_ALIASES + DEEP_SEARCH_TITLE_ALIASES`.
  É o mesmo validador chamado dentro do loop iterativo do people search para
  decidir card-a-card.
- `deduplicator.py` — heurística de similaridade (nome + empresa + URL).
- `scorer.py` — score final para ordenação.
- Taxonomias: `function_taxonomy.py`, `role_taxonomy.py`, `seniority_taxonomy.py`,
  `title_aliases.py` — vocabulário canônico de funções, áreas e senioridade.
- `lead_filter.py` — filtros configuráveis aplicados pelo sidecar.
- `company_size.py` — classificação de porte da empresa.
- `table_presets.py` — colunas pré-definidas por contexto de uso.

### 3.5 Storage e enriquecimento (`storage/`)

Persistência durável e enriquecimento de leads salvos:

- `saved_leads.py` — `SavedLeadsStore` (SQLite). Tabelas de leads salvos,
  dedup, import/export, e o helper crítico `_merge_email_verification` que
  governa o contrato cross-provider de e-mail.
- `internal_enrichment.py` — **enriquecimento grátis, in-process**:
  - `InternalLeadEnrichmentService.enrich_lead_multi_domain` — gera local-parts
    (`first`, `first.last`, `flast`...), tenta cada domínio 1-a-1, curto-circuita
    em VALID, senão mantém o melhor PROBABLE.
  - `collect_company_domains(leads)` — agrega por empresa todos os domínios
    já vistos (`lead.email` peso 3 + `lead.company_domain` peso 1). Permite
    herança lateral entre leads da mesma empresa.
  - `InternalEnrichmentOrchestrator` — três fases:
    `discovering → harvesting → validating`. Emite eventos SSE.
  - `SmtpMailboxVerifier` — probe RCPT conservador (sem DATA). Cache catch-all
    / no-mx / unreachable por domínio. Semáforo por host MX
    (`max_per_host=2`). `local_hostname` configurável obrigatório.
- `domain_discovery.py` — descoberta de domínios irmãos, **grátis**, gateada por MX:
  - `CrtShClient` — SANs de certificados via crt.sh.
  - `SpfDmarcDiscoverer` — TXT do SPF/DMARC.
  - `CctldVariantGenerator` — permuta core sobre TLDs comuns.
  - `DomainDiscoveryService` — orquestrador best-effort: source quebrada nunca
    derruba o run.
- `company_email_harvester.py` — Hunter.io leve. Crawla homepage + paths
  comuns (`/contato`, `/sobre`...) e extrai e-mails que terminam no domínio
  da empresa. HTTP client injetável.
- `enrichment.py` — **providers pagos** (Apollo, Lusha, Snovio, PDL) +
  `estimate_enrichment_cost()`. Apollo telefone exige
  `apollo_webhook_url` HTTPS. Snovio só faz e-mail. Lusha faz duas chamadas
  batch (e-mail + telefone).
- `experimental_search.py` — busca experimental opt-in com source type
  separado.

### 3.6 Contrato cross-provider de e-mail

Regra central: **o e-mail primário (`lead.email`) nunca é sobrescrito**. Tanto o
enriquecimento interno quanto o pago chamam `_merge_email_verification`:

- Segunda fonte retorna **mesmo e-mail** → label entra em `email_verified_by`.
  Quando `length >= 2`, UI mostra selo "✓ Verificado".
- Segunda fonte retorna **e-mail diferente** → vira entrada em
  `email_alternatives` (deduped por `(email, source)`).

Schema migrado via `_ensure_column` para colunas JSON
`email_verified_by_json` + `email_alternatives_json`.

## 4. Sidecar HTTP (FastAPI) — `server/`

O sidecar é o que o Electron consome. **Loopback only** — nunca exposto à
rede. Risky modes (Playwright) exigem `accept_risk: true` no payload como
guarda server-side.

### 4.1 Endpoints principais

| Método | Rota | Função |
| --- | --- | --- |
| `POST` | `/search` | Busca síncrona. |
| `POST` | `/search/start` | Busca assíncrona. Devolve `run_id`. |
| `GET`  | `/runs/{id}` | Estado e resultado de uma run. |
| `POST` | `/people-search/probe/start` | Sondagem CDP / Scrapling / Playwright. |
| `POST` | `/people-search/probe/state` | Estado da sondagem ativa. |
| `POST` | `/lead-tables/...` | CRUD + `import` + `export` + `merge`. |
| `POST` | `/lead-tables/{id}/enrich` | Enriquecimento pago. `confirmed=false` devolve só `EnrichmentEstimate`. |
| `POST` | `/lead-tables/{id}/internal-enrich` | Enriquecimento interno blocking. Só `fields="email"`. |
| `POST` | `/lead-tables/{id}/internal-enrich/stream` | Mesma lógica via SSE. Eventos `start / phase / discovery / domain / lead / progress / done`. |
| `POST` | `/lead-tables/{id}/experimental-search` | Busca experimental opt-in. |

### 4.2 Injeção de dependências e testabilidade

Todos os recursos de rede são lazy-resolved via helpers para que testes
monkeypatchem e fiquem offline:

- `_default_mx_resolver()`
- `_default_txt_resolver()`
- `_default_mailbox_verifier()`
- `_default_company_email_harvester()`
- `_default_domain_discoverer()`

Padrão de teste: fixture autouse `_disable_real_domain_discovery` em
`tests/test_internal_enrich_endpoint.py` serve de referência.

### 4.3 Timeouts dinâmicos

Provider timeout em modo `people_search` é escalonado por
`_people_search_timeout(max_results, cards_per_cycle)` — o loop iterativo pode
levar muitos cliques, então o timeout cresce proporcionalmente em vez de ser
fixo.

## 5. Aplicação desktop — `electron/`

Aplicação Electron com renderer React + TypeScript. Comunica com o sidecar via
HTTP loopback usando `ApiClient` (`src/shared/api.ts`). Tipos compartilhados
em `src/shared/types.ts` (espelha os payloads Python).

### 5.1 Boot do sidecar — `electron/src/main/`

- `sidecar.ts` — `startSidecar()` resolve o binário (PyInstaller empacotado ou
  Python do sistema), monta `PYTHONPATH`, define `SEARXNG_BASE_URL` para o
  endpoint local do docker-compose quando o usuário não configurou, e aguarda
  o `READY_TOKEN` na stdout antes de marcar pronto.
- `sidecar-protocol.ts` — parse do handshake stdout (porta + ready token).
- `chrome.ts` — bootstrap do Chrome com `--remote-debugging-port` para o modo
  CDP do people search.
- `index.ts` — registra IPC, gerencia ciclo de vida do sidecar e da janela.

### 5.2 Renderer — `electron/src/renderer/src/`

Componentes-chave (`components/`):

- `SavedLeadsLibrary.tsx` — lista/edita tabelas salvas, dispara
  enriquecimento, renderiza `LeadEmailCell` (primário + selo "Verificado" +
  `email_alternatives` expansíveis).
- `InternalEnrichProgress.tsx` — modal de progresso SSE do enriquecimento
  interno. `PHASE_LABEL` cobre as fases; `reduceProgress` agrega counters por
  fase e por fonte de discovery.
- `SearchForm.tsx`, `ResultsTable.tsx`, `FilterPanel.tsx` — fluxo de busca.
- `ChromeBootstrapModal.tsx` — UX do passo CDP.
- `RiskWarning.tsx`, `SmallCompanyDialog.tsx` — confirmações de modo risky.
- `ProbeProgress.tsx`, `LiveActivityBubbles.tsx`, `StatusPill.tsx`,
  `DiagnosticPanel.tsx`, `SettingsPanel.tsx` — telemetria e configurações.

State de run de enriquecimento (`enrichment/`):

- `EnrichmentRunnerContext.tsx` — provider App-level que mantém o run ativo em
  memória. O fetch SSE vive aqui, **não no componente** — trocar de view (Nova
  busca / Leads / Configurações) não cancela o run.
- `EnrichmentRunPill.tsx` — chip flutuante bottom-right quando há run ativo e
  o modal está fechado. Clique reabre; ✕ cancela.

### 5.3 SSE no renderer

`ApiClient.streamInternalEnrich(tableId, payload, onEvent, signal)` consome SSE
via `fetch` + `ReadableStream` com suporte a `AbortController`. A union
discriminada `InternalEnrichStreamEvent` (em `shared/types.ts`) reflete os
eventos do server.

## 6. Empacotamento

Build de release (ver `docs/PACKAGING.md`):

1. PyInstaller compila `src/beautiful_linkedin/server/` num executável único
   (`scripts/build-sidecar.ps1`).
2. `electron-builder` empacota o app e copia o sidecar como recurso.
3. Cliente final não precisa de Python instalado.

Python 3.11/3.12 recomendados para release — `rookiepy` pode falhar a instalar
em 3.14 por causa do PyO3 da dependência.

## 7. Persistência

Três SQLites locais, todos versionados sob `data/` (ignorado no git):

- `saved_leads.sqlite` — tabelas de leads salvos + enriquecimento.
- `cache.sqlite` (opt-in) — cache de respostas HTTP. Payload versionado:
  `{"cache_version": N, "request": ...}`.
- `ProgressStore` interno — estado do loop iterativo do people search.

## 8. Postura sobre rede e dados

- Sem login automatizado, sem solver de captcha, sem proxy, sem mascaramento
  de IP, sem evasão.
- Toda chamada HTTP externa passa pelos helpers de `api_logging.py`
  (`log_http_error`, `log_transport_error`, `log_unexpected_error`) com
  `provider=...` e contexto útil.
- `httpx.Client` síncrono com `timeout`, `transport` e `headers` explícitos. Sem
  cliente async.
- Enriquecimento pago **sempre** estima primeiro (`confirmed=false`) antes de
  gastar crédito.
- LGPD / ToS: dados públicos, postura conservadora, mensagens de UI em pt-BR
  preservando o vocabulário do projeto.
