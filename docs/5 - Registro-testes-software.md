# 5. Registro de Testes de Software — Termina's Agent

## 5.1 Sumário de Execução

| Métrica | Valor |
|---|---|
| **Data da Criação / Última Atualização** | 31/10/2026 |
| **Fase Atual do Projeto** | Especificação & Arquitetura (Pré-Desenvolvimento) |
| **Total de Casos de Teste Planejados** | 9 |
| **Casos Executados** | 0 (Aguardando implementação dos módulos do código-fonte) |
| **Casos com Sucesso** | 0 |
| **Casos com Falha** | 0 |
| **Status Geral** | **PLANEJADO / PRONTO PARA EXECUÇÃO** |

---

## 5.2 Matriz de Execução dos Casos de Teste

| ID | Caso de Teste | Requisito | Status | Resultado Obtido | Data / Executante |
|---|---|---|---|---|---|
| **CT-01** | Decomposição em Grafo (DAG) | RF-01 | A executar | — | — |
| **CT-02** | Execução Paralela de Nós | RF-03 | A executar | — | — |
| **CT-03** | Ingestão e Busca Semântica RAG | RF-05 / RNF-06 | A executar | — | — |
| **CT-04** | Catálogo de 9 Skills Nativas | RF-06 | A executar | — | — |
| **CT-05** | Interceptação HITL em Risco | RF-08 / RNF-04 | A executar | — | — |
| **CT-06** | Persistência de Memória SQLite | RF-09 | A executar | — | — |
| **CT-07** | Validação de Esquema A2A (Pydantic) | RF-02 / RNF-03 | A executar | — | — |
| **CT-08** | Portabilidade Windows/Linux | RNF-01 | A executar | — | — |
| **CT-09** | Tolerância a Falhas e Retry | RF-11 | A executar | — | — |

---

## 5.3 Detalhamento de Execuções e Evidências

### CT-01: Decomposição de Meta Complexa em Grafo (DAG)
- **Status:** Planejado
- **Evidências:** A ser preenchido na execução de `pytest tests/unit/test_planner.py`.

### CT-02: Execução Paralela de Nós Independentes
- **Status:** Planejado
- **Evidências:** A ser preenchido na execução de `pytest tests/integration/test_dispatcher.py`.

### CT-03: Ingestão de Documento e Recuperação Semântica via RAG
- **Status:** Planejado
- **Evidências:** A ser preenchido na execução de `pytest tests/unit/test_rag.py`.

### CT-04: Carregamento e Execução do Catálogo de Skills Nativas
- **Status:** Planejado
- **Evidências:** A ser preenchido na execução de `pytest tests/unit/test_skills.py`.

### CT-05: Interceptação por Guardrail (HITL em Comando Destrutivo)
- **Status:** Planejado
- **Evidências:** A ser preenchido na execução de `pytest tests/unit/test_guardrails.py`.

### CT-06: Persistência e Recuperação de Memória e Perfil no SQLite
- **Status:** Planejado
- **Evidências:** A ser preenchido na execução de `pytest tests/unit/test_memory.py`.

### CT-07: Validação do Esquema de Mensagens A2A
- **Status:** Planejado
- **Evidências:** A ser preenchido na execução de `pytest tests/unit/test_schemas.py`.

### CT-08: Portabilidade Multiplataforma (Normalização de Caminhos)
- **Status:** Planejado
- **Evidências:** A ser preenchido na execução de `pytest tests/unit/test_crossplatform.py`.

### CT-09: Tolerância a Falhas e Re-planejamento
- **Status:** Planejado
- **Evidências:** A ser preenchido na execução de `pytest tests/integration/test_resilience.py`.

---

## 5.4 Registro de Não-Conformidades e Bugs

| ID Bug | Caso Relacionado | Severidade | Descrição do Problema | Ação Corretiva | Status |
|---|---|---|---|---|---|
| — | — | — | Nenhum bug registrado nesta fase prévia de implementação. | — | — |

---

## 5.5 Avaliação de Prontidão
Todos os 9 casos de teste estão diretamente mapeados aos requisitos funcionais (RFs) e não-funcionais (RNFs) atualizados. A matriz cobre os componentes centrais do Termina's Agent: planejamento, RAG local, catálogo de skills, segurança HITL, memória e portabilidade multiplataforma.
