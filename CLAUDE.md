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

Estado atual dos testes: `pytest tests -q` deve passar com 120 testes.

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
  - `linkedin_people_search.py`: provider listing-only da aba People. Ordem de fetchers: (1) CDP no Chrome aberto via `--remote-debugging-port` (recomendado, nao desloga), (2) Scrapling, (3) Playwright com li_at (pode deslogar). Aceita URL completa de `/people/` colada pelo usuario.
  - `linkedin_sales_navigator.py`: sales-api; por padrao puxa funcionarios por empresa e filtra cargo localmente, com fallback para voyager.
  - `public_directories.py`: scraping leve de TheOrg/RocketReach.
  - `common_crawl.py`: provider pesado e opcional via indice CDX/Common Crawl.
- `src/beautiful_linkedin/search/`: motores de busca e composicao/fallback.
  - `searxng_search.py`: engine self-hosted via `SEARXNG_BASE_URL`.
  - `serper_search.py`: simplifica query automaticamente em HTTP 400.
  - `smart_composite_search.py`: consulta todos os engines em paralelo e deduplica por URL.
- `src/beautiful_linkedin/processing/`: extracao, normalizacao, scoring, deduplicacao e taxonomia.
- `src/beautiful_linkedin/scraping/`: scraping de sites oficiais de empresas.
- `src/beautiful_linkedin/export/`: exportadores CSV/XLSX.
- `src/beautiful_linkedin/ui/`: banner, prompts, progresso e tabelas Rich.
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

## Padroes De Mudanca

- Novo provider: implementar `LeadProvider`, registrar em `providers/factory.py`,
  adicionar variavel em `config.py` e `.env.example` se houver chave, registrar
  aliases em `cli.parse_lead_providers`, mapear source em `runner.py` se necessario,
  atualizar prompts quando aparecer na UI, e cobrir com testes mockados.
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
