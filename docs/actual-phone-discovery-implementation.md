# Implementação Real — Descoberta de Telefone (Fatia 1)

> **Propósito deste documento:** registrar com honestidade o que foi
> implementado, quais decisões foram tomadas, quais atalhos foram dados
> e quais lacunas conhecidas existem — para sustentar uma reflexão
> crítica posterior contra revisão externa. Não é material de marketing
> da feature; é o histórico técnico do que de fato existe no código no
> momento em que foi escrito.
>
> Documento companheiro de `docs/phone-discovery-architecture.md` (a
> proposta) e `docs/decisions.md` (as decisões prévias). Onde houver
> divergência entre proposta e implementação, este documento é a
> verdade.

---

## 1. Escopo entregue

Fatia 1 do plano: **caminho gratuito ponta-a-ponta, backend-only,
offline-testável.** Especificamente os buckets **A** (site da empresa)
e **B** parcial (JSON-LD do site — busca de PDF e SERP **não** entrou).

Fora de escopo nesta fatia (intencional):
- Bucket C: UsersBox, Eye of God, qualquer bot Telegram.
- Bucket D: dump local de leaks.
- Bucket E: paid providers (Apollo telefone, Lusha telefone) — a infra
  paga já existe no projeto mas **não foi conectada** ao novo merge helper
  de telefone. Hoje quem grava telefone via paid path é
  `apply_enrichment_updates` (caminho velho) que ainda usa só
  `_merge_email_verification`.
- Bucket F: HLR/MNP probe ativo. Validação é puramente offline via
  `phonenumbers`.
- Bucket G: crowdsourcing.
- Frontend Electron: zero mudanças. O endpoint devolve todos os campos
  novos, mas nenhum componente React lê eles ainda.
- Diretórios públicos (TheOrg, RocketReach, Páginas Amarelas) — não
  foram integrados ao pipeline novo, mesmo já existindo
  `public_directories.py`.

---

## 2. Arquivos criados

| Caminho | LOC aprox. | Função |
| --- | --- | --- |
| `src/beautiful_linkedin/storage/phone_harvester.py` | ~240 | Crawl + extração de telefones do site da empresa. |
| `src/beautiful_linkedin/storage/phone_validation.py` | ~170 | Wrapper sobre `phonenumbers` + heurísticas BR. |
| `src/beautiful_linkedin/storage/internal_phone_enrichment.py` | ~330 | Service + Orchestrator com SSE events. |
| `tests/test_phone_harvester.py` | ~160 | 10 testes. |
| `tests/test_phone_validation.py` | ~80 | 8 testes. |
| `tests/test_phone_merge.py` | ~120 | 7 testes. |
| `tests/test_internal_phone_enrich.py` | ~230 | 5 testes ponta-a-ponta. |
| `docs/actual-phone-discovery-implementation.md` | este arquivo | Documento de reflexão. |

## 3. Arquivos modificados

- `src/beautiful_linkedin/models.py` — 10 campos novos em `Lead`,
  todos `Optional` ou com default vazio.
- `src/beautiful_linkedin/storage/saved_leads.py` — `_ensure_column` de
  10 colunas novas, `_row_to_lead` lê elas, `_merge_phone_verification`
  + `_phone_compare_key` + `_digits_only` adicionados,
  `apply_internal_phone_enrichment_updates` adicionado.
- `src/beautiful_linkedin/server/app.py` — request schema aceita
  `"phone"|"both"`, summary ganhou 3 contadores de telefone, helpers
  `_default_phone_harvester`/`_default_phone_validator`/`_build_phone_orchestrator`/
  `_run_internal_phone_enrichment`, rota blocking e SSE roteiam por
  `fields`.
- `pyproject.toml` + `requirements.txt` — `phonenumbers>=8.13` adicionado.
- `tests/test_internal_enrich_endpoint.py` — teste antigo que
  exigia 422 para `fields="phone"` reescrito para exigir 422 só em valor
  desconhecido (`"smoke-signal"`).

---

## 4. Schema do banco

Migração via `_ensure_column` no `_ensure_schema`. Banco existente
continua válido sem migração manual:

```sql
ALTER TABLE saved_leads ADD COLUMN phone_type TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_country TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_carrier TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_region TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_validation_status TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_confidence INTEGER;
ALTER TABLE saved_leads ADD COLUMN phone_source TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_source_url TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_verified_by_json TEXT;
ALTER TABLE saved_leads ADD COLUMN phone_alternatives_json TEXT;
```

**Não criado nesta fatia:**
- `phone_validation_cache` (cache HLR proposto em §4.1 da arquitetura) —
  como HLR não foi implementado, não havia o que cachear.
- `phone_lookup_cache` (cache de respostas de bot Telegram) —
  bucket C não entrou.
- `phone_e164` como coluna separada — o `phone` existente é
  reaproveitado. O service grava E.164 lá direto (`+5511999999999`).
  Decisão consciente: não duplicar dado canônico vs display; a UI
  formata para exibição com `phonenumbers.format_number` sob demanda.
  **Risco:** quebra clientes que esperavam o formato antigo livre. Hoje
  não há cliente externo do schema, então OK por enquanto.

---

## 5. Contratos

### 5.1 `PhoneEnrichmentUpdate`

Dataclass duck-typed, mesma forma que `EnrichmentUpdate` do e-mail.
`saved_leads.apply_internal_phone_enrichment_updates` lê campos via
`getattr` para evitar dependência circular. Campos:

- `phone` (E.164 canônico)
- `national` (forma "(11) 99999-9999")
- `phone_type` (string do `PhoneType` enum)
- `country`, `carrier`, `region`
- `validation_status` (string do `PhoneValidationStatus` enum)
- `confidence` (0-100)
- `source` (sempre `"internal"` nesta fatia)
- `source_url`
- `skipped_existing_phone: bool`
- `failure_reason` (`"existing_phone"` | `"no_candidate"` | `None`)
- `discarded_candidates: list[str]`

### 5.2 `_merge_phone_verification`

Espelho exato do gêmeo de e-mail, mas com uma diferença crítica:
**comparação por E.164 canônico, não por dígitos crus.**

Por quê: `+55 11 99999-9999` (13 dígitos com country code) e
`11999999999` (11 dígitos sem country code) são o mesmo número, mas
`_digits_only` produziria chaves diferentes. `_phone_compare_key`
chama `phonenumbers.parse(value, "BR")` e formata em E.164 antes de
comparar. Se o parse falha, cai pra `_digits_only` como fallback
(comparação ainda funciona quando ambos os lados são E.164 já
normalizados, que é o caso default).

**Risco assumido:** o default region `"BR"` é hardcoded no helper. Se
o usuário tem leads US com telefones em formato nacional ("415-555-0132"),
o merge vai tentar parsear como BR primeiro e falhar — cai pro fallback
de dígitos, que ainda funciona. Mas dois números US no formato
nacional (sem `+1`) mas com formatações diferentes podem não casar.
Aceito conscientemente — público-alvo é BR.

### 5.3 Score do candidato

Função `_score(candidate, validation)` em `internal_phone_enrichment.py`.
Combina:
- **Base:** 50 se `VALID`, 35 se `PROBABLE`.
- **Context boost:** `tel:` +25, `wa.me` +25, JSON-LD +20, texto +0.
- **Type boost:** mobile +15, fixed_or_mobile +10, voip +5, fixed +5,
  unknown 0, pager −10, toll_free −50.
- Clamp em [0, 100].

**Pontos críticos do design:**
- Não há sinal de "nome do lead aparece perto do número na página".
  O score é puramente sobre tipo + contexto + validação. Resultado:
  para uma empresa que publica 3 mobiles no `/team`, todos os leads
  da empresa vão receber o mesmo número (o melhor pontuado),
  porque o orquestrador não correlaciona texto-do-card com candidato.
  É a maior limitação real desta fatia.
- "Não há diferença" entre `wa.me` e `tel:` no boost — ambos são +25.
  Discutível: `wa.me` é frequentemente o número do SAC / atendimento,
  não necessariamente de um indivíduo. Mantive empate pra não
  super-engenheirar sem dado real.
- `+50` no toll_free é negativo intencionalmente: já há um gate em
  `PhoneValidator` que marca 0800 como `RISKY` (excluído da seleção),
  então esse boost só serve de defesa em profundidade caso a heurística
  de prefixo do harvester falhe.

---

## 6. Decisões que divergem da arquitetura proposta

Pontos onde o código não bateu com `docs/phone-discovery-architecture.md`:

1. **PdfPhoneExtractor não foi implementado.** Proposto em §3.2.2, ficou
   inteiramente fora desta fatia. Bucket B portanto está só parcialmente
   coberto (JSON-LD).
2. **`PhoneLookupProvider` (interface §3.2.3) não foi criada.** A
   abstração para fontes externas (UsersBox, Eye of God, banco local)
   é zero-código hoje. O `PhoneEnrichmentUpdate` só tem `source="internal"`
   porque é o único produtor.
3. **`PhoneEnrichmentOrchestrator` tem 2 fases, não 5.** A proposta
   pedia `discovering → harvesting → lookup → validating → merging`. O
   código tem só `harvesting → validating`. As fases ausentes:
   - `discovering`: não chamei `DomainDiscoveryService` no
     orquestrador de telefone. Os domínios usados são os mesmos
     `collect_company_domains_from_leads` do e-mail. **Consequência:**
     o phone enrichment não descobre `nubank.io` automaticamente
     quando só `nubank.com.br` está conhecido. Reaproveitar o
     discoverer seria barato — não fiz por economia de escopo.
   - `lookup`: bucket C não existe.
   - `validating` foi fundida no service, não é uma fase separada do
     orquestrador.
   - `merging` é feita no `apply_internal_phone_enrichment_updates`, não
     numa fase do orquestrador. Diferença puramente de localização —
     o helper de merge é o mesmo.
4. **Sem `confidence_score` ponderado por frequência cross-source.**
   A proposta (§3.2.4 item 5) pede que números aparecendo em múltiplas
   fontes recebam boost. Hoje, com só uma fonte (`internal`), o sinal
   é zero — então adiar foi o certo. Mas quando paid for ligado, vai
   precisar entrar.
5. **Sem variáveis de ambiente novas.** §8 da proposta listava
   `USERSBOX_API_KEY`, `PHONE_HLR_*`, etc. Nada disso entrou no
   `Settings` porque nenhuma feature que consome essas keys foi
   implementada.

---

## 7. SSE e tagueamento de canal

Quando `fields="both"`, e-mail e telefone rodam **sequencialmente** no
mesmo worker thread:

1. Phase 1: e-mail completo (discovering + harvesting + validating).
2. Phase 2: telefone (harvesting + validating).
3. Done.

Eventos do telefone são "tagueados" com `channel="phone"` no helper
local `push_phone` para o renderer demultiplexar. **Eventos de e-mail
NÃO são tagueados com `channel="email"`** — o renderer assume default
"email" para qualquer evento sem `channel`. Isso é frágil:

- Se algum dia o consumer SSE tentar filtrar `event.channel == "email"`
  explicitamente, todos os eventos antigos somem.
- Decisão tomada por compatibilidade retroativa com o renderer atual
  que não conhece o conceito de canal.

Alternativa que considerei e descartei por escopo: emitir
`{"type": "channel", "channel": "email"|"phone"}` antes de cada bloco
para o consumer trocar de bucket. É mais limpo mas exige mudança no
renderer — fora da fatia.

---

## 8. Cobertura de testes

Sobe de 461 → 491 (todos os 30 novos passam, nada antigo quebra).
Distribuição:

- **`test_phone_harvester.py` (10):** tel anchor, wa.me + api.whatsapp,
  JSON-LD `telephone`, raw text BR, descarte de 0800/4004/3003/00000,
  dedup cross-path, HTTP errors, domínio vazio, `iter_jsonld_phones`
  com payload aninhado, strip de `<script>` antes do regex de texto.
- **`test_phone_validation.py` (8):** BR mobile → E.164, US com `+`,
  BR fixed, call-center risky, repetitivos inválidos, vazio/lixo,
  separadores sujos (`11.99999-9999`), override de região.
- **`test_phone_merge.py` (7):** primeira escrita seed, mesmo número
  formato diferente → verified_by, divergência → alternatives,
  dedup por `(phone, source)`, source diferente mesmo alt, incoming
  vazio noop, source já presente não duplica.
- **`test_internal_phone_enrich.py` (5):** harvest + score escolhe
  mobile, skip existente, falha sem candidato, `fields="both"`,
  SSE com `channel="phone"`.

**Pontos NÃO cobertos por teste:**

- O endpoint blocking com `fields="phone"` quando o harvester real
  bate na rede — fixture autouse só desativa domain discovery, **não**
  desativa o `PhoneNumberHarvester` real automaticamente. Os testes
  que dependem disso monkeypatcham explicitamente. Falta uma fixture
  autouse `_disable_real_phone_harvester` análoga à de discovery.
- Cancelamento via `cancel_check` na fase de telefone. Existe no
  e-mail, copiei a mesma estrutura, mas não há teste.
- Concorrência: `lead_concurrency=6` é o default, nenhum teste valida
  que rodar 100 leads paralelos não corrompe estado.
- Casos em que `phonenumbers` lança exceção fora de `NumberParseException`
  — confio no `try/except Exception` defensivo, mas não está testado.

---

## 9. Pontos de fragilidade reconhecidos

Listados aqui pra estarem visíveis na revisão crítica.

1. **Falsos positivos do regex de texto.** Pages com CNPJ, CEP, número
   de processo, ou tabelas de preços podem disparar o regex. O harvester
   tenta mitigar com `_is_plausible_phone` (8-15 dígitos, sem
   repetitivos, sem call-center), mas o validador é a defesa final.
   Resultado prático: vários candidatos descartados, custo de validação
   maior que ideal.
2. **Sem correlação card-a-card.** Já mencionado — todo lead da empresa
   herda o "melhor" número do site da empresa. Para empresas pequenas
   com um único telefone publicado, é o comportamento certo. Para
   empresas grandes com lista de telefones por pessoa, todos os leads
   vão receber o mesmo número.
3. **Hardcoded BR como default region.** Em `PhoneValidator` e em
   `_phone_compare_key`. Configurável via construtor no validator,
   não no compare key. Trocar pra `Settings.default_region` resolve.
4. **`_normalize_company_key` duplicado.** Existe igual em
   `internal_enrichment.py` (e-mail) e copiei pra
   `internal_phone_enrichment.py`. Coloquei comentário justificando
   ("evita import circular que já bateu em refactors anteriores") mas
   é dívida — se um lado mudar, o outro fica órfão.
5. **`apply_internal_phone_enrichment_updates` duck-typeia `update`
   inteiro via `getattr`.** Idêntico ao padrão do e-mail. Pro/contra
   bem documentado no código fonte; permite que paid providers
   eventualmente passem outro dataclass sem migration. Frágil porque
   typo no nome de campo silenciosamente vira `None`.
6. **`channel="phone"` em eventos SSE sem `channel="email"` simétrico.**
   Ver §7.
7. **O service não recebe `existing_company_phones` cross-table.**
   No e-mail, `existing_company_emails` é usado pra inferir pattern.
   Telefone não tem padrão equivalente — então o argumento ficou de
   fora. Mas significa que se Apollo já gravou um mobile pro CFO da
   Nubank, esse sinal **não** se propaga pra outros leads da Nubank
   no caminho interno. (Não que o caminho interno pudesse derivar
   nada útil dele — telefones individuais não têm pattern como
   `first.last@`.)
8. **`PhoneType.TOLL_FREE` retorna `RISKY` mas ainda preenche `e164`,
   `national`, `type`, `country`.** Útil pra UI debugar; mas significa
   que `validation_status="risky"` pode aparecer no banco com dados
   semi-preenchidos. O orquestrador filtra `RISKY` antes do score,
   então não vira primário; mas se alguém ler a row crua, vai ver um
   E.164 sem ele ter virado lead.phone.
9. **Sem rate limit por domínio no harvester.** Se uma empresa tiver
   `/contato`, `/sobre`, `/team` e mais 6 paths default, são 10
   requests sequenciais. Sem timeout configurável por path — só o
   timeout global de 5s. Empresa lenta = 50s pra harvest.
10. **Sem retry em falha transiente.** Status != 200 vira string
    vazia, candidato silenciosamente perdido. Não há diferença entre
    "site fora do ar" e "site sem telefones". O endpoint reporta
    `failed_no_candidate` para ambos.

---

## 10. Como reproduzir / verificar

```bash
# Suíte completa
pytest tests -q

# Apenas a fatia nova
pytest tests/test_phone_harvester.py tests/test_phone_validation.py tests/test_phone_merge.py tests/test_internal_phone_enrich.py -q
```

Resultado em 2026-05-20 nesta máquina: **491 passed, 0 failed, ~26s**.

Para testar o endpoint manualmente sem subir o Electron:

```bash
# Sobe o sidecar
python -m beautiful_linkedin.server

# Em outro terminal — supondo table_id já existente
curl -X POST http://127.0.0.1:8765/lead-tables/<TABLE_ID>/internal-enrich \
  -H "Content-Type: application/json" \
  -d '{"fields":"phone","confirmed":true}'
```

Sem credenciais externas. Sem chamadas pagas. Resposta inclui
`summary.enriched_phone_leads` e os campos novos preenchidos nos
`leads[*].phone*`.

---

## 11. Onde eu mais provavelmente errei

Pré-defesa antes da crítica vir. Em ordem de preocupação:

1. **Score function é arbitrária.** Não há dado real validando que
   `tel:` deve ser +25 vs JSON-LD +20. Vai precisar de calibração
   com dataset real depois do bucket A+B rodar em produção.
2. **A ausência de correlação nome ↔ candidato (§9.2) é o maior
   buraco arquitetural.** Eu sabia disso e implementei mesmo assim
   porque a alternativa "varrer DOM ao redor do nome do lead" exige
   parser HTML mais sério (BeautifulSoup ou lxml com xpath) e mais
   testes. Aceitei a dívida.
3. **Não rodei o pipeline contra um site real.** Toda a validação é
   em HTML sintético nos testes. Pages reais podem ter:
   - JavaScript que renderiza o telefone client-side (regex falha,
     porque HTTPX não executa JS — mesma limitação do harvester de
     e-mail existente, então consistente).
   - Cloudflare/captcha bloqueando o User-Agent que mandei.
   - Sites com 30 redirects.
   - Páginas que só funcionam com cookies.
4. **`phonenumbers` é uma dependência grande (~6 MB de metadata)** e
   eu adicionei sem checar impacto no PyInstaller bundle. Provavelmente
   aumenta o instalador Windows em ~10 MB. Não verificado.
5. **`docs/phone-discovery-architecture.md` afirma "frontend espelha
   LeadEmailCell".** Não fiz nada disso. O endpoint devolve dados, mas
   nenhuma UI consome. Um revisor pode argumentar que a fatia está
   "pela metade" e ele teria razão se considerar a UI parte do MVP.
6. **Sem doc no README.md mencionando o novo `fields="phone"|"both"`.**
   API mudou e o README não acompanhou. Conservei a CLAUDE.md também
   sem update.
7. **`field`/`fields` validator agora aceita 3 valores, mas
   `confirmed: bool` continua ignorado pelo internal path** (não há
   estimativa de custo a confirmar — é grátis). Mantive o campo por
   compat de schema, mas é cosmético.

---

## 12. O que eu defendo

Se a crítica disser "isso está incompleto / vocês deveriam ter feito
tudo de uma vez": eu defendo a fatia vertical mínima por dois
motivos concretos:

- **Cada bucket adicional carrega complexidade independente.** Bucket
  C (UsersBox) precisa de auth flow Telegram, créditos em cripto, e
  parser de resposta semi-estruturada. Bucket D precisa decisão de
  storage de leak (10+ GB). Bucket F precisa contratar provider HLR.
  Misturar tudo numa PR única dobra o tempo de revisão sem ganho de
  qualidade.
- **O bucket A+B sozinho é testável end-to-end sem chave externa,
  sem custo, sem risco legal.** Posso entregar essa fatia em
  produção em 1 dia se passar revisão. As outras fatias têm
  dependências externas que estendem o ciclo.

Se a crítica disser "vocês não cobriram o caminho real (sites
dinâmicos / cloudflare / etc)": eu reconheço (§11 item 3) e
proporia adicionar testes de integração contra fixtures HTML
reais (snapshots de páginas baixadas), não rodando em CI mas em
um job separado opt-in.

Se a crítica disser "o score é mágico": concordo (§11 item 1) e
proponho substituir por aprendizado supervisionado sobre as
escolhas do usuário (lead.phone editado manualmente → exemplo
positivo), mas isso pertence a uma fatia depois de ter dataset.
