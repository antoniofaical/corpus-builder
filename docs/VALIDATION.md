# Validação 0.3.0 — orchestrator

Em 30/09/2026: **115 testes aprovados** localmente (Linux/Python 3.12.14), lint e
formatação aprovados, wheel/sdist gerados. A suíte inclui os 85 testes anteriores
mais 30 casos de orquestração/input.

Verificações novas:

- N entradas geram N chamadas ordenadas ao core, incluindo entradas repetidas.
- JSON simples/contextual, preservação literal da expressão e validação completa antes da execução.
- Conflitos de ID/versão e alterações de lista/ordem/configuração não sobrescrevem o lote.
- Falhas de query não impedem entradas seguintes; autenticação rejeitada interrompe o lote.
- Retomada tenta apenas entradas incompletas; rechecagem opcional chama também as concluídas.
- Ctrl+C, marcadores deixados por encerramento forçado e falhas de callback preservam o estado.
- Exceções inesperadas são registradas sem dados sensíveis e propagadas.
- Bloqueio de dois escritores, rejeição de checkpoints com caminhos alterados e diretórios alheios.
- Intervalo entre queries respeita os limites ao recriar os clientes HTTP do core.
- Execução da CLI e do core real com serviços simulados, incluindo reuso e proveniência.

Integração remota pequena, sem a chave do usuário e com NCBI limitado a 3 requests/s:
input JSON com `35275515[uid]` e `35275515[PMID]`, IDs de query distintos,
formato XML e fonte PMC. O lote concluiu as **2 chamadas**; a primeira baixou um XML,
a segunda reutilizou o arquivo pelo catálogo compartilhado. Repetir o lote produziu
**0 chamadas ao core e 2 entradas puladas**, mantendo os estados concluídos.

Os entrypoints `orchestrator.py`, `python -m corpus_builder.orchestrator` e o script
instalável `corpus-builder-batch` compartilham a mesma implementação. A configuração
Unpaywall e suas limitações de validação continuam como documentadas na versão 0.2.0.
Deduplicação bibliográfica permanece fora do escopo.

---

# Validação 0.2.0 — proveniência e fontes abertas

Verificações em 30/09/2026, Linux/Python 3.12.14. Deduplicação bibliográfica foi
explicitamente excluída do escopo: mesmos DOI/título em PMIDs distintos permanecem
separados, e não há decisões de fusão automáticas ou manuais.

## Testes determinísticos

**85 testes aprovados**, com `ruff check`, `ruff format --check` e build de wheel/sdist.
A suíte verifica os casos existentes, mais:

- Duas queries/versionamentos/trilhas preservados no catálogo, com todos os vínculos.
- Reaproveitamento de arquivos entre runs e recuperação de cópias locais corrompidas.
- DOI igual em PMIDs distintos sem consolidação.
- Conflito de ID/versão/contexto e configuração incompatível rejeitados antes da rede.
- Importação v1 idempotente sem alteração do banco ou manifesto original.
- Migração de retomada v1 com backup consistente, mantendo o run_id.
- Metadados de artigos sem texto aberto, palavras-chave, MeSH e proveniência de campos.
- Europe PMC e Unpaywall em fixtures; DOI incorreto não permite baixar o arquivo.
- `open_access` com download falho; `closed` com evidência datada; 404 resulta em `unknown`.
- HTML estruturado aceito; páginas de resumo/login rejeitadas; redirects privados rejeitados.
- Limites de tamanho e limpeza de arquivos parciais.
- Bloqueio de dois escritores do mesmo catálogo; configuração de email Unpaywall.

Fixtures não representam artigos reais. CI executa lint, formatação, testes e build
em Windows/Linux e Python 3.11/3.13; confira os logs associados ao commit.

## Integração real 0.2.0

Executada sem a chave do usuário, explicitamente com limite NCBI de 3 requests/s,
Europe PMC habilitado e formato XML. Duas queries equivalentes, registradas como
estratégias distintas: `35275515[uid]` e `35275515[PMID]`.

| Execução | Resultado |
|---|---|
| `live-one`, versão `1`, trilha `T1` | 1 PMID; 2 XMLs baixados e verificados; completed. |
| `live-two`, versão `1`, trilha `T2` | 1 PMID; 2 XMLs reutilizados do catálogo; completed. |
| Retomada de `live-one` | Mesmo run_id; 2 arquivos reutilizados; nenhuma nova busca. |
| Catálogo | 2 queries, 2 execuções, 2 vínculos e 1 identidade de origem (`pubmed:35275515`). |

Os documentos têm conteúdos e hashes distintos e foram preservados separadamente:

| Fonte | Bytes | SHA-256 |
|---|---:|---|
| PMC, `PMC10009402.1.xml` | 109974 | `75d9e0ab27e80e8660296e33bb1832dd8237080d5782b229bbd81fb4919c323a` |
| Europe PMC, `PMC10009402/fullTextXML` | 105624 | `4813f68e740fcd463f8ec1b9f3435fc9a547be6b06078e4993d22bddbf753c3d` |

Também foi importada uma execução real do core 0.1.0 (`runs/live-smoke`): dois
registros, nenhum arquivo local indisponível, e hashes do banco, manifesto e relatório
de origem idênticos antes/depois. O download Europe PMC foi repetido com a validação
de URLs final, confirmando funcionamento com resolução remota via proxy.

Não foi realizado teste remoto do Unpaywall, pois não foi fornecido email de contato
para esse serviço. Sua integração foi verificada com respostas simuladas, incluindo
sucesso, 404, falha HTTP, identidade incorreta, formatos e proveniência. HTML foi
validado por fixtures, não por cobertura exaustiva de layouts de editoras.

Não foi repetida aquisição em grande volume nesta versão. O teste de enumeração
real de 14.303 PMIDs abaixo pertence ao core 0.1.0; a suíte de regressão continua
cobrindo o particionamento. Arquivos reais e bancos dos testes não são versionados.

---

# Validação do core 0.1.0

Verificações realizadas em 30/09/2026. Ambiente local: Linux, Python 3.12.14.
Não foi usada a API key do usuário: os testes públicos foram executados explicitamente
sem chave, com limite de 3 requests/s. O uso da variável configurável e o envio da
chave somente ao NCBI foram verificados por testes offline.

## Suíte determinística

Comando: `python -m pytest -q`. Resultado local: **46 testes aprovados**.
Fixtures artificiais são identificadas como fixtures e não representam artigos reais.

Cobertura inclui:

- Query vazia em resultados, resposta truncada, enumeração de 10.003 IDs e reconciliação.
- Partições persistidas, retomada e expiração do conjunto no History Server.
- Correspondência por parâmetros `id` repetidos no ELink e detecção de registros omitidos.
- PDF/XML, formato ausente, versão não-OA, ausência no serviço de distribuição e múltiplas versões.
- Interrupção com arquivo concluído, retomada sem rede e nova obtenção de arquivo adulterado.
- Checksum incorreto, 429 com `Retry-After`, 404 sem retry e JSON malformado.
- Rejeição de entidades XML externas e de URLs fora do artigo/bucket esperado.
- Chave via variável configurável, segredo ausente dos artefatos e autenticação inválida.
- Bloqueio de escritores concorrentes, preservação de diretório não relacionado e callback falho.
- CLI, configurações inválidas e incompatibilidade de query/formatos ao retomar.

`ruff check .` e `ruff format --check .`: aprovados localmente.
A workflow de CI repete lint, testes e build em Windows/Linux, Python 3.11/3.13.
Consulte o resultado da execução da CI associada ao commit; este documento não
substitui os logs nem afirma que uma execução futura já terminou.

## Integração real: descoberta, arquivos e retomada

Query: `35275515[uid] OR 20466091[uid]`.

| Resultado observado | Quantidade |
|---|---:|
| Registros esperados e enumerados | 2 |
| Artigos com arquivos obtidos | 1 |
| PDF baixado e verificado | 1 |
| XML baixado e verificado | 1 |
| PMCID sem versão encontrada no serviço de distribuição | 1 |
| Erros técnicos ao concluir | 0 |

O PMID `35275515` foi associado a `PMC10009402`, versão 1. Seus arquivos foram
baixados do serviço oficial e tiveram MD5 conferido contra os metadados da fonte:

| Arquivo | Bytes | MD5 da fonte, confirmado |
|---|---:|---|
| `PMC10009402.1.pdf` | 168815 | `e3ed5c4234595c6cc6f60a45118f7ce0` |
| `PMC10009402.1.xml` | 109974 | `bccf40c02e0a5577b0f4361e78b94f49` |

SHA-256 locais:

```text
PDF e51d50f44b332225607bc1e6e22f709db109f86059203eb439e59ec943f5102b
XML 75d9e0ab27e80e8660296e33bb1832dd8237080d5782b229bbd81fb4919c323a
```

O PMID `20466091` teve associação a `PMC2869000`, mas o bucket não retornou versões
para esse PMCID no momento da execução. A ferramenta registrou
`not_in_pmc_distribution`; isso não afirma que o artigo inexiste ou não pode ser lido
gratuitamente em outro local.

A segunda execução usou o mesmo run: **2 arquivos reutilizados, 0 novos downloads**.
Também foi verificada offline a ausência de chamadas de rede nessa retomada completa.

Fontes para reprodução:

- https://pubmed.ncbi.nlm.nih.gov/35275515/
- https://pubmed.ncbi.nlm.nih.gov/20466091/
- https://pmc-oa-opendata.s3.amazonaws.com/metadata/PMC10009402.1.json
- https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/

## Integração real: busca acima de 10 mil

Query técnica de validação: `1:15000[UID]`. Não é uma estratégia bibliográfica temática.

- Início registrado: `2026-09-30T18:19:57.772368+00:00`.
- Término registrado: `2026-09-30T18:21:00.269416+00:00`.
- Contagem informada pelo PubMed: **14.303**.
- PMIDs únicos efetivamente persistidos: **14.303**.
- Particionamento: 37 nós persistidos, incluindo subdivisões e folhas.
- Folhas com resultados: 7.730 + 6.573 registros.
- Eventos de retry registrados: 5; descoberta concluída e reconciliada.

Essa verificação executou somente `PubMed.discover`, sem baixar os 14.303 artigos.
A aquisição integral em grande volume não foi testada; o teste end-to-end foi pequeno.
Disponibilidades e contagens podem mudar. Para atualizar um corpus, crie uma nova execução.
