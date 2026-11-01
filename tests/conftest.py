import pytest


@pytest.fixture
def project_root(tmp_path):
    """Diretorio temporario isolado para testes que escrevem em disco."""
    return tmp_path
