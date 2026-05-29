# Beautiful LinkedIn - Guia Para Agentes

Este arquivo e o ponto de entrada para qualquer agente automatizado (Claude Code, Codex CLI, Cursor, Gemini CLI, OpenClaw, etc.) que opera neste repo. Use-o como mapa inicial do projeto; leia o `README.md` apenas quando precisar de detalhes de uso, exemplos completos ou texto de produto.

> O conteudo canonico vive em [`CLAUDE.md`](./CLAUDE.md). Este `AGENTS.md` e uma copia vinculada para ferramentas que procuram pelo nome neutro. Quando atualizar um, atualize o outro — eles devem ficar em sincronia.

## Contexto

Beautiful LinkedIn e uma CLI Python (com sidecar FastAPI e UI Electron) para descoberta publica de leads B2B a partir de buscadores, APIs configuradas, paginas oficiais de empresas, diretorios publicos e captures publicos. A ferramenta deve manter postura conservadora sobre dados, termos de uso e LGPD.

Stack principal:
- Python 3.11+
- Typer para CLI; FastAPI para o sidecar local
- Rich e questionary para interface terminal
- httpx, BeautifulSoup/lxml, duckduckgo-search e SearxNG para web
- Pydantic para modelos
- pandas/openpyxl para exportacao
- pytest para testes (atualmente ~470 testes em ~55 arquivos)
- Electron + React + TypeScript para o app desktop em `electron/`

## Comandos

Instalacao local:
```bash
pip install -e ".[dev]"
```

Rodar a CLI:
```bash
beautiful-linkedin
beautiful-linkedin search --company-name "Nubank" --titles "marketing,growth" --output output/leads.csv
python -m beautiful_linkedin.main
```

Testes:
```bash
pytest tests -q
pytest tests/test_query_builder.py -q
```

SearxNG local opcional:
```bash
docker compose -f docker-compose.searxng.yml up -d
```

## Arquivos Que Importam

- `src/beautiful_linkedin/cli.py`, `main.py`, `config.py`, `models.py`, `runner.py`, `scrape_modes.py`: nucleo da CLI e configuracoes.
- `src/beautiful_linkedin/providers/`: providers de leads estruturados ou busca publica.
  - `apollo.py`, `people_data_labs.py`, `coresignal.py`, `lusha.py`, `apify_linkedin.py`: APIs estruturadas.
  - `linkedin_cookie.py`, `linkedin_sales_navigator.py`, `linkedin_playwright.py`: caminhos com `li_at` (cookie).
  - `linkedin_people_search.py`: scraper listing-only da aba `/people/`. Fetchers em ordem: (1) **CDP** no Chrome aberto via `--remote-debugging-port=9222` (recomendado, nao desloga), (2) Scrapling, (3) Playwright + `li_at` (pode deslogar). Loop iterativo `click -> extract -> validate -> click` que para ao atingir `max_results` ou `max_scrolls_cap`. Expoe `resolve_company_size_via_page()`.
  - `linkedin_people_search_progress.py`: `PeopleScrapeProgress` + `ProgressStore` (SQLite) com `accepted_urls`/`rejected_urls`/posicoes chaveado por `(company_slug, titulos normalizados)`.
  - `public_directories.py`, `common_crawl.py`, `public_search.py`.
  - `factory.py`: registra todos os providers a partir do nome.
- `src/beautiful_linkedin/search/`: motores de busca (SearxNG, Serper, Google CSE, Brave, DuckDuckGo, composite/smart).
- `src/beautiful_linkedin/processing/`: extracao, normalizacao, scoring, deduplicacao, taxonomias de cargo/seniority e `title_validator.py` (gate estrito word-boundary + union dos alias maps, usado pelo loop iterativo do people_search).
- `src/beautiful_linkedin/scraping/`: scraping de sites oficiais de empresas.
- `src/beautiful_linkedin/storage/`: persistencia duravel e enriquecimento.
  - `saved_leads.py`: `SavedLeadsStore` (SQLite) com CRUD de tabelas, import, export, merge, `apply_enrichment_updates` e `apply_internal_enrichment_updates` — nunca sobrescrevem `email` existente, gravam metadados.
  - `internal_enrichment.py`: enriquecimento de e-mail **gratuito**, in-process. Gera padroes (`first`, `first.last`, ...), detecta o padrao da empresa via `detect_company_pattern` (pares salvos) e `infer_pattern_from_locals`, valida formato + dominio + MX, e devolve `EnrichmentStatus` + confidence.
  - `company_email_harvester.py`: Hunter.io-lite. Fetch homepage + paths comuns, extrai e-mails que terminam no dominio.
  - `domain_discovery.py`: descoberta de dominios irmaos grátis via crt.sh, SPF/DMARC e ccTLD variants.
  - `internal_phone_enrichment.py`: Serviço e orquestrador (`InternalPhoneEnrichmentOrchestrator`) para enriquecimento de telefone gratuito em lote.
  - `telegram_pipeline.py`: Pipeline principal de consultas do Telegram/Telethon. Orquestra a busca de CPFs via `/nome` (Findex/Gonzales/Unix), revisao de CPFs, e busca de telefones via `/cpf` (Gonzales SISREG-III), cruzando dados como localizacao, aniversário (DD/MM) e empresa.
  - `telegram_telethon_auth.py` e `telegram_telethon_lookup.py`: Gerenciamento de sessao `.session` do Telethon (envio de codigo, login, 2FA) e requisicoes de busca MTProto no Telegram.
  - `telegram_consult_matcher.py` e `telegram_consult_parser.py`: Parser regex das telas de consulta de CPF/Nome do Telegram, e algoritmo de scoring/cruzamento que compara os resultados com o lead.
  - `telegram_group_phone_lookup.py` e `telegram_group_playwright_lookup.py`: Automacao de Telegram Web via Playwright para grupos de consulta (fallback sem Telethon).
  - `linkedin_profile_validation.py`: Validação de experiência do LinkedIn para os leads salvos. Visita o perfil e captura dados autodeclarados (email, telefone, website, educação, aniversário DD/MM) via CDP (Chrome) ou Playwright com cookie `li_at`.
  - `whatsapp_checker.py`: Verifica se os numeros de telefone encontrados possuem WhatsApp ativo.
  - `phone_receita_cnpj.py`: Consulta telefones institucionais de empresas em bases publicas de CNPJ.
  - `phone_validation.py`, `phone_harvester.py`, `phone_hlr.py`, `phone_serp_search.py`: Suporte de normalizacao, status HLR e pesquisa de vazamentos na web.
  - `enrichment.py`: providers pagos (`ApolloEnrichmentProvider`, `LushaEnrichmentProvider`, `SnovioEnrichmentProvider`, `PdlEnrichmentProvider`) + `estimate_enrichment_cost`.
- `src/beautiful_linkedin/server/app.py`: FastAPI sidecar consumido pelo Electron (loopback). Endpoints chave:
  - `POST /search` e `POST /search/start` + `GET /runs/{id}` (async).
  - `POST /people-search/probe/(start|state)` + `POST /people-search/probe`.
  - `GET/POST/DELETE /lead-tables/...` + `import` + `export` + `merge`.
  - `POST /lead-tables/{id}/enrich` — paid; com `confirmed=false` estima custo.
  - `POST /lead-tables/{id}/internal-enrich` — e-mail gratis blocking.
  - `POST /lead-tables/{id}/internal-enrich/stream` — e-mail gratis via SSE.
  - `POST /lead-tables/{id}/telegram-consult/telethon-pipeline` — pipeline Telethon (Nome -> CPF -> Telefone).
  - `POST /lead-tables/{id}/telegram-phone/telethon-cpf-stage` — resolve telefone para CPF ja extraido.
  - `GET /telegram/telethon/auth/status`, `POST /telegram/telethon/auth/send-code`, `POST /telegram/telethon/auth/sign-in`, `POST /telegram/telethon/auth/logout` — fluxo de autenticacao Telethon (logout revoga a sessao e apaga o `.session` para trocar de conta).
- `electron/`: app desktop (TypeScript + React).
  - `src/renderer/src/components/SavedLeadsLibrary.tsx`: UI principal de visualizacao e enriquecimento de leads salvos.
  - `src/renderer/src/components/TelethonAuthDialog.tsx`: Dialog de login no Telegram/Telethon.
  - `src/renderer/src/components/CpfPickerTelethonDialog.tsx` e `CpfReviewList.tsx`: Interface para selecao de CPFs candidatos encontrados pelo Telethon.
  - `src/renderer/src/styles.css`: Estilização vanilla global.

## Pipeline de Enriquecimento

O enriquecimento de leads salvos roda em duas camadas, ambas opt-in pelo cliente:

1. **Interno (gratis)** — `POST /lead-tables/{id}/internal-enrich` (e-mail) ou `POST /lead-tables/{id}/telegram-consult/telethon-pipeline` (telefone).
   - **Emails**: Inferencia via dominios (DomainDiscoveryService), coleta em sites (CompanyEmailHarvester) e validacao SMTP/MX (SmtpMailboxVerifier).
   - **Telefones**: Pipeline Telethon realiza busca de CPF via nome, CPF Review na UI para selecao do operador, consulta de telefone via CPF (Gonzales), cruzamento de metadados de leads (data de nascimento, UF) e validacao de WhatsApp (WhatsappChecker).
   - **LinkedIn Profile Validation**: Visita o perfil via CDP/Playwright para extrair cargo/empresa atuais, educacao, site, email, telefone e aniversario (DD/MM).
   - Nunca sobrescreve dados de contato existentes.
2. **Pagos (creditos)** — `POST /lead-tables/{id}/enrich`. Lusha, Apollo, Snovio, PDL.

## Loop Iterativo da Aba People

O `LinkedInPeopleSearchProvider` roda na aba `/people/` de empresas no LinkedIn. Ele usa loop iterativo `click -> extract -> validate -> click` gateado pelo `validate_lead_titles(strict=True)` a cada scroll, parando ao atingir `max_results` ou `max_scrolls_cap`.

- **Dedup global (cross-tabela):** o provider recebe `exclude_lead_keys` (injetado pelo `runner` a partir de `SavedLeadsStore.global_dedupe_keys()`). Cards cuja `global_dedupe_key` (URL normalizada -> nome+empresa+cargo) já existe em qualquer tabela salva são ignorados sem contar para `max_results`, e o loop continua clicando (orçamento de scrolls é inflado quando há exclusões) até juntar `max_results` leads inéditos ou esgotar resultados. O mesmo `exclude_lead_keys` alimenta os helpers `_extract_*` do `runner`, então todos os providers respeitam a dedup global.
- **Feedback progressivo:** o provider chama `on_lead_found(lead)` a cada lead aceito durante o scroll. O sidecar acumula esses leads em `RunRecord.found_leads` (deduplicado) e os expõe em `GET /runs/{id}` (`found_leads`/`found_count`); o renderer faz polling via `waitForRun({ onState })` e renderiza `LiveFoundLeads`.

## Responsividade e Interface

Para evitar quebras de layout e sobreposicoes na tabela de leads salvos:
- A tabela `.saved-leads-table` possui largura minima de `1080px` e as colunas estao distribuidas de forma proporcional (Pessoa: 16%, Cargo: 22%, Contato: 18%, Consulta: 16%, Empresa: 12%, Score: 7%, LinkedIn: 9%).
- O scroll horizontal da tabela e orquestrado de forma sincronizada via componente `DualScrollTable` (com scrollbars no topo e base da tabela).
- Botoes longos na coluna "Consulta" (ex: `.api-consult-btn`) possuem truncamento via CSS (`text-overflow: ellipsis`, `overflow: hidden`, `white-space: nowrap`) limitados a `max-width: 100%`.
- O nome da consulta completo e exibido via tooltip HTML (`title`) na passagem do mouse do usuario.
- A tabela de leads salvos rola internamente: `.table-bottom-scroll` tem `max-height: min(68vh, 760px)` com `thead` sticky, e tabelas inline em linhas expandidas (candidatos/evidencias) tem `max-height` + scroll proprio, para a tabela nunca estourar vertical/horizontalmente.

## Privacidade de Fontes na UI

A UI nunca expoe nomes de providers/fontes internas (GON/Gonzales, Findex/Finder, Unix, SISREG, Void, Telethon) ao usuario final. Rotulos sao genericos: linhas de evidencia viram "Consulta por nome"/"Consulta por CPF", a trilha vira "Trilha da consulta", e mensagens de erro do Telegram sao humanizadas em `humanizeBlockedReason`. Codigo/identificadores internos podem manter os nomes; apenas strings renderizadas/`title`/`aria-label` devem ser genericas.

## Padroes de Mudanca

- Novo provider de leads/enriquecimento: implementar interface, registrar no factory, adicionar chaves ao `.env.example`, testar offline com mocks (ex: `respx`, `httpx.MockTransport`).
- Mudancas em Telethon/Telegram/Enriquecimento Interno: injetar resolvers e resolvers de DNS/MX falsificados nos testes, nao bater em rede ou API Telegram real nos testes.
- Alteracoes de banco de dados/schema: migrar schema em `saved_leads.py` usando `_ensure_column` de forma retrocompativel.

## Cuidados

- **Nao ler nem expor `.env`**; use `.env.example` para documentar variaveis de configuracao.
- Nunca commite sessoes do Telegram (`.session`), caches locais (`.sqlite`), segredos ou pastas grandes.
- Sempre preserve mensagens voltadas ao usuario em Portugues (pt-BR); codigo e identificadores em ingles.
- O EHLO do `SmtpMailboxVerifier` exige FQDN ou `local_hostname` configurado para evitar bloqueio por servidores de e-mail estritos.
