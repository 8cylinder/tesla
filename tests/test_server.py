import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from tesla.cli import cli
from tesla.config import (
    Config,
    ConfigError,
    load_config,
    save_config,
    set_dotted,
)
from tesla.server import (
    ServerError,
    container_info,
    format_uptime,
    split_image_tag,
    validate_url,
)


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def combined(result):
    return result.output + (result.stderr or "")


def inspect_payload(**state):
    payload = {
        "Name": "/tesla-app",
        "State": {
            "Status": "running",
            "StartedAt": "2026-11-08T04:51:00.123456789Z",
            "ExitCode": 0,
        },
        "Config": {
            "Image": "tesla-app:0.1.0-ec0f267",
            "Entrypoint": ["tesla"],
            "Cmd": ["powerwall", "notify", "--level", "90"],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/home/tether/tesla/config",
                "Destination": "/config",
                "Mode": "rw",
                "RW": True,
            }
        ],
    }
    payload["State"].update(state)
    return json.dumps([payload])


class FakeRun:
    def __init__(
        self,
        stdout="",
        stderr="",
        returncode=0,
        error=None,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.error = error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(
            argv, self.returncode, self.stdout, self.stderr
        )


def test_server_help_lists_commands(config_dir):
    result = invoke("server", "--help")
    assert result.exit_code == 0
    assert "info" in result.output
    assert "logs" in result.output


def test_info_renders_container_details(config_dir, monkeypatch):
    fake = FakeRun(stdout=inspect_payload())
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "info")

    assert result.exit_code == 0, combined(result)
    output = combined(result)
    assert "tesla-app" in output
    assert "running" in output
    assert "0.1.0-ec0f267" in output
    assert "powerwall notify --level 90" in output
    assert "/home/tether/tesla/config" in output
    assert "/config" in output
    assert fake.calls[0][0][3] == "beelink.local"


def test_info_uses_configured_url(config_dir, monkeypatch):
    (config_dir / "config.toml").write_text('[server]\nurl = "beelink.local"\n')
    fake = FakeRun(stdout=inspect_payload())
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "info")

    assert result.exit_code == 0, combined(result)
    assert fake.calls[0][0][3] == "beelink.local"


def test_url_flag_overrides_config(config_dir, monkeypatch):
    (config_dir / "config.toml").write_text(
        '[server]\nurl = "configured.local"\n'
    )
    fake = FakeRun(stdout=inspect_payload())
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "flagged.local", "info")

    assert result.exit_code == 0, combined(result)
    assert fake.calls[0][0][3] == "flagged.local"


def test_info_without_url_exits_2(config_dir):
    result = invoke("server", "info")

    assert result.exit_code == 2
    assert "no server URL configured" in combined(result)


def test_url_bypasses_invalid_local_config(config_dir, monkeypatch):
    (config_dir / "config.toml").write_text('region = "mars"\n')
    fake = FakeRun(stdout=inspect_payload())
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "info")

    assert result.exit_code == 0, combined(result)


def test_info_reports_stopped_container(config_dir, monkeypatch):
    fake = FakeRun(
        stdout=inspect_payload(
            Status="exited",
            StartedAt="0001-01-01T00:00:00Z",
            ExitCode=1,
        )
    )
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "info")

    assert result.exit_code == 0, combined(result)
    output = combined(result)
    assert "exited (exit 1)" in output
    assert "not running" in output


def test_info_reports_ssh_failure(config_dir, monkeypatch):
    fake = FakeRun(
        returncode=255,
        stderr="ssh: Could not resolve hostname beelink.local",
    )
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "info")

    assert result.exit_code == 3
    assert "cannot reach beelink.local" in combined(result)


def test_info_reports_missing_container(config_dir, monkeypatch):
    fake = FakeRun(returncode=1, stderr="Error: No such object: tesla-app")
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "info")

    assert result.exit_code == 3
    assert "container 'tesla-app' not found" in combined(result)


def test_info_reports_unexpected_output(config_dir, monkeypatch):
    fake = FakeRun(stdout="not json")
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "info")

    assert result.exit_code == 3
    assert "unexpected response" in combined(result)


def test_info_reports_missing_ssh(config_dir, monkeypatch):
    fake = FakeRun(error=FileNotFoundError())
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "info")

    assert result.exit_code == 3
    assert "ssh not found" in combined(result)


def test_logs_builds_following_command(config_dir, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke(
        "server",
        "--url",
        "beelink.local",
        "logs",
        "-f",
        "--tail",
        "50",
    )

    assert result.exit_code == 0, combined(result)
    assert fake.calls[0][0] == [
        "ssh",
        "-o",
        "ConnectTimeout=10",
        "beelink.local",
        "docker logs --follow --tail 50 tesla-app",
    ]


def test_logs_default_tail(config_dir, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "logs")

    assert result.exit_code == 0, combined(result)
    assert fake.calls[0][0][-1] == "docker logs --tail 200 tesla-app"


def test_logs_honors_short_tail_flag(config_dir, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "logs", "-t", "10")

    assert result.exit_code == 0, combined(result)
    assert fake.calls[0][0][-1] == "docker logs --tail 10 tesla-app"


def test_logs_ctrl_c_exits_130(config_dir, monkeypatch):
    fake = FakeRun(error=KeyboardInterrupt())
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "logs")

    assert result.exit_code == 130


def test_logs_propagates_remote_exit(config_dir, monkeypatch):
    fake = FakeRun(returncode=1)
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "logs")

    assert result.exit_code == 1


def test_logs_reports_ssh_failure(config_dir, monkeypatch):
    fake = FakeRun(returncode=255)
    monkeypatch.setattr("tesla.server.subprocess.run", fake)

    result = invoke("server", "--url", "beelink.local", "logs")

    assert result.exit_code == 3
    assert "cannot reach beelink.local" in combined(result)


def test_format_uptime_days():
    started = datetime(2026, 11, 8, 4, 0, tzinfo=UTC)
    now = started + timedelta(days=3, hours=4, minutes=12)
    assert format_uptime(started, now=now) == "3d 4h"


def test_format_uptime_minutes():
    started = datetime(2026, 11, 8, 4, 0, tzinfo=UTC)
    now = started + timedelta(minutes=12, seconds=5)
    assert format_uptime(started, now=now) == "12m 5s"


def test_format_uptime_unknown():
    assert format_uptime(None) == "unknown"


def test_split_image_tag():
    assert split_image_tag("tesla-app:0.1.0-ec0f267") == (
        "tesla-app",
        "0.1.0-ec0f267",
    )
    assert split_image_tag("registry:5000/tesla-app") == (
        "registry:5000/tesla-app",
        "",
    )
    assert split_image_tag("tesla-app@sha256:abc") == (
        "tesla-app",
        "sha256:abc",
    )


def test_container_info_parses_docker_timestamp():
    info = container_info(json.loads(inspect_payload())[0])
    assert info.started_at is not None
    assert info.started_at.year == 2026


@pytest.mark.parametrize("url", ["", "  ", "-oProxyCommand=bad", "two words"])
def test_validate_url_rejects(url):
    with pytest.raises(ServerError):
        validate_url(url)


def test_server_config_round_trip(config_dir):
    config = Config()
    config.server.url = "beelink.local"
    save_config(config)
    assert load_config().server.url == "beelink.local"


def test_server_url_set_dotted():
    config = Config()
    set_dotted(config, "server.url", "beelink.local")
    assert config.server.url == "beelink.local"


def test_server_url_empty_rejected():
    with pytest.raises(ConfigError, match="server.url"):
        set_dotted(Config(), "server.url", "")
