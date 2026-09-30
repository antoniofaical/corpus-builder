# corpus-builder

Módulo Python para executar **uma query booleana no PubMed**, coletar metadados de
**todos os resultados** e obter textos abertos nas fontes configuradas: PMC, Europe
PMC e Unpaywall. Oferece retomada, retries, arquivos verificados e proveniência entre
queries. Não utiliza LLM e não altera a expressão bibliográfica fornecida.

**Deduplicação bibliográfica está fora desta versão.** Dois PMIDs com DOI ou título
iguais continuam separados. Apenas a identidade exata de um registro de origem
(`pubmed:PMID`) é compartilhada entre suas ocorrências nas queries. Não existe
matching aproximado, fusão de artigos nem interface de decisão de duplicatas.

## Instalação

Python **3.11 ou superior**. No PowerShell:

```powershell
git clone https://github.com/antoniofaical/corpus-builder.git
cd corpus-builder
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Linux/macOS: `python3 -m venv .venv` e `.venv/bin/python -m pip install -e '.[dev]'`.
Para uso sem ferramentas de desenvolvimento: `pip install -e .`.

## Configuração

`configs.toml` contém o **nome da variável de ambiente**, nunca a chave:

```toml
[ncbi]
api_key_env = "NCBI_API_KEY"
require_api_key = true
requests_per_second = 10
email = ""
tool = "corpus-builder"

[download]
formats = ["pdf", "xml"] # html também é suportado, com validação conservadora
requests_per_second = 3
max_bytes = 268435456

[sources]
europe_pmc = true
unpaywall = false
unpaywall_email = ""

[resolvers]
requests_per_second = 1
```

O arquivo versionado habilita Europe PMC. Para adicionar Unpaywall, configure
`unpaywall = true` e um email de contato válido em `unpaywall_email`. Essa API usa
consulta por DOI, não seu antigo endpoint de busca. A chave NCBI nunca é enviada
às fontes adicionais. `configs.local.toml` é ignorado pelo Git.

`BuildConfig()` preserva o comportamento anterior: fontes adicionais desabilitadas.
O módulo só lê o TOML quando o chamador usa `BuildConfig.from_toml(...)`.
Para testes públicos sem chave, configure explicitamente `require_api_key = false`
e `requests_per_second = 3` ou menos.

Os limites são por processo: E-Utilities, downloads e resolvers possuem controles
separados; retries também são contabilizados. Processos que compartilham uma chave
NCBI, mesmo em corpora diferentes, precisam dividir o limite remoto. Não há controle
distribuído de taxa. Um catálogo aceita apenas um escritor por vez. O Unpaywall
limita o uso a 100.000 consultas/dia; o padrão de 1 request/s fica abaixo disso
para um processo contínuo, mas outros processos também consomem a cota.

## Executar e preservar o contexto

```powershell
.\.venv\Scripts\python.exe -m corpus_builder --query '("biosensors"[MeSH Terms]) AND microneedle*' --query-id Q01 --query-version 1 --track T1 --technical-stratum hardware --output-dir .\runs\Q01-v1 --corpus-dir .\corpus --config .\configs.toml
```

Use `--query-file .\query.txt` para ler **uma única query** em UTF-8, evitando aspas
do shell. O comando instalado `corpus-builder` tem a mesma interface.

- `query_id` identifica a estratégia; `query_version` identifica sua versão.
- Ambos são strings e devem ser fornecidos juntos. Sem eles, a identidade é derivada
  da expressão exata e da base, enquanto a versão fica desconhecida (`null`).
- Trilha e estrato são campos opcionais; não há significados T1–T6 embutidos no código.
- Expressão ou contexto diferentes para o mesmo ID/versão causam erro antes das
  chamadas remotas. Para alterar a estratégia, atribua uma nova versão.
- `run_id` identifica uma execução. Retomar mantém esse ID; um novo diretório cria
  outra execução, mesmo para a mesma query/versão.
- `--corpus-dir` aponta para o catálogo compartilhado. Sem ele, o padrão é
  `output-dir/catalog`; na retomada, o catálogo registrado anteriormente é reutilizado.
- Repetir o comando retoma o trabalho. Query, formatos, fontes habilitadas e contexto
  precisam coincidir. Para atualizar a busca ou mudar fontes, crie outra execução.
  `--no-resume` rejeita um diretório já usado, sem apagá-lo.

Progresso vai para stderr; stdout contém um objeto JSON. `--quiet` reduz mensagens.
Códigos: `0` concluído; `1` parcial/falha operacional; `2` configuração/credencial/
filesystem inválido; `130` Ctrl+C, com progresso salvo.

## API para scripts externos

```python
from corpus_builder import BuildConfig, QueryContext, build_corpus

result = build_corpus(
    query='("biosensors"[MeSH Terms]) AND microneedle*',
    output_dir="runs/Q01-v1",
    corpus_dir="corpus",
    context=QueryContext(
        query_id="Q01",
        query_version="1",
        track="T1",
        technical_stratum="hardware",
    ),
    config=BuildConfig.from_toml("configs.toml"),
    on_event=lambda event: print(event["stage"]),
)
print(result.status, result.counts, result.catalog_dir)
```

A chamada anterior `build_corpus(query, output_dir, config)` continua válida. O módulo
não usa input interativo nem encerra o processo. Retorna `BuildResult`; erros de
configuração levantam `ConfigurationError`. Ctrl+C salva e propaga `KeyboardInterrupt`.

Callbacks recebem `event_id`, `run_id`, timestamp UTC e etapa, após persistência no
SQLite. Exceções normais do callback não interrompem a coleta. Não há webhooks,
notificações externas, orchestrator de listas, API HTTP ou integração n8n nesta versão.

## Pipeline e fontes

1. Registrar query, versão, contexto e execução.
2. Enumerar todos os PMIDs, conferindo contagens e completude.
3. Coletar metadados de todos os resultados, inclusive sem texto aberto.
4. Registrar os vínculos entre execução e registros de origem.
5. Consultar versões PMC e fontes adicionais habilitadas.
6. Baixar os formatos pedidos, verificar arquivos e salvar estados de acesso/download.
7. Exportar os relatórios da execução e do catálogo.

A **base de descoberta continua sendo PubMed**. Europe PMC e Unpaywall são fontes de
metadados/localizações; não entram artificialmente na contagem de bases pesquisadas.

PubMed fornece título, autores (incluindo estrutura e identificadores disponíveis),
periódico, datas, DOI original/normalizado, resumo estruturado, palavras-chave dos
autores, MeSH separado e tipos de publicação. `year` usa o ano explícito de publicação;
em `MedlineDate`, só extrai quando há um único ano inequívoco. Intervalos ambíguos
ficam sem ano. Não são inventados metadados ausentes.

Europe PMC e Unpaywall podem preencher campos ausentes. Valores existentes do PubMed
não são substituídos por esse complemento; `field_provenance` indica fonte e data por
campo. As respostas de enriquecimento e snapshots históricos são preservados.

O PMC distribui versões PDF/XML; o core preserva cada versão disponível separadamente.
Europe PMC oferece XML do subconjunto aberto e links classificados como OA. Unpaywall
informa localizações de editoras/repositórios, licença e versão quando disponíveis.
Não se infere `publishedVersion` de uma versão numérica PMC.

HTML é tentado apenas em localizações abertas identificadas pelos provedores.
A validação exige marcação explícita de corpo de artigo, seções e texto substancial;
páginas de resumo/login são rejeitadas. Esse suporte é conservador e não cobre todos
os layouts: um HTML legítimo pode falhar, com o motivo registrado. A validação não
substitui uma inspeção científica ou visual. Não há contorno de paywalls ou login.

## Acesso e resultado do download

Estas dimensões são independentes:

| Campo | Valores |
|---|---|
| `access_status` | `open_access`, `closed`, `unknown` |
| `retrieval_status` | `pending`, `downloaded`, `not_found`, `download_error` |

`closed` significa classificação negativa fornecida pelo Unpaywall naquela data;
não é uma prova global de inexistência de cópia aberta. Ausência no PMC ou falha em
resolver acesso resulta em `unknown`. `not_found` se limita às fontes/formatos
consultados. Um artigo pode ser `open_access` e ter `download_error`.

Se um formato foi obtido e outro falhou, `retrieval_status` é `downloaded`, com
`retrieval_has_errors = true`, erros por arquivo e execução `partial`. O campo legado
`status` continua disponível (`no_pmc`, `not_in_pmc_distribution`, `not_in_oa_subset`,
`format_unavailable`, `downloaded_with_unavailable_formats`, `downloaded`, `error`,
`pending`). Nenhum desses estados é uma decisão científica de exclusão.

## Artefatos

Por execução:

| Arquivo | Conteúdo |
|---|---|
| `run.json`, `state.sqlite3` | Identidade, configuração sem chave, contexto e checkpoints. |
| `manifest.jsonl` | Registros, metadados, evidências de acesso, localizações, versões, arquivos e erros. |
| `manifest.csv` | Metadados completos para triagem, inclusive resumo, autores e palavras-chave. |
| `report.json`, `report.md` | Contagens, completude, fontes habilitadas e query traduzida. |
| `events.jsonl` | Eventos, incluindo expressões enviadas ao ESearch. |
| `articles/` | Documentos com nomes estáveis. |

No catálogo compartilhado:

| Arquivo | Conteúdo |
|---|---|
| `catalog.sqlite3` | Queries, execuções, registros de origem, relações e snapshots. |
| `articles.csv`, `articles.jsonl` | Uma linha por registro de origem; **sem deduplicação bibliográfica**. |
| `query_hits.csv` | Relações registro–query–versão–execução, sem sobrescrever vínculos anteriores. |
| `queries.csv`, `runs.csv` | Contextos, expressões exatas, datas e contagens. |
| `documents.jsonl` | Arquivos e tentativas observados por execução, com origem, hash e versão. |
| `observations.jsonl` | Snapshots dos registros, incluindo mudanças e falhas. |
| `events.jsonl`, `run_history.jsonl` | Histórico de eventos e estados das execuções. |
| `corpus_report.json`, `.md` | Contagens e sobreposição por identidade exata do registro. |
| `objects/` | Cópias verificadas, endereçadas pelo SHA-256, para reaproveitamento entre runs. |

A visão `articles` usa a observação mais recente importada/processada por registro;
os snapshots anteriores permanecem auditáveis. Relatórios de outras execuções não
são reescritos. Campos estruturados no CSV são JSON; células potencialmente
interpretadas como fórmulas recebem apóstrofo.

## Importar execuções antigas

```powershell
.\.venv\Scripts\python.exe -m corpus_builder --import-run .\runs\antigo --corpus-dir .\corpus
```

```python
from corpus_builder import import_run

report = import_run("runs/antigo", "corpus")
```

Importa schemas 1 e 2 sem rede ou alteração do banco/relatórios de origem. Preserva
`run_id`, metadados, relações, arquivos verificáveis e eventos; informa arquivos
locais ausentes/alterados. Pode receber `context=QueryContext(...)` para uma execução
antiga ainda sem contexto. Campos desconhecidos não são preenchidos por suposição.
Repetir a importação não duplica os vínculos.

Ao **retomar** diretamente um run v1, o banco é atualizado para v2 após validação de
compatibilidade, com backup SQLite consistente em `state.v1.backup.sqlite3`. O formato
novo de metadados é coletado na retomada. Não há migração destrutiva do corpus original.

## Completude e limites operacionais

- Consultas acima de 9.999 resultados são subdivididas por faixas de PMID dentro do
  conjunto congelado no History Server. Verifica-se a cobertura do intervalo
  `1:2147483647`, os IDs por partição e a contagem final. Se não reconciliar, falha.
- Se o histórico remoto expirar antes de concluir, a ferramenta registra a reinicialização.
  Depois de concluir, a descoberta permanece fixa; uma atualização requer outro run.
- Retomada reutiliza metadados/localizações resolvidos. Reuso entre runs verifica o
  arquivo local registrado por URL/hash; não afirma que o servidor remoto manteve o
  mesmo conteúdo. `downloaded_at` e `verified_at` distinguem download e verificação local.
- PDF: assinatura e marcador final; XML: parsing seguro e raiz de artigo; HTML: critérios
  conservadores descritos acima. MD5 remoto é conferido quando disponível, sempre com
  SHA-256 local. Documentos têm limite configurável de tamanho.
- Downloads em streaming usam `.part` e renomeação atômica. Interrompidos recomeçam;
  concluídos válidos são reutilizados. Cópias locais corrompidas podem ser restauradas
  do cache verificado; cache inválido provoca novo download.
- Retries limitados cobrem transporte, conteúdo inválido, 429, 408 e 500/502/503/504,
  respeitando `Retry-After`. Falhas são registradas; fontes inacessíveis não viram `closed`.
- `completed` significa processamento concluído nas fontes configuradas, não cobertura
  exaustiva da internet nem existência de texto completo para todo resultado.
- Escrita é serializada por run e catálogo. Não remova arquivos SQLite `-wal`/`-shm`
  enquanto houver execução ativa. Encerre antes de mover/copiar diretórios.
- Relatórios são gerados ao concluir, falhar ou receber Ctrl+C. Após encerramento
  forçado, retome usando os checkpoints SQLite para regenerar relatórios.

## Testes e fontes

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

A suíte padrão usa fixtures e não acessa a rede. CI: Windows/Linux, Python 3.11/3.13.
Evidências e limites: [docs/VALIDATION.md](docs/VALIDATION.md).

- [NCBI E-Utilities](https://www.ncbi.nlm.nih.gov/books/NBK25499/)
- [NCBI API keys](https://eutilities.github.io/site/API_Key/usageandkey/)
- [PMC Article Datasets](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/)
- [Europe PMC REST](https://europepmc.org/RestfulWebService)
- [Unpaywall DOI API](https://data.unpaywall.org/products/api)
- [Unpaywall schema](https://unpaywall.org/data-format)

Licenças das fontes/documentos permanecem aplicáveis. O projeto não é afiliado às fontes.
