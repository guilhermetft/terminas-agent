# 7. Autenticação dos Provedores de IA — Termina's Agent

## 7.1 Objetivo
Depois de instalar, o usuário precisa entrar em um provedor de IA antes de usar o agente. Provedores suportados: **Claude (Anthropic)**, **Codex (OpenAI)** e **Gemini (Google)**. Sem nenhum provedor conectado, o agente fica bloqueado: texto livre, `/plan`, `/run` e os demais comandos pedem `/login`.

## 7.2 Comportamento

| Situação | O que funciona |
|---|---|
| Nenhum provedor conectado | Somente `/help`, `/login`, `/logout`, `/status`, `/skills` e `/exit`. O resto mostra "o agente está bloqueado". |
| Pelo menos um provedor conectado | Tudo. O provedor e o modelo ativos são usados pelo planner na hora, sem reiniciar. |

Comandos:

| Comando | Efeito |
|---|---|
| `/login [claude\|codex\|gemini]` | Pede a chave de API em um campo **oculto**, valida com o provedor e só então grava. O provedor passa a ser o ativo. |
| `/logout [provedor\|all]` | Remove a chave gravada. Se a chave vier de uma variável de ambiente, avisa que ela continua definida. |
| `/model [provedor [modelo\|list]]` | Mostra ou troca o provedor e o modelo ativos; `list` consulta os modelos disponíveis na API do provedor. |

`terminas run "<meta>"` sem nenhum provedor conectado sai com código 2 e uma mensagem explicando como entrar. Variáveis de ambiente já existentes (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`) continuam valendo e contam como login (útil em CI e automações).

## 7.3 Onde ficam as credenciais
- **Chaves:** `~/.terminas/.env` (diretório do usuário, valendo para qualquer pasta). A pasta pode ser trocada com a variável `TERMINAS_HOME` ou `paths.auth_dir` no `config.yaml`.
- **Estado não secreto** (provedor ativo e modelo por provedor): `~/.terminas/auth.json`.
- **Permissões:** Linux/macOS `0600` no arquivo e `0700` na pasta; Windows, ACL só para o usuário atual (via `icacls`). A gravação é atômica (arquivo temporário já restrito, depois `replace`). Se não for possível restringir, o `/login` avisa.
- Os demais dados do Termina (memória, índice do RAG, log de auditoria) continuam em `.terminas/` na pasta onde o comando é executado.

## 7.4 Login por link (OAuth): estudo de viabilidade
Esta é a primeira tarefa da issue #20. Resultado: **nesta versão, os três provedores usam chave de API.** O login por link foi estudado e fica fora por motivos de termos de uso (Claude, Codex) ou de pré-requisitos que o Termina ainda não cumpre (Gemini).

| Provedor | Login por link para apps de terceiros? | Decisão |
|---|---|---|
| **Claude** | **Não permitido.** A documentação oficial diz que a Anthropic "não permite que desenvolvedores terceiros ofereçam login Claude.ai em seus aplicativos, nem roteiem requisições por credenciais dos planos Free, Pro, Max ou Ultra", e que desenvolvedores devem usar chave de API do Claude Console. | Só chave de API. |
| **Codex / OpenAI** | **Sem aval documentado.** A documentação do Codex descreve "Sign in with ChatGPT" e chave de API para o próprio Codex; não há texto autorizando outros aplicativos a reaproveitar o login da assinatura. | Só chave de API. |
| **Gemini** | **Possível, com pré-requisitos.** O OAuth do Google é oficial (cliente do tipo Desktop, escopo `generative-language`), mas o guia é descrito como apropriado para testes: exibe o aviso "app não verificado" e, fora de uma organização Google Workspace, os escopos precisam ser adicionados e verificados. | Adiado. |

Para habilitar o OAuth do Gemini no futuro seria preciso: (1) registrar um cliente OAuth do Termina no Google Cloud e passar pela verificação do app; (2) provar que o LiteLLM aceita token Bearer no endpoint do Gemini (hoje o provedor usa chave de API); (3) implementar PKCE com redirecionamento local, armazenamento e renovação do token. Nada disso pôde ser validado sem uma conta e um cliente registrados, por isso não foi implementado. O ponto de extensão natural é o `ProviderSpec` em `src/auth/providers.py`.

Fontes consultadas (nov/2026):
- [Anthropic — Legal and compliance (Authentication and credential use)](https://code.claude.com/docs/en/legal-and-compliance)
- [OpenAI — Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Google — Gemini API OAuth quickstart](https://ai.google.dev/gemini-api/docs/oauth)

## 7.5 Como a chave é validada
Antes de gravar, o Termina faz uma chamada barata que lista os modelos (sem gerar texto, sem custo): `GET /v1/models` (Claude e OpenAI) e `GET /v1beta/models` (Gemini). A chave vai **sempre em cabeçalho** (`x-api-key`, `Authorization: Bearer` ou `x-goog-api-key`), nunca na URL. Resultados: `200` aceita; `401/403` (e `400` no Gemini) recusa; `429` aceita (limite de uso); outro status ou falha de rede não grava. A lista devolvida escolhe o modelo padrão e alimenta `/model list`.

## 7.6 Segurança
- A chave é lida em campo oculto (`prompt_toolkit` com `is_password`; `getpass` no modo simples) e nunca entra no histórico de comandos, na memória da sessão ou no log de auditoria.
- Uma chave colada **como texto normal** (`sk-ant-…`, `sk-…`, `AIza…`, `ghp_…`) é recusada antes de qualquer processamento, não é gravada no histórico em disco e a tela nunca a exibe (mensagens de erro passam por redação).
- O log de auditoria (`audit.jsonl`) redige qualquer chave antes de gravar.
- A tela mostra apenas a chave mascarada (primeiros e últimos 4 caracteres).
- O `.env` do usuário já é ignorado pelo Git; mesmo assim, não o compartilhe.

## 7.7 Limitações conhecidas
- O Termina usa a API de **chat completions** via LiteLLM. Modelos que só existem na API Responses da OpenAI (por exemplo, variantes `-codex`) não aparecem em `/model list`; o padrão do provedor Codex é `gpt-5`.
- Os identificadores de modelo padrão (`claude-sonnet-5-5`, `gpt-5`, `gemini-2.5-flash`) são preferências: se não existirem na lista da API, o Termina escolhe o mais novo que combina com a preferência.
- A qualidade das respostas e a compatibilidade de cada modelo com o formato JSON do planner não foram medidas contra os provedores reais (exige chaves de API e custa tokens).
