import pytest

PROVIDER_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


@pytest.fixture
def project_root(tmp_path):
    """Diretorio temporario isolado para testes que escrevem em disco."""
    return tmp_path


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path_factory, monkeypatch):
    """Nenhum teste toca o ~/.terminas real nem enxerga chaves de API do ambiente do desenvolvedor."""
    monkeypatch.setenv("TERMINAS_HOME", str(tmp_path_factory.mktemp("terminas-home")))
    for name in PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def provider_server():
    """Servidor local que imita Claude, OpenAI e Gemini (sem rede, sem chaves reais)."""
    from tests.fake_providers import fake_provider_server

    with fake_provider_server() as server:
        yield server


@pytest.fixture(autouse=True)
def fast_permissions(request, monkeypatch):
    """No Windows, cada gravacao de credencial chama o icacls (lento). So os testes marcados com
    `real_permissions` exercitam as permissoes de verdade."""
    if request.node.get_closest_marker("real_permissions"):
        return
    monkeypatch.setattr("src.auth.store.restrict_to_owner", lambda *args, **kwargs: None)
