import contextlib
import stat
import tomllib
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner
from tesla_fleet_api.exceptions import VehicleOffline

from tesla import __version__
from tesla.cli import _estimate_eta, _format_clock_time, _notify_on_level, cli
from tesla.tokens import Tokens, load_tokens, save_tokens


def invoke(config_dir, *args, env=None):
    runner = CliRunner()
    return runner.invoke(cli, list(args), env=env)


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


def test_auth_login_missing_credentials_exits_2(
    config_dir, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    for name in (
        "TESLA_CLIENT_ID",
        "TESLA_CLIENT_SECRET",
        "TESLA_REDIRECT_URI",
    ):
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


def test_key_register_without_credentials_exits_2(
    config_dir, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    for name in (
        "TESLA_CLIENT_ID",
        "TESLA_CLIENT_SECRET",
        "TESLA_REDIRECT_URI",
    ):
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
    result = invoke(
        config_dir, "config", "set", "powerwall.energy_site_id", "123"
    )
    assert result.exit_code == 0
    result = invoke(config_dir, "powerwall", "status")
    assert result.exit_code == 2
    assert "tesla auth login" in combined(result)


def test_powerwall_notify_without_site_id_exits_2(config_dir):
    result = invoke(config_dir, "powerwall", "notify")
    assert result.exit_code == 2
    assert "energy_site_id" in combined(result)


def test_powerwall_notify_disabled_exits_2(config_dir):
    assert (
        invoke(
            config_dir, "config", "set", "powerwall.energy_site_id", "123"
        ).exit_code
        == 0
    )
    result = invoke(config_dir, "powerwall", "notify")
    assert result.exit_code == 2
    assert "disabled" in combined(result)


def test_powerwall_notify_missing_topic_exits_2(
    config_dir, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert (
        invoke(
            config_dir, "config", "set", "powerwall.energy_site_id", "123"
        ).exit_code
        == 0
    )
    assert (
        invoke(config_dir, "config", "set", "notify.enabled", "true").exit_code
        == 0
    )
    monkeypatch.delenv("TESLA_NTFY_TOPIC", raising=False)
    result = invoke(config_dir, "powerwall", "notify")
    assert result.exit_code == 2
    assert "TESLA_NTFY_TOPIC" in combined(result)


def test_powerwall_notify_without_tokens_exits_2(config_dir):
    assert (
        invoke(
            config_dir, "config", "set", "powerwall.energy_site_id", "123"
        ).exit_code
        == 0
    )
    assert (
        invoke(config_dir, "config", "set", "notify.enabled", "true").exit_code
        == 0
    )
    result = invoke(
        config_dir,
        "powerwall",
        "notify",
        env={"TESLA_NTFY_TOPIC": "test-topic"},
    )
    assert result.exit_code == 2
    assert "tesla auth login" in combined(result)


class _SourceExhausted(Exception):
    pass


def level_source(values):
    pending = list(values)

    async def source():
        if not pending:
            raise _SourceExhausted
        return pending.pop(0)

    return source


class FakeLevelNotifier:
    def __init__(self, result=True):
        self.result = result
        self.sent = []

    async def send(self, stage, message, **kwargs):
        self.sent.append((stage, message))
        return self.result


async def _no_sleep(_seconds):
    return None


async def test_notify_on_level_sends_once_per_cycle():
    notifier = FakeLevelNotifier()
    with pytest.raises(_SourceExhausted):
        await _notify_on_level(
            notifier,
            level_source([50.0, 100.0, 100.0, 99.0, 100.0]),
            level=100.0,
            interval=1,
            sleep=_no_sleep,
        )
    assert [message for _, message in notifier.sent] == [
        "Powerwall reached 100%",
        "Powerwall reached 100%",
    ]


async def test_notify_on_level_never_sends_below_level():
    notifier = FakeLevelNotifier()
    with pytest.raises(_SourceExhausted):
        await _notify_on_level(
            notifier,
            level_source([10.0, 50.0, 99.9]),
            level=100.0,
            interval=1,
            sleep=_no_sleep,
        )
    assert notifier.sent == []


async def test_notify_on_level_ignores_unavailable_readings():
    notifier = FakeLevelNotifier()
    with pytest.raises(_SourceExhausted):
        await _notify_on_level(
            notifier,
            level_source([None, 100.0, None]),
            level=100.0,
            interval=1,
            sleep=_no_sleep,
        )
    assert len(notifier.sent) == 1


async def test_notify_on_level_retries_failed_send():
    notifier = FakeLevelNotifier(result=False)
    with pytest.raises(_SourceExhausted):
        await _notify_on_level(
            notifier,
            level_source([100.0, 100.0]),
            level=90.0,
            interval=1,
            sleep=_no_sleep,
        )
    assert len(notifier.sent) == 2


async def test_notify_on_level_polls_hourly_after_notification():
    notifier = FakeLevelNotifier()
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    with pytest.raises(_SourceExhausted):
        await _notify_on_level(
            notifier,
            level_source([50.0, 100.0, 100.0, 99.0]),
            level=100.0,
            interval=300,
            sleep=sleep,
        )
    assert sleeps == [300, 3600, 3600, 300]


async def test_notify_on_level_logs_poll_interval_changes(caplog):
    notifier = FakeLevelNotifier()
    with (
        caplog.at_level("INFO", logger="tesla.cli"),
        pytest.raises(_SourceExhausted),
    ):
        await _notify_on_level(
            notifier,
            level_source([50.0, 100.0, 99.0]),
            level=100.0,
            interval=300,
            sleep=_no_sleep,
        )
    messages = [record.getMessage() for record in caplog.records]
    assert "poll interval slowed to 3600 s until the next cycle" in messages
    assert "poll interval back to 300 s" in messages


async def test_notify_on_level_recalculates_eta_after_new_cycle(caplog):
    notifier = FakeLevelNotifier()
    now = 1_000_000.0
    pending = [50.0, 51.0, 90.0, 50.0, 51.0, 52.0]

    async def source():
        if not pending:
            raise _SourceExhausted
        return pending.pop(0)

    def clock():
        return now

    async def sleep(seconds):
        nonlocal now
        now += seconds

    with (
        caplog.at_level("INFO", logger="tesla.cli"),
        pytest.raises(_SourceExhausted),
    ):
        await _notify_on_level(
            notifier,
            source,
            level=90.0,
            interval=300,
            sleep=sleep,
            clock=clock,
        )
    messages = [record.getMessage() for record in caplog.records]
    eta = _format_clock_time(1_016_200.0)
    assert f"waiting for 90%; eta: {eta}" in messages[-1]


def test_estimate_eta_linear():
    history = [(0.0, 50.0), (300.0, 51.0), (600.0, 52.0)]
    assert _estimate_eta(history, 90.0) == pytest.approx(12000.0)
    assert _estimate_eta(history, 52.0) is None
    assert _estimate_eta(history, 51.0) is None


def test_estimate_eta_needs_rise():
    assert _estimate_eta([], 90.0) is None
    assert _estimate_eta([(0.0, 50.0)], 90.0) is None
    assert _estimate_eta([(0.0, 60.0), (300.0, 59.0)], 90.0) is None
    assert _estimate_eta([(0.0, 50.0), (300.0, 50.0)], 90.0) is None


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 9, 13, 10), "1:10pm"),
        (datetime(2026, 10, 9, 1, 5), "1:05am"),
        (datetime(2026, 10, 9, 0, 5), "12:05am"),
        (datetime(2026, 10, 9, 12, 0), "12:00pm"),
    ],
)
def test_format_clock_time(moment, expected):
    assert _format_clock_time(moment.timestamp()) == expected


async def test_notify_on_level_logs_eta(caplog):
    notifier = FakeLevelNotifier()
    now = 1_000_000.0
    pending = [50.0, 51.0, 52.0]

    async def source():
        if not pending:
            raise _SourceExhausted
        return pending.pop(0)

    def clock():
        return now

    async def sleep(seconds):
        nonlocal now
        now += seconds

    with (
        caplog.at_level("INFO", logger="tesla.cli"),
        pytest.raises(_SourceExhausted),
    ):
        await _notify_on_level(
            notifier,
            source,
            level=90.0,
            interval=300,
            sleep=sleep,
            clock=clock,
        )
    messages = [record.getMessage() for record in caplog.records]
    eta = _format_clock_time(1_012_000.0)
    assert f"waiting for 90%; eta: {eta}" in messages[-1]


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


class FakeVehicle:
    def __init__(self, results):
        self.results = list(results)
        self.wakes = 0
        self.wake_timeouts = []

    async def vehicle_data(self, endpoints=None):
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def wake_up(self, wait=False, timeout=None):
        self.wakes += 1
        self.wake_timeouts.append(timeout)
        return {"response": {"state": "online"}}


class FakeVehicles:
    def __init__(self, vehicle):
        self._vehicle = vehicle

    def createFleet(self, vin):
        return self._vehicle


class FakeOAuth:
    def __init__(self, vehicle):
        self.vehicles = FakeVehicles(vehicle)

    async def check_access_token(self):
        return None


CAR_DATA = {
    "response": {
        "charge_state": {
            "battery_level": 55,
            "battery_range": 180.5,
            "charging_state": "Stopped",
            "charge_limit_soc": 80,
        }
    }
}


def patch_oauth(monkeypatch, vehicle):
    @contextlib.asynccontextmanager
    async def fake_oauth_client(**kwargs):
        yield FakeOAuth(vehicle)

    monkeypatch.setattr("tesla.cli.oauth_client", fake_oauth_client)


def car_status_env():
    return {
        "TESLA_CLIENT_ID": "id",
        "TESLA_CLIENT_SECRET": "secret",
        "TESLA_REDIRECT_URI": "http://localhost:8085/callback",
    }


def prepare_car_status(config_dir, wake_timeout=None):
    assert (
        invoke(config_dir, "config", "set", "vehicle.vin", "5YJTEST").exit_code
        == 0
    )
    assert (
        invoke(
            config_dir, "config", "set", "vehicle.command_mode", "fleet"
        ).exit_code
        == 0
    )
    if wake_timeout is not None:
        result = invoke(
            config_dir,
            "config",
            "set",
            "auto_charge.wake_timeout_seconds",
            str(wake_timeout),
        )
        assert result.exit_code == 0
    save_tokens(Tokens("refresh", 9999999999))


def test_car_status_wakes_asleep_vehicle(config_dir, monkeypatch):
    prepare_car_status(config_dir, wake_timeout=120)
    vehicle = FakeVehicle([VehicleOffline(), CAR_DATA])
    patch_oauth(monkeypatch, vehicle)
    result = invoke(config_dir, "car", "status", env=car_status_env())
    assert result.exit_code == 0
    assert "waking car" in combined(result)
    assert vehicle.wakes == 1
    assert vehicle.wake_timeouts == [120]
    assert "55%" in result.output


def test_car_status_does_not_wake_awake_vehicle(config_dir, monkeypatch):
    prepare_car_status(config_dir)
    vehicle = FakeVehicle([CAR_DATA])
    patch_oauth(monkeypatch, vehicle)
    result = invoke(config_dir, "car", "status", env=car_status_env())
    assert result.exit_code == 0
    assert vehicle.wakes == 0
    assert "55%" in result.output


def test_auto_charge_without_vin_exits_2(config_dir):
    result = invoke(config_dir, "auto-charge", "--once")
    assert result.exit_code == 2
    assert "VIN" in combined(result)


def test_auto_charge_without_site_id_exits_2(config_dir):
    assert (
        invoke(config_dir, "config", "set", "vehicle.vin", "5YJTEST").exit_code
        == 0
    )
    result = invoke(config_dir, "auto-charge", "--once")
    assert result.exit_code == 2
    assert "energy_site_id" in combined(result)


def test_auto_charge_without_tokens_exits_2(config_dir):
    assert (
        invoke(config_dir, "config", "set", "vehicle.vin", "5YJTEST").exit_code
        == 0
    )
    assert (
        invoke(
            config_dir, "config", "set", "powerwall.energy_site_id", "123"
        ).exit_code
        == 0
    )
    result = invoke(config_dir, "auto-charge", "--once")
    assert result.exit_code == 2
    assert "tesla auth login" in combined(result)


def test_car_charge_limit_out_of_range_exits_2(config_dir):
    result = invoke(config_dir, "car", "charge", "limit", "150")
    assert result.exit_code == 2


def test_car_charge_start_without_tokens_exits_2(config_dir):
    assert (
        invoke(config_dir, "config", "set", "vehicle.vin", "5YJTEST").exit_code
        == 0
    )
    result = invoke(config_dir, "car", "charge", "start")
    assert result.exit_code == 2
    assert "tesla auth login" in combined(result)


def test_doctor_missing_credentials_exits_2(config_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "TESLA_CLIENT_ID",
        "TESLA_CLIENT_SECRET",
        "TESLA_REDIRECT_URI",
    ):
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
    assert (
        invoke(config_dir, "config", "set", "notify.enabled", "true").exit_code
        == 0
    )
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
    assert (
        invoke(config_dir, "config", "set", "notify.enabled", "true").exit_code
        == 0
    )
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


def _command_paths(command, prefix=()):
    paths = [prefix]
    if hasattr(command, "list_commands"):
        import click

        ctx = click.Context(command)
        for name in command.list_commands(ctx):
            child = command.get_command(ctx, name)
            if child is not None and not child.hidden:
                paths.extend(_command_paths(child, prefix + (name,)))
    return paths


def test_config_flag_prints_path_and_creates_nothing(config_dir):
    result = invoke(config_dir, "--config")
    assert result.exit_code == 0
    assert result.output.strip() == str(config_dir / "config.toml")
    assert "\x1b[" not in result.output
    assert not (config_dir / "config.toml").exists()


def test_unknown_config_key_warns(config_dir):
    (config_dir / "config.toml").write_text("bogus = 1\n")
    result = invoke(config_dir, "config", "show")
    assert result.exit_code == 0
    assert "ignoring unknown config key" in combined(result)
    assert "bogus = 1" not in result.output


def test_invalid_toml_exits_and_names_path(config_dir):
    path = config_dir / "config.toml"
    path.write_text("not = [valid\n")
    result = invoke(config_dir, "config", "show")
    assert result.exit_code == 2
    assert "invalid TOML" in combined(result)
    assert "config.tom" in combined(result)


def test_doctor_reports_invalid_config(config_dir):
    (config_dir / "config.toml").write_text("not = [valid\n")
    result = invoke(config_dir, "doctor")
    assert result.exit_code != 0
    assert "config" in result.output
    assert "invalid TOML" in result.output


def test_color_always_emits_ansi(config_dir):
    result = invoke(
        config_dir,
        "--color",
        "always",
        "config",
        "set",
        "auto_charge.threshold",
        "90",
    )
    assert result.exit_code == 0
    assert "\x1b[" in combined(result)


def test_color_never_emits_no_ansi(config_dir):
    result = invoke(
        config_dir,
        "--color",
        "never",
        "config",
        "set",
        "auto_charge.threshold",
        "90",
    )
    assert result.exit_code == 0
    assert "\x1b[" not in combined(result)


def test_color_auto_emits_no_ansi_when_captured(config_dir):
    result = invoke(config_dir, "config", "set", "auto_charge.threshold", "90")
    assert result.exit_code == 0
    assert "\x1b[" not in combined(result)


def test_force_color_emits_ansi(config_dir):
    result = invoke(
        config_dir,
        "config",
        "set",
        "auto_charge.threshold",
        "90",
        env={"FORCE_COLOR": "1", "NO_COLOR": ""},
    )
    assert result.exit_code == 0
    assert "\x1b[" in combined(result)


def test_no_color_suppresses_ansi(config_dir):
    result = invoke(
        config_dir,
        "config",
        "set",
        "auto_charge.threshold",
        "90",
        env={"NO_COLOR": "1", "FORCE_COLOR": ""},
    )
    assert result.exit_code == 0
    assert "\x1b[" not in combined(result)


def test_term_dumb_suppresses_ansi(config_dir):
    result = invoke(
        config_dir,
        "config",
        "set",
        "auto_charge.threshold",
        "90",
        env={"TERM": "dumb", "FORCE_COLOR": ""},
    )
    assert result.exit_code == 0
    assert "\x1b[" not in combined(result)


def test_user_markup_is_escaped(config_dir):
    result = invoke(
        config_dir, "config", "set", "auto_charge.threshold", "[red]high"
    )
    assert result.exit_code == 2
    assert "[red]high" in combined(result)


def test_errors_go_to_stderr(config_dir):
    result = invoke(config_dir, "config", "set", "nope", "1")
    assert result.exit_code == 2
    assert "unknown key" in result.stderr
    assert "unknown key" not in result.stdout


def test_version_matches_pyproject(config_dir):
    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    assert version == __version__


def test_version_output_is_plain(config_dir):
    result = invoke(config_dir, "--version")
    assert result.exit_code == 0
    assert result.output.strip() == f"tesla, version {__version__}"
    assert "\x1b[" not in result.output


def test_help_works_for_every_command(config_dir):
    for path in _command_paths(cli):
        for flag in ("-h", "--help"):
            result = invoke(config_dir, *path, flag)
            assert result.exit_code == 0, (path, flag, result.output)
            assert "Usage:" in result.output
            assert "\x1b[" not in result.output


def test_no_args_shows_help(config_dir):
    result = invoke(config_dir)
    assert result.exit_code == 2
    assert "Usage:" in result.output
