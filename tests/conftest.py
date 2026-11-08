from pathlib import Path

import platformdirs
import pytest


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = Path(platformdirs.user_config_dir("tesla"))
    path.mkdir(parents=True, exist_ok=True)
    return path
