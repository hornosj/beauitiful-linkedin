# Beautiful LinkedIn - Guia Para Agentes

Este arquivo deve ser curto. Use-o como mapa inicial do projeto; leia o `README.md`
apenas quando precisar de detalhes de uso, exemplos completos ou texto de produto.

## Contexto

Beautiful LinkedIn e uma CLI Python para descoberta publica de leads B2B a partir de
buscadores, APIs configuradas, paginas oficiais de empresas, diretorios publicos e
captures publicos. A ferramenta deve manter postura conservadora sobre dados,
termos de uso e LGPD.

Stack principal:
- Python 3.11+
- Typer para CLI
- Rich e questionary para interface terminal
- httpx, BeautifulSoup/lxml, duckduckgo-search e SearxNG para web
- Pydantic para modelos
- pandas/openpyxl para exportacao
- pytest para testes

Estado atual dos testes: `pytest tests -q` deve passar com ~461 testes em ~52 arquivos.

## Comandos

Instalacao local:

```bash
pip install -e ".[dev]"
```

Rodar a CLI:

```bash
beautiful-linkedin
beautiful-linkedin search --company-name "Nubank" --titles "marketing,growth" --output output/leads.csv
beautiful-linkedin search --company-name "Nubank" --titles "marketing" --lead-providers "pdl,apollo,web" --search-engines auto --output output/leads.csv
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

- `src/beautiful_linkedin/cli.py`: comandos Typer, parsing de flags e fluxo interativo.
- `src/beautiful_linkedin/main.py`: ponto de entrada do modulo.
- `src/beautiful_linkedin/config.py`: variaveis de ambiente e defaults.
- `src/beautiful_linkedin/models.py`: modelos Pydantic compartilhados.
- `src/beautiful_linkedin/runner.py`: orquestracao de providers, scraping, dedupe e exportacao.
- `src/beautiful_linkedin/scrape_modes.py`: modos `api`, `serp` e `cookie`.
- `src/beautiful_linkedin/providers/`: providers de leads estruturados ou busca publica.
  - `apollo.py`, `people_data_labs.py`, `coresignal.py`: tentam resolver company_id e usar estrategia by-company antes do search antigo.
  - `linkedin_cookie.py`: voyager regular; por padrao puxa funcionarios por empresa e filtra cargo localmente.
  - `linkedin_people_search.py`: provider listing-only da aba People. Ordem de fetchers: (1) CDP no Chrome aberto via `--remote-debugging-port` (recomendado, nao desloga), (2) Scrapling, (3) Playwright com li_at (pode deslogar). Aceita URL completa de `/people/` colada pelo usuario. Loop iterativo `click -> extract -> validate -> click`: cada "Exibir mais resultados" e seguido por extracao + validacao estrita; o loop para assim que `max_results` validos sao obtidos ou apos `max_scrolls_cap`. Tambem expoe `resolve_company_size_via_page()` usado pelo probe do servidor.
  - `linkedin_people_search_progress.py`: estado persistente do loop (`PeopleScrapeProgress` + `ProgressStore` sobre `SqliteJsonCache`). Guarda cliques realizados, cards vistos, `accepted_urls`/`rejected_urls` e posicao 1-indexed por URL, chaveado por `(company_slug, titles normalizados)`. Um re-run pula URLs ja classificadas e anota `consultation_note` com "Extraido apos N cliques, posicao P".
  - `linkedin_sales_navigator.py`: sales-api; por padrao puxa funcionarios por empresa e filtra cargo localmente, com fallback para voyager.
  - `public_directories.py`: scraping leve de TheOrg/RocketReach.
  - `common_crawl.py`: provider pesado e opcional via indice CDX/Common Crawl.
- `src/beautiful_linkedin/search/`: motores de busca e composicao/fallback.
  - `searxng_search.py`: engine self-hosted via `SEARXNG_BASE_URL`.
  - `serper_search.py`: simplifica query automaticamente em HTTP 400.
  - `smart_composite_search.py`: consulta todos os engines em paralelo e deduplica por URL.
- `src/beautiful_linkedin/processing/`: extracao, normalizacao, scoring, deduplicacao e taxonomia.
  - `title_validator.py`: validacao estrita pos-extracao (`validate_lead_titles`). Usa word-boundary regex e a uniao de `TITLE_ALIASES + SEARCH_TITLE_ALIASES + DEEP_SEARCH_TITLE_ALIASES` para evitar que "Wholesale Analyst" passe numa busca por "sales". E o gate usado pelo loop iterativo do `linkedin_people_search` para decidir se um card vira lead.
- `src/beautiful_linkedin/scraping/`: scraping de sites oficiais de empresas.
- `src/beautiful_linkedin/storage/`: persistencia durava (saved leads SQLite) e camada de enriquecimento.
  - `saved_leads.py`: `SavedLeadsStore` (tabelas de leads salvos, dedup, import/export). `apply_enrichment_updates` (paid) e `apply_internal_enrichment_updates` (interno) compartilham `_merge_email_verification`: e-mail primario nunca e sobrescrito; quando uma segunda fonte chega com a MESMA resposta, a fonte e apendada em `email_verified_by` (drives o selo "Verificado" na UI); quando chega DIFERENTE, vira entrada em `email_alternatives` (deduped por `(email, source)`). Schema migrado via `_ensure_column` para colunas JSON `email_verified_by_json` + `email_alternatives_json`.
  - `internal_enrichment.py`: enriquecimento de e-mail **gratuito**, in-process. Tres camadas:
    - `InternalLeadEnrichmentService` + `enrich_lead_multi_domain`: gera local-parts (`first`, `first.last`, `flast`...), tenta cada dominio da lista 1-a-1, curto-circuito em VALID, senao mantem o melhor PROBABLE.
    - `collect_company_domains(leads)`: agrega por empresa todos os dominios ja vistos (vindos de `lead.email` peso 3 + `lead.company_domain` peso 1). Permite "Bruno tem `@nubank.com.br` via Apollo → Ana sem dominio herda o mesmo".
    - `InternalEnrichmentOrchestrator`: roda 3 fases — `discovering` (opcional, via `DomainDiscoveryService`), `harvesting` (parallel, `_harvest_safe`) e `validating` (parallel, lead-a-lead). Emite eventos para SSE.
    - `SmtpMailboxVerifier`: SMTP RCPT probe conservador (sem DATA), com cache catch-all/no-mx/unreachable POR DOMINIO (`_DomainCacheEntry`), semaforo por host MX (`max_per_host=2` default — protege contra throttle de Gmail/Outlook) e `local_hostname` configurado (EHLO sem FQDN e rejeitado).
  - `domain_discovery.py`: descoberta de dominios irmaos **grátis**. `CrtShClient` (HTTP em https://crt.sh, extrai SANs de certificados), `SpfDmarcDiscoverer` (DNS TXT, parseia `include:`/`redirect=` do SPF e `mailto:` do DMARC), `CctldVariantGenerator` (permuta core do dominio sobre TLDs comuns: `.com.br`, `.io`, `.co.uk`...). `DomainDiscoveryService` compoe os 3 e gateia tudo por MX (so domínios que realmente recebem mail entram na lista). Todos os clientes/resolvers sao injetaveis — testes offline.
  - `company_email_harvester.py`: tipo Hunter.io leve. Busca homepage + paths comuns (`/contato`, `/sobre`, `/team`...), extrai e-mails que terminam no dominio da empresa, devolve `HarvestedEmail(email, source_url)`. HTTP client injetavel para testes offline.
  - `enrichment.py`: providers **pagos** (`ApolloEnrichmentProvider`, `LushaEnrichmentProvider`, `SnovioEnrichmentProvider`, `PdlEnrichmentProvider`) + `estimate_enrichment_cost()` que casa `selected_leads x credit_costs_brl`. Apollo exige `apollo_webhook_url` para telefone; Snovio so faz e-mail; Lusha faz duas chamadas batch (uma por `filterBy`) para casar e-mail + telefone.
- `src/beautiful_linkedin/server/`: sidecar FastAPI consumido pelo Electron (loopback only).
  - `app.py`: `POST /search`, `POST /search/start` + `GET /runs/{id}` (assincrono), `POST /people-search/probe/(start|state)`, `POST /lead-tables/...` (CRUD + `import` + `export` + `merge`), `POST /lead-tables/{id}/enrich` (paid; `confirmed=false` retorna so o `EnrichmentEstimate`), `POST /lead-tables/{id}/internal-enrich` (interno blocking, somente `fields="email"`), `POST /lead-tables/{id}/internal-enrich/stream` (mesma logica via SSE com eventos `start/phase/discovery/domain/lead/progress/done`), `POST /lead-tables/{id}/experimental-search`. Provider timeout em modo `people_search` e escalonado por `_people_search_timeout(max_results, cards_per_cycle)`. Modo "browser" exige `accept_risk=true`.
  - Helpers de DI: `_default_mx_resolver()`, `_default_txt_resolver()`, `_default_mailbox_verifier()`, `_default_company_email_harvester()`, `_default_domain_discoverer()`. Todos lazy-resolved para que testes monkeypatchem e fiquem offline (use `_disable_real_domain_discovery` autouse fixture em `test_internal_enrich_endpoint.py` como referencia).
- `src/beautiful_linkedin/export/`: exportadores CSV/XLSX.
- `src/beautiful_linkedin/ui/`: banner, prompts, progresso e tabelas Rich.
- `electron/`: app desktop (TypeScript + React).
  - `src/renderer/src/components/SavedLeadsLibrary.tsx`: lista/edita tabelas salvas, dispara enriquecimento, renderiza `LeadEmailCell` (e-mail primario + selo `✓ Verificado` quando `email_verified_by.length >= 2` + lista expansivel de `email_alternatives`).
  - `src/renderer/src/components/InternalEnrichProgress.tsx`: modal de progresso do enriquecimento interno (consumo do SSE). PHASE_LABEL cobre `discovering/harvesting/validating/completed/cancelled`. `reduceProgress` agrega counters por fase e por fonte de discovery.
  - `src/renderer/src/enrichment/EnrichmentRunnerContext.tsx`: provider global que mantem o `run` ativo em memoria. Permite trocar de view (Nova busca / Leads / Configuracoes) sem perder progresso — o fetch SSE vive aqui, nao no SavedLeadsLibrary. Listeners se registram via `onCompleted(meta, done)` para atualizar UI quando termina.
  - `src/renderer/src/enrichment/EnrichmentRunPill.tsx`: chip flutuante bottom-right que aparece quando ha run e o modal esta fechado. Clique reabre o modal; ✕ cancela/descarta.
  - `src/shared/api.ts`: `ApiClient.streamInternalEnrich(tableId, payload, onEvent, signal)` consome SSE via `fetch` + `ReadableStream` (suporta `AbortController`).
  - `src/shared/types.ts` espelha os payloads — `Lead.email_verified_by`, `Lead.email_alternatives` + a union `InternalEnrichStreamEvent`.
- `tests/`: testes unitarios e de comportamento.
- `linkedin-api-scrapper/apify-linkedin-profile/`: subprojeto TypeScript do Actor Apify; nao e o core da CLI Python.
- `docker-compose.searxng.yml` e `searxng-config/settings.yml`: SearxNG local opcional; trocar `secret_key` antes de uso real.

Evite carregar `node_modules`, `.venv`, `.pytest_cache`, `output/`, `data/cache.sqlite*`
e arquivos grandes de debug, salvo quando forem explicitamente relevantes.

## Fluxo Mental

Entrada de CLI/interativo -> `scrape_modes.py` ajusta fonte e limites ->
`runner.run_prospecting()` monta settings, cache, motores e providers -> providers
retornam `Lead` -> processamento deduplica/rankeia -> exportacao CSV/XLSX -> resumo
Rich no terminal.

`auto` para providers significa APIs configuradas (`pdl`, `coresignal`, `apollo`,
`lusha`) mais `web`. `apify_linkedin`, `public_directories`, `common_crawl` e
providers com cookie precisam ser selecionados explicitamente. Se chaves estiverem
ausentes, providers sao ignorados com warning e a busca publica atua como fallback.

`auto` para search engines prioriza SearxNG local quando `SEARXNG_BASE_URL` existe,
depois Serper, Google CSE, DuckDuckGo e fallbacks HTML.

## Pipeline De Enriquecimento

O enriquecimento de leads salvos roda em duas camadas, ambas opt-in pelo cliente:

1. **Interno (gratis)** — `POST /lead-tables/{id}/internal-enrich` (blocking) ou
   `POST /lead-tables/{id}/internal-enrich/stream` (SSE com progresso ao vivo).
   So enriquece e-mail. O `InternalEnrichmentOrchestrator` roda 3 fases:
   - **`discovering`**: pra cada empresa, pega o dominio seed mais forte (top do
     `collect_company_domains`) e expande via `DomainDiscoveryService` (crt.sh +
     SPF/DMARC + ccTLD variants, gateado por MX). Dominios descobertos sao
     mesclados em `company_domains[key]` em prioridade menor que os seeds.
   - **`harvesting`**: pra cada dominio unico (paralelo), `CompanyEmailHarvester`
     busca emails publicados no site oficial. O shape dos local-parts vira sinal
     primario de pattern detection.
   - **`validating`**: pra cada lead (paralelo, `lead_concurrency=6`), tenta a
     lista ranqueada de dominios da empresa 1-a-1 via `enrich_lead_multi_domain`.
     Pra cada candidato chama validator (formato + personal-domain + MX +
     opcional SMTP RCPT probe). VALID curto-circuita; senao mantem o melhor
     PROBABLE. Cacheia catch-all/no-mx/unreachable por dominio + throttle por
     host MX (semaforo `max_per_host=2`).
   - Nunca sobrescreve `email` existente. Nunca chama API paga.

2. **Pagos (creditos)** — `POST /lead-tables/{id}/enrich`. Aceita lista de providers
   (`lusha`, `apollo`, `snovio`, `pdl`) e `fields` (`email|phone|both`). Primeira
   chamada com `confirmed=false` so devolve `EnrichmentEstimate` (creditos x
   `credit_costs_brl`); a segunda com `confirmed=true` dispara as APIs. `apollo`
   para telefone exige `apollo_webhook_url` HTTPS; `snovio` so faz e-mail e e
   ignorado quando `fields="phone"`.

**Trilha cross-provider** (`_merge_email_verification` em `saved_leads.py`):
ambas camadas chamam o mesmo helper. Quando a fonte que chega DEPOIS produz o
mesmo e-mail do primario, sua label entra em `Lead.email_verified_by` (UI mostra
selo "Verificado" quando length >= 2). Quando produz e-mail diferente, o valor
vai pra `Lead.email_alternatives` (deduped por `(email, source)`). O primario
**nunca** muda — paid nao sobrescreve internal e vice-versa. Os metadados
existentes (`enrichment_source/status/confidence/email_type/email_validation_status/
enriched_at`) continuam sendo gravados e `enrichment_status` da tabela vira
`enriched` quando ao menos um lead recebeu valor novo.

## Loop Iterativo da Aba People

`LinkedInPeopleSearchProvider` nao roda "fetch -> extrai tudo -> filtra". O fluxo e:

1. Abre `/company/<slug>/people/?keywords=<termos>` no fetcher escolhido (CDP,
   Scrapling ou Playwright).
2. A cada "Exibir mais resultados" o fetcher chama `on_step(clicks_done, html)`.
3. O provider extrai os cards do HTML novo, pula URLs ja em `accepted_urls` /
   `rejected_urls`, e roda `validate_lead_titles(strict=True)` por card.
4. Cards aceitos viram leads com `consultation_note = "Extraido apos N cliques,
   posicao P."`. Cards rejeitados sao salvos em `progress.rejected_urls`.
5. Retorna `False` no callback quando atinge `max_results` — o fetcher para de
   clicar e fecha o navegador.

O `ProgressStore` (chave `(slug, titulos normalizados)`) persiste o que ja foi
visto entre execucoes, evitando re-validar os mesmos cards. Use
`include_uncertain=True` para desabilitar o validador estrito (o provider emite
warning quando isso e feito).

## Padroes De Mudanca

- Novo provider de leads: implementar `LeadProvider`, registrar em
  `providers/factory.py`, adicionar variavel em `config.py` e `.env.example` se
  houver chave, registrar aliases em `cli.parse_lead_providers`, mapear source
  em `runner.py` se necessario, atualizar prompts quando aparecer na UI, e
  cobrir com testes mockados.
- Novo provider de enriquecimento pago: implementar a mesma interface dos
  existentes em `storage/enrichment.py` (`name`, `enrich(leads, options)`,
  atributo `errors: list[str]` com a mesma dedup do Apollo). Adicionar ao
  `_build_enrichment_providers` em `server/app.py`, ao `_normalized_providers`
  em `enrichment.py`, e expor variaveis no `Settings`/`ApiKeyOverrides`.
  Cubra com `respx`/`httpx.MockTransport` — nao chame a API real nos testes.
- Mudancas no enriquecimento interno: adicionar padroes novos em
  `EnrichmentPattern`, atualizar `_classify_local_shape` se mudar o shape,
  manter `PERSONAL_EMAIL_DOMAINS` em sincronia, e injetar `mx_resolver`,
  `mailbox_verifier`, `harvest_fn`, `discoverer` fakes nos testes (nao bater
  em DNS/HTTP/SMTP de verdade). O endpoint tem `_disable_real_domain_discovery`
  autouse fixture — siga o mesmo padrao em testes novos.
- Nova fonte de descoberta de dominios: implementar nova classe em
  `storage/domain_discovery.py` com mesma forma (`.query(seed)` ou
  `.discover(seed) -> list[str]`), registrar em `DomainDiscoveryService.__init__`
  + `discover()`, e expor no `_default_domain_discoverer` do server. Resultados
  passam pelo MX gate automaticamente — nao filtrar manualmente.
- Mudancas no `_merge_email_verification`: rodar `tests/test_saved_leads_enrichment.py`
  com cuidado — qualquer mudanca na semantica de `email_verified_by` /
  `email_alternatives` quebra o selo da UI. Manter o contrato: primario nunca
  muda, mesmo-email → verified_by, diferente-email → alternatives.
- Novo motor de busca: implementar `SearchEngine`, registrar em `search/factory.py`
  e `cli.parse_search_engines`, atualizar prompts se aparecer no interativo, e
  testar fallback/selecionamento sem depender de rede real.
- Mudancas de CLI: atualizar `cli.py`, README se alterar UX publica, e testes de CLI.
- Mudancas de extracao/dedupe/scoring: mexer em `processing/` e adicionar casos
  pequenos e deterministas em `tests/`.
- Dependencias novas devem entrar em `pyproject.toml`; mantenha `requirements.txt`
  coerente enquanto ele existir.
- Toda chamada HTTP externa deve usar os helpers de `api_logging.py`:
  `log_http_error`, `log_transport_error` e, quando aplicavel,
  `log_unexpected_error`, sempre com `provider=...` e contexto util.
- Use `httpx.Client(timeout=..., transport=..., headers=...)`; nao introduza cliente async.
- Cache opcional segue `SqliteJsonCache` com payload versionado:
  `{"cache_version": N, "request": ...}`.

## Cuidados

- Nao leia nem exponha `.env`; use `.env.example` para nomes de variaveis.
- Nao commite cache local, arquivos em `output/` ou segredos.
- Prefira testes offline com mocks/fakes para APIs externas e buscadores.
- Preserve mensagens de CLI em pt-BR quando o texto for voltado ao usuario.
- Identificadores de codigo seguem majoritariamente ingles.
- Ao alterar comportamento de rede, mantenha timeouts, limites e fallbacks claros.
- Common Crawl e diretorios publicos sao fallbacks lentos/opcionais; nao inclua em
  `auto` sem decisao explicita.
- Enriquecimento interno **nunca** sobrescreve `lead.email`; sempre persistir as
  colunas de metadado mesmo em falha (a UI mostra "tentou, sem dominio").
- Enriquecimento pago **sempre** roda primeiro com `confirmed=false` para o
  cliente ver custo em BRL antes de gastar credito. Apollo telefone sem
  `apollo_webhook_url` HTTPS e bloqueado no `model_validator` da request.
- Enriquecimento pago **tambem nao sobrescreve** o primario — quem chegou
  primeiro vence. Pago e interno chegando no mesmo e-mail vira selo "Verificado"
  (UI); resposta divergente vira `email_alternatives`.
- `SmtpMailboxVerifier` precisa de `local_hostname` (default `beautiful-linkedin.local`)
  — sem FQDN o EHLO e rejeitado por Gmail/Outlook. Cache de catch-all/no-mx e por
  dominio, semaforo por host MX (`max_per_host=2`) evita throttle.
- `DomainDiscoveryService` e best-effort: source quebrada nunca derruba o run.
  Producao bate na rede (crt.sh HTTP, DNS TXT) — em testes use o autouse fixture
  ou monkeypatch `_default_domain_discoverer` pra `lambda: None`.
- Estado de progresso do enriquecimento vive no `EnrichmentRunnerProvider` (App-level),
  nao em `SavedLeadsLibrary`. Trocar de view nao cancela o run; o `EnrichmentRunPill`
  mantem visibilidade.
- `linkedin_people_search` em CDP (`needs_li_at=False`) e o caminho seguro — nao
  toca no cookie e nao desloga. Fallback Playwright avisa via warning.
