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
| `/login [claude\|codex\|gemini]` | Pede a chave de API em um campo **oculto**, valida com o provedor e só então grava. O provedor passa a ser o ativo. No **Codex** pergunta como entrar: `/login codex chatgpt` (plano ChatGPT, pelo navegador, ver 7.8) ou `/login codex key` (chave de API). |
| `/logout [provedor\|all]` | Remove a chave gravada (e, no Codex, os tokens do plano ChatGPT). Se a chave vier de uma variável de ambiente, avisa que ela continua definida. |
| `/model [provedor [modelo\|list]]` | Mostra ou troca o provedor e o modelo ativos; `list` consulta os modelos disponíveis na API do provedor. |

`terminas run "<meta>"` sem nenhum provedor conectado sai com código 2 e uma mensagem explicando como entrar. Variáveis de ambiente já existentes (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`) continuam valendo e contam como login (útil em CI e automações).

## 7.3 Onde ficam as credenciais
- **Chaves:** `~/.terminas/.env` (diretório do usuário, valendo para qualquer pasta). A pasta pode ser trocada com a variável `TERMINAS_HOME` ou `paths.auth_dir` no `config.yaml`.
- **Estado não secreto** (provedor ativo e modelo por provedor): `~/.terminas/auth.json`.
- **Permissões:** Linux/macOS `0600` no arquivo e `0700` na pasta; Windows, ACL só para o usuário atual (via `icacls`). A gravação é atômica (arquivo temporário já restrito, depois `replace`). Se não for possível restringir, o `/login` avisa.
- Os demais dados do Termina (memória, índice do RAG, log de auditoria) continuam em `.terminas/` na pasta onde o comando é executado.

## 7.4 Login por link (OAuth): estudo de viabilidade
Esta é a primeira tarefa da issue #20, corrigida pela issue #23. Resultado: **Claude e Gemini usam só chave de API; o Codex também aceita o login pelo plano ChatGPT.** Para o Claude o login por link é vetado pelos termos de uso; para o Gemini faltam pré-requisitos que o Termina ainda não cumpre.

| Provedor | Login por link para apps de terceiros? | Decisão |
|---|---|---|
| **Claude** | **Não permitido.** A documentação oficial diz que a Anthropic "não permite que desenvolvedores terceiros ofereçam login Claude.ai em seus aplicativos, nem roteiem requisições por credenciais dos planos Free, Pro, Max ou Ultra", e que desenvolvedores devem usar chave de API do Claude Console. | Só chave de API. |
| **Codex / OpenAI** | **Permitido para apps open-source locais** (correção da issue #23). O estudo inicial concluiu, de forma incompleta, que a OpenAI não documentava o uso por terceiros; a OpenAI documenta o fluxo oficial "ChatGPT plan usage" para apps open-source locais (ver 7.8). | Chave de API **e** login pelo plano ChatGPT. |
| **Gemini** | **Possível, com pré-requisitos.** O OAuth do Google é oficial (cliente do tipo Desktop, escopo `generative-language`), mas o guia é descrito como apropriado para testes: exibe o aviso "app não verificado" e, fora de uma organização Google Workspace, os escopos precisam ser adicionados e verificados. | Adiado. |

Para habilitar o OAuth do Gemini no futuro seria preciso: (1) registrar um cliente OAuth do Termina no Google Cloud e passar pela verificação do app; (2) provar que o LiteLLM aceita token Bearer no endpoint do Gemini (hoje o provedor usa chave de API); (3) implementar PKCE com redirecionamento local, armazenamento e renovação do token. Nada disso pôde ser validado sem uma conta e um cliente registrados, por isso não foi implementado. O ponto de extensão natural é o `ProviderSpec` em `src/auth/providers.py`.

Fontes consultadas (nov/2026):
- [Anthropic — Legal and compliance (Authentication and credential use)](https://code.claude.com/docs/en/legal-and-compliance)
- [OpenAI — ChatGPT plan usage para apps open-source](https://developers.openai.com/siwc/token-sharing-open-source)
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
- Com chave de API, o Termina usa a API de **chat completions** via LiteLLM. Modelos que só existem na API Responses (por exemplo, variantes `-codex`) não aparecem em `/model list`; o padrão do provedor Codex é `gpt-5`. Com o plano ChatGPT, `/model list` mostra os modelos da Responses API (`visibility == "list"`).
- Os identificadores de modelo padrão (`claude-sonnet-5-5`, `gpt-5`, `gemini-2.5-flash`) são preferências: se não existirem na lista da API, o Termina escolhe o mais novo que combina com a preferência.
- A qualidade das respostas e a compatibilidade de cada modelo com o formato JSON do planner não foram medidas contra os provedores reais (exige chaves de API e custa tokens).

## 7.8 Login com o plano ChatGPT (Codex, issue #23)
O Codex aceita o fluxo oficial da OpenAI para apps open-source locais ("ChatGPT plan usage", parte do Sign in with ChatGPT): o Termina mostra e abre um link, o usuário entra no navegador e o agente passa a usar o plano, sem chave de API. A chave de API continua disponível (`/login codex key`). O recurso está em **preview** na OpenAI.

**Decisão: rota B (fluxo oficial), não a rota A (provider `chatgpt/` do LiteLLM).** A rota A reaproveita o client id público do Codex CLI, que a OpenAI não documenta para terceiros; a rota B é o caminho documentado para apps open-source. O custo é implementar registro, OAuth e um cliente de Responses (`src/auth/chatgpt.py`, `src/auth/responses.py`).

**Prova de conceito com conta real: pendente.** A decisão acima se baseia na documentação oficial; o JWKS (`https://auth.openai.com/.well-known/jwks.json`) foi confirmado na `openid-configuration`, e o primeiro teste real revelou que o `ext_agent_host_id` precisa do formato `urn:uuid:`; o fluxo foi testado contra servidores OAuth/Responses **falsos** (`tests/fake_chatgpt.py`). Antes de considerar a rota fechada, rode um login real e confira: registro dinâmico (`client_id` `oaiapp_...` no callback), uma resposta da Responses API e uma renovação. Registre o resultado aqui.

| Etapa | Como é feito |
|---|---|
| Registro | Cliente público, sem segredo. Primeiro login: `client_id=dynamic_agent_client` + `agent_name_hint` + `ext_agent_host_id` (`urn:uuid:<UUID>`, gerado uma vez; um UUID puro é recusado pelo authorize). O `client_id` emitido (`oaiapp_...`) vem no callback e é reutilizado. |
| OAuth | Authorization Code + PKCE (S256), `state` e `nonce`; `https://auth.openai.com/api/accounts/authorize` e `/oauth/token`; redirecionamento em `http://127.0.0.1:1455/auth/callback` (outra porta livre se a 1455 estiver ocupada); `resource=https://api.openai.com/v1`. |
| Validação | `state`, assinatura RS256 do ID token (JWKS), issuer, audience, expiração, nonce e escopo `chatgpt.tokens.use.direct`. Qualquer falha descarta tudo (nada é gravado). |
| Inferência | Só `POST https://api.openai.com/v1/responses` com `store: false` e `stream: true`, lido até `response.completed`. `system` vira `instructions`; o histórico vai em `input`; não se envia `temperature`, `max_output_tokens`, `top_p` nem `previous_response_id`. |
| Modelos | `GET /v1/models`, só `visibility == "list"`. |
| Renovação | O access token (1 h) é renovado sozinho 2 min antes de vencer, ou após um 401 (uma tentativa). O refresh token (30 dias) **rotaciona**: o novo é gravado a cada renovação. Refresh inválido apaga os tokens e pede `/login codex` (sem laço). |
| Erros | Consentimento negado/escopo ausente/conta não elegível: explica e oferece a chave de API. `subscription_sharing_usage_limit_exceeded` (429): pausa e aponta `chatgpt.com/settings/usage` (a sessão continua válida). |

**Armazenamento:** `~/.terminas/chatgpt.json` (0600 / ACL do usuário) com `client_id`, `ext_agent_host_id` e os tokens. O `/logout codex` apaga os tokens, mas mantém `client_id` e `ext_agent_host_id` (não dão acesso e o registro precisa deles). Tokens (JWT) nunca vão a tela, histórico, memória ou auditoria: `redact` e `detect_secret` reconhecem JWT, e a tela de `/status` mostra só "plano ChatGPT".

**Limitações:**
- **SSH / VM / contêiner:** o callback é em `127.0.0.1` da máquina onde o Termina roda. Em SSH, redirecione a porta (`ssh -L 1455:127.0.0.1:1455 usuario@host`) e abra o link no navegador local; sem isso o login expira em 5 minutos.
- Apps pagos ou hospedados remotamente seguem outro processo da OpenAI; o Termina é local e MIT.
- As diretrizes de interface da OpenAI falam em "Continue with ChatGPT"; não está claro se valem para terminais.
- Recurso em preview: endpoints e regras podem mudar.
