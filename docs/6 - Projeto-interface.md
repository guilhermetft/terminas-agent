# 6. Projeto de Interface — Termina's Agent

## 6.1 Filosofia de Design
A interface do **Termina's Agent** adota o conceito **Terminal-First**, priorizando agilidade de digitação, feedback visual em tempo real e densidade de informação limpa, sem poluição visual. O design é pensado para desenvolvedores que operam dentro do Windows Terminal ou shells modernos.

Princípios norteadores:
1. **Visibilidade de Estado:** O usuário sempre sabe qual subagente está trabalhando, o que está executando e quanto tempo está levando.
2. **Confirmação Explícita de Risco:** Cores de alerta visíveis (amarelo/vermelho ANSI) para pausas de segurança (*Human-in-the-Loop*).
3. **Mínimo Ruído de Saída:** Apenas sínteses acionáveis são apresentadas na visualização padrão; logs verbosos de ferramentas ficam acessíveis sob demanda.

---

## 6.2 Interface de Linha de Comando e Terminal (TUI)

A interface padrão é renderizada no terminal utilizando componentes do ecossistema `Rich` e `Textual`.

### 6.2.1 Wireframe: Visão Principal em Execução

```text
┌── Termina's Agent v0.1.0 ─────────────────────────────────────────────── [Session: #42] ─┐
│ Master: IDLE | Active Workers: 2 | Total Tokens: 2,410 | Budget: Normal                    │
└────────────────────────────────────────────────────────────────────────────────────────────┘

[Objetivo]: "Auditar o projeto term-agent, criar testes unitários e verificar padrões de tipagem"

┌── Plano de Execução (DAG) ─────────────────────────────────────────────────────────────────┐
│  ✔ Step 1: Analisar estrutura do projeto e dependências       [DevAgent]       (1.2s)      │
│  ● Step 2: Gerar casos de teste para o módulo core            [DevAgent]       [RODANDO...] │
│  ● Step 3: Executar linter e checagem de tipos com mypy       [ResearchAgent]  [RODANDO...] │
│  ○ Step 4: Sintetizar relatório de cobertura e inconsistências [MasterCore]    [PENDENTE]   │
└────────────────────────────────────────────────────────────────────────────────────────────┘

┌── Live Logs / Workers Output ──────────────────────────────────────────────────────────────┐
│ [DevAgent]      Gerando testes para engine.py e planner.py...                              │
│ [ResearchAgent] Executando mypy --strict ./src... 2 avisos encontrados.                    │
└────────────────────────────────────────────────────────────────────────────────────────────┘

terminas > _
```

---

### 6.2.2 Wireframe: Interrupção de Segurança (Human-in-the-Loop)

Quando um subagente precisa rodar uma operação classificada como sensível:

```text
┌── ⚠ ALERTA DE SEGURANÇA: APROVAÇÃO NECESSÁRIA ─────────────────────────────────────────────┐
│ Agente Solicitante: DevAgent                                                               │
│ Ação Solicitada:    Execução de comando de escrita no disco                                │
│ Comando:            git checkout -B fix-auth-logic && git clean -fd                        │
│ Justificativa:      Limpar artefatos não rastreados para isolar os testes.                 │
├────────────────────────────────────────────────────────────────────────────────────────────┤
│ Deseja autorizar a execução desta ação?                                                    │
│ [y] Aprovar uma vez   [a] Aprovar todas desta sessão   [n] Negar   [m] Modificar comando   │
└────────────────────────────────────────────────────────────────────────────────────────────┘
Sua escolha [y/a/n/m]: _
```

---

## 6.3 Comandos Rápidos do Terminal (Slash Commands)

O agente aceita comandos prefixados com barra para controle operacional:

| Comando | Descrição |
|---|---|
| `/plan <meta>` | Elabora e renderiza o DAG da meta informada sem iniciar a execução dos agentes. |
| `/run <meta>` | Cria o plano e inicia imediatamente a orquestração distribuída. |
| `/status` | Exibe resumo da rede de subagentes, processos em background e consumo de tokens. |
| `/agents` | Lista todos os subagentes especialistas registrados e suas respectivas ferramentas (MCP). |
| `/memory` | Inspeciona variáveis gravadas, contexto de sessão e preferências de usuário. |
| `/approve <id>` | Aprova manualmente uma tarefa suspensa pelo guardrail. |
| `/cancel` | Cancela todas as tarefas ativas e encerra execuções de subagentes imediatamente. |
| `/clear` | Limpa a tela do terminal e reseta a memória de trabalho volátil. |

---

## 6.4 Interface Móvel Complementar (Telegram Companion Bot)

Para controle e recebimento de resumos quando o usuário está longe do computador:
- **Resumo Matinal:** O agente envia no início do dia as tarefas agendadas e pendências.
- **Notificação de Bloqueio HITL:** Envia botões inline `[Aprovar] / [Recusar]` caso o agente encontre um comando crítico rodando em background.
- **Disparo Remoto:** O usuário envia uma mensagem de texto simples e o Master inicia o plano no computador local, reportando o progresso.

---

## 6.5 Paleta de Cores e Temas do Terminal

A paleta foi desenhada para harmonia com temas escuros (ex: One Dark Pro / Catppuccin Mocha):
- **Primária (Identidade Termina):** Ciano / Azul Elétrico (`#38BDF8` / ANSI 39)
- **Sucesso (Tarefas concluídas):** Verde Esmeralda (`#34D399` / ANSI 35)
- **Atenção / Guardrails:** Âmbar / Amarelo Alerta (`#FBBF24` / ANSI 214)
- **Erro / Bloqueio:** Vermelho Coral (`#F87171` / ANSI 196)
- **Texto Secundário / Dim:** Cinza Claro (`#94A3B8` / ANSI 245)
