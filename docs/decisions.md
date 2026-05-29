# Decisões — Beautiful LinkedIn

Registro das escolhas estruturais e operacionais do projeto, com o
*porquê* de cada uma. Para visão arquitetural completa, ver
`architecture.md`. Para mapa de arquivos, ver `CLAUDE.md`.

Cada decisão segue o formato: **decisão → motivação → consequências/edge cases**.

---

## 1. Postura de dados e legalidade

### 1.1 Só dados públicos, sem evasão

**Decisão:** o projeto não implementa login automatizado, solver de captcha,
proxy, mascaramento de IP, nem técnicas de evasão. Toda fonte é pública ou
exige opt-in explícito do usuário (com cookie/credencial próprios).

**Por quê:** o produto precisa ser usável dentro de termos de uso e LGPD sem
exigir do usuário decisões legais pesadas. Evasão criaria responsabilidade
solidária e bloqueio rápido de conta/IP.

**Consequência:** rate-limit é uma realidade de produção — daí o SearxNG
local default (1.6), o cache HTTP versionado (5.2), o `max_per_host=2` no SMTP
(3.2), e o esforço grande no people search via CDP (2.1) para evitar deslogar
a conta do usuário.

### 1.2 Risky modes exigem opt-in duplo

**Decisão:** modos que tocam cookie ou navegador (`cookie`, `apify_linkedin`,
Playwright direto) só rodam:

1. Quando o usuário seleciona explicitamente o provider (nunca via `auto`).
2. No sidecar HTTP, quando o payload inclui `accept_risk: true`.

**Por quê:** o renderer do Electron pode acidentalmente forwardar um scrape
mode errado. O `accept_risk` é uma guarda server-side intencionalmente
redundante.

**Como aplicar:** em qualquer novo provider que use Voyager, sales-api,
Apify ou Playwright direto, replique a checagem em `server/app.py` — não confie
só na UI.

---

## 2. Linkedin people search

### 2.1 CDP é o caminho preferido — Playwright só como último recurso

**Decisão:** `linkedin_people_search.py` tenta três fetchers em ordem rígida:
1. **CDP** anexado ao Chrome aberto do usuário com `--remote-debugging-port`.
2. **Scrapling**.
3. **Playwright + `li_at`**.

**Por quê:** Playwright com `li_at` injeta o cookie num browser limpo, o que
o LinkedIn detecta com frequência e desloga a sessão real do usuário. CDP
**anexa** a um Chrome já autenticado — não toca cookie, não desloga.

**Como aplicar:** qualquer scraping de área autenticada do LinkedIn deve
seguir essa hierarquia. Playwright só com warning visível e como fallback.

### 2.2 Loop iterativo `click → extract → validate → click`

**Decisão:** o people search não é "fetch HTML → extrai tudo → filtra
depois". É um loop: a cada clique em "Exibir mais resultados", o fetcher
chama `on_step(clicks_done, html)`; o provider extrai os cards novos, valida
um a um, e o loop para assim que atinge `max_results` válidos ou
`max_scrolls_cap`.

**Por quê:** a aba `/people/` do LinkedIn carrega lazy. Buscar até atingir um
número específico de **válidos** (não de cards crus) gasta menos cliques,
sai mais cedo, evita rate-limit e produz resultados melhores quando o
filtro de cargo é estreito.

**Consequência:** o timeout do provider em modo `people_search` precisou
ser escalonado por `_people_search_timeout(max_results, cards_per_cycle)` no
sidecar — não pode ser fixo, senão runs grandes morrem no meio do loop.

### 2.3 Validação estrita por título com word-boundary regex

**Decisão:** `processing/title_validator.py` é o gate único pós-extração e
faz match com regex word-boundary sobre a união de
`TITLE_ALIASES + SEARCH_TITLE_ALIASES + DEEP_SEARCH_TITLE_ALIASES`.

**Por quê:** sem word boundary, uma busca por "sales" aceita "Wholesale
Analyst". Sem usar todas as listas de aliases, busca por "growth" ignora
"performance marketing" mesmo sendo o sinônimo configurado.

**Como aplicar:** mexer em aliases é mexer em três listas — manter
sincronizado. `include_uncertain=True` desabilita o validador (com warning
no log) e só deve ser usado por usuário avançado.

### 2.4 ProgressStore persiste estado entre execuções

**Decisão:** o loop iterativo guarda em SQLite (`ProgressStore`)
`accepted_urls`, `rejected_urls` e posição 1-indexed por URL, chaveado por
`(company_slug, títulos normalizados)`. Re-runs pulam URLs já classificadas
e anotam `consultation_note` com "Extraído após N cliques, posição P".

**Por quê:** o usuário re-roda a mesma busca para refinar filtros ou retomar
após erro. Re-validar do zero queima cliques e tempo. Manter o estado torna
re-runs incrementais.

---

## 3. Pipeline de enriquecimento de e-mail

### 3.1 Duas camadas, sempre opt-in, **grátis primeiro**

**Decisão:** o enriquecimento é separado em duas camadas independentes:

1. **Interno (grátis, in-process)** — `internal_enrichment.py` + endpoints
   `/internal-enrich` (blocking) e `/internal-enrich/stream` (SSE).
2. **Pago (créditos)** — `enrichment.py` + endpoint `/enrich`. **Sempre** roda
   com `confirmed=false` primeiro para devolver `EnrichmentEstimate` em BRL;
   `confirmed=true` é a segunda chamada que de fato gasta crédito.

**Por quê:** créditos são caros e o usuário precisa ver o custo em moeda
local antes de aprovar. A camada gratuita resolve uma parte significativa
sem custo nenhum — chamar pago "para ver se preenche" é desperdício.

**Como aplicar:** novo provider pago precisa entrar em
`_build_enrichment_providers` (server) + `_normalized_providers` + variáveis
de ambiente, e suportar a mesma interface (`name`, `enrich`, `errors`).
Apollo telefone exige `apollo_webhook_url` HTTPS, validado no
`model_validator` da request — bloqueia no boundary, não no provider.

### 3.2 SMTP probe conservador

**Decisão:** `SmtpMailboxVerifier` faz RCPT probe sem DATA, com cache
catch-all/no-mx/unreachable **por domínio** e semáforo por host MX
(`max_per_host=2` default). `local_hostname` é obrigatório (default
`beautiful-linkedin.local`).

**Por quê:**
- Sem `local_hostname` FQDN, Gmail/Outlook rejeitam o EHLO direto.
- Sem semáforo por MX, dois leads no Gmail abrem múltiplas conexões e
  caem em throttle. `max_per_host=2` é o ponto onde Gmail e Outlook ainda
  respondem confiavelmente.
- Cache **por domínio** (não por e-mail) — se `nubank.com.br` é catch-all,
  toda tentativa subsequente é tratada como tal sem nova conexão.

### 3.3 Domínios descobertos só entram se receberem mail (MX gate)

**Decisão:** `DomainDiscoveryService` agrega `crt.sh` (SANs de certificados) +
SPF/DMARC (DNS TXT) + ccTLD variants, e **todo domínio passa por MX gate**
antes de virar candidato.

**Por quê:** crt.sh devolve subdomínios que nunca receberam e-mail
(`legacy-staging.foo.com`). SPF/DMARC pode listar provedores transacionais
que não são o domínio corporativo. Permutas de ccTLD geram muito ruído. MX
é o filtro definitivo de "domínio que serve para gerar e-mail probable".

**Como aplicar:** nova source de discovery não filtra manualmente — só
implementa `.query(seed)` ou `.discover(seed)` e o gate centralizado cuida.
Source quebrada **nunca** derruba o run (best-effort).

### 3.4 Domínios da empresa têm ranking; seeds vencem descobertos

**Decisão:** `collect_company_domains(leads)` agrega domínios já vistos por
empresa com peso 3 para `lead.email` e peso 1 para `lead.company_domain`.
Domínios vindos do `DomainDiscoveryService` entram em **prioridade menor**
que os seeds.

**Por quê:** se Bruno tem `bruno@nubank.com.br` via Apollo, esse domínio é
sinal forte; deve ser tentado primeiro para Ana antes do `nubank.io` que o
crt.sh devolveu. `enrich_lead_multi_domain` corta no primeiro VALID — ordem
importa para latência.

### 3.5 Contrato cross-provider: primário nunca é sobrescrito

**Decisão:** o e-mail primário (`lead.email`) **nunca** muda depois de
gravado. `_merge_email_verification` (em `saved_leads.py`) é o único helper
que ambas as camadas chamam:

- Segunda fonte produz **mesmo e-mail** → fonte vira label em
  `email_verified_by`. UI mostra selo "✓ Verificado" quando `length >= 2`.
- Segunda fonte produz **e-mail diferente** → vira entrada em
  `email_alternatives` (deduped por `(email, source)`).

**Por quê:** o usuário precisa de previsibilidade — exportar uma tabela
amanhã não pode trazer e-mail diferente do que ele viu hoje. Confiança
cresce *quando duas fontes concordam*; o sinal de "concordância" precisa
ser explícito (selo), não implícito (sobrescrita silenciosa).

**Como aplicar:** mexer em `_merge_email_verification` quebra o selo da UI.
Rodar `tests/test_saved_leads_enrichment.py` com cuidado. O contrato é:
**primário imutável**, mesmo-email → verified_by, diferente-email →
alternatives.

### 3.6 Internal enrichment nunca chama API paga

**Decisão:** a camada `internal_enrichment.py` não toca nenhum SDK pago, só
DNS / HTTP público / SMTP. Mesmo em falha (sem domínio, MX morto), persiste
as colunas de metadado para a UI mostrar "tentou, sem domínio".

**Por quê:** o nome "grátis" tem que ser literal. Caso contrário, o usuário
desconfia de cobranças escondidas.

---

## 4. Search engines

### 4.1 `auto` prioriza SearxNG local antes de qualquer API

**Decisão:** quando `SEARXNG_BASE_URL` está definido (o Electron faz isso
por default apontando para `http://127.0.0.1:8080`), o composite usa SearxNG
**antes** de consumir Serper / Google CSE / Brave API.

**Por quê:** SearxNG self-hosted é gratuito e robusto contra rate-limit por
IP residencial (um único IP doméstico bate em DDG/Bing/Google scrapers em
minutos). Ordem `auto` reflete isso: barato/local primeiro, pago depois,
HTML scrapers como fallback final.

### 4.2 Composite paralelo + dedupe por URL

**Decisão:** `smart_composite_search.py` dispara todos os engines
selecionados em paralelo e deduplica por URL. Não tenta engines
sequencialmente em fallback.

**Por quê:** latência. Cada engine fica entre 1-5s; encadear seriamente
mata UX. Dedup por URL é suficiente porque a normalização de URL do LinkedIn
é estável.

### 4.3 Serper auto-simplifica query em 400

**Decisão:** `serper_search.py` detecta HTTP 400 e refaz a query removendo
dorks complexos antes de devolver erro definitivo.

**Por quê:** Serper rejeita queries muito longas com 400 silenciosamente. A
simplificação salva a maior parte das buscas sem propagar o erro pro
usuário.

---

## 5. Cache e HTTP

### 5.1 `httpx.Client` síncrono em todo o projeto

**Decisão:** todo o I/O HTTP usa `httpx.Client` síncrono. Não há cliente
async no projeto.

**Por quê:** paralelismo já existe via threads no runner e nas fases do
orquestrador (`harvesting`, `validating`). Misturar async/sync gera
complexidade desproporcional para o ganho — testes ficam mais fáceis e
debug fica linear.

**Como aplicar:** não introduzir `httpx.AsyncClient`. Threads + semáforos
quando precisar coordenar.

### 5.2 Cache opcional com payload versionado

**Decisão:** `SqliteJsonCache` armazena `{"cache_version": N, "request": ...}`
em vez do payload cru.

**Por quê:** quando muda o formato de uma chamada (novo campo, novo header
relevante), bumpar `cache_version` invalida entradas antigas sem precisar
limpar o SQLite. Pra cache de busca paga, é o que evita o "deletei o
arquivo errado e perdi a janela do crédito".

### 5.3 Logging HTTP centralizado

**Decisão:** toda chamada externa passa por `api_logging.py`
(`log_http_error`, `log_transport_error`, `log_unexpected_error`) com
`provider=...` e contexto útil.

**Por quê:** suporte ao usuário precisa de logs comparáveis entre providers.
Sem helper centralizado, cada provider loga de jeito diferente e fica
impossível diagnosticar à distância.

---

## 6. Server (sidecar) e renderer (Electron)

### 6.1 FastAPI loopback, nunca exposto à rede

**Decisão:** o sidecar escuta loopback only. Binding a `0.0.0.0` é
intencionalmente impossível pelo código de boot.

**Por quê:** o sidecar tem chaves de API em env vars e endpoints que
disparam scraping. Expor a outra máquina seria delegar credenciais e ação.

### 6.2 Handshake stdout com `READY_TOKEN`

**Decisão:** o Electron lê stdout do sidecar até encontrar o `READY_TOKEN`
com a porta antes de considerar pronto. Não há retry-em-loop nem heurística
de tempo.

**Por quê:** porta é dinâmica (escolhida pelo sistema) — o sidecar precisa
comunicar qual usou. Handshake explícito é mais rápido e determinístico do
que polling.

### 6.3 SSE para o enriquecimento interno

**Decisão:** o enriquecimento interno expõe duas variantes — blocking
(`/internal-enrich`) e SSE (`/internal-enrich/stream`). O renderer usa
SSE via `fetch` + `ReadableStream` (`ApiClient.streamInternalEnrich`).

**Por quê:** a fase `validating` pode levar minutos com dezenas de leads. Sem
streaming, o usuário só vê o resultado no fim e pensa que travou. Eventos
`phase / discovery / domain / lead / progress / done` dão feedback granular.
SSE > WebSocket aqui porque é unidirecional, atravessa loopback sem
configuração e cabe em `fetch` nativo do renderer.

### 6.4 State de run mora em context global, não no componente

**Decisão:** `EnrichmentRunnerContext` é montado no nível do App. O fetch
SSE vive lá, não no `SavedLeadsLibrary`.

**Por quê:** o usuário troca de view (Nova busca / Leads / Configurações) no
meio do enriquecimento. Se o stream estivesse no componente, desmontar
cancelaria o run. Mover pro context permite o `EnrichmentRunPill` flutuar
em qualquer view e o usuário voltar.

### 6.5 Injeção de dependências em todos os helpers de rede

**Decisão:** `_default_mx_resolver`, `_default_txt_resolver`,
`_default_mailbox_verifier`, `_default_company_email_harvester`,
`_default_domain_discoverer` são *factories* lazy. Todos os testes que
encostam no internal enrichment monkeypatcham essas funções.

**Por quê:** sem isso, testes batem em DNS/HTTP/SMTP de verdade — flaky,
lento e barulhento. A fixture autouse `_disable_real_domain_discovery` em
`tests/test_internal_enrich_endpoint.py` serve de modelo para novos testes.

---

## 7. Empacotamento

### 7.1 PyInstaller + electron-builder, sidecar embutido

**Decisão:** release Windows compila `server/` em executável único com
PyInstaller e empacota como recurso do Electron. Cliente final não instala
Python.

**Por quê:** o público-alvo é não-técnico (vendedores). Pedir para
instalar Python + venv + dependências é barreira intransponível.

### 7.2 Python 3.11/3.12 para release, embora dev rode em 3.14

**Decisão:** documentado em `docs/PACKAGING.md` — release usa 3.11 ou 3.12;
dev local roda em 3.14.

**Por quê:** `rookiepy` (dependência para resolver cookie) falha a instalar
em 3.14 do zero por causa do limite do PyO3. Em vez de remover a
dependência ou esperar upstream, fixar a versão de build é o caminho de
menor surpresa.

### 7.3 Build unsigned por enquanto

**Decisão:** `electron-builder` está configurado sem assinatura de
executável.

**Por quê:** o produto está em piloto — comprar certificado de code
signing antes de validar PMF é desperdício. **Como aplicar:** ao
distribuir publicamente, ativar assinatura no `electron-builder` config
antes do release.

---

## 8. Testes

### 8.1 Tudo offline com mocks/fakes

**Decisão:** ~461 testes rodam sem rede. `respx` / `httpx.MockTransport` /
monkeypatch de resolvers DNS / fixture autouse para domain discovery.

**Por quê:** CI confiável + iteração local rápida. Teste que bate em rede é
flaky e bloqueia desenvolvimento quando o serviço externo cai.

### 8.2 Tipagem Pydantic em todos os boundaries

**Decisão:** entrada/saída do sidecar, modelos compartilhados, e payloads
de provider passam por `BaseModel`. `Lead`, `ProspectingResult`,
`EnrichmentEstimate`, etc.

**Por quê:** o renderer TypeScript depende de `shared/types.ts` que espelha
esses modelos. Quebrar contrato sem mudar o Pydantic é uma das poucas
formas de produzir bug de UI difícil de diagnosticar. Pydantic + revisão
do `types.ts` é a checagem dupla.

---

## 9. Convenções

### 9.1 Mensagens de UI em pt-BR; identificadores em inglês

**Decisão:** strings voltadas ao usuário (CLI, Electron, prompts) ficam em
pt-BR. Nomes de função, classe, módulo e teste são em inglês.

**Por quê:** público-alvo brasileiro; manutenibilidade do código se
beneficia do vocabulário técnico inglês.

### 9.2 Documentação curta em `CLAUDE.md`, longa em `docs/`

**Decisão:** `CLAUDE.md` é o mapa rápido (mapa de arquivos, padrões de
mudança, cuidados). `README.md` é o produto. `docs/` guarda
arquitetura, decisões, empacotamento.

**Por quê:** `CLAUDE.md` é sempre injetado no contexto de agentes — manter
curto preserva budget. Documentação extensa fica em arquivos lidos sob
demanda.
