import asyncio
import os
import signal

import pytest
from tesla_fleet_api.exceptions import ServiceUnavailable, VehicleOffline

from tesla.auto_charge import (
    Action,
    AlreadyRunning,
    LockFile,
    backoff_delay,
    decide,
    run,
    watch,
)
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
