# Beautiful LinkedIn - Guia Para Agentes (AGENTS.md)

Este arquivo e o ponto de entrada para qualquer agente automatizado (Claude
Code, Codex CLI, Cursor, Gemini CLI, OpenClaw, etc.) que opera neste repo.
Mantenha-o curto: e um mapa, nao um manual. Para o texto de produto e
exemplos de uso final, leia `README.md`.

> O conteudo canonico vive em [`CLAUDE.md`](./CLAUDE.md). Este `AGENTS.md` e
> uma copia vinculada para ferramentas que procuram pelo nome neutro.
> Quando atualizar um, atualize o outro — eles devem ficar em sincronia.

## Contexto

Beautiful LinkedIn e uma CLI Python (com sidecar FastAPI e UI Electron) para
descoberta publica de leads B2B a partir de buscadores, APIs configuradas,
paginas oficiais de empresas, diretorios publicos e captures publicos. A
ferramenta deve manter postura conservadora sobre dados, termos de uso e LGPD.

Stack principal:
- Python 3.11+
- Typer para CLI; FastAPI para o sidecar local
- Rich e questionary para interface terminal
- httpx, BeautifulSoup/lxml, duckduckgo-search e SearxNG para web
- Pydantic para modelos
- pandas/openpyxl para exportacao
- pytest para testes (atualmente ~395 testes em ~49 arquivos)
- Electron + React + TypeScript para o app desktop em `electron/`

## Comandos

```bash
pip install -e ".[dev]"
beautiful-linkedin
beautiful-linkedin search --company-name "Nubank" --titles "marketing,growth" --output output/leads.csv
python -m beautiful_linkedin.main
pytest tests -q
docker compose -f docker-compose.searxng.yml up -d   # SearxNG local opcional
```

## Mapa de arquivos

- `src/beautiful_linkedin/cli.py`, `main.py`, `config.py`, `models.py`,
  `runner.py`, `scrape_modes.py`: nucleo da CLI.
- `src/beautiful_linkedin/providers/`: providers de leads.
  - `apollo.py`, `people_data_labs.py`, `coresignal.py`, `lusha.py`,
    `apify_linkedin.py`: APIs estruturadas.
  - `linkedin_cookie.py`, `linkedin_sales_navigator.py`,
    `linkedin_playwright.py`: caminhos com `li_at` (cookie).
  - `linkedin_people_search.py`: scraper listing-only da aba `/people/`.
    Fetchers em ordem: (1) **CDP** no Chrome aberto via
    `--remote-debugging-port=9222` (recomendado, nao desloga), (2) Scrapling,
    (3) Playwright + `li_at` (pode deslogar). Loop iterativo
    `click -> extract -> validate -> click` que para ao atingir `max_results`
    ou `max_scrolls_cap`. Tambem expoe `resolve_company_size_via_page()`.
  - `linkedin_people_search_progress.py`: `PeopleScrapeProgress` +
    `ProgressStore` (SQLite) com `accepted_urls`/`rejected_urls`/posicoes
    chaveado por `(company_slug, titulos normalizados)`.
  - `public_directories.py`, `common_crawl.py`, `public_search.py`.
  - `factory.py`: registra todos os providers a partir do nome.
- `src/beautiful_linkedin/search/`: motores de busca (SearxNG, Serper, Google
  CSE, Brave, DuckDuckGo, composite/smart).
- `src/beautiful_linkedin/processing/`: extracao, normalizacao, scoring,
  deduplicacao, taxonomias de cargo/seniority e
  `title_validator.py` (gate estrito word-boundary + union dos alias maps,
  usado pelo loop iterativo do people_search).
- `src/beautiful_linkedin/scraping/`: scraping de sites oficiais de empresas.
- `src/beautiful_linkedin/storage/`: persistencia durava e enriquecimento.
  - `saved_leads.py`: `SavedLeadsStore` (SQLite) com CRUD de tabelas, import,
    export, merge, `apply_enrichment_updates` e
    `apply_internal_enrichment_updates` — nunca sobrescrevem `email`
    existente, sempre gravam metadado de enriquecimento.
  - `internal_enrichment.py`: enriquecimento de e-mail **gratis**,
    in-process. Gera padroes (`first`, `first.last`, `flast`, ...), detecta o
    padrao da empresa via `detect_company_pattern` (pares nome+email da
    tabela) e `infer_pattern_from_locals` (shape dos local-parts coletados),
    valida formato + dominio (rejeita gmail/hotmail/...) + MX (resolver
    injetavel), e devolve `EnrichmentStatus` +
    `enrichment_confidence` (0-100).
  - `company_email_harvester.py`: Hunter.io-lite. Fetch homepage + paths
    comuns, extrai e-mails que terminam no dominio, devolve
    `HarvestedEmail(email, source_url)`. HTTP client injetavel.
  - `enrichment.py`: providers pagos
    (`ApolloEnrichmentProvider`, `LushaEnrichmentProvider`,
    `SnovioEnrichmentProvider`, `PdlEnrichmentProvider`) +
    `estimate_enrichment_cost`. Apollo exige `apollo_webhook_url` para
    telefone; Snovio so faz e-mail; Lusha faz 2 chamadas batch
    (uma por `filterBy`).
- `src/beautiful_linkedin/server/app.py`: FastAPI sidecar consumido pelo
  Electron (loopback). Endpoints chave:
  - `POST /search` e `POST /search/start` + `GET /runs/{id}` (async).
  - `POST /people-search/probe/(start|state)` + `POST /people-search/probe`.
  - `GET/POST/DELETE /lead-tables/...` + `import` + `export` + `merge`.
  - `POST /lead-tables/{id}/enrich` — paid; com `confirmed=false` devolve
    so o `EnrichmentEstimate` em BRL.
  - `POST /lead-tables/{id}/internal-enrich` — interno, gratis, so
    `fields="email"`.
  - `POST /lead-tables/{id}/experimental-search`.
  - Modo `browser` exige `accept_risk=true`. Timeout do `people_search`
    escalado por `_people_search_timeout(max_results, cards_per_cycle)`.
- `src/beautiful_linkedin/export/`: CSV/XLSX.
- `src/beautiful_linkedin/ui/`: banner, prompts, progresso, tabelas Rich.
- `electron/`: app desktop. `src/renderer/src/components/SavedLeadsLibrary.tsx`
  consome o enriquecimento; `src/shared/types.ts` espelha os payloads.
- `tests/`: testes unitarios. Convenção: 1 arquivo por modulo testado
  (`test_internal_enrichment.py`, `test_company_email_harvester.py`,
  `test_internal_enrich_endpoint.py`, `test_linkedin_people_search.py`,
  `test_linkedin_people_search_iterative.py`,
  `test_people_scrape_progress.py`, `test_title_validator.py`,
  `test_saved_leads_enrichment.py`, ...).
- `linkedin-api-scrapper/apify-linkedin-profile/`: subprojeto Apify Actor
  (TypeScript). Nao e o core da CLI.
- `docker-compose.searxng.yml`, `searxng-config/settings.yml`: SearxNG opcional.

Evite carregar `node_modules`, `.venv`, `.pytest_cache`, `output/`,
`data/cache.sqlite*` e arquivos grandes de debug sem motivo explicito.

## Fluxo mental

Entrada CLI/interativo -> `scrape_modes.py` ajusta fonte e limites ->
`runner.run_prospecting()` monta settings, cache, motores e providers ->
providers retornam `Lead` -> processamento deduplica/rankeia/valida ->
exportacao CSV/XLSX -> resumo Rich.

`auto` para providers de leads = APIs configuradas (`pdl`, `coresignal`,
`apollo`, `lusha`) + `web`. `apify_linkedin`, `public_directories`,
`common_crawl` e providers com cookie precisam ser pedidos explicitamente.

`auto` para search engines prioriza SearxNG local (`SEARXNG_BASE_URL`),
depois Serper, Google CSE, DuckDuckGo e fallbacks HTML.

## Pipeline de enriquecimento (resumo)

Duas camadas opt-in pelo cliente:

1. **Interno (gratis)** — `POST /lead-tables/{id}/internal-enrich`. So
   e-mail. Resolve dominio -> gera padroes -> detecta padrao da empresa
   pelos pares ja salvos OU pelo shape de local-parts do harvester ->
   valida formato + dominio + MX -> escolhe melhor candidato. Nao
   sobrescreve `email` existente. Nunca chama API paga.
2. **Pagos (creditos)** — `POST /lead-tables/{id}/enrich`. Providers
   `lusha|apollo|snovio|pdl`, `fields=email|phone|both`. `confirmed=false`
   so devolve o `EnrichmentEstimate` (creditos x `credit_costs_brl`);
   `confirmed=true` dispara as APIs. Apollo+telefone exige
   `apollo_webhook_url` HTTPS; Snovio so faz e-mail.

Ambos atualizam `Lead.enrichment_source/status/confidence/email_type/
email_validation_status/enriched_at` via
`SavedLeadsStore.apply_*_enrichment_updates`. O `enrichment_status` da
tabela vira `enriched` quando ao menos um lead recebeu valor novo.

## Loop iterativo da aba People

`LinkedInPeopleSearchProvider` nao roda "fetch -> extrai tudo -> filtra".
O fluxo e:

1. Abre `/company/<slug>/people/?keywords=<termos>` no fetcher escolhido.
2. A cada "Exibir mais resultados" o fetcher chama `on_step(clicks, html)`.
3. O provider extrai cards, ignora URLs ja em `accepted_urls`/`rejected_urls`,
   e roda `validate_lead_titles(strict=True)` por card.
4. Aceitos viram lead com `consultation_note = "Extraido apos N cliques,
   posicao P."`. Rejeitados sao salvos em `progress.rejected_urls`.
5. Retorna `False` no callback quando atinge `max_results` — o fetcher para
   e fecha o navegador.

`ProgressStore` persiste estado entre execucoes. `include_uncertain=True`
desabilita o validador estrito (provider emite warning).

## Padroes de mudanca

- Novo provider de leads: implementar `LeadProvider`, registrar em
  `providers/factory.py`, adicionar var em `config.py`/`.env.example` se
  houver chave, registrar aliases em `cli.parse_lead_providers`, mapear
  source em `runner.py` se necessario, atualizar prompts, cobrir com testes
  mockados.
- Novo provider de enriquecimento pago: mesma interface dos existentes em
  `storage/enrichment.py` (`name`, `enrich(leads, options)`, atributo
  `errors: list[str]` deduplicado). Adicionar ao
  `_build_enrichment_providers` em `server/app.py`, ao
  `_normalized_providers` em `enrichment.py`, expor vars em
  `Settings`/`ApiKeyOverrides`, testar com `respx`/`httpx.MockTransport`.
- Mudancas no enriquecimento interno: novos `EnrichmentPattern` exigem
  atualizar `_classify_local_shape`; manter `PERSONAL_EMAIL_DOMAINS` em
  sincronia; **sempre** injetar `mx_resolver` fake nos testes.
- Novo motor de busca: implementar `SearchEngine`, registrar em
  `search/factory.py` e `cli.parse_search_engines`, atualizar prompts e
  testar fallback sem depender de rede real.
- Mudancas de CLI: atualizar `cli.py`, README se mudar UX publica, e testes
  de CLI.
- Mudancas de extracao/dedupe/scoring: mexer em `processing/` e adicionar
  casos pequenos e deterministas em `tests/`.
- Dependencias novas entram em `pyproject.toml`; mantenha `requirements.txt`
  coerente enquanto existir.
- Toda chamada HTTP externa deve usar os helpers de `api_logging.py`:
  `log_http_error`, `log_transport_error`, `log_unexpected_error`, sempre
  com `provider=...` e contexto util.
- Use `httpx.Client(timeout=..., transport=..., headers=...)`; nao
  introduza cliente async.
- Cache opcional segue `SqliteJsonCache` com payload versionado:
  `{"cache_version": N, "request": ...}`.

## Cuidados

- Nao leia nem exponha `.env`; use `.env.example` para nomes de variaveis.
- Nao commite cache local, arquivos em `output/` ou segredos.
- Prefira testes offline com mocks/fakes para APIs externas e buscadores.
- Preserve mensagens de CLI em pt-BR quando voltadas ao usuario;
  identificadores em ingles.
- Ao alterar comportamento de rede, mantenha timeouts, limites e fallbacks
  claros.
- Common Crawl e diretorios publicos sao fallbacks lentos/opcionais; nao
  inclua em `auto` sem decisao explicita.
- Enriquecimento interno **nunca** sobrescreve `lead.email`; sempre persista
  metadado mesmo em falha (a UI mostra "tentou, sem dominio").
- Enriquecimento pago **sempre** roda primeiro com `confirmed=false` para o
  cliente ver custo em BRL antes de gastar credito.
- Apollo+telefone sem `apollo_webhook_url` HTTPS e bloqueado no
  `model_validator` da request.
- `linkedin_people_search` em CDP (`needs_li_at=False`) e o caminho seguro
  — nao toca no cookie e nao desloga. Fallback Playwright avisa via warning.
- Modo `browser` (Playwright logado fora do CDP) e ARRISCADO: exige
  `accept_risk=true` no endpoint e pode bloquear a conta do LinkedIn.
