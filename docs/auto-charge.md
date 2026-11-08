# Auto-charge semantics

`tesla auto-charge` acquires a single-instance lock, refreshes the
token, then polls the Powerwall every `poll_interval_seconds`. Below the
threshold it only reads the Powerwall. At the threshold it reads the car
and acts on this table:

| Powerwall    | Vehicle             | `require_plugged` | Action                          |
|--------------|---------------------|-------------------|---------------------------------|
| >= threshold | Charging / Complete | any               | done, exit 0                    |
| >= threshold | Disconnected        | true              | wait for plug                   |
| >= threshold | Stopped / NoPower   | any               | wake, set limit, `charge_start` |
| >= threshold | asleep (offline)    | true              | wait, do not wake               |
| >= threshold | asleep (offline)    | false             | wake and start                  |
| < threshold  | any                 | any               | sleep                           |

After a start it verifies `Charging` with a follow-up read and exits 0
(it never stays resident to stop charging later). Transient API errors
back off exponentially (5 s, x2, cap 300 s, ±20% jitter) and give up
after 5 consecutive failures. Exit codes: `0` triggered/nothing to do,
`2` config/auth error, `3` API/vehicle error.

Flags: `--threshold N`, `--interval S`, `--dry-run` (never wakes or
commands), `--once` (one evaluation pass, no sleeping).

## Charge on Solar gate mode

With `auto_charge.charge_on_solar = true`, the loop stops being a
one-shot trigger and becomes a gate around the charge switch while
**Charge on Solar** stays enabled in the Tesla app (the CLI never
changes the CoS setting):

- Start charging at `threshold` (set it to 95% for the Powerwall-first
  behavior) when the car is stopped and its SOC is at or above the CoS
  lower limit.
- Leave CoS alone while it charges on surplus solar, pauses for clouds,
  or reports `POWERWALL_CHARGE_PRIORITY`.
- Below `threshold - stop_margin` (default 5%, so below 90%), latch
  charging off with `charge_stop`; CoS stays enabled but will not resume
  by itself until the loop sends `charge_start` again at the threshold.
- Stop any non-solar charging (`charging_on_anything`) and alert.
- Never start when CoS is disabled or the car SOC is below the CoS
  lower limit (that would allow any-source charging and drain the
  Powerwall).

The decision function is a flat truth table over
`charge_state.managed_charging_state.charge_on_solar_state` and the
Powerwall reading; the executor performs the wake/start/stop. If the
REST `vehicle_data` call does not carry `managed_charging_state` on a
given firmware, the loop falls back to `charging_state`. `tesla car
status` shows CoS enabled/disabled, the live state and reason, and the
lower/upper limits.

```bash
uv run tesla config set auto_charge.charge_on_solar true
uv run tesla config set auto_charge.threshold 95
uv run tesla config set auto_charge.stop_margin 5
```

`--once` performs one evaluation pass (timer mode); a resident
`tesla auto-charge` keeps running and latches/re-arms across the day.
Exit codes and backoff are unchanged.
