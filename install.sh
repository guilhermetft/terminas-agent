#!/usr/bin/env bash
# Termina's Agent - instalador para Linux e macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/guilhermetft/terminas-agent/main/install.sh | bash
#
# O que faz: instala o "uv" (se faltar) e usa o uv para instalar o Termina como ferramenta
# isolada. O uv baixa o Python se precisar, entao nao exige Python nem git instalados,
# nao usa sudo e nao mexe no Python do sistema. Reexecutar o comando atualiza.
#
# Opcoes (flags ou variaveis de ambiente):
#   --version REF    TERMINAS_VERSION   tag, branch ou commit a instalar   (padrao: main)
#   --extras LISTA   TERMINAS_EXTRAS    extras opcionais: rag,mcp ou none  (padrao: rag,mcp)
#   --uninstall      TERMINAS_UNINSTALL=1   remove o Termina (seus dados em .terminas/ ficam)
#   TERMINAS_PYTHON  versao do Python usada pelo Termina                   (padrao: 3.12)
#   TERMINAS_SOURCE  URL ou pasta local a instalar no lugar do GitHub      (para testes)
#
# Com o pipe, flags vao assim: curl -fsSL <url> | bash -s -- --uninstall

set -euo pipefail

REPO="guilhermetft/terminas-agent"
PACKAGE="terminas-agent"
COMMAND_NAME="terminas"
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"

VERSION="${TERMINAS_VERSION:-main}"
EXTRAS="${TERMINAS_EXTRAS-rag,mcp}"
PYTHON_VERSION="${TERMINAS_PYTHON:-3.12}"
SOURCE="${TERMINAS_SOURCE:-}"
UNINSTALL="${TERMINAS_UNINSTALL:-}"

say() { printf '%s\n' "$*"; }
warn() { printf 'AVISO: %s\n' "$*" >&2; }
die() { printf 'ERRO: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Termina's Agent - instalador (Linux e macOS)

  curl -fsSL https://raw.githubusercontent.com/guilhermetft/terminas-agent/main/install.sh | bash

Opcoes (flags ou variaveis de ambiente):
  --version REF    TERMINAS_VERSION       tag, branch ou commit (padrao: main)
  --extras LISTA   TERMINAS_EXTRAS        extras opcionais: rag,mcp ou none (padrao: rag,mcp)
  --uninstall      TERMINAS_UNINSTALL=1   remove o Termina (seus dados em .terminas/ ficam)
  TERMINAS_PYTHON  versao do Python do Termina (padrao: 3.12)
  TERMINAS_SOURCE  URL ou pasta local a instalar no lugar do GitHub (para testes)
  TERMINAS_NO_MODIFY_PATH=1   nao altera o PATH do shell (para testes)

Com o pipe, as flags vao assim: curl -fsSL <url> | bash -s -- --uninstall
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --version) [ $# -ge 2 ] || die "--version exige um valor"; VERSION="$2"; shift 2 ;;
    --extras) [ $# -ge 2 ] || die "--extras exige um valor (ex.: rag,mcp)"; EXTRAS="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "opcao desconhecida: $1 (use --help)" ;;
  esac
done

[ "$EXTRAS" != "none" ] || EXTRAS=""

# --- validacoes de entrada (valores vao para a linha de comando do uv) -------------------
case "$(uname -s 2>/dev/null || echo desconhecido)" in
  Linux|Darwin) ;;
  MINGW*|MSYS*|CYGWIN*) die "no Windows use o install.ps1: irm https://raw.githubusercontent.com/$REPO/main/install.ps1 | iex" ;;
  *) die "sistema nao suportado: $(uname -s 2>/dev/null || echo desconhecido) (suportados: Linux e macOS)" ;;
esac
case "$VERSION" in
  ""|*[!A-Za-z0-9._/-]*) die "versao invalida: '$VERSION' (use tag, branch ou commit)" ;;
esac
case "$EXTRAS" in
  *[!a-z0-9,_-]*) die "extras invalidos: '$EXTRAS' (ex.: rag,mcp)" ;;
esac
case "$PYTHON_VERSION" in
  ""|*[!0-9.]*) die "TERMINAS_PYTHON invalido: '$PYTHON_VERSION' (ex.: 3.12)" ;;
esac

# --- uv ---------------------------------------------------------------------------------
UV_BIN_GUESS="${UV_INSTALL_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}"

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
  elif [ -x "$UV_BIN_GUESS/uv" ]; then
    printf '%s\n' "$UV_BIN_GUESS/uv"
  else
    return 1
  fi
}

UV="$(find_uv || true)"

if [ -n "$UNINSTALL" ]; then
  [ -n "$UV" ] || die "uv nao encontrado: nada a desinstalar por aqui"
  "$UV" tool uninstall "$PACKAGE" || die "falha ao desinstalar $PACKAGE (ele esta instalado?)"
  say "Termina's Agent removido. Seus dados (.terminas/) nao foram apagados."
  exit 0
fi

if [ -z "$UV" ]; then
  command -v curl >/dev/null 2>&1 || die "curl nao encontrado; instale o curl e rode de novo"
  say "Instalando o uv (gerenciador de Python)..."
  curl -LsSf "$UV_INSTALLER_URL" | sh || die "falha ao instalar o uv (sem internet?)"
  UV="$(find_uv || true)"
  [ -n "$UV" ] || die "o uv foi instalado, mas nao foi encontrado em $UV_BIN_GUESS; abra um novo terminal e rode de novo"
fi

# --- instalacao -------------------------------------------------------------------------
if [ -n "$SOURCE" ]; then
  if [ -d "$SOURCE" ]; then
    SOURCE="file://$(cd "$SOURCE" && pwd)"
  fi
  URL="$SOURCE"
else
  # Arquivo .zip do GitHub: nao exige git instalado.
  URL="https://github.com/$REPO/archive/$VERSION.zip"
fi
SPEC="$PACKAGE"
[ -z "$EXTRAS" ] || SPEC="$PACKAGE[$EXTRAS]"
SPEC="$SPEC @ $URL"

say "Instalando o Termina's Agent ($VERSION) com Python $PYTHON_VERSION..."
"$UV" tool install --force --reinstall --python "$PYTHON_VERSION" "$SPEC" \
  || die "falha ao instalar o Termina's Agent (veja a mensagem do uv acima)"

# --- PATH e verificacao -----------------------------------------------------------------
BIN_DIR="$("$UV" tool dir --bin)" || die "nao consegui descobrir a pasta de executaveis do uv"
if [ "${TERMINAS_NO_MODIFY_PATH:-}" != "1" ]; then
  "$UV" tool update-shell >/dev/null 2>&1 || warn "nao consegui atualizar o PATH do seu shell automaticamente"
fi
ON_PATH=""
case ":$PATH:" in *":$BIN_DIR:"*) ON_PATH=1 ;; esac
export PATH="$BIN_DIR:$PATH"

INSTALLED="$("$BIN_DIR/$COMMAND_NAME" --version 2>&1)" \
  || die "instalei, mas '$COMMAND_NAME --version' falhou: $INSTALLED"

say ""
say "Pronto: $INSTALLED"
say ""
say "Proximos passos:"
if [ -z "$ON_PATH" ]; then
  say "  1. Abra um NOVO terminal (para o PATH atualizar)."
else
  say "  1. (o comando ja esta disponivel neste terminal)"
fi
say "  2. Rode: $COMMAND_NAME"
say "  3. Para usar um LLM, exporte a chave do provedor (ex.: GEMINI_API_KEY ou ANTHROPIC_API_KEY)."
say "     Use /help dentro do terminal para ver os comandos."
say ""
say "Atualizar: rode o mesmo comando de instalacao de novo. Remover: ... | bash -s -- --uninstall"
