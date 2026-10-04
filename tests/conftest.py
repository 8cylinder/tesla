import pytest


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TESLA_CONFIG_DIR", str(tmp_path))
    return tmp_path
