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
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

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

from .car import (
    ChargeOnSolarSettings,
    VehicleStatus,
)
from .car import (
    charge_on_solar_settings as read_cos_settings,
)
from .car import charging_state as read_charging_state
from .car import vehicle_status as read_vehicle_status
from .client import build_vehicle, ensure_fresh_token
from .config import Config, ConfigError
from .notify import Notifier
from .powerwall import percentage_charged

LOGGER = logging.getLogger(__name__)

BACKOFF_INITIAL = 5.0
BACKOFF_CAP = 300.0
MAX_FAILURES = 5
AUTH_ERRORS = (OAuthExpired, InvalidToken, LoginRequired, InvalidScope)

NOTIFY_POLL_FAILED_COOLDOWN = 3600
NOTIFY_TRIGGER_FAILED_COOLDOWN = 3600


class Action(Enum):
    DONE = "done"
    WAIT_PLUG = "wait_plug"
    START = "start"
    STOP = "stop"
    WAIT = "wait"
    SLEEP = "sleep"


@dataclass(frozen=True)
class Decision:
    """One row of the CoS gate truth table: an action plus a notify reason."""

    action: Action
    reason: str | None = None


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


def _at_upper_limit(status: VehicleStatus, cos: ChargeOnSolarSettings) -> bool:
    return (
        status.battery_level is not None
        and cos.upper_limit is not None
        and status.battery_level >= cos.upper_limit
    )


def _below_lower_limit(
    status: VehicleStatus, cos: ChargeOnSolarSettings
) -> bool:
    return (
        status.battery_level is not None
        and cos.lower_limit is not None
        and status.battery_level < cos.lower_limit
    )


def _below_floor(
    powerwall_pct: float | None,
    stop: float,
    low_reason: str,
    high_reason: str | None = None,
) -> Decision:
    if powerwall_pct is not None and powerwall_pct < stop:
        return Decision(Action.STOP, low_reason)
    return Decision(Action.SLEEP, high_reason)


def decide_charge_on_solar(
    powerwall_pct: float | None,
    threshold: float,
    stop_margin: float,
    status: VehicleStatus,
    cos: ChargeOnSolarSettings,
) -> Decision:
    """CoS gate truth table; one branch per row. No side effects."""
    stop = threshold - stop_margin
    state = status.charging_state
    if not cos.enabled or status.cos_state == "user_disabled":
        return Decision(Action.WAIT, "cos_disabled")
    if state == "Disconnected":
        return Decision(Action.SLEEP, "wait_plug")
    if state == "Complete":
        return Decision(Action.DONE)
    if _at_upper_limit(status, cos):
        return Decision(Action.DONE)
    if status.cos_state == "charging_on_anything":
        return Decision(Action.STOP, "charging_on_grid")
    if state in ("Charging", "Starting", "Calibrating"):
        return _below_floor(powerwall_pct, stop, "charging_stopped")
    if status.cos_state == "no_charge_recommended":
        if status.cos_reason == "POWERWALL_CHARGE_PRIORITY":
            return Decision(Action.SLEEP, "waiting_powerwall")
        return _below_floor(
            powerwall_pct, stop, "charging_stopped", "charging_paused"
        )
    if status.cos_state == "waiting_for_server":
        return _below_floor(
            powerwall_pct, stop, "charging_stopped", "charging_paused"
        )
    if status.cos_state == "charging_on_excess_solar":
        return Decision(Action.SLEEP)
    if status.cos_state == "user_stopped":
        if powerwall_pct is None or powerwall_pct < threshold:
            return Decision(Action.SLEEP, "waiting_powerwall")
        if _below_lower_limit(status, cos):
            return Decision(Action.WAIT, "cos_lower_limit")
        return Decision(Action.START)
    if state in ("Stopped", "NoPower"):
        if powerwall_pct is None:
            return Decision(Action.WAIT)
        if powerwall_pct >= threshold:
            if _below_lower_limit(status, cos):
                return Decision(Action.WAIT, "cos_lower_limit")
            return Decision(Action.START)
        return Decision(Action.SLEEP, "waiting_powerwall")
    return Decision(Action.WAIT)


def backoff_delay(
    failures: int, jitter: Callable[[float, float], float] = random.uniform
) -> float:
    """Exponential backoff with +/-20% jitter, capped at 300 s."""
    base = min(BACKOFF_INITIAL * (2 ** (failures - 1)), BACKOFF_CAP)
    return float(base * jitter(0.8, 1.2))


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
        self._vehicle: Any = None
        self._site: Any = None

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

    async def vehicle_status(self) -> VehicleStatus:
        data = await self._vehicle.vehicle_data(endpoints=["charge_state"])
        return read_vehicle_status(data)

    async def charge_on_solar_settings(self) -> ChargeOnSolarSettings | None:
        """Read the app-side CoS setting; None when unsupported (fleet mode)."""
        get = getattr(self._vehicle, "get_charge_on_solar", None)
        if get is None:
            return None
        return read_cos_settings(await get())

    async def stop_charging(self) -> None:
        await self._vehicle.charge_stop()

    async def wake(self) -> None:
        await self._vehicle.wake_up(
            wait=True, timeout=self._config.auto_charge.wake_timeout_seconds
        )

    async def set_charge_limit(self, percent: int) -> None:
        await self._vehicle.set_charge_limit(percent)

    async def start_charging(self) -> None:
        await self._vehicle.charge_start()

    async def verify_charging(
        self, attempts: int = 6, delay: float = 10.0
    ) -> bool:
        state = None
        for attempt in range(attempts):
            try:
                state = await self.charging_state()
            except (TeslaFleetError, aiohttp.ClientError) as exc:
                LOGGER.debug("verification read failed: %s", exc)
                state = None
            if state in ("Charging", "Complete"):
                return True
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
        LOGGER.warning("charging verification gave up; last state=%r", state)
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
        client: TeslaAutoChargeClient,
        config: Config,
        *,
        dry_run: bool = False,
        once: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
        notifier: Notifier | None = None,
    ) -> None:
        self.client = client
        self.auto = config.auto_charge
        self.notify = config.notify
        self.dry_run = dry_run
        self.once = once
        self.sleep = sleep
        self.clock = clock
        self.jitter = jitter
        self.notifier = notifier
        self.failures = 0
        self._report_due = False
        self.started_at = clock()

    async def _notify(
        self,
        stage: str,
        message: str,
        *,
        priority: int | None = None,
        tags: tuple[str, ...] | list[str] = (),
        cooldown: float | None = None,
        on_change: str | None = None,
    ) -> None:
        """Best-effort notification; never affects the loop."""
        if self.notifier is None or self.dry_run:
            return
        await self.notifier.send(
            stage,
            message,
            priority=priority,
            tags=tags,
            cooldown=cooldown,
            on_change=on_change,
        )

    def _note_situation(self, value: str) -> None:
        """Record the current situation without sending a notification."""
        if self.notifier is None or self.dry_run:
            return
        if hasattr(self.notifier, "note_situation"):
            self.notifier.note_situation(value)

    def _startup_report_due(self) -> bool:
        if self.notifier is None or self.dry_run or not self.notify.on_start:
            return False
        if not self.once:
            return True
        last = (
            self.notifier.last_run()
            if hasattr(self.notifier, "last_run")
            else None
        )
        if last is None:
            return True
        return (time.time() - last) >= 2 * self.auto.poll_interval_seconds

    def _touch(self) -> None:
        if self.notifier is None or self.dry_run:
            return
        if hasattr(self.notifier, "touch"):
            self.notifier.touch()

    async def _startup_report(
        self,
        pct: float | None,
        action: Action,
        state: str | None,
    ) -> None:
        if action is Action.SLEEP:
            if pct is None:
                message = "watcher started; Powerwall reading unavailable"
            else:
                message = (
                    f"watcher started; Powerwall at {pct:.1f}% "
                    f"(threshold {self.auto.threshold}%)"
                )
        elif action is Action.WAIT:
            if state == "Asleep":
                message = (
                    "watcher started; vehicle asleep, waiting for plug "
                    "(require_plugged)"
                )
            else:
                message = "watcher started; charge state unknown, waiting"
        elif action is Action.WAIT_PLUG:
            message = "watcher started; waiting for the car to be plugged in"
        elif action is Action.DONE:
            message = "watcher started; vehicle already charging or complete"
        elif pct is None:
            message = "watcher started; starting charge"
        else:
            message = (
                f"watcher started; Powerwall at {pct:.1f}%; starting charge"
            )
        await self._notify(
            "on_start", message, priority=2, tags=("information_source",)
        )
        if action in (Action.WAIT, Action.WAIT_PLUG):
            self._note_situation("wait_plug")
        elif action is Action.DONE:
            self._note_situation("already_charging")

    async def run(self) -> int:
        if self.auto.charge_on_solar:
            return await self._run_gate()
        return await self._run_classic()

    async def _run_classic(self) -> int:
        self._report_due = self._startup_report_due()
        self._touch()
        while True:
            if self.auto.max_runtime_hours:
                elapsed = self.clock() - self.started_at
                if elapsed >= self.auto.max_runtime_hours * 3600:
                    LOGGER.error(
                        "max runtime of %s h reached before a trigger",
                        self.auto.max_runtime_hours,
                    )
                    await self._notify(
                        "max_runtime",
                        "max runtime reached before a trigger",
                        priority=4,
                        tags=("warning",),
                    )
                    return 3
            try:
                await self.client.refresh()
                pct = await self.client.powerwall_percentage()
            except AUTH_ERRORS as exc:
                LOGGER.error(
                    "authentication failed: %s; re-run 'tesla auth login'", exc
                )
                await self._notify(
                    "auth_failed",
                    f"authentication failed: {exc}; re-run 'tesla auth login'",
                    priority=5,
                    tags=("rotating_light",),
                )
                return 2
            except (TeslaFleetError, aiohttp.ClientError) as exc:
                code = await self._backoff(exc)
                if code is not None:
                    return code
                continue

            action = decide(
                pct, self.auto.threshold, None, self.auto.require_plugged
            )
            state = None
            if pct is not None and pct >= self.auto.threshold:
                try:
                    state = await self.client.charging_state()
                except AUTH_ERRORS as exc:
                    LOGGER.error(
                        "authentication failed: %s; re-run 'tesla auth login'",
                        exc,
                    )
                    await self._notify(
                        "auth_failed",
                        f"authentication failed: {exc}; "
                        "re-run 'tesla auth login'",
                        priority=5,
                        tags=("rotating_light",),
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
            if self._report_due:
                await self._startup_report(pct, action, state)
                self._report_due = False
            if action is Action.SLEEP:
                LOGGER.info(
                    "Powerwall %s is below threshold %s",
                    pct,
                    self.auto.threshold,
                )
                self._note_situation("below_threshold")
            elif action is Action.WAIT:
                if state == "Asleep":
                    LOGGER.info(
                        "Powerwall %s is at threshold; vehicle asleep, waiting "
                        "for plug (require_plugged)",
                        pct,
                    )
                    await self._notify(
                        "wait_plug",
                        "Powerwall at threshold; vehicle asleep, waiting for "
                        "plug (require_plugged)",
                        priority=3,
                        tags=("electric_plug",),
                        on_change="wait_plug",
                    )
                else:
                    LOGGER.info(
                        "Powerwall %s is at threshold but charge state is "
                        "unknown",
                        pct,
                    )
                    await self._notify(
                        "wait_plug",
                        "Powerwall at threshold; charge state unknown, waiting",
                        priority=3,
                        tags=("warning",),
                        on_change="wait_plug",
                    )
            elif action is Action.WAIT_PLUG:
                LOGGER.info(
                    "Powerwall %s is at threshold; waiting for plug", pct
                )
                await self._notify(
                    "wait_plug",
                    "Powerwall at threshold; waiting for the car to be "
                    "plugged in",
                    priority=3,
                    tags=("electric_plug",),
                    on_change="wait_plug",
                )
            elif action is Action.DONE:
                LOGGER.info("already charging or complete; nothing to do")
                await self._notify(
                    "already_charging",
                    "vehicle already charging or complete; nothing to do",
                    priority=2,
                    tags=("heavy_check_mark",),
                    on_change="already_charging",
                )
                return 0
            else:
                try:
                    result = await self._trigger()
                except _ExitCode as exc:
                    return exc.code
                if result is TriggerResult.STARTED:
                    await self._notify(
                        "charging",
                        "charging started",
                        priority=4,
                        tags=("zap", "heavy_check_mark"),
                    )
                    self._note_situation("already_charging")
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
                    await self._notify(
                        "gave_up",
                        f"giving up after {self.failures} consecutive "
                        "trigger failures",
                        priority=5,
                        tags=("rotating_light",),
                    )
                    return 3
                await self.sleep(backoff_delay(self.failures, self.jitter))
                continue

            if self.once:
                return 0
            await self.sleep(self.auto.poll_interval_seconds)

    async def _auth_failure(self, exc: BaseException) -> int:
        LOGGER.error(
            "authentication failed: %s; re-run 'tesla auth login'", exc
        )
        await self._notify(
            "auth_failed",
            f"authentication failed: {exc}; re-run 'tesla auth login'",
            priority=5,
            tags=("rotating_light",),
        )
        return 2

    async def _run_gate(self) -> int:
        """Keep the charge switch off until the Powerwall is ready for CoS."""
        self._report_due = self._startup_report_due()
        self._touch()
        settings: ChargeOnSolarSettings | None = None
        loaded = False
        while True:
            if self.auto.max_runtime_hours:
                elapsed = self.clock() - self.started_at
                if elapsed >= self.auto.max_runtime_hours * 3600:
                    LOGGER.error(
                        "max runtime of %s h reached before a trigger",
                        self.auto.max_runtime_hours,
                    )
                    await self._notify(
                        "max_runtime",
                        "max runtime reached before a trigger",
                        priority=4,
                        tags=("warning",),
                    )
                    return 3
            try:
                await self.client.refresh()
                pct = await self.client.powerwall_percentage()
            except AUTH_ERRORS as exc:
                return await self._auth_failure(exc)
            except (TeslaFleetError, aiohttp.ClientError) as exc:
                code = await self._backoff(exc)
                if code is not None:
                    return code
                continue
            try:
                status = await self.client.vehicle_status()
            except AUTH_ERRORS as exc:
                return await self._auth_failure(exc)
            except VehicleOffline:
                status = VehicleStatus()
            except (TeslaFleetError, aiohttp.ClientError) as exc:
                code = await self._backoff(exc)
                if code is not None:
                    return code
                continue
            if not loaded:
                try:
                    settings = await self.client.charge_on_solar_settings()
                except AUTH_ERRORS as exc:
                    return await self._auth_failure(exc)
                except (TeslaFleetError, aiohttp.ClientError) as exc:
                    code = await self._backoff(exc)
                    if code is not None:
                        return code
                    continue
                loaded = True
            decision = decide_charge_on_solar(
                pct,
                self.auto.threshold,
                self.auto.stop_margin,
                status,
                settings or ChargeOnSolarSettings(),
            )
            self.failures = 0
            if self._report_due:
                await self._gate_startup_report(pct, status, decision)
                self._report_due = False
            if decision.action is Action.START:
                try:
                    result = await self._start_gate()
                except _ExitCode as exc:
                    return exc.code
                if result is TriggerResult.STARTED:
                    await self._notify(
                        "charging",
                        "charging started",
                        priority=4,
                        tags=("zap", "heavy_check_mark"),
                    )
                elif result is TriggerResult.RETRY:
                    code = await self._gate_retry()
                    if code is not None:
                        return code
                    continue
            elif decision.action is Action.STOP:
                try:
                    result = await self._stop_gate(decision.reason)
                except _ExitCode as exc:
                    return exc.code
                if result is TriggerResult.RETRY:
                    code = await self._gate_retry()
                    if code is not None:
                        return code
                    continue
            else:
                await self._note_gate(decision, pct)
            if self.once:
                return 0
            await self.sleep(self.auto.poll_interval_seconds)

    async def _gate_retry(self) -> int | None:
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            LOGGER.error("giving up after %s gate failures", self.failures)
            await self._notify(
                "gave_up",
                f"giving up after {self.failures} consecutive gate failures",
                priority=5,
                tags=("rotating_light",),
            )
            return 3
        await self.sleep(backoff_delay(self.failures, self.jitter))
        return None

    async def _gate_startup_report(
        self, pct: float | None, status: VehicleStatus, decision: Decision
    ) -> None:
        pct_text = "unavailable" if pct is None else f"{pct:.1f}%"
        car = status.charging_state or status.cos_state or "unknown"
        message = (
            f"watcher started; Powerwall {pct_text}; car {car}; "
            f"action {decision.action.value}"
        )
        await self._notify(
            "on_start", message, priority=2, tags=("information_source",)
        )
        if decision.reason:
            self._note_situation(decision.reason)

    async def _note_gate(self, decision: Decision, pct: float | None) -> None:
        reason = decision.reason
        if reason == "cos_disabled":
            await self._notify(
                "cos_disabled",
                "Charge on Solar is not enabled; not starting "
                "(would drain the Powerwall)",
                priority=5,
                tags=("rotating_light",),
                on_change="cos_disabled",
            )
        elif reason == "cos_lower_limit":
            await self._notify(
                "cos_lower_limit",
                "car is below the Charge on Solar lower limit; waiting to "
                "avoid any-source charging",
                priority=3,
                tags=("warning",),
                on_change="cos_lower_limit",
            )
        elif reason == "charging_paused":
            await self._notify(
                "charging_paused",
                "charging paused: solar too low; CoS will resume on its own",
                priority=3,
                tags=("cloud",),
                on_change="charging_paused",
            )
        elif reason == "waiting_powerwall":
            detail = "reading unavailable" if pct is None else f"at {pct:.1f}%"
            await self._notify(
                "waiting_powerwall",
                f"waiting for the Powerwall ({detail}) to reach "
                f"{self.auto.threshold:g}%",
                priority=3,
                tags=("battery",),
                on_change="waiting_powerwall",
            )
        elif reason == "wait_plug":
            await self._notify(
                "wait_plug",
                "waiting for the car to be plugged in",
                priority=3,
                tags=("electric_plug",),
                on_change="wait_plug",
            )

    async def _start_gate(self) -> TriggerResult:
        """Wake and charge_start; CoS governs the power from there."""
        if self.dry_run:
            LOGGER.info("[dry-run] would wake the car and charge_start")
            return TriggerResult.STARTED
        try:
            await self.client.wake()
            if self.auto.require_plugged:
                state = await self.client.charging_state()
                if state == "Disconnected":
                    LOGGER.info("vehicle is awake but not plugged in; waiting")
                    await self._notify(
                        "wait_plug",
                        "vehicle is awake but not plugged in; waiting for plug",
                        priority=3,
                        tags=("electric_plug",),
                        on_change="wait_plug",
                    )
                    return TriggerResult.WAIT
            if self.auto.set_charge_limit:
                await self.client.set_charge_limit(self.auto.set_charge_limit)
            await self.client.start_charging()
        except AUTH_ERRORS as exc:
            LOGGER.error(
                "authentication failed: %s; re-run 'tesla auth login'", exc
            )
            await self._notify(
                "auth_failed",
                f"authentication failed: {exc}; re-run 'tesla auth login'",
                priority=5,
                tags=("rotating_light",),
            )
            raise _ExitCode(2) from exc
        except (TeslaFleetError, aiohttp.ClientError) as exc:
            if self.once:
                LOGGER.error("trigger failed: %s", exc)
                await self._notify(
                    "trigger_failed",
                    f"charge trigger failed: {exc}",
                    priority=4,
                    tags=("warning",),
                    cooldown=NOTIFY_TRIGGER_FAILED_COOLDOWN,
                )
                raise _ExitCode(3) from exc
            LOGGER.warning("trigger failed: %s", exc)
            await self._notify(
                "trigger_failed",
                f"charge trigger failed: {exc}",
                priority=4,
                tags=("warning",),
                cooldown=NOTIFY_TRIGGER_FAILED_COOLDOWN,
            )
            return TriggerResult.RETRY
        LOGGER.info("charge_start sent (Charge on Solar governs the power)")
        return TriggerResult.STARTED

    async def _stop_gate(self, reason: str | None) -> TriggerResult:
        """Latch charging off; CoS stays enabled and re-arms at threshold."""
        if self.dry_run:
            LOGGER.info(
                "[dry-run] would wake the car and charge_stop (%s)", reason
            )
            return TriggerResult.STARTED
        try:
            await self.client.wake()
            await self.client.stop_charging()
        except AUTH_ERRORS as exc:
            LOGGER.error(
                "authentication failed: %s; re-run 'tesla auth login'", exc
            )
            await self._notify(
                "auth_failed",
                f"authentication failed: {exc}; re-run 'tesla auth login'",
                priority=5,
                tags=("rotating_light",),
            )
            raise _ExitCode(2) from exc
        except (TeslaFleetError, aiohttp.ClientError) as exc:
            if self.once:
                LOGGER.error("stop failed: %s", exc)
                await self._notify(
                    "trigger_failed",
                    f"charge stop failed: {exc}",
                    priority=4,
                    tags=("warning",),
                    cooldown=NOTIFY_TRIGGER_FAILED_COOLDOWN,
                )
                raise _ExitCode(3) from exc
            LOGGER.warning("stop failed: %s", exc)
            await self._notify(
                "trigger_failed",
                f"charge stop failed: {exc}",
                priority=4,
                tags=("warning",),
                cooldown=NOTIFY_TRIGGER_FAILED_COOLDOWN,
            )
            return TriggerResult.RETRY
        LOGGER.info("charge_stop sent; Charge on Solar stays enabled")
        if reason == "charging_on_grid":
            await self._notify(
                "charging_on_grid",
                "vehicle was charging from grid/Powerwall; charge_stop sent",
                priority=4,
                tags=("warning",),
                cooldown=NOTIFY_TRIGGER_FAILED_COOLDOWN,
            )
        else:
            await self._notify(
                "charging_stopped",
                "charging stopped: Powerwall below the stop floor; "
                "Charge on Solar stays enabled",
                priority=3,
                tags=("stop_sign",),
                on_change="charging_stopped",
            )
        return TriggerResult.STARTED

    async def _backoff(self, exc: BaseException) -> int | None:
        if self.once:
            LOGGER.error("poll failed: %s", exc)
            await self._notify(
                "poll_failed",
                f"poll failed: {exc}",
                priority=3,
                tags=("warning",),
            )
            return 3
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            LOGGER.error(
                "giving up after %s consecutive failures: %s",
                self.failures,
                exc,
            )
            await self._notify(
                "gave_up",
                f"giving up after {self.failures} consecutive failures: {exc}",
                priority=5,
                tags=("rotating_light",),
            )
            return 3
        if self.failures == 1:
            await self._notify(
                "poll_failed",
                f"poll failed: {exc}",
                priority=3,
                tags=("warning",),
                cooldown=NOTIFY_POLL_FAILED_COOLDOWN,
            )
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
            LOGGER.info(
                "[dry-run] would wake the car (%s) and charge_start", limit
            )
            return TriggerResult.STARTED
        try:
            await self.client.wake()
            if self.auto.require_plugged:
                state = await self.client.charging_state()
                if state == "Disconnected":
                    LOGGER.info(
                        "vehicle is awake but not plugged in; waiting for plug"
                    )
                    await self._notify(
                        "wait_plug",
                        "vehicle is awake but not plugged in; waiting for plug",
                        priority=3,
                        tags=("electric_plug",),
                        on_change="wait_plug",
                    )
                    return TriggerResult.WAIT
            if self.auto.set_charge_limit:
                await self.client.set_charge_limit(self.auto.set_charge_limit)
            await self.client.start_charging()
        except AUTH_ERRORS as exc:
            LOGGER.error(
                "authentication failed: %s; re-run 'tesla auth login'", exc
            )
            await self._notify(
                "auth_failed",
                f"authentication failed: {exc}; re-run 'tesla auth login'",
                priority=5,
                tags=("rotating_light",),
            )
            raise _ExitCode(2) from exc
        except (TeslaFleetError, aiohttp.ClientError) as exc:
            if self.once:
                LOGGER.error("trigger failed: %s", exc)
                await self._notify(
                    "trigger_failed",
                    f"charge trigger failed: {exc}",
                    priority=4,
                    tags=("warning",),
                    cooldown=NOTIFY_TRIGGER_FAILED_COOLDOWN,
                )
                raise _ExitCode(3) from exc
            LOGGER.warning("trigger failed: %s", exc)
            await self._notify(
                "trigger_failed",
                f"charge trigger failed: {exc}",
                priority=4,
                tags=("warning",),
                cooldown=NOTIFY_TRIGGER_FAILED_COOLDOWN,
            )
            return TriggerResult.RETRY
        if await self.client.verify_charging():
            LOGGER.info("charging started")
            return TriggerResult.STARTED
        state = None
        try:
            state = await self.client.charging_state()
        except (TeslaFleetError, aiohttp.ClientError):
            state = None
        detail = f"vehicle reports {state!r}" if state else "state unavailable"
        LOGGER.error(
            "charge_start was sent but the vehicle is not charging (%s)", detail
        )
        await self._notify(
            "trigger_failed",
            "charge_start was sent but the vehicle is not charging "
            f"({detail}); check Charge on Solar or scheduled charging",
            priority=4,
            tags=("warning",),
            cooldown=NOTIFY_TRIGGER_FAILED_COOLDOWN,
        )
        raise _ExitCode(3)


async def watch(
    client: TeslaAutoChargeClient,
    config: Config,
    *,
    dry_run: bool = False,
    once: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    jitter: Callable[[float, float], float] = random.uniform,
    notifier: Notifier | None = None,
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
        notifier=notifier,
    )
    return await charger.run()


async def run(
    client: TeslaAutoChargeClient,
    config: Config,
    *,
    dry_run: bool = False,
    once: bool = False,
    notifier: Notifier | None = None,
) -> int:
    """Run the watch loop with SIGINT/SIGTERM handling; returns exit code."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed: list[int] = []
    watch_task = asyncio.create_task(
        watch(client, config, dry_run=dry_run, once=once, notifier=notifier)
    )
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
        if notifier is not None and not dry_run:
            await notifier.send(
                "stopped",
                "watch stopped by signal",
                priority=3,
                tags=["stop_sign"],
            )
        return 0
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        stopper.cancel()
        with suppress(asyncio.CancelledError):
            await stopper
