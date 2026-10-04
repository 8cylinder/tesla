from tesla.car import charge_state, charging_state, is_plugged

VEHICLE = {
    "response": {
        "charge_state": {
            "battery_level": 80,
            "charging_state": "Charging",
            "charge_limit_soc": 90,
        }
    }
}


def test_charge_state():
    state = charge_state(VEHICLE)
    assert state is not None
    assert state["battery_level"] == 80


def test_charge_state_missing():
    assert charge_state({}) is None
    assert charge_state({"response": {"climate_state": {}}}) is None


def test_charging_state():
    assert charging_state(VEHICLE) == "Charging"
    assert charging_state({}) is None


def test_is_plugged():
    assert is_plugged(VEHICLE) is True
    disconnected = {"response": {"charge_state": {"charging_state": "Disconnected"}}}
    assert is_plugged(disconnected) is False
    assert is_plugged({}) is None
