# Dossie de Dores — Fluxo Telegram Phone

> Data: 2026-05-22  
> Objetivo: consolidar todas as dores reportadas no fluxo de descoberta de telefone via Telegram, os erros de arquitetura/implementacao que Codex e Claude vem cometendo, e um caminho objetivo para corrigir hoje.

---

## 1. Intencao real do fluxo

O botao **Pegar telefone via Telegram** deve ser o unico ponto operacional do fluxo de telefone via Telegram.

Ele nao deve ser interpretado como "validar LinkedIn". A validacao/aprofundamento no LinkedIn pode acontecer como etapa auxiliar para obter sinais de qualidade, mas o fluxo precisa obrigatoriamente chegar no Telegram quando o lead nao estiver bloqueado por uma regra real.

Fluxo esperado por lead:

1. Verificar se ja existe CPF persistido e confiavel para o lead.
2. Se existe CPF persistido/verificado, pular `/nome` e fazer `/cpf <cpf>` diretamente no Gonzales.
3. Se nao existe CPF:
   - fazer `/nome <nome>` no Gonzales;
   - parsear CPFs candidatos;
   - ranquear candidatos com dados do LinkedIn;
   - consultar no maximo 3 CPFs acima do threshold;
   - para cada CPF aprovado, fazer `/cpf <cpf>`;
   - extrair telefones da resposta.
4. Se `/nome` nao retornar CPF util:
   - usar e-mail ja encontrado pelo Mail Finder;
   - e agora tambem usar e-mail encontrado na etapa de aprofundar/validar LinkedIn;
   - fazer `/usa <email>` no Findex;
   - clicar em **RESULTADO AQUI**;
   - raspar a pagina aberta;
   - extrair telefone.
5. Persistir telefone sem sobrescrever telefone existente.
6. Se ja tinha telefone, isso e resultado neutro/sucesso informativo, nao erro.

---

## 2. Individualidade dos bots

### Gonzales

Uso atual correto:

- URL: `https://web.telegram.org/k/#@ConsultoriaGonzalesbot`
- Consultas:
  - `/nome <nome>`
  - `/cpf <cpf>`
- Resultado:
  - o bot responde no privado;
  - aparece botao tipo **Ver Resultado Completo** / **ver resultado completo**;
  - clicar no botao;
  - aceitar popup de link externo;
  - raspar a pagina aberta.

Erro que vinha acontecendo:

- parte do codigo/documentacao ainda pensava no Gonzales via grupo gratis antigo.
- isso conflita com o disclaimer: Gonzales agora deve ser privado, nao grupo.

### Findex / FDX

Uso correto:

- URL: `https://web.telegram.org/k/#@FdxGP_bot`
- Consulta:
  - `/usa <email>`
- Resultado:
  - clicar no botao **RESULTADO AQUI**;
  - aceitar popup/link externo;
  - raspar a pagina aberta;
  - extrair telefone.

Regra de entrada:

- Findex nao substitui Mail Finder.
- Mail Finder continua separado do fluxo Telegram.
- O fluxo Telegram pode consumir e-mail ja persistido pelo Mail Finder.
- Nova regra reportada: se a etapa LinkedIn encontrar e-mail no contato do perfil, tambem pode usar esse e-mail no Findex.

### Unix

Status esperado:

- Manter o fluxo Unix existente.
- Ele nao deve ser confundido com Gonzales.
- Se for usado como fallback de dados brutos, manter a rota propria e parser proprio.

Ponto de cuidado:

- O fluxo de telefone do botao unico nao pode depender do Unix se a regra operacional definida for Gonzales + Findex.
- Se Unix entrar, precisa ser decisao explicita: "fallback de raw data", nao caminho acidental.

---

## 3. Dores reportadas ate agora

### Dor 1: O botao de telefone abre LinkedIn e nao inicia Telegram

Sintoma:

- Ao clicar em **Pegar telefone via Telegram**, o app abre/raspa LinkedIn.
- O usuario nao ve o fluxo iniciar no Telegram.
- Resultado no banner: `Telegram phone: 0 com telefone novo · 0 persistidos · 0 ja tinha telefone · 1 bloqueado(s).`

Causa provavel:

- O endpoint `/telegram-phone` roda `_refresh_linkedin_signals_for_telegram()` antes de chamar Gonzales/Findex.
- Essa etapa e "best effort", mas na pratica virou gate bloqueante indireto.
- Quando o LinkedIn salva um cargo ruim, especialmente `Pular para conteudo principal`, o gate de cargo considera divergente e bloqueia antes do Telegram.

Como resolver corretamente:

- Separar semanticamente:
  - `linkedin_prefetch`: coleta sinais, nunca deve bloquear sozinho.
  - `title_gate`: so bloqueia se houver cargo real e confiavel.
  - `telegram_dispatch`: deve ser registrado/logado quando chamado.
- Se o cargo lido for ruido de navegacao, ignorar e usar o cargo original salvo.
- Se nao ha cargo confiavel, preferir continuar Telegram ou marcar `linkedin_titulo_ausente` somente quando a tabela realmente exige gate e nao existe nenhum titulo.
- Adicionar telemetria por lead: `stage=linkedin_prefetch_started`, `stage=linkedin_prefetch_done`, `stage=telegram_name_started`, `stage=telegram_cpf_started`, `stage=findex_email_started`, `stage=blocked`.

### Dor 2: Validacao LinkedIn troca cargo por "Pular para conteudo principal"

Sintoma:

- Na tabela, o cargo vira `Pular para conteudo principal`.
- Abaixo aparece "Validado no LinkedIn".
- Isso destrói a qualidade da tabela e bloqueia o fluxo Telegram por cargo divergente.

Causa provavel:

- Parser de experiencia LinkedIn pega textos visiveis/`aria-hidden`.
- O skip-link de acessibilidade entra como primeira linha antes da experiencia real.
- Quando nao encontra periodo/empresa do jeito esperado, o fallback pega os primeiros candidatos limpos, e o skip-link passa como "cargo".

Como resolver corretamente:

- Filtrar ruido de navegacao em todas as etapas do parser:
  - `Pular para conteudo principal`
  - `Skip to main content`
  - variacoes com acento quebrado: `conte?do`, `conte do`
- Nunca persistir esse texto em:
  - `title`
  - `linkedin_experience_title`
  - notas de validacao.
- Ao persistir uma nova validacao sem cargo confiavel, limpar cargo LinkedIn contaminado anterior, mas nao inventar cargo novo.
- Testar especificamente HTML sem periodo, porque e nesse fallback que o erro aparece.

### Dor 3: E-mail achado no LinkedIn nao alimenta Findex

Sintoma:

- O aprofundamento LinkedIn encontra e-mail de contato.
- Mesmo assim, quando `/nome` nao acha CPF, o fluxo nao faz `/usa <email>`.

Causa provavel:

- O fallback Findex estava olhando apenas `lead.email`.
- `linkedin_contact_email` fica em outro campo e pode ir apenas para alternativas/trail.

Como resolver corretamente:

- Resolver e-mail para Findex nesta ordem:
  1. `lead.email`, se existir;
  2. `lead.linkedin_contact_email`, se existir;
  3. primeira alternativa de e-mail confiavel com fonte `linkedin_contact` ou Mail Finder, se existir e estiver modelada;
  4. senao bloquear com `no_email_for_findex_fallback`.
- Registrar no `provenance` qual e-mail foi usado e qual era a fonte: `primary`, `linkedin_contact`, `mail_finder`, `alternative`.

### Dor 4: Mail Finder foi confundido com Telegram

Sintoma:

- O fluxo de e-mail e o fluxo Telegram foram misturados conceitualmente.
- Houve risco de implementar "mail finder dentro do Telegram".

Regra correta:

- Mail Finder e separado.
- Telegram Findex nao acha e-mail; ele usa e-mail como entrada para tentar telefone.
- Botao **Achar e-mails** deve continuar responsavel por descobrir e-mail.
- Botao **Pegar telefone via Telegram** pode consumir e-mails ja persistidos.

Como resolver corretamente:

- Nomear funcoes com precisao:
  - `run_mail_finder_email_enrichment`
  - `run_telegram_phone_flow`
  - `run_findex_email_to_phone_fallback`
- Evitar nomes genericos como `internal phone`, `telegram group`, `email fallback` sem provider.

### Dor 5: "Ja tinha telefone" aparece como erro

Sintoma:

- Banner vermelho mesmo quando o fluxo apenas identificou que o lead ja tinha telefone.

Causa:

- UI tratava `leads_with_phone == 0` e `phones_persisted == 0` como erro, ignorando `skipped_existing_phone`.

Como resolver corretamente:

- Se `skipped_existing_phone > 0`, feedback deve ser `success` ou `info`.
- Mensagem deve diferenciar:
  - "sem candidato"
  - "bloqueado"
  - "ja tinha telefone"
  - "erro do bot"
  - "rate limit".

### Dor 6: CPF persistido nao estava claramente priorizado

Regra correta:

- Se existe CPF persistido com score/confiabilidade suficiente, nao rodar `/nome`.
- Rodar direto `/cpf <cpf>`.

Risco atual:

- CPF pode estar persistido como row de `tabela_telegram` de query `name`, nao como campo explicito do lead.
- O fluxo precisa saber diferenciar:
  - CPF candidato fraco;
  - CPF aprovado;
  - CPF usado em `/cpf`;
  - CPF com telefone retornado.

Como resolver corretamente:

- Introduzir/usar conceito claro de `verified_cpf_candidate`.
- Regras:
  - `match_score >= 65` e sem erro -> elegivel.
  - maximo 3 candidatos por lead.
  - ordenar por score desc.
  - registrar `source_run_id`.

### Dor 7: Parsers e saida do Telegram nao estao alinhados

Sintoma:

- Telegram retorna paginas/textos diferentes por bot.
- O pipeline tenta tratar tudo como se fosse o mesmo tipo de dado.

Como resolver corretamente:

- Modelar raw data com campos minimos:
  - `provider`: `gon | unix | findex | void`
  - `query_type`: `name | cpf | email`
  - `query_value`
  - `raw_text`
  - `source_url`
  - `downloaded_at`
  - `error`
- Parser por provider:
  - `parse_gonzales_name_result`
  - `parse_gonzales_cpf_result`
  - `parse_findex_email_result`
  - `parse_unix_name_result`
- Normalizador comum depois do parser:
  - `peopleData.name`
  - `peopleData.mail`
  - `peopleData.address`
  - `peopleData.cpf`
  - `peopleData.phone`
  - `confidence`
  - `provenance`.

### Dor 8: Rate limit e timing humano foram tratados como detalhe

Requisito do diagrama:

- Bots tem limite de uso.
- Quando aparecer limite/muitas requisicoes, parar aquele provider temporariamente.
- Manter timing humano.
- Nao disparar consultas rapido demais.

Como resolver corretamente:

- Cooldown por provider:
  - `gon`: ao detectar uso excessivo, cooldown.
  - `findex`: cooldown separado.
  - `unix`: cooldown separado.
- Persistir cooldown em SQLite, nao so memoria.
- Logs:
  - `provider_state.cooldown_until`
  - `last_error`
  - `last_seen_at`.
- Delay humano entre:
  - abrir chat;
  - enviar query;
  - clicar botao;
  - abrir resultado;
  - fechar/alternar tab.

### Dor 9: O fluxo foi arquitetado como se fosse EDA, mas sem eventos observaveis

Intencao arquitetural:

- Traduzir topicos Kafka/EDA para algo local/in-process.
- Nao quebrar em microservicos.
- Nao introduzir Kafka.

Erro cometido:

- Foram criadas funcoes/rotas, mas nao um "event log" claro o suficiente para debugar.
- Sem eventos por stage, fica impossivel saber se parou no LinkedIn, no gate, no Gonzales, no Findex, no parser ou no merge.

Como resolver corretamente:

- Usar SQLite como event store local.
- Cada etapa escreve row/evento:
  - `run_started`
  - `linkedin_prefetch_started`
  - `linkedin_prefetch_done`
  - `title_gate_passed`
  - `title_gate_blocked`
  - `persisted_cpf_selected`
  - `gonzales_name_sent`
  - `gonzales_name_result_opened`
  - `gonzales_name_parsed`
  - `gonzales_cpf_sent`
  - `gonzales_cpf_parsed`
  - `findex_email_sent`
  - `findex_result_opened`
  - `findex_parsed`
  - `phone_merge_done`
  - `run_finished`.
- UI deve conseguir mostrar o ultimo stage por lead.

### Dor 10: CNS/documentos do Gonzales foram persistidos como telefone

Sintoma:

- O Gonzales/SISREG retorna um bloco **Dados pessoais** com `CPF`, `CNS`,
  nome e outros documentos.
- O sistema pegou o valor do `CNS` (`700003672681800`) e persistiu como
  telefone.
- O telefone real estava mais abaixo, na aba/secao **Contatos**, em campos
  como:
  - `TELEFONE 1 [RESIDENCIAL] ((12)) 3949-2060`
  - `TELEFONE 2 [RESIDENCIAL] ((12)) 98198-4989`

Causa:

- A extracao de telefone usava regex permissiva no texto inteiro da pagina.
- Um CNS tem 15 digitos, que passa em muitos filtros genericos porque E.164
  tambem permite ate 15 digitos.
- Como o CNS aparece antes da secao **Contatos**, ele virava primeiro
  candidato e podia ser salvo como telefone primario.

Como resolver corretamente:

- Para resultado Gonzales/SISREG, preferir numeros em contexto de contato:
  - linhas com `Telefone`, `Fone`, `Celular`, `WhatsApp`, `Contato`;
  - janela curta de linhas logo abaixo desses labels.
- Normalizar DDD renderizado como `((12))` para `(12)` antes da regex.
- Se nao houver label de contato, so entao usar fallback generico.
- No fallback generico, remover/mascarar linhas de contexto documental:
  - `CPF`
  - `CNS`
  - `RG`
  - `CEP`
  - `Documentos`
  - `Cartao/Cartão Nacional`.
- Testar que `CNS 700003672681800` nunca entra em `phone_candidates`.
- Se um CNS ja foi salvo anteriormente no campo `phone`, ele nao pode
  bloquear a proxima rodada como "ja tinha telefone"; ao chegar um telefone
  real do Gonzales, o valor documental deve ser substituido.

Teste obrigatorio:

- Raw text com bloco `Dados pessoais` contendo CPF/CNS e bloco `Contatos`
  contendo dois telefones deve retornar apenas os dois telefones da secao
  contato.
- Lead com `phone="700003672681800"` deve permitir persistir
  `+551239492060` quando o Gonzales retornar esse telefone real.

---

## 4. Erros que Codex/Claude vem cometendo

### Erro 1: Confundir "testes passam" com "fluxo real funciona"

O fluxo tem automacao de browser, Telegram Web, botoes dinamicos, popup externo e paginas geradas por bot. Teste unitario com fake prova contrato local, mas nao prova que:

- abriu o chat certo;
- enviou a mensagem certa;
- clicou no botao certo;
- aceitou popup;
- raspou a aba correta;
- retornou texto parseavel.

Correcao:

- Adicionar modo dry-run/instrumentado com Playwright/CDP que registra cada stage.
- Fazer pelo menos um teste manual assistido com lead controlado e print/log por etapa.

### Erro 2: Implementar em volta do fluxo antigo em vez de obedecer o fluxo novo

Exemplos:

- Manter comentario/copy falando de `CONSULTASGRATIS4NV` para Gonzales.
- Reusar nomes como `telegram_group` quando agora o caminho principal e bot privado.
- Preservar endpoint antigo mentalmente e colocar remendos no botao.

Correcao:

- Renomear a linguagem do codigo:
  - `GonzalesBotConsult`, nao `TelegramGroup...` para Gonzales.
  - `FindexEmailConsult`, nao fallback generico.
  - `telegram_phone_flow`, nao `internal phone`.

### Erro 3: Deixar a etapa LinkedIn bloquear silenciosamente o Telegram

O usuario clica telefone via Telegram e ve LinkedIn abrindo. Se depois bloqueia, parece que o Telegram nunca foi implementado.

Correcao:

- Preflight LinkedIn deve ser visivel e nao silencioso.
- Se bloquear, mostrar exatamente:
  - `bloqueado antes do Telegram: cargo divergente`
  - `cargo lido: X`
  - `cargo original: Y`
  - `target_titles: [...]`
- Se cargo lido e ruido, ignorar.

### Erro 4: Persistir ruido como dado canonico

`Pular para conteudo principal` jamais deveria substituir `title`.

Correcao:

- Parser filtra.
- Store filtra.
- Gate filtra.
- UI poderia tambem esconder/alertar como defesa final.

### Erro 5: Fazer fallback de e-mail estreito demais

O e-mail do LinkedIn fica em `linkedin_contact_email`, mas o fallback so usava `email`.

Correcao:

- Resolver e-mail em uma funcao central.
- Testar todos os caminhos:
  - primary email;
  - LinkedIn contact email;
  - sem e-mail;
  - e-mail invalido.

### Erro 6: Misturar "consulta por e-mail" com `cpf_consult`

Em alguns payloads, a row de fallback Findex e devolvida como `cpf_consult`, apenas porque a UI tinha dois slots: name/cpf.

Risco:

- Confusao futura: `cpf_consult.query_type == "email"`.

Correcao:

- Ajustar payload para `stage_consults: TelegramConsult[]`.
- Enquanto nao refatorar UI, documentar claramente que `cpf_consult` pode carregar `query_type=email`, mas isso e divida tecnica.

### Erro 7: Pouca observabilidade para uma automacao instavel

Sem eventos por etapa, qualquer bug vira "nao funcionou".

Correcao:

- Retornar no response por lead:
  - `last_stage`
  - `blocked_reason`
  - `queries_sent`
  - `telegram_rows_created`
  - `used_email`
  - `used_cpf`
  - `provider_errors`.

---

## 5. Contrato de implementacao que deve ser seguido

### Entrada do botao

Payload:

```json
{
  "lead_refs": ["..."],
  "target_titles": ["marketing", "tech"],
  "max_leads": 10
}
```

### Saida minima por lead

```json
{
  "lead_ref": "...",
  "lead_name": "Ana Silva",
  "last_stage": "findex_parsed",
  "blocked_reason": null,
  "used_cpf": null,
  "used_email": "ana@empresa.com",
  "candidates": [
    {
      "phone_digits": "11999990000",
      "source_provider": "findex",
      "confidence": 60,
      "provenance": {
        "score_source": "findex_email_fallback",
        "email_source": "linkedin_contact"
      }
    }
  ],
  "consults": []
}
```

### Block reasons aceitaveis

- `linkedin_cargo_divergente`
- `linkedin_titulo_ausente`
- `no_cpf_from_name_stage`
- `no_eligible_cpf`
- `no_email_for_findex_fallback`
- `email_stage_no_phone`
- `rate_limited`
- `telegram_navigation_failed`
- `result_button_not_found`
- `parser_no_phone`

Qualquer outro erro precisa cair como `provider_error:<provider>:<code>`.

---

## 6. Plano pratico para corrigir hoje

### Passo 1: Provar onde o fluxo esta parando

Adicionar log/evento antes e depois de cada chamada relevante:

- antes do LinkedIn prefetch;
- depois do LinkedIn prefetch;
- antes do gate;
- resultado do gate;
- antes de `name_driver.consult`;
- antes de `cpf_driver.consult`;
- antes de `email_driver.consult`;
- depois do parse;
- depois do merge.

Critério de pronto:

- Clicar no botao e saber se parou antes do Telegram ou dentro do Telegram.

### Passo 2: Corrigir definitivamente o parser de LinkedIn

Regras:

- `Pular para conteudo principal` nunca e cargo.
- Se experiencia nao tem cargo confiavel, nao atualizar `title`.
- Se ja existe `title` poluido, limpar na proxima validacao.

Testes obrigatorios:

- HTML com skip-link antes da experiencia e com periodo.
- HTML com skip-link antes da experiencia e sem periodo.
- Persistencia de contato LinkedIn sem cargo nao pode manter title poluido.

### Passo 3: Garantir que o gate nao bloqueia por ruido

Regras:

- Se `linkedin_experience_title` e ruido, usar `lead.title`.
- Se ambos sao ruido/ausentes, decidir:
  - se tabela exige gate, marcar `linkedin_titulo_ausente`;
  - se nao exige gate, continuar.

Teste obrigatorio:

- Lead com `linkedin_experience_title="Pular para conteudo principal"` e `title="Head of Marketing"` precisa chamar `/nome`.

### Passo 4: Garantir fallback Findex com e-mail LinkedIn

Regras:

- Se `/nome` nao trouxe CPF e existe `linkedin_contact_email`, chamar `/usa`.
- Registrar `query_type=email`.
- Registrar `query_value=<email>`.
- Registrar `provenance.email_source=linkedin_contact`.

Teste obrigatorio:

- Lead sem `email`, com `linkedin_contact_email`, name sem CPF, deve chamar `email_consult_fn`.

### Passo 5: Validar browser real com um lead controlado

Checklist manual:

- Chrome CDP aberto em `http://127.0.0.1:9222`.
- Telegram Web logado.
- LinkedIn logado.
- Selecionar 1 lead.
- Clicar botao.
- Ver no log:
  - entrou no endpoint;
  - passou ou explicou gate;
  - abriu `@ConsultoriaGonzalesbot`;
  - enviou `/nome` ou `/cpf`;
  - clicou resultado;
  - parseou;
  - se sem CPF, abriu `@FdxGP_bot`;
  - enviou `/usa`;
  - clicou `RESULTADO AQUI`.

### Passo 6: UI precisa mostrar diagnostico, nao so resumo

Hoje o banner agregado e insuficiente.

Adicionar por lead:

- ultimo stage;
- provider;
- query type;
- blocked reason;
- erro bruto curto;
- link/source_url se existir.

---

## 7. Prompt curto para Claude/Codex executar sem se perder

```text
Voce deve corrigir o fluxo do botao "Pegar telefone via Telegram".

Nao implemente outro fluxo de telefone. Nao use Mail Finder dentro do Telegram. Mail Finder e separado; o Telegram consome e-mail ja persistido.

Fluxo correto por lead:
1. Rodar prefetch LinkedIn apenas como coleta de sinais. Ele nao pode bloquear silenciosamente o Telegram.
2. Se LinkedIn retornar cargo "Pular para conteudo principal" ou "Skip to main content", ignore esse valor e nao persista como title/linkedin_experience_title.
3. Gate de cargo deve usar linkedin_experience_title somente se for cargo real; se for ruido, usar lead.title.
4. Se houver CPF persistido e confiavel, chamar Gonzales /cpf diretamente.
5. Se nao houver CPF, chamar Gonzales privado:
   https://web.telegram.org/k/#@ConsultoriaGonzalesbot
   com /nome <nome>.
6. Parsear CPFs, ranquear, consultar no maximo 3 acima do threshold com /cpf <cpf> no mesmo bot.
7. Se nao houver CPF util e houver lead.email ou lead.linkedin_contact_email, chamar Findex:
   https://web.telegram.org/k/#@FdxGP_bot
   com /usa <email>, clicar RESULTADO AQUI e parsear telefone.
8. "Ja tinha telefone" nao e erro.
9. Adicione eventos/logs por etapa para provar onde o fluxo parou.

Antes de mudar codigo, escreva testes que falhem para:
- parser LinkedIn com "Pular para conteudo principal" antes da experiencia;
- gate Telegram ignorando linkedin_experience_title poluido e chamando /nome;
- fallback Findex usando linkedin_contact_email quando lead.email esta vazio;
- UI tratando skipped_existing_phone como sucesso/info.

Depois rode:
python -m pytest tests/test_telegram_phone_unified.py tests/test_linkedin_profile_validation.py tests/test_telegram_pipeline_phone.py -q
python -m pytest tests -q
```

---

## 8. Definicao de pronto

So considerar resolvido quando:

- O botao inicia Telegram em um lead nao bloqueado.
- O log mostra explicitamente qual query foi enviada.
- `Pular para conteudo principal` nao aparece mais como cargo.
- Lead com e-mail achado no LinkedIn cai no Findex se o nome nao trouxer CPF.
- CPF persistido pula `/nome`.
- `ja tinha telefone` nao gera banner vermelho.
- Testes unitarios passam.
- Um teste manual com browser real gera evidencias de stage.
