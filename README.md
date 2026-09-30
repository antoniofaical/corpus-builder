# corpus-builder

Módulo Python para executar **uma query booleana no PubMed**, identificar os artigos
disponíveis no **PMC Open Access Subset** e baixar seus PDFs e XMLs/JATS disponíveis.
Inclui retomada, retries limitados, controle de requisições, deduplicação, verificação
de integridade e relatórios. Não utiliza LLM.

A busca é feita no PubMed, preservando sua sintaxe e campos. O texto completo é
recuperado pelo serviço oficial atual de distribuição do PMC. Não há busca por
citações, similaridade, editoras externas ou reescrita automática da query.
O resultado representa a disponibilidade observada, não uma triagem de relevância.

## Instalação

Python **3.11 ou superior**. No PowerShell, na pasta onde deseja clonar:

```powershell
git clone https://github.com/antoniofaical/corpus-builder.git
cd corpus-builder
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

No Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Para uso sem ferramentas de desenvolvimento, instale com `pip install -e .`.
Não é necessário ativar o ambiente virtual ao chamar seu executável diretamente.

## Chave e configuração

Edite `configs.toml`. O campo `api_key_env` recebe **o nome** da variável de ambiente
que já contém sua chave, nunca o valor da chave:

```toml
[ncbi]
api_key_env = "NCBI_API_KEY"
require_api_key = true
requests_per_second = 10
email = ""
tool = "corpus-builder"
```

A variável deve estar disponível no processo que executa o Python. Se foi criada
após abrir o terminal, abra um novo terminal para que ele receba o ambiente atualizado.
Uma variável ausente ou vazia causa erro antes de iniciar a coleta. Chaves rejeitadas
pelo NCBI interrompem a execução; a ferramenta não tenta adivinhar outra credencial.

Há configurações para formatos (`pdf`, `xml`), limite do serviço de download,
timeout, tentativas totais, backoff e retomada. Configurações desconhecidas são
rejeitadas para detectar erros de digitação. Use `configs.local.toml`, ignorado pelo
Git, se preferir manter ajustes pessoais fora do arquivo versionado.

Os 10 requests/s são compartilhados entre chamadas E-Utilities e retries deste
processo. Outros processos ou ferramentas usando a mesma chave também consomem
o limite remoto: reduza o valor se necessário. O core executa sequencialmente e
não implementa coordenação distribuída de rate limits.

É possível trabalhar sem chave **explicitamente** com `require_api_key = false`
e `requests_per_second = 3` ou menos. Essa configuração foi usada no teste público
de integração; uma chave do usuário não é necessária para os testes offline.

## Executar uma query

```powershell
.\.venv\Scripts\python.exe -m corpus_builder --query '("biosensors"[MeSH Terms]) AND microneedle*' --output-dir .\runs\biosensors --config .\configs.toml
```

Para evitar regras de aspas do shell, salve **uma única query**, inclusive em várias
linhas, em um arquivo UTF-8 e use:

```powershell
.\.venv\Scripts\python.exe -m corpus_builder --query-file .\query.txt --output-dir .\runs\biosensors --config .\configs.toml
```

Repita o mesmo comando para retomar. Query e formatos precisam coincidir com a
execução existente. Para uma nova consulta ou atualização da busca, use um novo
diretório. `--no-resume` impede reutilizar uma execução existente; não apaga arquivos.
O comando instalado `corpus-builder` oferece a mesma interface.

Progresso vai para stderr; stdout contém um único objeto JSON com o resultado.
`--quiet` reduz as mensagens. Códigos de saída:

| Código | Significado |
|---|---|
| 0 | Execução concluída, incluindo busca sem resultados ou textos indisponíveis. |
| 1 | Falha operacional ou resultado parcial; conferir relatório. |
| 2 | Configuração, credencial ou acesso ao sistema de arquivos inválido. |
| 130 | Interrupção por Ctrl+C, com trabalho concluído preservado. |

## Chamar por outro script

```python
from corpus_builder import BuildConfig, build_corpus

config = BuildConfig.from_toml("configs.toml")
result = build_corpus(
    query='("biosensors"[MeSH Terms]) AND microneedle*',
    output_dir="runs/biosensors",
    config=config,
    on_event=lambda event: print(event["stage"]),  # opcional
)

print(result.status)
print(result.counts)
print(result.report_path)
```

`BuildConfig()` também pode ser instanciada diretamente. O módulo não lê um TOML
implicitamente: o chamador escolhe o arquivo/configuração. `build_corpus` não usa
input interativo nem encerra o processo. Retorna `BuildResult`; erros de configuração
levantam `ConfigurationError`. Falhas operacionais conhecidas geram resultado
`partial`/`failed`. Ctrl+C persiste relatórios e propaga `KeyboardInterrupt`.

O callback recebe eventos com `event_id`, `run_id`, data UTC e etapa. Uma exceção
normal do callback é registrada e não interrompe a coleta. Os eventos são persistidos
no SQLite antes do callback, mas não existe entrega externa, webhook ou fila de notificações.

## Saídas e rastreabilidade

| Artefato | Conteúdo |
|---|---|
| `run.json` | Identidade, configuração sem chave e versão do pacote. |
| `state.sqlite3` | Estado transacional, checkpoints e eventos para retomada. |
| `manifest.jsonl` | Uma linha por PMID, metadados, PMCIDs, versões, fontes, arquivos e erros. |
| `manifest.csv` | Visão resumida para Excel; textos potencialmente interpretados como fórmulas recebem apóstrofo. |
| `report.json` | Contagens, completude, query traduzida pelo PubMed e avisos. |
| `report.md` | Resumo legível da execução. |
| `events.jsonl` | Histórico de eventos exportado ao encerrar a tentativa. |
| `articles/PMCID.versão/` | PDF/XML com nome estável, sem depender do título do artigo. |

Não edite `state.sqlite3` nem remova apenas parte de uma execução. Arquivos auxiliares
SQLite `-wal`/`-shm` podem existir durante a execução: não os exclua enquanto o processo
estiver ativo. Para mover/copiar uma execução, encerre o processo primeiro e preserve
todo o diretório. PDFs e corpus não são versionados no Git.

`completed` significa que a descoberta foi reconciliada e todos os arquivos pedidos
**disponíveis no serviço** foram verificados. Não significa que todo resultado do
PubMed possui texto completo. O manifesto distingue:

- `no_pmc`: ELink não retornou associação PMC.
- `not_in_pmc_distribution`: existe PMCID, mas nenhuma versão foi encontrada no serviço.
- `not_in_oa_subset`: versões distribuídas existem, mas não pertencem ao subconjunto OA.
- `format_unavailable`: nenhuma versão OA oferece os formatos pedidos.
- `downloaded_with_unavailable_formats`: há arquivos obtidos e formatos ausentes.
- `downloaded`: arquivos disponíveis obtidos/verificados.
- `error`: há falha técnica pendente; não equivale a indisponibilidade.
- `pending`: processamento ainda não concluído.

Todas as versões OA distribuídas são preservadas separadamente. O número da versão
não é usado para inferir qual é a versão publicada ou preferida. Informações de licença,
manuscrito e retratação disponíveis no PMC são mantidas; não há exclusão científica
automática por esses atributos. As licenças continuam aplicáveis ao uso posterior.

## Completude, retomada e limites

- Queries acima de 9.999 registros são particionadas por intervalos numéricos de PMID
  dentro do conjunto congelado no History Server do NCBI. A contagem e os IDs de cada
  partição são conferidos; a união única deve corresponder à contagem original.
- O limite superior numérico usado é 2.147.483.647. A cobertura desse intervalo é
  conferida contra o conjunto original: um resultado fora dele causa falha explícita.
- Partições concluídas são persistidas. Se o History Server expirar antes de concluir
  a descoberta, a ferramenta tenta reiniciá-la com um novo conjunto e registra o fato.
  Erros remotos que impeçam essa validação deixam a execução incompleta.
- Após concluir a descoberta, a lista local de PMIDs fica fixa. Retomar não adiciona
  publicações novas nem atualiza metadados ou disponibilidades já resolvidas.
- Cada arquivo concluído é validado por MD5 da fonte quando disponível e SHA-256 local,
  além de assinatura/fim de PDF ou parsing seguro de XML de artigo. A validação não é
  uma revisão do conteúdo científico nem uma inspeção visual de todas as páginas.
- Downloads usam streaming, arquivo `.part` e renomeação atômica. Um arquivo interrompido
  recomeça do início; arquivos concluídos válidos são reutilizados.
- Um arquivo local alterado é baixado novamente. A retomada verifica a integridade contra
  a versão registrada, sem prometer que ela continua sendo a versão remota mais recente.
- Retries cobrem falhas de transporte, respostas malformadas, checksums incorretos, 429,
  408 e 500/502/503/504; respeitam `Retry-After`. Erros permanentes são registrados.
- Falha em um artigo não elimina os demais. Credenciais inválidas, falta de espaço e
  problemas estruturais impedem uma conclusão bem-sucedida.
- O bloqueio de diretório evita escritores concorrentes no mesmo run. Deduplicação é
  interna à execução; compartilhamento de corpus entre diferentes runs fica fora desta versão.
- Relatórios são atualizados ao término normal, falha tratada ou Ctrl+C. Após encerramento
  forçado, o SQLite é a fonte dos checkpoints; retome para regenerar os relatórios.

Esta versão não inclui orchestrator de listas, n8n, API HTTP, buscas externas ou notificações.

## Testes

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

A suíte padrão não acessa rede. A CI verifica Windows/Linux e Python 3.11/3.13.
Veja [docs/VALIDATION.md](docs/VALIDATION.md) para evidências e limites dos testes reais.

## Fontes técnicas

- [NCBI E-Utilities: parâmetros e History Server](https://www.ncbi.nlm.nih.gov/books/NBK25499/)
- [NCBI: uso de API keys e limites](https://eutilities.github.io/site/API_Key/usageandkey/)
- [Mapeamento de PMID/PMCID](https://pmc.ncbi.nlm.nih.gov/tools/xref-ids/)
- [PMC Open Access Subset](https://pmc.ncbi.nlm.nih.gov/tools/openftlist/)
- [PMC Article Datasets: serviço atual](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/)
- [Esquema do bucket e metadados por versão](https://pmc-oa-opendata.s3.amazonaws.com/README.txt)

Fonte dos textos: **NIH NLM NCBI PubMed Central Article Datasets**. Este projeto não
é afiliado nem endossado pelo NCBI, NLM ou NIH.
