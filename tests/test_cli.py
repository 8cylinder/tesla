import stat

from click.testing import CliRunner

from tesla import __version__
from tesla.cli import cli
from tesla.tokens import Tokens, load_tokens, save_tokens


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


def test_help_short_flag(config_dir):
    result = invoke(config_dir, "-h")
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_subcommand_help_short_flag(config_dir):
    result = invoke(config_dir, "config", "-h")
    assert result.exit_code == 0
    assert "Usage:" in result.output


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


def test_auth_logout_no_token(config_dir):
    result = invoke(config_dir, "auth", "logout")
    assert result.exit_code == 0
    assert "nothing to do" in result.output


def test_auth_logout_removes_token(config_dir):
    save_tokens(Tokens("refresh", 1))
    result = invoke(config_dir, "auth", "logout")
    assert result.exit_code == 0
    assert load_tokens() is None


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


def test_key_generate_creates_private_key(config_dir):
    result = invoke(config_dir, "key", "generate")
    assert result.exit_code == 0
    assert "BEGIN PUBLIC KEY" in result.output
    path = config_dir / "private_key.pem"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_key_generate_pem_only(config_dir):
    result = invoke(config_dir, "key", "generate", "--pem-only")
    assert result.exit_code == 0
    assert result.output.startswith("-----BEGIN PUBLIC KEY-----")


def test_key_register_without_domain_exits_2(config_dir):
    result = invoke(config_dir, "key", "register")
    assert result.exit_code == 2
    assert "no domain" in combined(result)


def test_key_register_without_credentials_exits_2(config_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    result = invoke(config_dir, "key", "register", "--domain", "example.com")
    assert result.exit_code == 2
    assert "TESLA_CLIENT_ID" in combined(result)


def test_key_status_without_key_exits_2(config_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke(config_dir, "config", "set", "domain", "example.com")
    assert result.exit_code == 0
    result = invoke(
        config_dir,
        "key",
        "status",
        env={
            "TESLA_CLIENT_ID": "id",
            "TESLA_CLIENT_SECRET": "secret",
            "TESLA_REDIRECT_URI": "http://localhost:8085/callback",
        },
    )
    assert result.exit_code == 2
    assert "key generate" in combined(result)


def test_powerwall_status_without_site_id_exits_2(config_dir):
    result = invoke(config_dir, "powerwall", "status")
    assert result.exit_code == 2
    assert "energy_site_id" in combined(result)


def test_powerwall_status_without_tokens_exits_2(config_dir):
    result = invoke(config_dir, "config", "set", "powerwall.energy_site_id", "123")
    assert result.exit_code == 0
    result = invoke(config_dir, "powerwall", "status")
    assert result.exit_code == 2
    assert "tesla auth login" in combined(result)


def test_car_status_without_vin_exits_2(config_dir):
    result = invoke(config_dir, "car", "status")
    assert result.exit_code == 2
    assert "VIN" in combined(result)


def test_car_status_without_tokens_exits_2(config_dir):
    result = invoke(config_dir, "config", "set", "vehicle.vin", "5YJTEST")
    assert result.exit_code == 0
    result = invoke(config_dir, "car", "status")
    assert result.exit_code == 2
    assert "tesla auth login" in combined(result)


def test_auto_charge_without_vin_exits_2(config_dir):
    result = invoke(config_dir, "auto-charge", "--once")
    assert result.exit_code == 2
    assert "VIN" in combined(result)


def test_auto_charge_without_site_id_exits_2(config_dir):
    assert invoke(config_dir, "config", "set", "vehicle.vin", "5YJTEST").exit_code == 0
    result = invoke(config_dir, "auto-charge", "--once")
    assert result.exit_code == 2
    assert "energy_site_id" in combined(result)


def test_auto_charge_without_tokens_exits_2(config_dir):
    assert invoke(config_dir, "config", "set", "vehicle.vin", "5YJTEST").exit_code == 0
    assert (
        invoke(config_dir, "config", "set", "powerwall.energy_site_id", "123").exit_code
        == 0
    )
    result = invoke(config_dir, "auto-charge", "--once")
    assert result.exit_code == 2
    assert "tesla auth login" in combined(result)


def test_car_charge_limit_out_of_range_exits_2(config_dir):
    result = invoke(config_dir, "car", "charge", "limit", "150")
    assert result.exit_code == 2


def test_car_charge_start_without_tokens_exits_2(config_dir):
    assert invoke(config_dir, "config", "set", "vehicle.vin", "5YJTEST").exit_code == 0
    result = invoke(config_dir, "car", "charge", "start")
    assert result.exit_code == 2
    assert "tesla auth login" in combined(result)


def test_doctor_missing_credentials_exits_2(config_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    result = invoke(config_dir, "doctor")
    assert result.exit_code == 2
    output = combined(result)
    assert "credentials" in output
    assert "tokens" in output


def test_notify_test_disabled_exits_2(config_dir):
    result = invoke(config_dir, "notify", "test")
    assert result.exit_code == 2
    assert "disabled" in combined(result)


def test_notify_test_missing_topic_exits_2(config_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert invoke(config_dir, "config", "set", "notify.enabled", "true").exit_code == 0
    monkeypatch.delenv("TESLA_NTFY_TOPIC", raising=False)
    result = invoke(config_dir, "notify", "test")
    assert result.exit_code == 2
    assert "TESLA_NTFY_TOPIC" in combined(result)


def test_notify_test_sends(config_dir, monkeypatch):
    class FakeNotifier:
        def __init__(self):
            self.stages = []

        async def send(self, stage, message, **kwargs):
            self.stages.append(stage)
            return True

    notifier = FakeNotifier()
    monkeypatch.setattr("tesla.cli.build_notifier", lambda config: notifier)
    assert invoke(config_dir, "config", "set", "notify.enabled", "true").exit_code == 0
    result = invoke(config_dir, "notify", "test")
    assert result.exit_code == 0
    assert notifier.stages == ["test"]


def test_notify_reset_clears_state(config_dir):
    path = config_dir / "notify_state.json"
    path.write_text('{"threshold": 1.0}')
    result = invoke(config_dir, "notify", "reset")
    assert result.exit_code == 0
    assert not path.exists()
    result = invoke(config_dir, "notify", "reset")
    assert result.exit_code == 0
    assert "No notification state to clear" in result.output
