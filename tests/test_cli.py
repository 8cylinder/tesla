from click.testing import CliRunner

from tesla import __version__
from tesla.cli import cli


def invoke(config_dir, *args, env=None):
    runner = CliRunner()
    full_env = {"TESLA_CONFIG_DIR": str(config_dir)}
    if env:
        full_env.update(env)
    return runner.invoke(cli, list(args), env=full_env)


def combined(result):
    return result.output + (result.stderr or "")


def test_version(config_dir):
    result = invoke(config_dir, "--version")
    assert result.exit_code == 0
    assert __version__ in result.output


def test_config_show_defaults(config_dir):
    result = invoke(config_dir, "config", "show")
    assert result.exit_code == 0
    assert "[auto_charge]" in result.output
    assert "threshold = 100.0" in result.output


def test_config_set_then_show(config_dir):
    result = invoke(config_dir, "config", "set", "auto_charge.threshold", "90")
    assert result.exit_code == 0
    result = invoke(config_dir, "config", "show")
    assert "threshold = 90.0" in result.output


def test_config_set_unknown_key_exits_2(config_dir):
    result = invoke(config_dir, "config", "set", "nope", "1")
    assert result.exit_code == 2
    assert "unknown key" in combined(result)


def test_config_set_invalid_value_exits_2(config_dir):
    result = invoke(config_dir, "config", "set", "auto_charge.threshold", "150")
    assert result.exit_code == 2


def test_auth_status_without_tokens_exits_2(config_dir):
    result = invoke(config_dir, "auth", "status")
    assert result.exit_code == 2
    assert "tesla auth login" in combined(result)


def test_products_without_tokens_exits_2(config_dir):
    result = invoke(config_dir, "products")
    assert result.exit_code == 2
    assert "tesla auth login" in combined(result)


def test_auth_login_missing_credentials_exits_2(config_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    result = invoke(config_dir, "auth", "login")
    assert result.exit_code == 2
    assert "TESLA_CLIENT_ID" in combined(result)
