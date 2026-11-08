from tesla.powerwall import backup_reserve, grid_status, percentage_charged


def test_percentage_direct():
    assert (
        percentage_charged({"response": {"percentage_charged": 73.5}}) == 73.5
    )


def test_percentage_direct_wins_over_ratio():
    data = {
        "response": {
            "percentage_charged": 50.0,
            "energy_left": 1,
            "total_pack_energy": 100,
        }
    }
    assert percentage_charged(data) == 50.0


def test_percentage_ratio_fallback():
    data = {"response": {"energy_left": 2500, "total_pack_energy": 10000}}
    assert percentage_charged(data) == 25.0


def test_percentage_zero_pack_energy():
    data = {"response": {"energy_left": 1, "total_pack_energy": 0}}
    assert percentage_charged(data) is None


def test_percentage_missing():
    assert percentage_charged({}) is None
    assert percentage_charged({"response": "nope"}) is None


def test_percentage_rejects_bool():
    assert percentage_charged({"percentage_charged": True}) is None


def test_backup_reserve_and_grid_status():
    data = {
        "response": {
            "backup_reserve_percent": 20,
            "grid_status": "SystemGridConnected",
        }
    }
    assert backup_reserve(data) == 20.0
    assert grid_status(data) == "SystemGridConnected"
    assert backup_reserve({}) is None
    assert grid_status({}) is None
