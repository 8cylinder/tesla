"""Auto-charge decision logic, watch loop, and PID lock."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from enum import Enum
from pathlib import Path

import aiohttp
from tesla_fleet_api import TeslaFleetOAuth
from tesla_fleet_api.exceptions import (
    InvalidScope,
    InvalidToken,
    LoginRequired,
    OAuthExpired,
    TeslaFleetError,
    VehicleOffline,
)

from .car import charging_state as read_charging_state
from .client import build_vehicle, ensure_fresh_token
from .config import Config, ConfigError
from .powerwall import percentage_charged

LOGGER = logging.getLogger(__name__)

BACKOFF_INITIAL = 5.0
BACKOFF_CAP = 300.0
MAX_FAILURES = 5
AUTH_ERRORS = (OAuthExpired, InvalidToken, LoginRequired, InvalidScope)


class Action(Enum):
    DONE = "done"
    WAIT_PLUG = "wait_plug"
    START = "start"
    WAIT = "wait"
    SLEEP = "sleep"


def decide(
    powerwall_pct: float | None,
    threshold: float,
    charging_state: str | None,
    require_plugged: bool,
) -> Action:
    """Map the current readings to one action (the decide() truth table)."""
    if powerwall_pct is None or powerwall_pct < threshold:
        return Action.SLEEP
    if charging_state in ("Charging", "Complete"):
        return Action.DONE
    if charging_state == "Disconnected":
        return Action.WAIT_PLUG if require_plugged else Action.START
    if charging_state in ("Stopped", "NoPower"):
        return Action.START
    if charging_state == "Asleep":
        return Action.WAIT if require_plugged else Action.START
    return Action.WAIT


def backoff_delay(
    failures: int, jitter: Callable[[float, float], float] = random.uniform
) -> float:
    """Exponential backoff with +/-20% jitter, capped at 300 s."""
    base = min(BACKOFF_INITIAL * (2 ** (failures - 1)), BACKOFF_CAP)
    return base * jitter(0.8, 1.2)


class AlreadyRunning(Exception):
    def __init__(self, pid: int) -> None:
        super().__init__(f"auto-charge is already running (pid {pid})")
        self.pid = pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LockFile:
    """Single-instance PID lock; a live PID blocks, a stale one is replaced."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pid = os.getpid()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = None
            if pid is not None and _pid_alive(pid):
                raise AlreadyRunning(pid)
        self.path.write_text(f"{self.pid}\n", encoding="utf-8")

    def release(self) -> None:
        with suppress(OSError):
            if self.path.read_text(encoding="utf-8").strip() == str(self.pid):
                self.path.unlink()


class TeslaAutoChargeClient:
    """Thin adapter over the live Tesla objects used by the watch loop."""

    def __init__(self, oauth: TeslaFleetOAuth, config: Config) -> None:
        self._oauth = oauth
        self._config = config
        self._vehicle = None
        self._site = None

    async def build(self) -> None:
        await ensure_fresh_token(self._oauth)
        config = self._config
        self._vehicle = await build_vehicle(self._oauth, config)
        site_id = config.powerwall.energy_site_id
        if site_id is None:
            raise ConfigError(
                "no energy_site_id set; run "
                "'tesla config set powerwall.energy_site_id <id>'"
            )
        self._site = self._oauth.energySites.create(site_id)

    async def refresh(self) -> None:
        await ensure_fresh_token(self._oauth)

    async def powerwall_percentage(self) -> float | None:
        return percentage_charged(await self._site.live_status())

    async def charging_state(self) -> str | None:
        data = await self._vehicle.vehicle_data(endpoints=["charge_state"])
        return read_charging_state(data)

    async def wake(self) -> None:
        await self._vehicle.wake_up(
            wait=True, timeout=self._config.auto_charge.wake_timeout_seconds
        )

    async def set_charge_limit(self, percent: int) -> None:
        await self._vehicle.set_charge_limit(percent)

    async def start_charging(self) -> None:
        await self._vehicle.charge_start()

    async def verify_charging(self, attempts: int = 3, delay: float = 5.0) -> bool:
        for attempt in range(attempts):
            try:
                state = await self.charging_state()
            except (TeslaFleetError, aiohttp.ClientError):
                state = None
            if state in ("Charging", "Complete"):
                return True
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
        return False


class TriggerResult(Enum):
    STARTED = "started"
    RETRY = "retry"
    WAIT = "wait"


class _ExitCode(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class AutoCharger:
    """Stateful watch-loop executor around a client adapter."""

    def __init__(
        self,
        client,
        config: Config,
        *,
        dry_run: bool = False,
        once: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.client = client
        self.auto = config.auto_charge
        self.dry_run = dry_run
        self.once = once
        self.sleep = sleep
        self.clock = clock
        self.jitter = jitter
        self.failures = 0
        self.started_at = clock()

    async def run(self) -> int:
        while True:
            if self.auto.max_runtime_hours:
                elapsed = self.clock() - self.started_at
                if elapsed >= self.auto.max_runtime_hours * 3600:
                    LOGGER.error(
                        "max runtime of %s h reached before a trigger",
                        self.auto.max_runtime_hours,
                    )
                    return 3
            try:
                await self.client.refresh()
                pct = await self.client.powerwall_percentage()
            except AUTH_ERRORS as exc:
                LOGGER.error(
                    "authentication failed: %s; re-run 'tesla auth login'", exc
                )
                return 2
            except (TeslaFleetError, aiohttp.ClientError) as exc:
                code = await self._backoff(exc)
                if code is not None:
                    return code
                continue

            action = decide(pct, self.auto.threshold, None, self.auto.require_plugged)
            if pct is not None and pct >= self.auto.threshold:
                try:
                    state = await self.client.charging_state()
                except AUTH_ERRORS as exc:
                    LOGGER.error(
                        "authentication failed: %s; re-run 'tesla auth login'", exc
                    )
                    return 2
                except VehicleOffline:
                    state = "Asleep"
                except (TeslaFleetError, aiohttp.ClientError) as exc:
                    code = await self._backoff(exc)
                    if code is not None:
                        return code
                    continue
                action = decide(
                    pct, self.auto.threshold, state, self.auto.require_plugged
                )

            self.failures = 0
            if action is Action.SLEEP:
                LOGGER.info(
                    "Powerwall %s is below threshold %s", pct, self.auto.threshold
                )
            elif action is Action.WAIT:
                if state == "Asleep":
                    LOGGER.info(
                        "Powerwall %s is at threshold; vehicle asleep, waiting "
                        "for plug (require_plugged)",
                        pct,
                    )
                else:
                    LOGGER.info(
                        "Powerwall %s is at threshold but charge state is unknown",
                        pct,
                    )
            elif action is Action.WAIT_PLUG:
                LOGGER.info("Powerwall %s is at threshold; waiting for plug", pct)
            elif action is Action.DONE:
                LOGGER.info("already charging or complete; nothing to do")
                return 0
            else:
                try:
                    result = await self._trigger()
                except _ExitCode as exc:
                    return exc.code
                if result is TriggerResult.STARTED:
                    return 0
                if result is TriggerResult.WAIT:
                    if self.once:
                        return 0
                    await self.sleep(self.auto.poll_interval_seconds)
                    continue
                self.failures += 1
                if self.failures >= MAX_FAILURES:
                    LOGGER.error(
                        "giving up after %s consecutive trigger failures",
                        self.failures,
                    )
                    return 3
                await self.sleep(backoff_delay(self.failures, self.jitter))
                continue

            if self.once:
                return 0
            await self.sleep(self.auto.poll_interval_seconds)

    async def _backoff(self, exc: BaseException) -> int | None:
        if self.once:
            LOGGER.error("poll failed: %s", exc)
            return 3
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            LOGGER.error(
                "giving up after %s consecutive failures: %s", self.failures, exc
            )
            return 3
        delay = backoff_delay(self.failures, self.jitter)
        LOGGER.warning(
            "poll failed (%s/%s): %s; retrying in %.0f s",
            self.failures,
            MAX_FAILURES,
            exc,
            delay,
        )
        await self.sleep(delay)
        return None

    async def _trigger(self) -> TriggerResult:
        if self.auto.set_charge_limit:
            limit = f"limit {self.auto.set_charge_limit}%"
        else:
            limit = "no limit change"
        if self.dry_run:
            LOGGER.info("[dry-run] would wake the car (%s) and charge_start", limit)
            return TriggerResult.STARTED
        try:
            await self.client.wake()
            if self.auto.require_plugged:
                state = await self.client.charging_state()
                if state == "Disconnected":
                    LOGGER.info("vehicle is awake but not plugged in; waiting for plug")
                    return TriggerResult.WAIT
            if self.auto.set_charge_limit:
                await self.client.set_charge_limit(self.auto.set_charge_limit)
            await self.client.start_charging()
        except AUTH_ERRORS as exc:
            LOGGER.error("authentication failed: %s; re-run 'tesla auth login'", exc)
            raise _ExitCode(2) from exc
        except (TeslaFleetError, aiohttp.ClientError) as exc:
            if self.once:
                LOGGER.error("trigger failed: %s", exc)
                raise _ExitCode(3) from exc
            LOGGER.warning("trigger failed: %s", exc)
            return TriggerResult.RETRY
        if await self.client.verify_charging():
            LOGGER.info("charging started")
            return TriggerResult.STARTED
        LOGGER.error("charge_start was sent but the vehicle is not charging")
        raise _ExitCode(3)


async def watch(
    client,
    config: Config,
    *,
    dry_run: bool = False,
    once: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    jitter: Callable[[float, float], float] = random.uniform,
) -> int:
    """Poll and trigger once; returns the process exit code (0/2/3)."""
    charger = AutoCharger(
        client,
        config,
        dry_run=dry_run,
        once=once,
        sleep=sleep,
        clock=clock,
        jitter=jitter,
    )
    return await charger.run()


async def run(
    client, config: Config, *, dry_run: bool = False, once: bool = False
) -> int:
    """Run the watch loop with SIGINT/SIGTERM handling; returns exit code."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed: list[int] = []
    watch_task = asyncio.create_task(watch(client, config, dry_run=dry_run, once=once))
    stopper = asyncio.create_task(stop.wait())
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                continue
        done, _ = await asyncio.wait(
            {watch_task, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        if watch_task in done:
            return watch_task.result()
        watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await watch_task
        LOGGER.info("stopped by signal")
        return 0
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        stopper.cancel()
        with suppress(asyncio.CancelledError):
            await stopper
