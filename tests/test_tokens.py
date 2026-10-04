import json
import stat

import pytest

from tesla.tokens import TokenError, Tokens, load_tokens, save_tokens


def test_round_trip(config_dir):
    save_tokens(Tokens("refresh-abc", 1234567890))
    assert load_tokens() == Tokens("refresh-abc", 1234567890)


def test_saved_file_is_0600(config_dir):
    save_tokens(Tokens("refresh-abc", 1))
    mode = stat.S_IMODE((config_dir / "tokens.json").stat().st_mode)
    assert mode == 0o600


def test_missing_file_returns_none(config_dir):
    assert load_tokens() is None


def test_loose_mode_rejected(config_dir):
    save_tokens(Tokens("refresh-abc", 1))
    (config_dir / "tokens.json").chmod(0o644)
    with pytest.raises(TokenError, match="0600"):
        load_tokens()


def test_malformed_json_rejected(config_dir):
    path = config_dir / "tokens.json"
    path.write_text("not json")
    path.chmod(0o600)
    with pytest.raises(TokenError):
        load_tokens()


def test_missing_fields_rejected(config_dir):
    path = config_dir / "tokens.json"
    path.write_text(json.dumps({"refresh_token": "x"}))
    path.chmod(0o600)
    with pytest.raises(TokenError):
        load_tokens()


def test_rotation_persists(config_dir):
    save_tokens(Tokens("first", 1))
    save_tokens(Tokens("second", 2))
    assert load_tokens() == Tokens("second", 2)


def test_no_temp_files_left(config_dir):
    save_tokens(Tokens("refresh-abc", 1))
    assert [p.name for p in config_dir.iterdir()] == ["tokens.json"]
