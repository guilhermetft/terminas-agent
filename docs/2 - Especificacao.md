# 2. Especificação do Sistema — Termina's Agent

## 2.1 Personas

### Persona 1: Engenheiro / Desenvolvedor de Software (Guilherme)
- **Perfil:** Usuário técnico, atua em desenvolvimento de sistemas, automação e orquestração de ferramentas em ambiente Windows e Linux.
- **Necessidade:** Um agente mestre para receber objetivos amplos em linguagem natural, planejar etapas, acionar subagentes especialistas, consultar bases de código e documentos via RAG e consolidar relatórios acionáveis.
- **Frustrações:** Perda de tempo chaveando entre ferramentas, perda de contexto em conversas longas e necessidade de configurar comportamentos repetidamente a cada sessão.

### Persona 2: Usuário Operacional / Pesquisador Técnico
- **Perfil:** Profissional focado em produtividade diária, pesquisa de documentações e análise de relatórios extensos.
- **Necessidade:** Enviar documentos técnicos (PDFs, Markdown, especificações) para o agente fazer perguntas, extrair dados com citação de fontes e executar rotinas agendadas.
- **Frustrações:** Alucinações de modelos genéricos em documentos longos e respostas prolixas sem aplicabilidade prática.

---

## 2.2 Histórias de Usuário (User Stories)

| ID | Como... | Quero... | Para que... |
|---|---|---|---|
| **US-01** | Desenvolvedor | Enviar um objetivo em linguagem natural | O agente gere um plano de execução detalhado em etapas com dependências (DAG). |
| **US-02** | Desenvolvedor | Que tarefas independentes rodem em paralelo | O tempo total de execução seja minimizado. |
| **US-03** | Usuário | Submeter arquivos locais (PDF, Markdown, código) para consulta via RAG | Eu possa fazer perguntas com respostas contextualizadas e baseadas estritamente no material fornecido. |
| **US-04** | Usuário | Contar com um conjunto de Skills pré-definidas prontas para uso | O agente realize ações úteis imediatamente após a instalação sem necessidade de programação prévia. |
| **US-05** | Desenvolvedor | Personalizar o comportamento, tom e regras do agente via arquivo de configuração | O agente se adapte ao meu estilo de trabalho sem perder suas definições nativas de fábrica. |
| **US-06** | Usuário | Que o agente peça confirmação antes de alterar arquivos ou executar comandos destrutivos | Eu mantenha o controle e evite perda acidental de dados (*Human-in-the-Loop*). |
| **US-07** | Usuário | Que o agente funcione identicamente em sistemas Windows e Linux | Eu possa migrar ou sincronizar meus fluxos entre meu notebook pessoal e servidores/estações Linux. |
| **US-08** | Desenvolvedor | Conectar novos subagentes e servidores MCP via configuração simples | A rede de especialistas seja estendida modularmente. |

---

## 2.3 Requisitos Funcionais (RF)

| ID | Nome | Descrição | Prioridade |
|---|---|---|---|
| **RF-01** | Decomposição de Metas (DAG) | Decompor objetivos gerais em um grafo acíclico dirigido de tarefas com dependências explícitas. | Alta |
| **RF-02** | Despacho e Orquestração de Subagentes | Rotear, instanciar e monitorar tarefas em agentes especialistas (Dev, Pesquisa, Rotinas, etc.). | Alta |
| **RF-03** | Execução Paralela / Assíncrona | Executar nós independentes do DAG de forma concorrente sem bloqueio de thread ou da interface. | Alta |
| **RF-04** | Síntese de Respostas | Consolidar os outputs de múltiplos subagentes em uma resposta final coesa, direta e sem redundâncias. | Alta |
| **RF-05** | Mecanismo de RAG (Busca Documental) | Permitir envio de arquivos locais (PDF, MD, TXT, código-fonte), indexar via chunking/embeddings e responder perguntas usando recuperação aumentada por geração. | Alta |
| **RF-06** | Catálogo de Skills Nativas | Disponibilizar 9 skills nativas de fábrica: (1) `issue-flow` (gestão completa de issues e PRs no GitHub), (2) `doc-gen` (geração de relatórios em PDF/DOCX e imagens/diagramas), (3) `google-workspace` (integração MCP com Google Calendar/Agenda, Gmail e Drive), (4) `deep-search` (pesquisa web avançada, scraping e síntese), (5) `swarm-inspector` (monitoramento de saúde e métricas de subagentes externos), (6) `daily-briefing` (briefing matinal/noturno integrando agenda e tarefas), (7) `rag-indexer` (indexação vetorial e busca semântica em arquivos locais), (8) `shell-safe-runner` (execução segura de comandos de terminal com guardrail HITL), (9) `quick-notes` (captura rápida de notas e memória de longo prazo). Suporte a novas skills via plugins e servidores MCP adicionais. | Alta |
| **RF-07** | Comportamento Padrão e Customizável | Fornecer *system prompt* e comportamento inicial pronto (focado, técnico, conciso) com suporte a customização posterior via arquivo de configuração (`config.yaml`) ou comando `/behavior`. | Alta |
| **RF-08** | Human-in-the-Loop (HITL) | Suspender a execução e solicitar confirmação explícita do usuário antes de rodar comandos de alto risco ou alterar arquivos críticos. | Alta |
| **RF-09** | Gestão de Memória Multinível | Persistir contexto de curto prazo (sessão ativa), dados de perfil/preferências e histórico episódico de execuções passadas em banco local. | Alta |
| **RF-10** | Suporte a Ferramentas MCP | Carregar e consumir dinamicamente ferramentas de servidores compatíveis com o padrão *Model Context Protocol*. | Alta |
| **RF-11** | Auto-Recuperação e Retry | Detectar falhas ou alucinações de subagentes, reexecutar com ajuste de prompt ou acionar rota de fallback até o limite estipulado. | Média |
| **RF-12** | Rotinas Agendadas (Cron) | Executar tarefas agendadas em segundo plano (resumos periódicos, monitoramento de pastas ou relatórios matinais). | Média |
| **RF-13** | Comandos de Controle no Terminal | Disponibilizar comandos rápidos com barra (`/plan`, `/run`, `/rag`, `/skills`, `/behavior`, `/status`, `/cancel`, `/memory`). | Média |

---

## 2.4 Requisitos Não-Funcionais (RNF)

| ID | Nome | Métrica / Critério de Aceitação | Prioridade |
|---|---|---|---|
| **RNF-01** | Portabilidade Multiplataforma | O sistema deve operar de forma 100% idêntica tanto em **Windows 11** quanto em distribuições **Linux** (Ubuntu, Debian, Fedora), utilizando caminhos normalizados (`pathlib`) e chamadas de shell agnósticas. | Alta |
| **RNF-02** | Desempenho e Concorrência | O núcleo de orquestração deve ser assíncrono (asyncio), suportando 5+ subagentes concorrentes sem travamento de UI ou consumo excessivo de CPU. | Alta |
| **RNF-03** | Rastreabilidade e Auditoria | Toda transação, troca de mensagens A2A, recuperação do RAG e consumo de tokens deve ser gravada em log estruturado (JSONL). | Alta |
| **RNF-04** | Isolamento e Segurança | Subagentes devem rodar com privilégio mínimo e operar em diretórios delimitados, sem capacidade de executar comandos destrutivos sem aprovação do Master/Usuário. | Alta |
| **RNF-05** | Flexibilidade de Modelos (LLM-Agnostic) | Suporte a provedores de nuvem (Anthropic Claude, Google Gemini, OpenAI) e modelos locais via Ollama através de interface unificada de chamada. | Alta |
| **RNF-06** | Otimização de Custos (Tokenomics) | O RAG deve retornar apenas os chunks estritamente relevantes com limite de contexto; tarefas de triagem devem utilizar modelos rápidos/econômicos. | Média |
| **RNF-07** | Tempo de Resposta da Interface | A interface CLI/TUI deve exibir feedback visual de processamento em menos de 200ms após o envio de uma instrução pelo usuário. | Média |

---

## 2.5 Restrições

1. **Compatibilidade de Sistemas Operacionais:** O software deve rodar nativamente em **Windows** e **Linux**, sem dependência de comandos exclusivos de um único sistema (ex: usar abstrações universais para comandos de shell e manipulação de arquivos).
2. **Hardware de Execução Local:** O projeto deve ser viável em máquinas com hardware equivalente a Intel Core i5 (12ª geração), 8 GB de RAM e 4 GB de VRAM. Modelos de linguagem acima de 7B devem ser consultados via API; modelos locais devem se limitar a SLMs quantizados (1B a 3B) ou embeddings leves em CPU.
3. **Limite de Iterações e Prevenção de Loops:** Cada subagente possui teto estrito de passos (`max_steps = 15`) e timeout por tarefa para evitar consumo descontrolado de tokens ou loops infinitos de delegação.
4. **Armazenamento Local:** Banco de dados de memória e vetores do RAG devem ser gravados localmente (ex: SQLite com extensão vetorial ou DuckDB/Chroma local em arquivo), sem exigência de clusters ou serviços de nuvem externos para persistência.
