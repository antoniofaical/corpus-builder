# Orchestrator sequencial

O orchestrator chama o mesmo `build_corpus` **uma vez por entrada**, em ordem. Na
primeira execução de uma lista com N entradas, são N chamadas ao core, salvo
interrupção ou falha global que impeça prosseguir. Nenhuma deduplicação bibliográfica
é aplicada. Entradas repetidas também são executadas, em diretórios independentes.

## Input recomendado: JSON versionado

```json
{
  "schema_version": 1,
  "queries": [
    {
      "query_id": "Q01",
      "query_version": "1",
      "track": "T1",
      "technical_stratum": "hardware",
      "query": "(\"biosensors\"[MeSH Terms]) AND microneedle*"
    },
    {
      "query_id": "Q02",
      "query_version": "1",
      "query": "(\"biosensors\"[MeSH Terms]) AND wearable*"
    }
  ]
}
```

O JSON mantém cada expressão junto de sua proveniência, suporta aspas, vírgulas,
Unicode e quebras de linha (`\n`) e pode ser produzido por outro serviço sem dependências
adicionais. `schema_version` versiona o formato do arquivo; `query_version` versiona
uma estratégia específica. IDs e versões de query são **strings**.

Regras:

- `query` é obrigatória em cada objeto. Não é reescrita nem normalizada pelo orchestrator.
- `query_id` e `query_version` são opcionais, mas devem ser fornecidos juntos.
- `track` e `technical_stratum` são opcionais. Nenhuma taxonomia está embutida no código.
- `database` é opcional e aceita apenas `pubmed`, a base de descoberta atual.
- A lista inteira é validada antes da primeira chamada: campos desconhecidos, chaves
  JSON repetidas e conflitos de ID/versão/contexto são rejeitados. A sintaxe bibliográfica
  é interpretada pelo PubMed quando o core faz a busca.
- Uma lista vazia conclui com zero chamadas. Uma entrada vazia é inválida.

Para uso mínimo, o mesmo formato aceita strings no array:

```json
{
  "schema_version": 1,
  "queries": [
    "biosensors AND microneedles",
    "biosensors AND wearables"
  ]
}
```

Nesse caso, o core deriva a identidade da expressão e mantém a versão desconhecida.
Objetos são preferíveis quando o corpus precisa acompanhar um plano de busca.
Exemplo editável: [queries.example.json](../examples/queries.example.json). As queries
são ilustrativas, não constituem uma estratégia de busca validada.

## Executar no PowerShell

Após atualizar o repositório e instalar `pip install -e .`, crie `queries.json` a partir
do exemplo e ajuste `configs.toml`:

```powershell
.\.venv\Scripts\python.exe .\orchestrator.py --queries .\queries.json --output-dir .\runs\lote-01 --corpus-dir .\corpus --config .\configs.toml
```

Também estão disponíveis:

```powershell
.\.venv\Scripts\python.exe -m corpus_builder.orchestrator --queries .\queries.json --output-dir .\runs\lote-01 --corpus-dir .\corpus
```

O comando instalado `corpus-builder-batch` recebe os mesmos argumentos. Caminhos
relativos são resolvidos a partir do diretório de execução do comando. `--corpus-dir`
é opcional: o padrão é `output-dir/catalog`; na retomada, utiliza o caminho registrado.
A configuração é comum a todas as queries, incluindo a variável da API key e as fontes.

## API para outros scripts

```python
from corpus_builder import BuildConfig, load_queries, run_batch

result = run_batch(
    queries=load_queries("queries.json"),
    output_dir="runs/lote-01",
    corpus_dir="corpus",
    config=BuildConfig.from_toml("configs.toml"),
)
print(result.status, result.counts)
```

Sem arquivo:

```python
from corpus_builder import QueryContext, QuerySpec, run_batch

result = run_batch(
    [
        "biosensors AND microneedles",
        QuerySpec("biosensors AND wearables", QueryContext("Q02", "1", "T2")),
    ],
    output_dir="runs/lote-02",
    corpus_dir="corpus",
)
```

A API retorna `BatchResult` e não encerra o processo. `on_event` é opcional. Recebe
marcadores de lote/query; eventos do core aparecem como `stage="core_event"`, com o
evento original em `event` e o identificador da entrada em `entry_id`. Exceções comuns
do callback são registradas sem parar o lote. Ainda não há notificações externas.

## Execução, falhas e retomada

1. Validar toda a lista e a compatibilidade com um lote existente.
2. Para cada entrada pendente, registrar início e chamar o core com sua query/contexto.
3. Gravar resultado e histórico da tentativa antes de seguir para a próxima entrada.
4. Gerar o relatório geral e os relatórios individuais do core.

O diretório de cada entrada depende de sua posição, não de textos livres:
`runs/000001`, `runs/000002`, etc. O catálogo compartilhado preserva os vínculos de
todas as queries e permite reutilizar arquivos verificados entre execuções.

| Situação | Comportamento |
|---|---|
| Query concluída | Segue para a próxima entrada. |
| Core retorna parcial/falha operacional de query | Registra e continua a lista. |
| Chave ausente | Falha de configuração antes da primeira chamada pendente. |
| Autenticação rejeitada pela API | Interrompe o lote; entradas seguintes ficam pendentes. |
| Falha de filesystem ou exceção inesperada | Persiste o possível e propaga o erro; não continua silenciosamente. |
| Ctrl+C | Salva estado e relatório, mantém trabalho do core e propaga a interrupção. |
| Encerramento forçado | Retoma pelos checkpoints; tentativa que ficou `running` é registrada como interrompida. |

**Repita o mesmo comando para retomar.** Entradas concluídas são puladas. As parciais,
falhas e interrompidas são chamadas novamente, uma vez por invocação, usando o estado
do core. Retries HTTP continuam sob responsabilidade do core; não há repetição infinita
no orchestrator.

`--recheck-completed` chama o core também para as concluídas, verificando novamente os
arquivos locais. Sem essa opção, pular uma entrada concluída não revalida seus arquivos.
Rechecagem não é atualização da busca: a lista de PMIDs do core permanece congelada.

`--no-resume` rejeita um lote existente. Alterar a lista, ordem, contexto, formatos,
fontes ou catálogo exige outro diretório de lote. O mesmo catálogo pode ser reutilizado
pelo novo lote. Não edite checkpoints manualmente.

A execução é sequencial. Além dos limitadores do core, existe um intervalo entre
chamadas suficiente para preservar as taxas configuradas ao recriar clientes HTTP.
Isso não coordena processos externos usando a mesma chave. Um lock impede dois
orchestrators de escreverem simultaneamente no mesmo diretório de lote.

## Saídas

| Artefato | Conteúdo |
|---|---|
| `batch_state.json` | Snapshot da lista/configuração sem chave, estado e tentativas por entrada. |
| `batch_report.json` | Resultado do lote, contagens, resultados e caminhos dos relatórios do core. |
| `batch_report.md` | Resumo legível do lote. |
| `batch_events.jsonl` | Histórico de eventos com batch_id, entrada e timestamp UTC. |
| `runs/000001/`, etc. | Estados, relatórios e documentos produzidos pelo core. |
| Catálogo escolhido | Dados compartilhados de todas as queries, sem consolidação bibliográfica. |

Não some os resultados por query para estimar artigos distintos: uma mesma identidade
de origem pode aparecer em várias queries. O catálogo registra essa sobreposição.

O stdout da CLI contém um JSON; progresso e erros vão para stderr. `--quiet` reduz o
progresso. Códigos de saída: `0` concluído; `1` lote parcial/falho; `2` configuração ou
filesystem; `130` interrupção. Exceções inesperadas deixam traceback para diagnóstico.
