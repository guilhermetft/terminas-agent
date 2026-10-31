# 4. Plano de Testes de Software — Termina's Agent

## 4.1 Introdução
Este documento estabelece a estratégia de validação e garantia da qualidade para o **Termina's Agent**. O plano cobre testes unitários e de integração dos módulos centrais (Planejamento em DAG, RAG Documental, Catálogo de Skills Nativas, Memória SQLite, Contratos A2A e Guardrails de Segurança *Human-in-the-Loop*), assegurando conformidade estrita entre Windows e Linux.

---

## 4.2 Escopo dos Testes

### 4.2.1 Em Escopo (In-Scope)
- Motor de planejamento e decomposição em grafo (DAG).
- Pipeline RAG: ingestão de arquivos (`.pdf`, `.md`, `.txt`, código), chunking semântico e busca por similaridade vetorial.
- Execução e carregamento do catálogo de 9 Skills Nativas e conexão MCP.
- Barramento de mensagens e validação do esquema A2A (JSON Schema / Pydantic).
- Persistência e isolamento no banco SQLite (sessão, perfil e memória episódica).
- Interrupção de segurança e aprovação (*Human-in-the-Loop*) em ações sensíveis.
- Portabilidade multiplataforma de caminhos e comandos (Windows 11 e Linux).

### 4.2.2 Fora de Escopo (Out-of-Scope)
- Avaliação de qualidade intrínseca ou criatividade dos modelos fundacionais das empresas provedoras (Claude, Gemini, OpenAI).
- Testes de estresse da infraestrutura externa dos provedores de nuvem.

---

## 4.3 Estratégia de Teste

| Nível | Objetivo | Ferramentas |
|---|---|---|
| **Testes Unitários** | Validar funções e classes isoladas (parsers, chunker, embeddings locais, guardrails, memória). | `pytest`, `pytest-mock` |
| **Testes de Integração** | Validar ciclo completo: Planner -> RAG Context -> Dispatcher -> A2A Contract -> Resposta consolidada. | `pytest-asyncio`, `respx` |
| **Testes de Segurança (HITL)** | Garantir bloqueio automático de comandos destrutivos sem aprovação do usuário. | Asserções de estado `AWAITING_APPROVAL` no pytest |
| **Testes Multiplataforma** | Validar que manipulações de arquivo usam `pathlib.Path` e funcionam tanto no Windows quanto no Linux. | `pytest` com matriz de caminhos POSIX/Windows |
| **Testes de Interface (CLI/TUI)** | Validar parser de slash commands (`/plan`, `/rag`, `/skills`, etc.) e renderização de saída. | `rich.console.Console` (buffer capture) |

---

## 4.4 Ambiente e Ferramentas
- **Linguagem:** Python 3.11+
- **Runner de Testes:** `pytest` com plugin `pytest-asyncio`
- **Mocks de LLM e APIs:** Respostas determinísticas mockadas para execução rápida e custo zero de tokens.
- **Cobertura de Código:** `pytest-cov` (meta mínima: 80% nos módulos centrais `core/`, `rag/`, `skills/` e `guardrails/`).

---

## 4.5 Critérios de Entrada e Saída
- **Critérios de Entrada:** Módulo implementado com tipagem Pydantic validada e dependências instaladas.
- **Critérios de Saída:** 100% dos testes planejados aprovados; zero falhas nos guardrails de segurança; tempo de execução total da suíte unitária inferior a 15 segundos.

---

## 4.6 Casos de Teste Planejados

### CT-01: Decomposição de Meta Complexa em Grafo (DAG)
- **Requisito Associado:** RF-01
- **Objetivo:** Verificar se uma instrução composta gera múltiplos nós com dependências corretas.
- **Entrada:** String: `"Crie um script em Python para ler o arquivo dados.csv e em seguida gere os testes unitários"`.
- **Procedimento:** Executar `Planner.create_dag(input)`.
- **Resultado Esperado:** Grafo com 2 nós: Nó 1 (criar script), Nó 2 (gerar testes dependendo do término do Nó 1).

---

### CT-02: Execução Paralela de Nós Independentes
- **Requisito Associado:** RF-03
- **Objetivo:** Verificar se tarefas sem dependência mútua são despachadas simultaneamente.
- **Entrada:** DAG com dois nós independentes: Nó A (`deep-search` sobre tema X) e Nó B (`deep-search` sobre tema Y).
- **Procedimento:** Executar motor de despacho assíncrono medindo timestamp de início.
- **Resultado Esperado:** Ambos os nós devem iniciar no mesmo ciclo de event-loop (diferença de início < 50ms).

---

### CT-03: Ingestão de Documento e Recuperação Semântica via RAG
- **Requisito Associado:** RF-05, RNF-06
- **Objetivo:** Garantir que um arquivo local enviado seja fatiado, vetorizado e consultado com precisão.
- **Entrada:** Arquivo `manual.txt` com regra de negócio específica e pergunta `"Qual a porta padrão do serviço?"`.
- **Procedimento:** Invocar `RAGEngine.index_file('manual.txt')` e em seguida `RAGEngine.query('Qual a porta padrão?')`.
- **Resultado Esperado:** Retorno do chunk contendo a porta correta, com score de similaridade > 0.75 e metadados de arquivo/linha.

---

### CT-04: Carregamento e Execução do Catálogo de Skills Nativas
- **Requisito Associado:** RF-06
- **Objetivo:** Validar que as 9 skills nativas são registradas na inicialização e despachadas por identificador.
- **Entrada:** Comando de execução da skill `quick-notes` com o texto `"Reunião de alinhamento às 15h"`.
- **Procedimento:** Consultar `SkillsRegistry.get_skill('quick-notes')` e executar payload.
- **Resultado Esperado:** Skill executada com sucesso e nota persistida no repositório.

---

### CT-05: Interceptação por Guardrail (HITL em Comando Destrutivo)
- **Requisito Associado:** RF-08, RNF-04
- **Objetivo:** Garantir que tentativa de execução de comando de alto risco seja bloqueada sem consentimento do usuário.
- **Entrada:** Solicitação de execução da tool `shell-safe-runner` com comando `rm -rf /` ou `Remove-Item -Recurse -Force C:\`.
- **Procedimento:** Submeter ação ao método `GuardrailEngine.evaluate()`.
- **Resultado Esperado:** Transição imediata para status `AWAITING_USER_APPROVAL`; o comando não é executado no shell.

---

### CT-06: Persistência e Recuperação de Memória e Perfil no SQLite
- **Requisito Associado:** RF-09
- **Objetivo:** Garantir integridade de leitura e escrita de preferências do usuário e sessões anteriores.
- **Entrada:** Gravação de preferência `{"theme": "dark", "preferred_model": "gemini-2.5-flash"}`.
- **Procedimento:** Gravar via `MemoryStore`, fechar conexão SQLite, abrir nova conexão e executar `get_preferences()`.
- **Resultado Esperado:** Retorno idêntico dos dados persistidos sem corrupção.

---

### CT-07: Validação do Esquema de Mensagens A2A
- **Requisito Associado:** RF-02, RNF-03
- **Objetivo:** Impedir que subagentes externos processem ou enviem mensagens fora do padrão estipulado.
- **Entrada:** Payload de retorno sem os campos obrigatórios `task_id` e `status`.
- **Procedimento:** Validar payload com o modelo Pydantic `AgentTaskResult`.
- **Resultado Esperado:** Lançamento de exceção `ValidationError` e rejeição segura da mensagem no barramento.

---

### CT-08: Portabilidade Multiplataforma (Normalização de Caminhos)
- **Requisito Associado:** RNF-01
- **Objetivo:** Garantir que o sistema manipule arquivos e caminhos de forma idêntica em Windows e Linux.
- **Entrada:** Caminho com separadores mistos (ex: `docs\2 - Especificacao.md` vs `docs/2 - Especificacao.md`).
- **Procedimento:** Processar via módulo de caminhos utilitários do Termina (`PathHelper.normalize`).
- **Resultado Esperado:** Resolução idêntica de arquivo existente independente do sistema operacional anfitrião.

---

### CT-09: Tolerância a Falhas e Re-planejamento
- **Requisito Associado:** RF-11
- **Objetivo:** Detectar falha de um subagente externo e aplicar política de retentativa ou re-roteamento.
- **Entrada:** Retorno de erro `TIMEOUT` ou falha de conexão do subagente externo.
- **Procedimento:** Acionar rotina de resiliência do orquestrador.
- **Resultado Esperado:** Registro do erro em log JSONL, incremento de retry e acionamento de rota alternativa.
