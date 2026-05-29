# Descoberta de Telefone — Proposta Arquitetural

> **Status:** exploração de design. Nada implementado.
> **Escopo:** mapear como construir um pipeline grátis-primeiro para
> descobrir `telefone` a partir de `(nome, empresa, [email, linkedin_url])`,
> análogo ao que Apollo/Lusha/Cognism fazem pagos, e dimensionar persistência.
> **Postura legal:** deliberadamente postergada conforme orientação do
> dono do projeto. Implicações listadas brevemente em §10 — algumas afetam
> diretamente decisões de schema/distribuição, então o documento não pode
> ignorar 100%.

---

## 1. Estado da arte

### 1.1 O que as APIs pagas fazem por baixo do capô

Apollo, Lusha, Cognism, ZoomInfo, PDL e Snovio não têm **uma única fonte
mágica**. Quem chega num mobile rate acima de 40-50% combina:

1. **Crowdsourcing via extensão de navegador.** Lusha, Apollo e similares
   pedem permissão para ler contatos/Gmail/Outlook em troca de créditos.
   Cada vendedor que instala envia seu rolodex inteiro para o pool.
2. **Scraping massivo.** Currículos públicos, GitHub, decks de pitch
   indexados no Google, PDFs em domínios corporativos, formulários de
   inscrição em eventos, ZoomInfo Community Edition (antigo Jigsaw).
3. **Data licensing.** Acordos B2B com integradores de
   carrier/operadora, registries (RIPE, ARIN), provedores de KYC e
   telcos secundárias.
4. **Pesquisa humana ("Diamond Data" da Cognism).** Operadores ligam para
   o número antes de marcá-lo verificado. É o que justifica preço alto e
   accuracy acima de 95%.
5. **Waterfall enrichment.** Quando uma fonte falha, cascateia para a
   próxima. SyncGTM, Databar e Clay vendem exatamente essa orquestração
   como produto — chegam a 70%+ combinando 50+ fornecedores.
6. **Validação HLR/MNP.** Independente de onde o número veio, validam
   no Home Location Register e Mobile Number Portability para descartar
   inválidos antes de cobrar crédito.

**Insight central:** o que esse projeto pode replicar de graça é a etapa
de orquestração (waterfall + validação). O que ele *não* consegue replicar
sem fonte de dados é o pool crowdsourced.

### 1.2 Bots de Telegram (Gonzales, Eye of God, UsersBox, Sherlock, etc.)

Categoria distinta dos provedores B2B: agregadores de **databases vazadas**
acessíveis via Telegram. O direcionamento típico é
**telefone → identidade** (reverso do que o Beautiful LinkedIn precisa),
mas vários permitem **full-text search** — passar `nome empresa` e receber
registros que contenham aquela string, incluindo telefone quando existir.

| Bot | Username | O que retorna | Tem API? |
| --- | --- | --- | --- |
| **Eye of God** (Глаз бога) | `@eyeofgod_bot` | Telefone → nome, operadora, região, e-mail, VK, WhatsApp, Telegram, contagem de buscas anteriores. Username, placa, e-mail também aceitos. | Não oficial. Existe wrapper Python (`Ax0107/TgEyeOfGod`) que automatiza via Telegram Client. |
| **UsersBox** | `@usersbox_bot` | Full-text sobre ~20 bilhões de documentos de leaks. Busca por nome, telefone, e-mail, endereço, IP, link social. | **Sim** — `api.usersbox.ru/v1/{método}`, auth via `/api > create application` no próprio bot. Métodos: `/search`, `/{db}/{collection}/search`, `/sources`, `/getMe`, `/explain`, `/nearbySearch`, `/sample`. |
| **Sherlock Bot** | `@sherlock_bot` (variações) | Posiciona-se como alternativa ao Eye of God com 50+ databases. Telefone, foto (face search), VIN. | Documentação esparsa, integração via bot. |
| **Gonzales / Gonzalo Bot** | múltiplas variantes; nome é genérico no ecossistema russo/LatAm | Lookups similares ao Eye of God. Sem documentação pública estável — o nome aparece em listas curadas mas o bot oficial muda com frequência (banidos e re-publicados). | Não. |
| **@PhoneLeaks_bot** | — | Cruza telefone contra dumps conhecidos (COMB, etc). | Não. |
| **@NewLeakOSINT1bot** | — | Agregador de breaches do breachforums. | Não. |
| **Unamer** | — | Histórico de usernames, ID, data de criação. Reverso — útil para enriquecer LinkedIn → Telegram. | Não. |

**Observação prática sobre Gonzales:** o ecossistema desses bots é volátil.
Bots são banidos pela equipe do Telegram, renascem em outro handle, mudam
de dono. Confiar em um bot específico é frágil. Os que sobrevivem mais
tempo (Eye of God, UsersBox) cobram em cripto e são tratados como
infra cinzenta pelos próprios usuários.

**Cobertura Brasil dos bots russos.** Eye of God e UsersBox indexam o
**vazamento Serasa 2021** (223M CPFs + nomes + nascimento + telefones +
endereços) e o **dump MORGUE 2026** (251M registros do Gov.br). Para
nomes corporativos brasileiros, a cobertura é alta — quase toda a PEA já
está num dos dois dumps.

### 1.3 Outras vias geralmente subestimadas

- **WhatsApp Business profile scraping.** Empresas com link `wa.me/<num>`
  no site publicam telefone direto. Extensão natural do
  `CompanyEmailHarvester` que já existe no projeto.
- **Cliques de "fale conosco" e schema.org JSON-LD.** Páginas
  corporativas frequentemente declaram `telephone` em microdata.
- **PDFs públicos (decks, propostas, currículos).** `pdftotext` + regex
  pegam telefones que Google indexa em domínios `.pdf`. Currículo
  público no GitHub Pages é caso clássico.
- **Páginas de equipe / sobre.** TheOrg, RocketReach e o site oficial
  publicam telefones de C-level com frequência maior do que o esperado.
- **Truecaller / Drupe (não oficial).** Indexação reversa por
  crowdsourcing de catálogo. Não há API pública; existem wrappers PHP/Python
  não oficiais que quebram com frequência.
- **NumLookupAPI / Numverify / OpenCNAM / AbstractAPI.** Não são
  *descoberta* — são **validação**. Dão carrier, country, type
  (mobile/fixed), MNP. Custam centavos por lookup.
- **HLR/MNP lookups (hlr-lookups.com, Neutrino, XConnect).** Validação
  *ativa*: derrubam números não atribuídos sem mandar SMS. Crítico para
  não persistir lixo no banco.
- **Diretórios públicos LatAm.** Páginas Amarelas (BR), TeleListas,
  Econodata têm scraping leve já parcialmente coberto pelo
  `public_directories.py` atual.

---

## 2. Buckets de fontes — ranking por custo, risco e cobertura

| Bucket | Custo | Risco legal | Cobertura BR | Match rate esperado |
| --- | --- | --- | --- | --- |
| A. Site da empresa + WhatsApp link + schema.org | Zero | Baixo (dado publicado) | Baixa por lead (alta para C-level) | 5-10% |
| B. SERPs públicas + PDFs indexados + diretórios | Zero | Baixo | Média | 10-15% |
| C. Bots Telegram (Eye of God / UsersBox API) | Cripto, ~$10-50/mês | **Alto** (dados de leak) | Muito alta (>70% PEA brasileira) | 50-70% |
| D. Dumps de leak hospedados localmente | Custo de storage | **Muito alto** | Muito alta | 60-80% |
| E. APIs pagas existentes (Apollo, Lusha, etc.) | R$ por crédito | Baixo (terceiriza) | Variável | 30-50% |
| F. HLR/MNP (validação, não descoberta) | Centavos/lookup | Baixo | Total | N/A |
| G. Crowdsourcing próprio via extensão | Investimento de produto | Médio | Constrói-se com o tempo | Cresce |

A arquitetura proposta combina **A + B** como camada grátis nativa, **C** como
provider externo via bot, **D** como banco local opcional, **E** já está
pronto no projeto, **F** como gate de qualidade obrigatório, **G** como
roadmap de longo prazo.

---

## 3. Arquitetura proposta

### 3.1 Visão geral — espelhar o pipeline de e-mail

O projeto já tem um pipeline canônico para enriquecimento gratuito de
e-mail (`InternalEnrichmentOrchestrator` com três fases:
`discovering → harvesting → validating`). A proposta é **replicar o mesmo
formato** para telefone, com fases análogas:

```
PhoneEnrichmentOrchestrator
   │
   ├─► fase 1: discovering     domínios da empresa + páginas candidatas
   │                           (reuso de DomainDiscoveryService)
   │
   ├─► fase 2: harvesting      colhe telefones do site, PDFs, schema.org,
   │                           WhatsApp links, public directories
   │
   ├─► fase 3: lookup          (opcional, opt-in) consulta bots Telegram
   │                           e/ou banco local de leaks por nome+empresa
   │
   ├─► fase 4: validating      formato E.164 + HLR/MNP gate por número
   │
   └─► fase 5: merging         _merge_phone_verification (espelha o de e-mail)
```

Decisões de design copiadas do pipeline de e-mail:
- Primário (`lead.phone`) **nunca** é sobrescrito.
- Segunda fonte concordando → `phone_verified_by` (selo "Verificado").
- Segunda fonte divergindo → `phone_alternatives`.
- Fontes injetáveis para teste offline.
- Best-effort: source quebrada não derruba o run.

### 3.2 Componentes novos

#### 3.2.1 `PhoneNumberHarvester` (storage/)
Análogo de `CompanyEmailHarvester`. Crawla:
- Homepage + paths comuns (`/contato`, `/contact`, `/sobre`, `/team`,
  `/leadership`, `/equipe`).
- Extrai via:
  - Regex E.164 e formatos BR `(11) 99999-9999`, `+55 11 99999-9999`.
  - `wa.me/<num>` e `api.whatsapp.com/send?phone=...`.
  - `<a href="tel:...">`.
  - JSON-LD `Organization.telephone`, `Person.telephone`.
  - Microformatos hCard.
- Devolve `HarvestedPhone(number_e164, source_url, raw, context_text)`.
- HTTP client injetável (mesmo padrão dos harvesters existentes).

#### 3.2.2 `PdfPhoneExtractor` (storage/)
Submódulo opcional. Busca no Google/SearxNG por
`"{nome} {empresa}" filetype:pdf` e por `"{empresa}" filetype:pdf`,
baixa PDFs (com timeout e size cap), roda `pdfminer.six` ou `pypdf`
para extrair texto, e aplica os mesmos regex do `PhoneNumberHarvester`.
Gated por flag — pesado.

#### 3.2.3 `PhoneLookupProvider` (interface — providers/phone_lookup/)
Interface comum para fontes de "name+company → phone". Implementações:

```
PhoneLookupProvider
├── UsersBoxLookupProvider          # API oficial do UsersBox
├── EyeOfGodLookupProvider          # via TG Client (não oficial)
├── LocalLeakDbLookupProvider       # banco local — §4
├── PublicDirectoryLookupProvider   # reuso de public_directories.py
└── PaidPhoneProvider               # já existe (Apollo/Lusha/PDL)
```

Cada implementação:
- Recebe `LookupQuery(full_name, company, optional[email, linkedin_url])`.
- Devolve `list[PhoneCandidate(number, source, confidence, raw_excerpt)]`.
- Tem `name`, `errors: list[str]` (mesmo padrão dos providers de
  enriquecimento existentes).
- Configurada via `Settings` + variáveis de ambiente para credenciais.

#### 3.2.4 `PhoneValidator` (storage/)
Camada de validação obrigatória **antes** de qualquer telefone virar
primário. Stack:

1. **Normalização** — `phonenumbers` (lib do Google, port libphonenumber).
   Devolve E.164, country, type (`MOBILE`/`FIXED_LINE`/`VOIP`), região.
2. **Format gate** — descarta inválidos sintáticos.
3. **Heurística de descarte** — números 0800, 4004, ranges óbvios de
   call center que aparecem em sites corporativos mas não correspondem ao
   lead individual.
4. **HLR/MNP probe** — opt-in, custa centavos. Providers configuráveis
   (hlr-lookups.com, Neutrino, AbstractAPI). Cache **por número**
   (igual cache `por domínio` do SMTP probe).
5. **Confidence score** — combinação ponderada de:
   - Fonte (site oficial > diretório > leak bot > pagas).
   - Match estrutural (nome no contexto do número na página).
   - Validação HLR (reachable > not reachable).
   - Frequência cross-source (idem regra do `verified_by`).

#### 3.2.5 `PhoneEnrichmentOrchestrator` (storage/)
Equivalente do `InternalEnrichmentOrchestrator`. Roda as 5 fases acima
em paralelo controlado, com `lead_concurrency` configurável e emissão
de eventos SSE compatíveis com o frontend já existente
(`InternalEnrichProgress.tsx` ganha eventos de tipo `phone_*`).

### 3.3 Integração com o pipeline atual

- `internal_enrichment.py` exporta `enrich_lead_multi_domain` (e-mail).
  Adicionar `enrich_lead_phone(lead, sources, validator)` no mesmo
  módulo (ou em `internal_phone_enrichment.py` para isolar) com a
  mesma semântica curto-circuita-em-VALID.
- Endpoint `POST /lead-tables/{id}/internal-enrich` ganha
  `fields: "email" | "phone" | "both"`. Hoje só aceita `"email"`.
- Endpoint `/internal-enrich/stream` emite os mesmos eventos com payload
  enriquecido.
- `_merge_email_verification` recebe gêmeo `_merge_phone_verification`.
  Mesmas regras, colunas espelhadas:
  `phone_verified_by_json`, `phone_alternatives_json`,
  `phone_validation_status`, `phone_carrier`, `phone_type`.
- Provider pago de telefone (Apollo, Lusha) continua em
  `/lead-tables/{id}/enrich` paid — **o pago e o interno usam o mesmo
  merge helper**, mantendo o selo "Verificado" cross-camada que o
  e-mail já tem.

### 3.4 Frontend (Electron)

Já existem `LeadEmailCell` + selo. Adicionar `LeadPhoneCell` com a mesma
lógica:
- Primário grande, com `tel:` clicável.
- Selo "✓ Verificado" quando `phone_verified_by.length >= 2`.
- Tag de tipo (📱 mobile / ☎ fixed) com base em
  `phone_type`.
- Tag de carrier.
- `phone_alternatives` expansíveis.
- `InternalEnrichProgress.tsx` ganha label de fase
  `phone_harvesting`, `phone_lookup`, `phone_validating`.

---

## 4. Banco de dados necessário

### 4.1 Extensão do schema atual (mínimo)

`saved_leads.sqlite` já tem `phone TEXT`. Adicionar via `_ensure_column`:

```sql
ALTER TABLE saved_leads ADD COLUMN phone_e164 TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_type TEXT;          -- mobile|fixed|voip
ALTER TABLE saved_leads ADD COLUMN phone_carrier TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_country TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_validation_status TEXT;
                                                              -- valid|invalid|catch_carrier|unreachable|untested
ALTER TABLE saved_leads ADD COLUMN phone_confidence REAL;     -- 0.0–1.0
ALTER TABLE saved_leads ADD COLUMN phone_verified_by_json TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_alternatives_json TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_source TEXT;         -- harvester|usersbox|apollo|...
ALTER TABLE saved_leads ADD COLUMN phone_source_url TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_enriched_at TEXT;
```

Caches dedicados (tabelas novas em `data/cache.sqlite`):

```sql
CREATE TABLE phone_validation_cache (
    e164 TEXT PRIMARY KEY,
    status TEXT,
    type TEXT,
    carrier TEXT,
    country TEXT,
    checked_at TEXT,
    provider TEXT
);

CREATE TABLE phone_lookup_cache (
    query_hash TEXT PRIMARY KEY,        -- sha256(name+company+source)
    name TEXT,
    company TEXT,
    source TEXT,
    response_json TEXT,
    fetched_at TEXT,
    cache_version INTEGER
);
```

### 4.2 Banco local de leaks (opcional, bucket D)

Se o produto absorver dumps localmente em vez de depender de bots, a
forma natural é **SQLite com FTS5** (Full-Text Search) — sem servidor,
embedded, cabe no instalador Electron.

```sql
CREATE VIRTUAL TABLE leak_records USING fts5(
    full_name,
    email,
    phone,
    company,
    cpf,         -- BR-specific
    address,
    raw,         -- linha original normalizada
    source UNINDEXED,    -- "serasa_2021", "morgue_2026", etc.
    record_date UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2"
);
```

Volumetria estimada para Brasil:
- Serasa 2021: 223M registros × ~500 bytes ≈ **110 GB raw**, ~40 GB com
  compressão FTS5. Inviável para empacotamento desktop.
- Dump filtrado para "perfil B2B" (executivos, empresas, telefones
  mobile com DDD de capitais): pode cair para 5-10 GB.

**Implicação:** se o produto for desktop, o banco local **não** vem com o
instalador. Ele precisa ser:
- Servido por um servidor próprio (backend B2B do projeto), com cliente
  fazendo lookup remoto, ou
- Distribuído como **add-on** baixável separadamente, com checksum, ou
- Substituído pelo bucket C (Telegram bots) que já carregam essa
  infraestrutura.

### 4.3 Recomendação prática

**Não** levar dumps locais no escopo do produto. O custo de storage,
distribuição, atualização e exposição é proibitivo. **Usar bots
Telegram como camada de leak indireta** — eles já têm os dumps, já
têm CDN, já lidam com novas leaks. O projeto só consome a API.

A camada de banco local fica reservada para:
- **Cache de respostas** dos bots (não re-pagar consulta repetida).
- **Cache de validação HLR/MNP** (não re-pagar lookup).
- **Pool crowdsourced próprio futuro** (bucket G).

---

## 5. Integração com bots Telegram

### 5.1 Caminho oficial — UsersBox API

UsersBox tem API documentada e é o caminho menos frágil. Fluxo:

1. Usuário autentica no `@usersbox_bot` e cria uma "application" via
   comando `/api` no próprio bot. Recebe `api_key`.
2. Configura no Beautiful LinkedIn via `USERSBOX_API_KEY` (env var no
   mesmo padrão dos providers pagos existentes).
3. `UsersBoxLookupProvider`:
   - Endpoint: `https://api.usersbox.ru/v1/search`.
   - Query: `q="{full_name} {company}"`.
   - Resposta: registros com campos `phone`, `email`, `name`,
     `source` (qual leak veio).
   - Filtra por `lead.company` no campo `company`/`employer` do
     registro retornado para reduzir falso positivo.
   - Devolve `list[PhoneCandidate]`.
4. Pagamento em cripto direto no bot. **Fora** do fluxo do
   `EnrichmentEstimate` em BRL — UI mostra "saldo UsersBox: X
   requests" lido via `/getMe`.

### 5.2 Caminho não oficial — Eye of God via TG Client

Para casos em que UsersBox não cobre, usar wrapper sobre Telegram
Client (Pyrogram / Telethon):

1. Sessão Telegram pessoal do operador (não é bot — é cliente).
2. Cliente envia mensagem para `@eyeofgod_bot` com a consulta.
3. Parser captura a resposta (texto livre + tabela).
4. Normaliza para `PhoneCandidate`.

Referência de implementação existente: `Ax0107/TgEyeOfGod` no GitHub.

Riscos técnicos (independente do legal):
- Telegram limita rate de mensagens por cliente — não dá para escalar.
- Resposta é texto não-estruturado e o formato muda com updates do bot.
- Sessão pode ser banida.
- Não funciona offline / em servidor sem 2FA da conta.

**Não recomendado como provider principal.** Deixar como provider
manual opt-in para investigação one-off, não para pipeline batch.

### 5.3 Ranking de uso recomendado

```
1. Site da empresa + WhatsApp + schema.org     (PhoneNumberHarvester)
2. Public directories (TheOrg, Páginas Amarelas, etc.)
3. SERP + PDF                                  (PdfPhoneExtractor)
4. UsersBox API                                (bucket C oficial)
5. APIs pagas (Apollo telefone, Lusha)         (já implementado)
6. Eye of God via TG Client                    (manual, opt-in)
```

Mesma lógica de "auto" do enriquecimento de e-mail: 1-3 são default;
4+ exigem opt-in com credencial configurada.

---

## 6. Heurísticas Brasil-específicas

Importantes porque a base de usuários é BR:

- **DDD + 9 dígito (mobile pós-2016).** Validar prefixo regional contra
  a empresa-alvo: lead em SP com mobile DDD 11 é mais provável que
  DDD 31.
- **Pattern de tronco corporativo.** Empresas grandes usam blocos
  (`+55 11 3030-XXXX`); harvester pode inferir ramais individuais a
  partir de uma página de equipe que lista o tronco e o ramal por nome.
- **WhatsApp ubiquidade.** No Brasil, link `wa.me` no rodapé do site
  cobre 60%+ de PMEs. É a fonte grátis mais valiosa.
- **TeleSíntese / Anatel MNP.** Brasil tem portabilidade — número não
  necessariamente está com a operadora que emitiu. HLR/MNP é mais
  importante aqui que em mercados sem MNP.
- **0800, 4004, 3003.** Descartar com regex no validator —
  call center, não pessoa.

---

## 7. Crowdsourcing próprio (bucket G — roadmap longo)

Se o produto ganhar tração, o caminho de longo prazo que diferencia
Apollo/Lusha é construir pool próprio. Caminhos:

1. **Extensão de navegador opcional.** Usuário instala, dá permissão
   para extensão ler contatos do Gmail/Outlook do trabalho, em troca
   recebe créditos de enriquecimento grátis. Servidor central agrega
   anonimamente.
2. **Imports manuais.** Usuário pode subir CSV próprio para o pool
   compartilhado em troca de créditos.
3. **CRM connectors.** Plugins de HubSpot/Pipedrive/Salesforce que
   espelham contatos para o pool.

Requer **backend remoto** — não cabe em desktop puro. Esse é o ponto
em que o produto migra de "CLI + Electron local" para "Electron + SaaS
backend".

---

## 8. Variáveis de configuração novas

Para manter o padrão do `.env.example` atual:

```bash
# Lookup providers
USERSBOX_API_KEY=
EYE_OF_GOD_TG_SESSION=                 # path para .session file (Telethon)
EYE_OF_GOD_TG_API_ID=
EYE_OF_GOD_TG_API_HASH=

# Validation
PHONE_HLR_PROVIDER=                    # hlr-lookups | neutrino | abstract | none
PHONE_HLR_API_KEY=
PHONE_HLR_TIMEOUT_SECONDS=8

# Harvester knobs
PHONE_HARVEST_MAX_PAGES_PER_DOMAIN=10
PHONE_HARVEST_INCLUDE_PDF=false        # PdfPhoneExtractor é opt-in
PHONE_HARVEST_CONCURRENCY=4

# Custo BRL (UI mostra)
ENRICHMENT_COST_BRL_USERSBOX=
ENRICHMENT_COST_BRL_HLR=
```

---

## 9. Plano de testes (sem implementar agora — só desenho)

- `tests/test_phone_harvester.py` — fake HTTP serve páginas com
  formatos BR/E.164/wa.me/schema.org; harvester deve extrair todos.
- `tests/test_phone_validator.py` — `phonenumbers` é puro; mock
  do HLR provider devolve respostas canônicas.
- `tests/test_usersbox_lookup.py` — `respx`/`httpx.MockTransport`,
  zero rede.
- `tests/test_phone_merge.py` — `_merge_phone_verification` espelha
  o gêmeo de e-mail; mesmas regras de primário imutável.
- `tests/test_phone_endpoint.py` — fixture autouse
  `_disable_real_phone_lookups` análoga à existente para domain
  discovery.

---

## 10. Avisos de risco (postergados, não eliminados)

O usuário pediu para ignorar legal "POR enquanto". Para o documento ficar
honesto, deixo enumerados os pontos que **vão** voltar antes de
release público — não pra travar a arquitetura, mas pra evitar refatoração
forçada depois:

1. **Bots de leak (bucket C/D)** distribuem dados de origem
   indiscutivelmente ilícita no Brasil (Serasa 2021 é objeto de
   investigação da ANPD e do MPF). Persistir esses dados localmente
   (bucket D) eleva o produto a controlador de dados sensíveis. Consumir
   via API de terceiro (bucket C) terceiriza o problema mas não elimina.
2. **HLR probe** é legítimo (telco-to-telco).
3. **Harvest de site corporativo** é dado público — baixo risco se
   respeitar `robots.txt` e rate limit.
4. **Crowdsourcing via extensão (G)** exige consentimento granular do
   usuário cujo rolodex está sendo coletado, no padrão LGPD —
   Apollo/Lusha foram processadas por isso em múltiplas jurisdições.
5. **Apify Actor de LinkedIn**, já no projeto, segue a mesma lógica
   de opt-in explícito que devemos replicar para qualquer fonte risky.

**Implicação que afeta arquitetura agora:** isolar fontes risky por
trás de provider interface separada (`PhoneLookupProvider`) com flag de
ativação por configuração — exatamente o que a §3.2.3 propõe. Isso
permite ligar/desligar buckets inteiros sem mexer no core.

---

## 11. Próximos passos sugeridos (em ordem)

1. Validar o desenho do `PhoneNumberHarvester` num spike isolado
   (`scripts/spike_phone_harvest.py`) sobre 20 empresas de teste —
   medir match rate real do bucket A+B antes de investir nos
   buckets pagos/risky.
2. Decidir se o produto vai pagar UsersBox como **default opt-in com
   chave do operador final** (cada cliente paga o seu) ou se vai
   embutir uma chave compartilhada (caro e arriscado).
3. Definir contrato `PhoneCandidate` e `_merge_phone_verification`
   antes de qualquer provider — é o invariante que sustenta tudo.
4. Schema migration via `_ensure_column` com colunas da §4.1.
5. Endpoint `internal-enrich` com `fields="phone"|"both"` e fase
   `phone_*` no SSE.
6. Provider `UsersBoxLookupProvider` (mais previsível dos cinzentos).
7. `PhoneValidator` com `phonenumbers` (offline) + HLR opt-in.
8. Frontend `LeadPhoneCell` espelhando `LeadEmailCell`.
9. Spike opcional do `EyeOfGodLookupProvider` via Telethon — só se 1-8
   não chegarem a >40% match rate.

Bucket G (crowdsourcing próprio) fica fora deste roadmap — exige
servidor central e modelo de negócio próprio.
