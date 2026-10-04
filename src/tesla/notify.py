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
    """Per-stage last-sent timestamps for dedup and cooldowns."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or notify_state_path()
        self._data: dict[str, float] | None = None

    def _load(self) -> dict[str, float]:
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = {
                    str(key): float(value)
                    for key, value in raw.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                }
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def allow(self, stage: str, cooldown: float | None) -> bool:
        if not cooldown:
            return True
        last = self._load().get(stage)
        return last is None or (time.time() - last) >= cooldown

    def mark(self, stage: str) -> None:
        self._load()[stage] = time.time()
        self._save()

    def clear(self, *stages: str) -> None:
        data = self._load()
        for stage in stages:
            data.pop(stage, None)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.write_text(json.dumps(self._data or {}), encoding="utf-8")
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
    ) -> bool:
        """Send one notification; returns False (never raises) on failure."""
        effective_priority = priority if priority is not None else self.config.priority
        if effective_priority < self.config.min_priority:
            return False
        if self.config.verbose:
            cooldown = None
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
        self.state.mark(stage)
        return True

    def _tags(self, extra: tuple[str, ...] | list[str]) -> list[str]:
        tags = [tag.strip() for tag in self.config.tags.split(",") if tag.strip()]
        for tag in extra:
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    def reset(self, *stages: str) -> None:
        """Clear cooldown state for the given stages."""
        self.state.clear(*stages)


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
