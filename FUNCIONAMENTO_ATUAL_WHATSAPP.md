# Beautiful LinkedIn - funcionamento atual

O Beautiful LinkedIn é uma ferramenta CLI para prospecção B2B pública.

Hoje o sistema recebe uma empresa, domínio e cargos-alvo, por exemplo:

```text
Empresa: BTG Pactual
Domínio: btgpactual.com
Cargos: marketing
Meta: 30 leads
Fontes: PDL, Coresignal, Apollo, Lusha e busca pública
```

O sistema consulta APIs e fontes públicas em paralelo, deduplica os resultados, calcula uma confiança por lead e exporta tudo em CSV ou XLSX.

O resultado gerado contém:

- nome da pessoa
- cargo
- empresa
- URL pública do LinkedIn quando disponível
- fonte do dado
- score de confiança

O sistema também mostra no terminal:

- resumo da execução
- progresso da busca
- total de leads encontrados
- total deduplicado
- contribuição por fonte/API
- prévia dos primeiros leads

Ponto importante: a ferramenta não usa login do LinkedIn, não automatiza navegador e não faz scraping direto de perfis `linkedin.com/in`. Ela trabalha com APIs configuradas, resultados públicos de busca e dados disponíveis publicamente.

Limitação atual: algumas fontes retornam mais resultados que outras. Por isso a próxima etapa é melhorar o enriquecimento e ampliar a gama de fontes para aumentar volume, qualidade e cobertura.

## Próximos passos possíveis

### Ramificação 1A - Enriquecer com Apollo

Usar os leads encontrados por PDL, Coresignal e outras fontes como entrada para enriquecer com Apollo.

Objetivo:

- completar dados faltantes
- melhorar cargo/empresa atual
- encontrar e-mail corporativo quando disponível pela API
- aumentar a qualidade do CSV final

### Ramificação 1B - Ampliar busca pública

Configurar Google Search API, Brave Search API e Bing Search API para aumentar a gama de resultados públicos.

Objetivo:

- depender menos de uma única fonte
- encontrar mais URLs públicas
- melhorar cobertura quando uma API não retorna dados suficientes

### Ramificação 2A - Sales Navigator API

Avaliar uso de API ligada ao LinkedIn Sales Navigator, caso exista acesso comercial viável.

Ponto de atenção:

- pode ter custo alto
- pode exigir contrato, permissões e limitações comerciais
- precisa validar regras de uso antes de integrar

### Ramificação 3A - Scraping direto do LinkedIn

Essa é a opção mais arriscada e deve ser tratada como última alternativa.

Scraping direto de LinkedIn pode violar termos de uso, gerar bloqueios e criar risco jurídico/comercial. Também costuma exigir técnicas como rotação de IP, simulação de navegador e alteração de requisições, o que aumenta bastante o risco e a complexidade.

Recomendação atual: priorizar APIs, enriquecimento e busca pública antes de considerar qualquer abordagem desse tipo.
