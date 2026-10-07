import asyncio
import os
import signal
import time

import pytest
from tesla_fleet_api.exceptions import (
    InvalidToken,
    ServiceUnavailable,
    VehicleOffline,
)

from tesla.auto_charge import (
    Action,
    AlreadyRunning,
    Decision,
    LockFile,
    backoff_delay,
    decide,
    decide_charge_on_solar,
    run,
    watch,
)
from tesla.car import ChargeOnSolarSettings, VehicleStatus
from tesla.config import load_config


@pytest.mark.parametrize(
    ("pct", "state", "require_plugged", "expected"),
    [
        (None, "Charging", True, Action.SLEEP),
        (99.9, "Stopped", True, Action.SLEEP),
        (100.0, "Charging", True, Action.DONE),
        (100.0, "Complete", True, Action.DONE),
        (100.0, "Disconnected", True, Action.WAIT_PLUG),
        (100.0, "Disconnected", False, Action.START),
        (100.0, "Stopped", True, Action.START),
        (100.0, "NoPower", True, Action.START),
        (100.0, "Asleep", True, Action.WAIT),
        (100.0, "Asleep", False, Action.START),
        (100.0, None, True, Action.WAIT),
        (100.0, "Weird", True, Action.WAIT),
    ],
)
def test_decide_truth_table(pct, state, require_plugged, expected):
    assert decide(pct, 100.0, state, require_plugged) is expected


def test_backoff_delay_without_jitter():
    def flat(a, b):
        return 1.0

    assert backoff_delay(1, flat) == 5.0
    assert backoff_delay(2, flat) == 10.0
    assert backoff_delay(3, flat) == 20.0
    assert backoff_delay(7, flat) == 300.0
    assert backoff_delay(3, lambda a, b: 0.8) == 16.0


def test_lock_round_trip(config_dir):
    path = config_dir / "auto-charge.lock"
    lock = LockFile(path)
    lock.acquire()
    assert path.read_text().strip() == str(os.getpid())
    lock.release()
    assert not path.exists()


def test_lock_live_pid_blocks(config_dir):
    path = config_dir / "auto-charge.lock"
    path.write_text(str(os.getpid()))
    with pytest.raises(AlreadyRunning):
        LockFile(path).acquire()


def test_lock_stale_pid_replaced(config_dir):
    path = config_dir / "auto-charge.lock"
    path.write_text("999999")
    LockFile(path).acquire()
    assert path.read_text().strip() == str(os.getpid())


class FakeClient:
    def __init__(
        self,
        pcts=None,
        states=None,
        wake_error=None,
        start_error=None,
        verify=True,
        error=None,
    ):
        self.pcts = list(pcts) if pcts is not None else []
        self.states = list(states) if states is not None else []
        self.current_pct = 0.0
        self.current_state = "Disconnected"
        self.wake_error = wake_error
        self.start_error = start_error
        self.verify = verify
        self.error = error
        self.refreshes = 0
        self.woken = 0
        self.started = 0
        self.limits = []

    async def refresh(self):
        self.refreshes += 1
        if self.error is not None:
            raise self.error

    async def powerwall_percentage(self):
        if self.pcts:
            self.current_pct = self.pcts.pop(0)
        return self.current_pct

    async def charging_state(self):
        if self.states:
            self.current_state = self.states.pop(0)
        if isinstance(self.current_state, BaseException):
            raise self.current_state
        return self.current_state

    async def wake(self):
        if self.wake_error is not None:
            raise self.wake_error
        self.woken += 1

    async def set_charge_limit(self, percent):
        self.limits.append(percent)

    async def start_charging(self):
        if self.start_error is not None:
            raise self.start_error
        self.started += 1

    async def verify_charging(self):
        return self.verify


async def test_watch_triggers_at_threshold(config_dir):
    client = FakeClient(pcts=[100.0], states=["Stopped"])
    assert await watch(client, load_config()) == 0
    assert (client.woken, client.started) == (1, 1)


async def test_watch_done_when_charging(config_dir):
    client = FakeClient(pcts=[100.0], states=["Charging"])
    assert await watch(client, load_config()) == 0
    assert (client.woken, client.started) == (0, 0)


async def test_watch_waits_for_plug(config_dir):
    client = FakeClient(pcts=[100.0], states=["Disconnected"])
    assert await watch(client, load_config(), once=True) == 0
    assert (client.woken, client.started) == (0, 0)


async def test_watch_below_threshold_never_reads_car(config_dir):
    client = FakeClient(pcts=[42.0])
    assert await watch(client, load_config(), once=True) == 0
    assert client.states == []


async def test_watch_dry_run_sends_nothing(config_dir):
    client = FakeClient(pcts=[100.0], states=["Stopped"])
    assert await watch(client, load_config(), dry_run=True) == 0
    assert (client.woken, client.started, client.limits) == (0, 0, [])


async def test_watch_backoff_gives_up(config_dir):
    client = FakeClient(error=ServiceUnavailable())
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    assert (
        await watch(client, load_config(), sleep=fake_sleep, jitter=lambda a, b: 1.0)
        == 3
    )
    assert delays == [5.0, 10.0, 20.0, 40.0]


async def test_watch_wake_timeout_once(config_dir):
    client = FakeClient(pcts=[100.0], states=["Stopped"], wake_error=VehicleOffline())
    assert await watch(client, load_config(), once=True) == 3


async def test_watch_asleep_car_waits_when_plug_required(config_dir):
    client = FakeClient(pcts=[100.0], states=[VehicleOffline()])
    assert await watch(client, load_config(), once=True) == 0
    assert (client.woken, client.started) == (0, 0)


async def test_watch_wakes_asleep_car_when_plug_not_required(config_dir):
    config = load_config()
    config.auto_charge.require_plugged = False
    client = FakeClient(pcts=[100.0], states=[VehicleOffline()])
    assert await watch(client, config) == 0
    assert (client.woken, client.started) == (1, 1)


async def test_watch_dry_run_asleep_sends_nothing(config_dir):
    config = load_config()
    config.auto_charge.require_plugged = False
    client = FakeClient(pcts=[100.0], states=[VehicleOffline()])
    assert await watch(client, config, dry_run=True) == 0
    assert (client.woken, client.started) == (0, 0)


async def test_watch_verify_failure_exits_3(config_dir):
    client = FakeClient(pcts=[100.0], states=["Stopped"], verify=False)
    assert await watch(client, load_config()) == 3
    assert client.started == 1


async def test_run_sigint_exits_cleanly(config_dir):
    client = FakeClient(pcts=[0.0])
    task = asyncio.create_task(run(client, load_config()))
    await asyncio.sleep(0.05)
    os.kill(os.getpid(), signal.SIGINT)
    assert await asyncio.wait_for(task, timeout=2) == 0


class FakeNotifier:
    def __init__(self):
        self.calls = []
        self.on_changes = []
        self.cooldowns = []
        self.situations = []
        self.situation = None
        self.last_run_value = None
        self.touch_calls = 0

    async def send(self, stage, message, **kwargs):
        on_change = kwargs.get("on_change")
        if on_change is not None and self.situation == on_change:
            return False
        if on_change is not None:
            self.situation = on_change
        self.calls.append(stage)
        self.on_changes.append(on_change)
        self.cooldowns.append(kwargs.get("cooldown"))
        return True

    def note_situation(self, value):
        self.situation = value
        self.situations.append(value)

    def last_run(self):
        return self.last_run_value

    def touch(self):
        self.touch_calls += 1
        self.last_run_value = time.time()


async def test_watch_notifies_charging(config_dir):
    notifier = FakeNotifier()
    client = FakeClient(pcts=[100.0], states=["Stopped"])
    assert await watch(client, load_config(), notifier=notifier) == 0
    assert notifier.calls == ["on_start", "charging"]
    assert notifier.situations == ["already_charging"]


async def test_watch_startup_report_replaces_wait_plug(config_dir):
    notifier = FakeNotifier()
    client = FakeClient(pcts=[100.0], states=["Disconnected"])
    assert await watch(client, load_config(), once=True, notifier=notifier) == 0
    assert notifier.calls == ["on_start"]
    assert notifier.situations == ["wait_plug"]


async def test_watch_condition_uses_on_change_when_recent(config_dir):
    notifier = FakeNotifier()
    notifier.last_run_value = time.time()
    client = FakeClient(pcts=[100.0], states=["Disconnected"])
    assert await watch(client, load_config(), once=True, notifier=notifier) == 0
    assert notifier.calls == ["wait_plug"]
    assert notifier.on_changes == ["wait_plug"]


async def test_watch_startup_report_after_gap(config_dir):
    notifier = FakeNotifier()
    notifier.last_run_value = time.time() - 3600
    client = FakeClient(pcts=[42.0])
    assert await watch(client, load_config(), once=True, notifier=notifier) == 0
    assert notifier.calls == ["on_start"]


async def test_watch_startup_report_disabled(config_dir):
    config = load_config()
    config.notify.on_start = False
    notifier = FakeNotifier()
    notifier.last_run_value = time.time()
    client = FakeClient(pcts=[100.0], states=["Disconnected"])
    assert await watch(client, config, once=True, notifier=notifier) == 0
    assert notifier.calls == ["wait_plug"]


async def test_watch_notifies_already_charging(config_dir):
    notifier = FakeNotifier()
    client = FakeClient(pcts=[100.0], states=["Charging"])
    assert await watch(client, load_config(), once=True, notifier=notifier) == 0
    assert notifier.calls == ["on_start"]
    assert notifier.situations == ["already_charging"]


async def test_watch_notifies_wait_plug_when_asleep(config_dir):
    notifier = FakeNotifier()
    client = FakeClient(pcts=[100.0], states=[VehicleOffline()])
    assert await watch(client, load_config(), once=True, notifier=notifier) == 0
    assert notifier.calls == ["on_start"]
    assert notifier.situations == ["wait_plug"]


async def test_watch_notifies_wait_plug_when_state_unknown(config_dir):
    notifier = FakeNotifier()
    client = FakeClient(pcts=[100.0], states=[None])
    assert await watch(client, load_config(), once=True, notifier=notifier) == 0
    assert notifier.calls == ["on_start"]


async def test_watch_below_threshold_notes_situation(config_dir):
    notifier = FakeNotifier()
    client = FakeClient(pcts=[42.0])
    assert await watch(client, load_config(), once=True, notifier=notifier) == 0
    assert notifier.calls == ["on_start"]
    assert notifier.situations == ["below_threshold"]


async def test_watch_dry_run_sends_no_notifications(config_dir):
    notifier = FakeNotifier()
    client = FakeClient(pcts=[100.0], states=["Stopped"])
    assert await watch(client, load_config(), dry_run=True, notifier=notifier) == 0
    assert notifier.calls == []
    assert notifier.situations == []
    assert notifier.touch_calls == 0


async def test_watch_notifies_gave_up(config_dir):
    notifier = FakeNotifier()
    client = FakeClient(error=ServiceUnavailable())
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    assert (
        await watch(
            client,
            load_config(),
            sleep=fake_sleep,
            jitter=lambda a, b: 1.0,
            notifier=notifier,
        )
        == 3
    )
    assert notifier.calls == ["poll_failed", "gave_up"]


async def test_watch_notifies_auth_failure(config_dir):
    notifier = FakeNotifier()
    client = FakeClient(error=InvalidToken())
    assert await watch(client, load_config(), notifier=notifier) == 2
    assert notifier.calls == ["auth_failed"]


async def test_watch_trigger_failed_uses_cooldown(config_dir):
    notifier = FakeNotifier()
    notifier.last_run_value = time.time()
    client = FakeClient(
        pcts=[100.0], states=["Stopped"], start_error=ServiceUnavailable()
    )
    assert await watch(client, load_config(), once=True, notifier=notifier) == 3
    assert notifier.calls == ["trigger_failed"]
    assert notifier.cooldowns == [3600]


async def test_run_sigint_notifies_stopped(config_dir):
    notifier = FakeNotifier()
    client = FakeClient(pcts=[0.0])
    task = asyncio.create_task(run(client, load_config(), notifier=notifier))
    await asyncio.sleep(0.05)
    os.kill(os.getpid(), signal.SIGINT)
    assert await asyncio.wait_for(task, timeout=2) == 0
    assert "stopped" in notifier.calls


COS = ChargeOnSolarSettings(enabled=True, lower_limit=20.0, upper_limit=80.0)


@pytest.mark.parametrize(
    ("pct", "state", "battery", "cos_state", "cos_reason", "cos", "action", "reason"),
    [
        (95.0, "Stopped", 60, None, None, COS, Action.START, None),
        (94.9, "Stopped", 60, None, None, COS, Action.SLEEP, "waiting_powerwall"),
        (95.0, "Stopped", 10, None, None, COS, Action.WAIT, "cos_lower_limit"),
        (None, "Stopped", 60, None, None, COS, Action.WAIT, None),
        (92.0, "Charging", 60, None, None, COS, Action.SLEEP, None),
        (89.9, "Charging", 60, None, None, COS, Action.STOP, "charging_stopped"),
        (
            96.0,
            "Charging",
            60,
            "charging_on_anything",
            None,
            COS,
            Action.STOP,
            "charging_on_grid",
        ),
        (
            96.0,
            "Charging",
            60,
            "charging_on_excess_solar",
            None,
            COS,
            Action.SLEEP,
            None,
        ),
        (
            92.0,
            "Stopped",
            60,
            "no_charge_recommended",
            "INSUFFICIENT_SOLAR",
            COS,
            Action.SLEEP,
            "charging_paused",
        ),
        (
            89.0,
            "Stopped",
            60,
            "no_charge_recommended",
            "INSUFFICIENT_SOLAR",
            COS,
            Action.STOP,
            "charging_stopped",
        ),
        (
            89.0,
            "Stopped",
            60,
            "no_charge_recommended",
            "POWERWALL_CHARGE_PRIORITY",
            COS,
            Action.SLEEP,
            "waiting_powerwall",
        ),
        (
            92.0,
            "Stopped",
            60,
            "waiting_for_server",
            None,
            COS,
            Action.SLEEP,
            "charging_paused",
        ),
        (95.0, "Stopped", 60, "user_stopped", None, COS, Action.START, None),
        (
            95.0,
            "Stopped",
            10,
            "user_stopped",
            None,
            COS,
            Action.WAIT,
            "cos_lower_limit",
        ),
        (
            94.9,
            "Stopped",
            60,
            "user_stopped",
            None,
            COS,
            Action.SLEEP,
            "waiting_powerwall",
        ),
        (95.0, "Disconnected", 60, None, None, COS, Action.SLEEP, "wait_plug"),
        (95.0, "Complete", 60, None, None, COS, Action.DONE, None),
        (95.0, "Stopped", 80, None, None, COS, Action.DONE, None),
        (
            95.0,
            "Stopped",
            60,
            None,
            None,
            ChargeOnSolarSettings(enabled=False),
            Action.WAIT,
            "cos_disabled",
        ),
        (95.0, "Stopped", 60, "user_disabled", None, COS, Action.WAIT, "cos_disabled"),
        (95.0, None, None, None, None, COS, Action.WAIT, None),
    ],
)
def test_decide_charge_on_solar(
    pct, state, battery, cos_state, cos_reason, cos, action, reason
):
    status = VehicleStatus(
        charging_state=state,
        battery_level=battery,
        cos_state=cos_state,
        cos_reason=cos_reason,
    )
    decision = decide_charge_on_solar(pct, 95.0, 5.0, status, cos)
    assert decision == Decision(action, reason)


class FakeGateClient:
    def __init__(self, pcts=None, statuses=None, cos=None):
        self.pcts = list(pcts) if pcts is not None else []
        self.statuses = list(statuses) if statuses is not None else []
        self.cos = (
            cos
            if cos is not None
            else ChargeOnSolarSettings(enabled=True, lower_limit=20.0, upper_limit=80.0)
        )
        self.current_pct = 0.0
        self.current_status = VehicleStatus()
        self.woken = 0
        self.started = 0
        self.stopped = 0
        self.limits = []

    async def refresh(self):
        return None

    async def powerwall_percentage(self):
        if self.pcts:
            self.current_pct = self.pcts.pop(0)
        return self.current_pct

    async def vehicle_status(self):
        if self.statuses:
            self.current_status = self.statuses.pop(0)
        return self.current_status

    async def charge_on_solar_settings(self):
        return self.cos

    async def wake(self):
        self.woken += 1

    async def charging_state(self):
        return self.current_status.charging_state

    async def set_charge_limit(self, percent):
        self.limits.append(percent)

    async def start_charging(self):
        self.started += 1

    async def stop_charging(self):
        self.stopped += 1


def gate_config():
    config = load_config()
    config.auto_charge.charge_on_solar = True
    config.auto_charge.threshold = 95.0
    config.auto_charge.stop_margin = 5.0
    return config


async def test_gate_starts_at_threshold(config_dir):
    client = FakeGateClient(
        pcts=[95.0],
        statuses=[VehicleStatus(charging_state="Stopped", battery_level=60)],
    )
    assert await watch(client, gate_config(), once=True) == 0
    assert (client.woken, client.started, client.stopped) == (1, 1, 0)


async def test_gate_ignores_band_while_charging(config_dir):
    client = FakeGateClient(
        pcts=[92.0],
        statuses=[VehicleStatus(charging_state="Charging", battery_level=60)],
    )
    assert await watch(client, gate_config(), once=True) == 0
    assert (client.started, client.stopped) == (0, 0)


async def test_gate_latches_below_floor(config_dir):
    client = FakeGateClient(
        pcts=[89.0],
        statuses=[VehicleStatus(charging_state="Charging", battery_level=60)],
    )
    assert await watch(client, gate_config(), once=True) == 0
    assert (client.started, client.stopped) == (0, 1)


async def test_gate_rearms_when_powerwall_full(config_dir):
    client = FakeGateClient(
        pcts=[95.0],
        statuses=[
            VehicleStatus(
                charging_state="Stopped",
                battery_level=60,
                cos_state="user_stopped",
            )
        ],
    )
    assert await watch(client, gate_config(), once=True) == 0
    assert client.started == 1


async def test_gate_waits_when_cos_disabled(config_dir):
    client = FakeGateClient(
        pcts=[95.0],
        statuses=[VehicleStatus(charging_state="Stopped", battery_level=60)],
        cos=ChargeOnSolarSettings(enabled=False),
    )
    assert await watch(client, gate_config(), once=True) == 0
    assert (client.started, client.stopped) == (0, 0)


async def test_gate_waits_below_lower_limit(config_dir):
    client = FakeGateClient(
        pcts=[95.0],
        statuses=[VehicleStatus(charging_state="Stopped", battery_level=10)],
    )
    assert await watch(client, gate_config(), once=True) == 0
    assert client.started == 0


async def test_gate_stops_non_solar_charge(config_dir):
    notifier = FakeNotifier()
    client = FakeGateClient(
        pcts=[96.0],
        statuses=[
            VehicleStatus(
                charging_state="Charging",
                battery_level=60,
                cos_state="charging_on_anything",
            )
        ],
    )
    assert await watch(client, gate_config(), once=True, notifier=notifier) == 0
    assert client.stopped == 1
    assert "charging_on_grid" in notifier.calls


async def test_gate_paused_above_floor_is_left_alone(config_dir):
    notifier = FakeNotifier()
    client = FakeGateClient(
        pcts=[92.0],
        statuses=[
            VehicleStatus(
                charging_state="Stopped",
                battery_level=60,
                cos_state="no_charge_recommended",
                cos_reason="INSUFFICIENT_SOLAR",
            )
        ],
    )
    assert await watch(client, gate_config(), once=True, notifier=notifier) == 0
    assert (client.started, client.stopped) == (0, 0)
    assert notifier.situations == ["charging_paused"]


async def test_gate_dry_run_sends_nothing(config_dir):
    client = FakeGateClient(
        pcts=[95.0],
        statuses=[VehicleStatus(charging_state="Stopped", battery_level=60)],
    )
    assert await watch(client, gate_config(), dry_run=True, once=True) == 0
    assert (client.woken, client.started, client.stopped) == (0, 0, 0)
