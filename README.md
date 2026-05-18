# Beautiful LinkedIn

Uma interface de linha de comando (CLI) bonita e poderosa para descoberta de leads B2B públicos a partir de resultados de buscas na web e páginas oficiais de empresas. 

O Beautiful LinkedIn foi projetado para auxiliar no processo de prospecção extraindo informações públicas sem a necessidade de acessar diretamente contas do LinkedIn, garantindo maior segurança e conformidade com as restrições da plataforma.

## 🚀 Como a Aplicação Funciona

A aplicação funciona como um agregador e orquestrador de buscas. Ao receber o nome de uma empresa e os cargos almejados, ela paraleliza requisições para diversos **provedores de dados estruturados** e **motores de busca** para encontrar os leads correspondentes.

### Fluxo de Funcionamento

1. **Entrada de Dados:** A aplicação aceita a entrada dos dados via painel interativo (CLI) ou via argumentos/comandos informando o nome da empresa, domínio, URLs e os títulos de cargos (ex: CEO, Marketing, Sales).
2. **Definição de Estratégia:** Baseado nas configurações (variáveis de ambiente) e flags, o sistema determina quais provedores usar. Existem dois tipos principais de provedores:
   - **Provedores de API Estruturados:** Como People Data Labs (PDL) e Coresignal.
   - **Provedores de Busca Pública (Web):** Como Brave HTML, DuckDuckGo HTML, DuckDuckGo, Brave Search API, Google Custom Search e Serper.
   - **Fontes públicas gratuitas opcionais:** SearxNG local, diretórios públicos como TheOrg/RocketReach e Common Crawl.
3. **Execução Paralela:** As requisições são disparadas em paralelo (`--parallelism`) para os provedores de busca, otimizando o tempo. Caso a opção de `official-sites` esteja ativa, a aplicação fará scraping nas páginas web da própria empresa.
4. **Agregação e Deduplicação:** Os leads encontrados em múltiplas fontes passam por uma fase de processamento, onde os perfis são normalizados e os duplicados são removidos com base em regras heurísticas de similaridade.
5. **Exportação:** Por fim, um sumário é exibido num terminal lindamente formatado (graças à biblioteca `rich`) e os resultados são salvos em formato `.csv` ou `.xlsx`.

### Como as Requisições Funcionam

- As requisições HTTP são realizadas para APIs públicas de motores de busca e para os sites das empresas (via scraping).
- O tráfego para a web aberta usa queries montadas (dorks) para filtrar perfis do LinkedIn baseados nos cargos requisitados. No modo `serp`, a ferramenta usa buscadores públicos em HTML, sem chaves de API.
- Existe uma limitação de segurança e requisições configurável (ex: `BEAUTIFUL_LINKEDIN_WEB_QUERY_LIMIT`, `BEAUTIFUL_LINKEDIN_PROVIDER_TIMEOUT_SECONDS`) para evitar bloqueios temporários.
- Possui um mecanismo de **Cache** interno via SQLite (opcional) que salva respostas de APIs para não gastar créditos ou limites da taxa de requisição atoa em pesquisas repetidas dentro do tempo de vida (TTL) configurado.

---

## ⚙️ Instalação e Configuração

### Pré-requisitos
- **Python:** Versão 3.11 ou superior.

### Configurando o Ambiente

O projeto utiliza um arquivo `.env` para carregar chaves de acesso para os provedores de busca (caso opte por utilizá-los) e configurações do CLI.

Copie o arquivo de exemplo e edite as variáveis:

```bash
cp .env.example .env
```

**Principais variáveis disponíveis no `.env`:**
- `BEAUTIFUL_LINKEDIN_MAX_RESULTS`: Limite de resultados por busca (padrão 20).
- Motores de busca opcionais (Deixe em branco para usar os motores públicos HTML ou apenas os que estiverem configurados):
  - `BRAVE_SEARCH_API_KEY`
  - `GOOGLE_CUSTOM_SEARCH_API_KEY`
  - `GOOGLE_CUSTOM_SEARCH_CX`
  - `SERPER_API_KEY`
  - `SEARXNG_BASE_URL`
- APIs de Leads Estruturados:
  - `PEOPLE_DATA_LABS_API_KEY`
  - `CORESIGNAL_API_KEY`
  - `APOLLO_API_KEY`
  - `LUSHA_API_KEY`
  - `APIFY_API_KEY` para o provider opcional `apify_linkedin`
- Enriquecimento pago: a UI exibe R$/crédito como leitura. As APIs públicas
  não retornam preço por crédito; se o seu plano tiver custo diferente dos
  defaults do servidor, ajuste via `ENRICHMENT_COST_BRL_APOLLO`,
  `ENRICHMENT_COST_BRL_LUSHA`, `ENRICHMENT_COST_BRL_SNOVIO` e/ou
  `ENRICHMENT_COST_BRL_PDL`.

> **Atenção sobre Apify/LinkedIn:** o provider `apify_linkedin` chama um Actor terceiro da Apify para LinkedIn Company Employees. Ele não entra no modo `auto` e deve ser selecionado explicitamente. Use apenas com base legal, permissão operacional e validação dos termos aplicáveis. O Beautiful LinkedIn não implementa login, automação de navegador local, proxy, captcha solver, mascaramento de IP ou técnicas de evasão.

> **Nota:** Se a chave de uma API estruturada não for configurada, o respectivo provedor será ignorado de forma transparente (fallback amigável) e a busca utilizará apenas os que estiverem ativos.

### Instalação

Como o projeto utiliza `hatchling` (ou `pip`), basta rodar no diretório principal:

```bash
pip install -e .
```

---

## Modo grátis: SearxNG local

O SearxNG permite rodar um meta-buscador local gratuito. Quando `SEARXNG_BASE_URL` está configurado, o modo `auto` usa essa instância antes de consumir créditos de APIs como Serper.

1. Suba a instância local:

```bash
docker compose -f docker-compose.searxng.yml up -d
```

2. Configure o `.env`:

```bash
SEARXNG_BASE_URL=http://localhost:8080
```

3. Rode normalmente:

```bash
beautiful-linkedin
```

Antes de subir em ambiente real, troque `server.secret_key` em `searxng-config/settings.yml`.

---

## 🛠️ Como Utilizar e Exemplos

A aplicação pode ser utilizada de duas formas: através de um menu interativo super amigável ou diretamente passando os comandos (ótimo para automações).

### Modo Interativo

Simplesmente execute no terminal e siga as instruções na tela:

```bash
beautiful-linkedin
```
O menu interativo iniciará um fluxo simplificado:
1. Qual empresa você quer prospectar? (ex: "Nubank")
2. Qual o domínio da empresa? (opcional, ex: "nubank.com.br")
3. Qual fonte de dados usar:
   - **APIs externas + busca pública configurada**
   - **SERP-only sem APIs, sem cookies e sem login**
   - **LinkedIn com cookie atual (`li_at`)**
4. Qual área você deseja buscar? (Opções prontas como *Marketing*, *Vendas*, *Tecnologia* - que já contêm seus próprios termos de busca - ou uma área customizada)
5. Confirmação do preview e início da busca.

### Modo Linha de Comando (CLI)

#### 1. Pesquisa Rápida para Uma Empresa (`search`)

Procure por leads de uma empresa específica passando os dados via argumentos.

```bash
beautiful-linkedin search --company-name "Nubank" --company-domain "nubank.com.br" --titles "marketing,growth" --output "leads_nubank.csv"
```

Exemplo sem APIs e sem cookies, usando apenas resultados públicos de buscadores:

```bash
beautiful-linkedin search --company-name "Nubank" --company-domain "nubank.com.br" --titles "marketing,growth" --scrape-mode serp --search-depth deep --output "leads_nubank_serp.csv"
```

No modo `serp`, a CLI força `brave_html,duckduckgo_html`, balanceia as consultas entre os cargos e evita APIs externas de busca. Esse modo ainda depende dos buscadores aceitarem tráfego automatizado; se houver captcha/rate limit, a CLI avisará no log.

Exemplo usando o cookie atual do LinkedIn (`li_at`). Se `--linkedin-cookie` for omitido, a aplicação tenta ler `LINKEDIN_LI_AT_COOKIE` e depois detectar no navegador configurado por `--linkedin-cookie-browser`:

```bash
beautiful-linkedin search --company-name "Nubank" --linkedin-url "https://www.linkedin.com/company/nubank/" --titles "marketing,growth" --scrape-mode cookie --linkedin-cookie-browser auto --output "leads_nubank_cookie.csv"
```

Atalho dedicado para o modo com cookie:

```bash
beautiful-linkedin linkedin-cookie --company-name "Nubank" --linkedin-url "https://www.linkedin.com/company/nubank/" --titles "marketing,growth" --output "leads_nubank_cookie.csv"
```

Exemplo usando explicitamente o Actor da Apify:

```bash
beautiful-linkedin search --company-name "BTG Pactual" --company-domain "btgpactual.com" --linkedin-url "https://www.linkedin.com/company/btg-pactual/" --titles "marketing" --lead-providers "pdl,apollo,apify_linkedin" --max-results 30 --official-sites false --output output/leads_btg_apify.csv
```

#### Seção separada: Scraper via LinkedIn

Existe uma seção separada no CLI para discutir scraping direto via LinkedIn:

```bash
beautiful-linkedin linkedin-scraper
```

Essa seção **não usa Apify nem APIs terceiras**. Ela também **não executa scraping direto no MVP**.

O objetivo dessa separação é deixar claro para o cliente que scraping direto de LinkedIn é uma trilha diferente das integrações por API. Por risco jurídico/comercial e por termos de uso, o app não implementa login, automação de navegador, rotação ou mascaramento de IP, captcha solver, modificação de requisições ou técnicas de evasão.

Essa frente só deve evoluir se houver autorização explícita, base legal validada e revisão dos termos aplicáveis.

**Outros parâmetros úteis no modo `search`:**
- `--scrape-mode api|serp|cookie`: Escolhe entre providers externos, busca pública sem cookies ou LinkedIn com cookie.
- `--linkedin-cookie "li_at=..."`: Informa manualmente o cookie atual quando usar `--scrape-mode cookie`.
- `--linkedin-cookie-browser auto|chrome|edge|brave|firefox|none`: Controla a tentativa de detectar o cookie atual no navegador.
- `--max-results 50`: Aumentar limite de resultados.
- `--official-sites false`: Desabilita a raspagem (scraping) do site oficial da empresa.
- `--search-engines "brave_html,duckduckgo_html"`: Força uso de engines específicos (se configurados ou públicos).
- `--lead-providers "public_directories,common_crawl,web"`: Usa diretórios públicos e Common Crawl como fontes gratuitas opcionais.
- `--output-format xlsx`: Exporta diretamente como uma planilha Excel.

#### 2. Pesquisa em Lote Usando um Arquivo CSV (`from-csv`)

Caso possua uma lista com várias empresas, crie um arquivo CSV (ex: `empresas.csv`) com as seguintes colunas:
`company_name`, `company_domain`, `linkedin_url`, `titles`

Execute o processamento em lote para toda a planilha:

```bash
beautiful-linkedin from-csv --input empresas.csv --output leads_prospeccao.xlsx --max-results 10
```

#### 3. Gerar um CSV de Exemplo

Para entender exatamente como formatar o seu arquivo de entrada CSV de empresas:

```bash
beautiful-linkedin example-csv
```

#### 4. Sobre a Aplicação (Aviso de Segurança)

Para ler as regras de uso e os avisos de segurança sobre dados públicos:

```bash
beautiful-linkedin about
```

---

## ⚠️ Nota de Responsabilidade (Disclaimer)

O Beautiful LinkedIn pesquisa dados estritamente **públicos**. Ele **não** automatiza ações logadas numa conta do LinkedIn, **não** dribla restrições privadas, nem utiliza técnicas de evasão para captchas.
Como utiliza indexação de buscadores, certifique-se de usar a ferramenta conforme as normativas de proteção de dados (como a LGPD) e termos de uso das ferramentas da web abertas.
