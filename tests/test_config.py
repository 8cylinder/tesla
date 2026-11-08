import stat

import pytest

from tesla.config import (
    Config,
    ConfigError,
    config_path,
    dump_toml,
    get_dotted,
    key_path,
    load_config,
    save_config,
    set_dotted,
)


def write_config(config_dir, text):
    path = config_dir / "config.toml"
    path.write_text(text)
    return path


def test_defaults_when_missing(config_dir):
    config = load_config()
    assert config.region == "na"
    assert config.vehicle.command_mode == "signed"
    assert config.auto_charge.threshold == 100.0


def test_config_path_uses_platformdirs(config_dir):
    assert config_path() == config_dir / "config.toml"
    assert config_path().is_absolute()


def test_save_config_is_0600(config_dir):
    save_config(Config())
    mode = stat.S_IMODE(config_path().stat().st_mode)
    assert mode == 0o600


def test_relative_key_path_resolves_against_config(config_dir):
    write_config(config_dir, '[vehicle]\nkey_path = "keys/private.pem"\n')
    config = load_config()
    assert key_path(config) == config_dir / "keys" / "private.pem"


def test_absolute_key_path_is_kept(config_dir, tmp_path):
    target = tmp_path / "elsewhere.pem"
    write_config(config_dir, f'[vehicle]\nkey_path = "{target}"\n')
    config = load_config()
    assert key_path(config) == target


def test_default_key_path(config_dir):
    assert key_path(Config()) == config_dir / "private_key.pem"


def test_load_from_file(config_dir):
    write_config(
        config_dir,
        'region = "na"\n'
        "[vehicle]\n"
        'vin = "5YJTEST"\n'
        'command_mode = "fleet"\n'
        "[powerwall]\n"
        "energy_site_id = 123\n"
        "[auto_charge]\n"
        "threshold = 90.5\n",
    )
    config = load_config()
    assert config.vehicle.vin == "5YJTEST"
    assert config.vehicle.command_mode == "fleet"
    assert config.powerwall.energy_site_id == 123
    assert config.auto_charge.threshold == 90.5
    assert config.auto_charge.poll_interval_seconds == 300


def test_unknown_key_warns_and_is_ignored(config_dir):
    write_config(config_dir, "bogus = 1\n")
    unknown = []
    config = load_config(on_unknown=unknown.append)
    assert config.region == "na"
    assert unknown == ["bogus"]


def test_unknown_section_key_warns_and_is_ignored(config_dir):
    write_config(config_dir, '[vehicle]\ncolor = "red"\n')
    unknown = []
    config = load_config(on_unknown=unknown.append)
    assert config.vehicle.command_mode == "signed"
    assert unknown == ["vehicle.color"]


def test_unknown_nested_notify_key_warns_and_is_ignored(config_dir):
    write_config(config_dir, "[notify.pushover]\ncolor = 1\n")
    unknown = []
    config = load_config(on_unknown=unknown.append)
    assert config.notify.pushover.device == ""
    assert unknown == ["notify.pushover.color"]


def test_wrong_type_rejected(config_dir):
    write_config(config_dir, '[auto_charge]\nthreshold = "high"\n')
    with pytest.raises(
        ConfigError, match="'auto_charge.threshold' must be float"
    ):
        load_config()


def test_bool_is_not_int(config_dir):
    write_config(config_dir, "[auto_charge]\npoll_interval_seconds = true\n")
    with pytest.raises(ConfigError, match="must be int"):
        load_config()


def test_bad_command_mode_rejected(config_dir):
    write_config(config_dir, '[vehicle]\ncommand_mode = "rsa"\n')
    with pytest.raises(ConfigError, match="command_mode"):
        load_config()


def test_bad_region_rejected(config_dir):
    write_config(config_dir, 'region = "mars"\n')
    with pytest.raises(ConfigError, match="region"):
        load_config()


def test_save_load_round_trip(config_dir):
    config = Config()
    config.vehicle.vin = "5YJTEST"
    config.powerwall.energy_site_id = 42
    config.auto_charge.threshold = 88.0
    save_config(config)
    assert load_config() == config


def test_dump_omits_unset():
    text = dump_toml(Config())
    assert "vin" not in text
    assert "energy_site_id" not in text
    assert "key_path" not in text


def test_set_dotted_domain():
    config = Config()
    set_dotted(config, "domain", "example.com")
    assert config.domain == "example.com"


def test_set_dotted_coerces():
    config = Config()
    set_dotted(config, "auto_charge.threshold", "85.5")
    set_dotted(config, "auto_charge.require_plugged", "false")
    set_dotted(config, "powerwall.energy_site_id", "99")
    assert config.auto_charge.threshold == 85.5
    assert config.auto_charge.require_plugged is False
    assert config.powerwall.energy_site_id == 99


def test_set_dotted_unknown_key():
    with pytest.raises(ConfigError, match="unknown key"):
        set_dotted(Config(), "vehicle.color", "red")


def test_set_dotted_validation():
    with pytest.raises(ConfigError, match="threshold"):
        set_dotted(Config(), "auto_charge.threshold", "150")


def test_get_dotted():
    assert get_dotted(Config(), "auto_charge.poll_interval_seconds") == 300
    assert get_dotted(Config(), "notify.ntfy.url") == "https://ntfy.sh"
    assert get_dotted(Config(), "notify.pushover.device") == ""


def test_notify_defaults_and_set():
    config = Config()
    assert config.notify.enabled is False
    assert config.notify.service == "ntfy"
    assert config.notify.ntfy.url == "https://ntfy.sh"
    assert config.notify.pushover.url == (
        "https://api.pushover.net/1/messages.json"
    )
    assert config.notify.pushover.device == ""
    assert config.notify.pushover.sound == ""
    assert config.notify.priority == 3
    assert config.notify.min_priority == 1
    assert config.notify.verbose is False
    assert config.notify.on_start is True
    set_dotted(config, "notify.enabled", "true")
    set_dotted(config, "notify.service", "pushover")
    set_dotted(config, "notify.priority", "5")
    set_dotted(config, "notify.tags", "tesla,powerwall")
    set_dotted(config, "notify.min_priority", "4")
    set_dotted(config, "notify.verbose", "true")
    set_dotted(config, "notify.on_start", "false")
    set_dotted(config, "notify.ntfy.url", "https://ntfy.example")
    set_dotted(
        config,
        "notify.pushover.url",
        "https://pushover.example/messages.json",
    )
    set_dotted(config, "notify.pushover.device", "iphone")
    set_dotted(config, "notify.pushover.sound", "siren")
    assert config.notify.enabled is True
    assert config.notify.service == "pushover"
    assert config.notify.priority == 5
    assert config.notify.tags == "tesla,powerwall"
    assert config.notify.min_priority == 4
    assert config.notify.verbose is True
    assert config.notify.on_start is False
    assert config.notify.ntfy.url == "https://ntfy.example"
    assert config.notify.pushover.url == (
        "https://pushover.example/messages.json"
    )
    assert config.notify.pushover.device == "iphone"
    assert config.notify.pushover.sound == "siren"


def test_notify_priority_validation():
    with pytest.raises(ConfigError, match="notify.priority"):
        set_dotted(Config(), "notify.priority", "9")
    with pytest.raises(ConfigError, match="notify.min_priority"):
        set_dotted(Config(), "notify.min_priority", "0")
    with pytest.raises(ConfigError, match="notify.service"):
        set_dotted(Config(), "notify.service", "email")
    with pytest.raises(ConfigError, match="notify.ntfy.url"):
        set_dotted(Config(), "notify.ntfy.url", "")
    with pytest.raises(ConfigError, match="notify.pushover.url"):
        set_dotted(Config(), "notify.pushover.url", "")


def test_notify_round_trip(config_dir):
    config = Config()
    config.notify.enabled = True
    config.notify.service = "pushover"
    config.notify.ntfy.url = "https://ntfy.example"
    config.notify.pushover.url = "https://pushover.example/messages.json"
    config.notify.pushover.device = "iphone"
    config.notify.pushover.sound = "siren"
    config.notify.tags = "tesla,powerwall"
    save_config(config)
    assert load_config().notify == config.notify


def test_notify_legacy_url_is_migrated(config_dir):
    write_config(config_dir, '[notify]\nurl = "https://legacy.example"\n')
    assert load_config().notify.ntfy.url == "https://legacy.example"


def test_notify_nested_url_wins_over_legacy(config_dir):
    write_config(
        config_dir,
        '[notify]\nurl = "https://legacy.example"\n'
        "[notify.ntfy]\n"
        'url = "https://nested.example"\n',
    )
    assert load_config().notify.ntfy.url == "https://nested.example"


def test_notify_dump_omits_credentials():
    config = Config()
    config.notify.enabled = True
    config.notify.service = "pushover"
    text = dump_toml(config)
    assert "token" not in text.lower()
    assert "user" not in text.lower()
    assert "TESLA_PUSHOVER" not in text


def test_auto_charge_cos_defaults_and_set():
    config = Config()
    assert config.auto_charge.charge_on_solar is False
    assert config.auto_charge.stop_margin == 5.0
    set_dotted(config, "auto_charge.charge_on_solar", "true")
    set_dotted(config, "auto_charge.stop_margin", "5")
    assert config.auto_charge.charge_on_solar is True
    assert config.auto_charge.stop_margin == 5.0


def test_stop_margin_validation():
    with pytest.raises(ConfigError, match="stop_margin"):
        set_dotted(Config(), "auto_charge.stop_margin", "-1")
    with pytest.raises(ConfigError, match="stop_margin"):
        set_dotted(Config(), "auto_charge.stop_margin", "100")


def test_auto_charge_cos_round_trip(config_dir):
    config = Config()
    config.auto_charge.charge_on_solar = True
    config.auto_charge.threshold = 95.0
    config.auto_charge.stop_margin = 5.0
    save_config(config)
    loaded = load_config()
    assert loaded.auto_charge.charge_on_solar is True
    assert loaded.auto_charge.threshold == 95.0
    assert loaded.auto_charge.stop_margin == 5.0
