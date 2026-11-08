# Troubleshooting

- `412 Precondition Failed` on `products`: the app has not completed
  partner registration — do step 4 of {doc}`setup`.
- `charge trigger failed: ... Content-Type ... application/json`: a
  known tesla-fleet-api issue where bodyless POSTs (`wake_up`, unsigned
  commands) omit the JSON Content-Type; this CLI patches it in
  `client.py` (`_fix_empty_json`). Remove the patch when upstream fixes
  the default.
- `charge_start was sent but the vehicle is not charging`: the CLI waits
  about 60 s (six reads, 10 s apart) before giving up and logs the state
  it last saw. Common causes: Tesla's **Charge on Solar** is enabled (it
  owns charging while waiting for solar), a scheduled/departure-time
  charge is set, or the battery is already at its charge limit. Turn off
  Charge on Solar if you want this CLI to command charging
  unconditionally, or switch to the CoS gate
  (`auto_charge.charge_on_solar = true`), which expects CoS to be parked
  and does not treat that as a failure.
- `Tesla did not return a refresh token` or other auth errors: re-run
  `tesla auth login`; authorization codes expire quickly.
- `tokens.json is mode 0644`: the CLI refuses to read a token file with
  group/other permissions — `chmod 600`.
- The car asleep with `require_plugged = true` is reported as waiting,
  without waking it; set `auto_charge.require_plugged false` to allow
  charging regardless.
- Never run `tesla auth status` while the service is running: both
  rotate the refresh token, and only `auto-charge` is protected by the
  lock file.
- More detail: `tesla -vv <command>` or `--log-file`.
