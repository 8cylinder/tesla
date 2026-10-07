import pytest

from tesla.config import (
    Config,
    ConfigError,
    dump_toml,
    get_dotted,
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


def test_unknown_key_rejected(config_dir):
    write_config(config_dir, "bogus = 1\n")
    with pytest.raises(ConfigError, match="unknown key 'bogus'"):
        load_config()


def test_unknown_section_key_rejected(config_dir):
    write_config(config_dir, '[vehicle]\ncolor = "red"\n')
    with pytest.raises(ConfigError, match="unknown key 'vehicle.color'"):
        load_config()


def test_wrong_type_rejected(config_dir):
    write_config(config_dir, '[auto_charge]\nthreshold = "high"\n')
    with pytest.raises(ConfigError, match="'auto_charge.threshold' must be float"):
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


def test_notify_defaults_and_set():
    config = Config()
    assert config.notify.enabled is False
    assert config.notify.url == "https://ntfy.sh"
    assert config.notify.priority == 3
    assert config.notify.min_priority == 1
    assert config.notify.verbose is False
    assert config.notify.on_start is True
    set_dotted(config, "notify.enabled", "true")
    set_dotted(config, "notify.priority", "5")
    set_dotted(config, "notify.tags", "tesla,powerwall")
    set_dotted(config, "notify.min_priority", "4")
    set_dotted(config, "notify.verbose", "true")
    set_dotted(config, "notify.on_start", "false")
    assert config.notify.enabled is True
    assert config.notify.priority == 5
    assert config.notify.tags == "tesla,powerwall"
    assert config.notify.min_priority == 4
    assert config.notify.verbose is True
    assert config.notify.on_start is False


def test_notify_priority_validation():
    with pytest.raises(ConfigError, match="notify.priority"):
        set_dotted(Config(), "notify.priority", "9")
    with pytest.raises(ConfigError, match="notify.min_priority"):
        set_dotted(Config(), "notify.min_priority", "0")


def test_notify_round_trip(config_dir):
    config = Config()
    config.notify.enabled = True
    config.notify.url = "https://ntfy.example"
    config.notify.tags = "tesla,powerwall"
    save_config(config)
    assert load_config().notify == config.notify


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
