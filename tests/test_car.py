from tesla.car import (
    ChargeOnSolarSettings,
    VehicleStatus,
    battery_level,
    charge_on_solar_reason,
    charge_on_solar_settings,
    charge_on_solar_state,
    charge_state,
    charging_state,
    is_plugged,
    vehicle_status,
)

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
    disconnected = {
        "response": {"charge_state": {"charging_state": "Disconnected"}}
    }
    assert is_plugged(disconnected) is False
    assert is_plugged({}) is None


MANAGED = {
    "response": {
        "charge_state": {
            "battery_level": 61,
            "charging_state": "Stopped",
            "managed_charging_state": {
                "charge_on_solar_state": {
                    "no_charge_recommended": {
                        "reason": (
                            "CHARGE_ON_SOLAR_NO_CHARGE_REASON_"
                            "INSUFFICIENT_SOLAR"
                        )
                    }
                },
                "minutes_to_lower_limit": 12,
            },
        }
    }
}


def test_battery_level():
    assert battery_level(VEHICLE) == 80
    assert battery_level({}) is None
    assert (
        battery_level({"response": {"charge_state": {"battery_level": True}}})
        is None
    )


def test_charge_on_solar_state_and_reason():
    assert charge_on_solar_state(MANAGED) == "no_charge_recommended"
    assert charge_on_solar_reason(MANAGED) == "INSUFFICIENT_SOLAR"
    excess = {
        "response": {
            "charge_state": {
                "managed_charging_state": {
                    "charge_on_solar_state": {"charging_on_excess_solar": {}}
                }
            }
        }
    }
    assert charge_on_solar_state(excess) == "charging_on_excess_solar"
    assert charge_on_solar_reason(excess) is None
    plain = {
        "response": {
            "charge_state": {
                "managed_charging_state": {
                    "charge_on_solar_state": "user_stopped"
                }
            }
        }
    }
    assert charge_on_solar_state(plain) == "user_stopped"


def test_vehicle_status():
    status = vehicle_status(MANAGED)
    assert status.charging_state == "Stopped"
    assert status.battery_level == 61
    assert status.cos_state == "no_charge_recommended"
    assert status.cos_reason == "INSUFFICIENT_SOLAR"
    assert vehicle_status({}) == VehicleStatus()


def test_charge_on_solar_settings_nested_and_camel():
    snake = {
        "response": {
            "getChargeOnSolarFeatureResponse": {
                "charge_on_solar": {
                    "enabled": True,
                    "lower_charge_limit": 50,
                    "upper_charge_limit": 80,
                }
            }
        }
    }
    assert charge_on_solar_settings(snake) == ChargeOnSolarSettings(
        True, 50.0, 80.0
    )
    camel = {
        "chargeOnSolar": {
            "enabled": False,
            "lowerChargeLimit": 20,
            "upperChargeLimit": 90,
        }
    }
    assert charge_on_solar_settings(camel) == ChargeOnSolarSettings(
        False, 20.0, 90.0
    )
    assert charge_on_solar_settings({}) is None
    assert charge_on_solar_settings(None) is None
