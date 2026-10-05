"""Best-effort notifications via ntfy (https://ntfy.sh)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import aiohttp

from .client import env_value
from .config import Config, NotifyConfig, notify_state_path

LOGGER = logging.getLogger(__name__)
SEND_TIMEOUT_SECONDS = 10
DEFAULT_TITLE = "tesla"


class NotifyError(Exception):
    """Raised when notifications are enabled but not configured."""


class NotifyState:
    """Persisted notification state: current situation plus cooldowns."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or notify_state_path()
        self._situation: str | None = None
        self._last_run: float | None = None
        self._sent: dict[str, float] | None = None

    def _load(self) -> None:
        if self._sent is not None:
            return
        situation: str | None = None
        last_run: float | None = None
        sent: dict[str, float] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            entries = raw
            if "state" in raw or "sent" in raw:
                state = raw.get("state")
                situation = state if isinstance(state, str) else None
                stamp = raw.get("last_run")
                if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                    last_run = float(stamp)
                entries = raw.get("sent")
            if isinstance(entries, dict):
                sent = {
                    str(key): float(value)
                    for key, value in entries.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                }
        self._situation = situation
        self._last_run = last_run
        self._sent = sent

    def situation(self) -> str | None:
        self._load()
        return self._situation

    def set_situation(self, value: str) -> None:
        self._load()
        self._situation = value
        self._save()

    def last_run(self) -> float | None:
        self._load()
        return self._last_run

    def touch(self) -> None:
        self._load()
        self._last_run = time.time()
        self._save()

    def allow(self, stage: str, cooldown: float | None) -> bool:
        if not cooldown:
            return True
        self._load()
        last = self._sent.get(stage)
        return last is None or (time.time() - last) >= cooldown

    def mark(self, stage: str) -> None:
        self._load()
        self._sent[stage] = time.time()
        self._save()

    def clear(self, *stages: str) -> None:
        self._load()
        for stage in stages:
            self._sent.pop(stage, None)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": self._situation,
            "sent": self._sent or {},
            "last_run": self._last_run,
        }
        try:
            self.path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            LOGGER.warning("cannot save notify state to %s: %s", self.path, exc)


class NtfyNotifier:
    """Publish messages to an ntfy topic; failures never propagate."""

    def __init__(
        self,
        config: NotifyConfig,
        topic: str,
        *,
        url: str | None = None,
        token: str | None = None,
        state: NotifyState | None = None,
    ) -> None:
        self.config = config
        self.topic = topic
        self.url = (url or config.url).rstrip("/")
        self.token = token
        self.state = state or NotifyState()

    async def send(
        self,
        stage: str,
        message: str,
        *,
        title: str | None = None,
        priority: int | None = None,
        tags: tuple[str, ...] | list[str] = (),
        cooldown: float | None = None,
        on_change: str | None = None,
    ) -> bool:
        """Send one notification; returns False (never raises) on failure.

        With ``on_change`` the message is only sent when the persisted
        situation differs from that value; the situation is then updated.
        ``verbose`` disables both the situation check and cooldowns.
        """
        effective_priority = priority if priority is not None else self.config.priority
        if effective_priority < self.config.min_priority:
            return False
        if not self.config.verbose:
            if on_change is not None and self.state.situation() == on_change:
                return False
            if cooldown and not self.state.allow(stage, cooldown):
                return False
        payload: dict[str, object] = {
            "topic": self.topic,
            "message": message,
            "title": title or DEFAULT_TITLE,
            "priority": effective_priority,
        }
        all_tags = self._tags(tags)
        if all_tags:
            payload["tags"] = all_tags
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        timeout = aiohttp.ClientTimeout(total=SEND_TIMEOUT_SECONDS)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(self.url, json=payload, headers=headers) as resp,
            ):
                if not 200 <= resp.status < 300:
                    LOGGER.warning("ntfy publish failed: HTTP %s", resp.status)
                    return False
        except (TimeoutError, aiohttp.ClientError) as exc:
            LOGGER.warning("ntfy publish failed: %s", exc)
            return False
        if on_change is not None:
            self.state.set_situation(on_change)
        self.state.mark(stage)
        return True

    def note_situation(self, value: str) -> None:
        """Record the current situation without sending anything."""
        self.state.set_situation(value)

    def last_run(self) -> float | None:
        """Wall-clock time of the previous run, if any."""
        return self.state.last_run()

    def touch(self) -> None:
        """Record that a run has started."""
        self.state.touch()

    def _tags(self, extra: tuple[str, ...] | list[str]) -> list[str]:
        tags = [tag.strip() for tag in self.config.tags.split(",") if tag.strip()]
        for tag in extra:
            if tag and tag not in tags:
                tags.append(tag)
        return tags


def build_notifier(config: Config, *, environ=None) -> NtfyNotifier | None:
    """Build a notifier when enabled; topic/token come from env or .env."""
    if not config.notify.enabled:
        return None
    topic = env_value("TESLA_NTFY_TOPIC", environ)
    if not topic:
        raise NotifyError(
            "TESLA_NTFY_TOPIC is not set; add it to .env or the environment"
        )
    url = env_value("TESLA_NTFY_URL", environ) or config.notify.url
    token = env_value("TESLA_NTFY_TOKEN", environ)
    return NtfyNotifier(config.notify, topic, url=url, token=token)
