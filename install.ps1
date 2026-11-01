# Termina's Agent - instalador para Windows (PowerShell 5.1 ou 7).
#
#   irm https://raw.githubusercontent.com/guilhermetft/terminas-agent/main/install.ps1 | iex
#
# O que faz: instala o "uv" (se faltar) e usa o uv para instalar o Termina como ferramenta
# isolada. O uv baixa o Python se precisar, entao nao exige Python nem git instalados,
# nao pede administrador e nao altera a ExecutionPolicy. Reexecutar o comando atualiza.
#
# Opcoes (variaveis de ambiente; parametros so valem ao rodar o arquivo ou um scriptblock):
#   TERMINAS_VERSION       tag, branch ou commit a instalar          (padrao: main)
#   TERMINAS_EXTRAS        extras opcionais: rag,mcp ou none         (padrao: rag,mcp)
#   TERMINAS_UNINSTALL=1   remove o Termina (seus dados em .terminas\ ficam)
#   TERMINAS_PYTHON        versao do Python usada pelo Termina       (padrao: 3.12)
#   TERMINAS_SOURCE        URL ou pasta local no lugar do GitHub     (para testes)
#   TERMINAS_NO_MODIFY_PATH=1   nao altera o PATH do usuario         (para testes)
#
# Exemplo com parametros:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/guilhermetft/terminas-agent/main/install.ps1))) -Uninstall

param(
    [string]$Version = $env:TERMINAS_VERSION,
    [string]$Extras = $env:TERMINAS_EXTRAS,
    [string]$PythonVersion = $env:TERMINAS_PYTHON,
    [string]$Source = $env:TERMINAS_SOURCE,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

function Install-Terminas {
    param(
        [string]$Version,
        [string]$Extras,
        [string]$PythonVersion,
        [string]$Source,
        [bool]$Uninstall
    )

    $repo = 'guilhermetft/terminas-agent'
    $package = 'terminas-agent'
    $commandName = 'terminas'

    if (-not $Version) { $Version = 'main' }
    if (-not $PythonVersion) { $PythonVersion = '3.12' }
    if ($null -eq $Extras -or $Extras -eq '') { $Extras = 'rag,mcp' }
    if ($Extras -eq 'none') { $Extras = '' }
    if ($env:TERMINAS_UNINSTALL -eq '1') { $Uninstall = $true }

    # --- validacoes de entrada (os valores vao para a linha de comando do uv) -----------
    $onWindows = ($env:OS -eq 'Windows_NT')
    if (-not $onWindows) {
        throw "Este instalador e para Windows. No Linux/macOS use: curl -fsSL https://raw.githubusercontent.com/$repo/main/install.sh | bash"
    }
    if ($Version -notmatch '^[A-Za-z0-9._/-]+$') { throw "versao invalida: '$Version' (use tag, branch ou commit)" }
    if ($Extras -notmatch '^[a-z0-9,_-]*$') { throw "extras invalidos: '$Extras' (ex.: rag,mcp ou none)" }
    if ($PythonVersion -notmatch '^[0-9.]+$') { throw "TERMINAS_PYTHON invalido: '$PythonVersion' (ex.: 3.12)" }

    # --- uv ----------------------------------------------------------------------------
    if ($env:UV_INSTALL_DIR) { $uvDir = $env:UV_INSTALL_DIR } else { $uvDir = Join-Path $env:USERPROFILE '.local\bin' }

    function Find-Uv {
        $found = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { return $found.Source }
        $guess = Join-Path $uvDir 'uv.exe'
        if (Test-Path -LiteralPath $guess) { return $guess }
        return $null
    }

    $uv = Find-Uv

    if ($Uninstall) {
        if (-not $uv) { throw 'uv nao encontrado: nada a desinstalar por aqui' }
        & $uv tool uninstall $package
        if ($LASTEXITCODE -ne 0) { throw "falha ao desinstalar $package (ele esta instalado?)" }
        Write-Host "Termina's Agent removido. Seus dados (.terminas\) nao foram apagados."
        return
    }

    if (-not $uv) {
        Write-Host 'Instalando o uv (gerenciador de Python)...'
        try {
            Invoke-RestMethod 'https://astral.sh/uv/install.ps1' | Invoke-Expression
        } catch {
            throw "falha ao instalar o uv (sem internet?): $($_.Exception.Message)"
        }
        $uv = Find-Uv
        if (-not $uv) { throw "o uv foi instalado, mas nao foi encontrado em $uvDir; abra um novo terminal e rode de novo" }
    }

    # --- instalacao --------------------------------------------------------------------
    if ($Source) {
        if (Test-Path -LiteralPath $Source -PathType Container) {
            $url = ([Uri](Resolve-Path -LiteralPath $Source).ProviderPath).AbsoluteUri
        } else {
            $url = $Source
        }
    } else {
        # Arquivo .zip do GitHub: nao exige git instalado.
        $url = "https://github.com/$repo/archive/$Version.zip"
    }
    if ($Extras) { $spec = "$package[$Extras] @ $url" } else { $spec = "$package @ $url" }

    Write-Host "Instalando o Termina's Agent ($Version) com Python $PythonVersion..."
    & $uv tool install --force --reinstall --python $PythonVersion $spec
    if ($LASTEXITCODE -ne 0) { throw "falha ao instalar o Termina's Agent (veja a mensagem do uv acima)" }

    # --- PATH e verificacao ------------------------------------------------------------
    $binDir = (& $uv tool dir --bin | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $binDir) { throw 'nao consegui descobrir a pasta de executaveis do uv' }

    $onPath = (($env:Path -split ';') -contains $binDir)
    if ($env:TERMINAS_NO_MODIFY_PATH -ne '1') {
        try { & $uv tool update-shell *> $null } catch { Write-Host 'AVISO: nao consegui atualizar o PATH do usuario automaticamente' }
    }
    if (-not $onPath) { $env:Path = "$binDir;$env:Path" }

    $exe = Join-Path $binDir "$commandName.exe"
    $installed = (& $exe --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "instalei, mas '$commandName --version' falhou: $installed" }

    Write-Host ''
    Write-Host "Pronto: $installed"
    Write-Host ''
    Write-Host 'Proximos passos:'
    if (-not $onPath) { Write-Host '  1. Abra um NOVO terminal (para o PATH atualizar).' } else { Write-Host '  1. (o comando ja esta disponivel neste terminal)' }
    Write-Host "  2. Rode: $commandName"
    Write-Host '  3. Para usar um LLM, defina a chave do provedor (ex.: $env:GEMINI_API_KEY ou $env:ANTHROPIC_API_KEY).'
    Write-Host '     Use /help dentro do terminal para ver os comandos.'
    Write-Host ''
    Write-Host 'Atualizar: rode o mesmo comando de instalacao de novo. Remover: TERMINAS_UNINSTALL=1 (ou -Uninstall).'
}

$failed = $false
try {
    Install-Terminas -Version $Version -Extras $Extras -PythonVersion $PythonVersion -Source $Source -Uninstall ([bool]$Uninstall)
} catch {
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
    $failed = $true
}

if ($failed) {
    # "exit" dentro de "irm | iex" fecharia a janela do usuario (ou o script que chamou o iex).
    # So sai de verdade quando o arquivo em execucao e o proprio install.ps1.
    $self = $null
    try { $self = $MyInvocation.MyCommand.Path } catch { }
    if ($self -and ((Split-Path -Leaf $self) -eq 'install.ps1')) { exit 1 } else { $global:LASTEXITCODE = 1 }
}
