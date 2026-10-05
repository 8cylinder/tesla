import json
import time

import aiohttp
import pytest

from tesla.config import Config
from tesla.notify import NotifyError, NotifyState, NtfyNotifier, build_notifier


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    instances = []
    status = 200

    def __init__(self, **kwargs):
        self.posts = []
        FakeSession.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(FakeSession.status)


@pytest.fixture(autouse=True)
def fake_sessions(monkeypatch):
    FakeSession.instances = []
    FakeSession.status = 200
    monkeypatch.setattr("tesla.notify.aiohttp.ClientSession", FakeSession)
    return FakeSession


def make_config(**notify):
    config = Config()
    for key, value in notify.items():
        setattr(config.notify, key, value)
    return config


def test_build_notifier_disabled():
    assert build_notifier(Config(), environ={}) is None


def test_build_notifier_missing_topic(config_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = make_config(enabled=True)
    with pytest.raises(NotifyError, match="TESLA_NTFY_TOPIC"):
        build_notifier(config, environ={})


def test_build_notifier_env_values(config_dir):
    config = make_config(enabled=True, url="https://config.example")
    notifier = build_notifier(
        config,
        environ={
            "TESLA_NTFY_TOPIC": "topic",
            "TESLA_NTFY_URL": "https://env.example",
            "TESLA_NTFY_TOKEN": "tk_x",
        },
    )
    assert notifier is not None
    assert notifier.topic == "topic"
    assert notifier.url == "https://env.example"
    assert notifier.token == "tk_x"


async def test_send_posts_json(config_dir):
    config = make_config(enabled=True, tags="tesla,powerwall")
    notifier = NtfyNotifier(config.notify, "topic", token="tk_secret")
    assert await notifier.send(
        "charging", "charging started", priority=4, tags=("zap",)
    )
    post = FakeSession.instances[-1].posts[-1]
    assert post["url"] == "https://ntfy.sh"
    assert post["json"] == {
        "topic": "topic",
        "message": "charging started",
        "title": "tesla",
        "priority": 4,
        "tags": ["tesla", "powerwall", "zap"],
    }
    assert post["headers"]["Authorization"] == "Bearer tk_secret"


async def test_send_http_error_returns_false(config_dir):
    FakeSession.status = 500
    notifier = NtfyNotifier(make_config(enabled=True).notify, "topic")
    assert await notifier.send("started", "hi") is False


async def test_send_network_error_returns_false(config_dir, monkeypatch):
    class BoomSession(FakeSession):
        def post(self, url, json=None, headers=None):
            raise aiohttp.ClientError("boom")

    monkeypatch.setattr("tesla.notify.aiohttp.ClientSession", BoomSession)
    notifier = NtfyNotifier(make_config(enabled=True).notify, "topic")
    assert await notifier.send("started", "hi") is False


async def test_cooldown_suppresses_repeat(config_dir):
    def total_posts():
        return sum(len(session.posts) for session in FakeSession.instances)

    state = NotifyState(config_dir / "notify_state.json")
    notifier = NtfyNotifier(make_config(enabled=True).notify, "topic", state=state)
    assert await notifier.send("threshold", "one", cooldown=3600)
    assert await notifier.send("threshold", "two", cooldown=3600) is False
    assert total_posts() == 1
    notifier.state.clear("threshold")
    assert await notifier.send("threshold", "three", cooldown=3600)
    assert total_posts() == 2


async def test_on_change_suppresses_same_situation(config_dir):
    def total_posts():
        return sum(len(session.posts) for session in FakeSession.instances)

    notifier = NtfyNotifier(make_config(enabled=True).notify, "topic")
    assert await notifier.send("wait_plug", "one", on_change="wait_plug")
    assert await notifier.send("wait_plug", "two", on_change="wait_plug") is False
    assert total_posts() == 1
    notifier.note_situation("below_threshold")
    assert await notifier.send("wait_plug", "three", on_change="wait_plug")
    assert total_posts() == 2


async def test_verbose_bypasses_situation_and_cooldown(config_dir):
    config = make_config(enabled=True, verbose=True)
    notifier = NtfyNotifier(config.notify, "topic")
    assert await notifier.send("wait_plug", "one", on_change="wait_plug")
    assert await notifier.send("wait_plug", "two", on_change="wait_plug")
    assert await notifier.send("threshold", "three", cooldown=3600)
    assert await notifier.send("threshold", "four", cooldown=3600)


async def test_failed_send_does_not_mark(config_dir):
    FakeSession.status = 500
    state = NotifyState(config_dir / "notify_state.json")
    notifier = NtfyNotifier(make_config(enabled=True).notify, "topic", state=state)
    assert await notifier.send("threshold", "one", cooldown=3600) is False
    FakeSession.status = 200
    assert await notifier.send("threshold", "two", cooldown=3600)


def test_notify_state_round_trip(config_dir):
    state = NotifyState(config_dir / "notify_state.json")
    state.mark("a")
    assert state.allow("a", 3600) is False
    assert state.allow("b", 3600) is True
    state.clear("a")
    assert state.allow("a", 3600) is True
    state.set_situation("wait_plug")
    assert state.situation() == "wait_plug"


def test_notify_state_reads_legacy_format(config_dir):
    path = config_dir / "notify_state.json"
    path.write_text(json.dumps({"threshold": time.time()}))
    state = NotifyState(path)
    assert state.allow("threshold", 3600) is False
    assert state.situation() is None


async def test_min_priority_filters_stages(config_dir):
    config = make_config(enabled=True, min_priority=4)
    notifier = NtfyNotifier(config.notify, "topic")
    assert await notifier.send("started", "hi", priority=2) is False
    assert await notifier.send("charging", "hi", priority=4)
    total = sum(len(session.posts) for session in FakeSession.instances)
    assert total == 1
