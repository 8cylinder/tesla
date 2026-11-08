# Notifications with ntfy

The CLI can push [ntfy](https://ntfy.sh) notifications at each
auto-charge stage and after manual charging commands.

## Get started

1. Install the ntfy app
   ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy),
   [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)) or open the web app
   at <https://ntfy.sh/app>, and subscribe to a topic.

2. Pick a hard-to-guess topic. On public ntfy.sh the topic name is
   effectively a **password** — anyone who knows it can read your
   notifications. The web app includes a topic generator.

3. Put the credentials in `.env` next to the Tesla ones (or export them):

   ```shell
   TESLA_NTFY_TOPIC=tesla-<unguessable>
   # optional, for a protected topic or self-hosted server:
   TESLA_NTFY_TOKEN=tk_...
   TESLA_NTFY_URL=https://ntfy.example.com
   ```

4. Enable it and send a test:

   ```shell
   uv run tesla config set notify.enabled true
   uv run tesla notify test
   ```

## Powerwall level watch

`tesla powerwall notify [--level N]` watches the battery level and sends
one notification each charge cycle when it reaches N% (default 100). It
does not re-arm until the level drops below N, so leaving it running as
a service produces a single push per cycle. `--level`/`-l` accepts a
percentage in (0, 100], and `--interval` (default 300 s) sets the poll
rate. The latch is in memory, so a process restart re-arms it.

While waiting, each poll logs a straight-line projection of when the
level will be reached (`eta: 1:10pm`) fitted over the last five
readings; it is omitted while the level is flat or falling. Treat it as
a rough estimate, since solar charging rarely holds a steady rate.

After the notification, polling slows to once an hour until the level
drops below N again. The normal `--interval` rate then resumes and the
ETA is recalculated from fresh readings, so the next cycle is watched
in full.

```bash
uv run tesla powerwall notify --level 90
```

## What gets sent

| Stage                        | When                                                                                        | Priority |
|------------------------------|---------------------------------------------------------------------------------------------|----------|
| on_start                     | watcher starts/restarts: snapshot of the current Powerwall/situation                        | 2        |
| wait_plug                    | entered "at threshold, waiting": asleep, unplugged with `require_plugged`, or unknown state | 3        |
| charging                     | `charge_start` verified `Charging`                                                          | 4        |
| already_charging             | entered "at threshold, already charging/complete"                                           | 2        |
| charging_paused              | CoS-gate only: paused for insufficient solar, will resume                                   | 3        |
| charging_stopped             | CoS-gate only: latched off below `threshold - stop_margin`                                  | 3        |
| waiting_powerwall            | CoS-gate only: waiting for the Powerwall to reach `threshold`                               | 3        |
| cos_disabled                 | CoS-gate only: Charge on Solar not enabled, not starting                                    | 5        |
| cos_lower_limit              | CoS-gate only: car below the CoS lower limit, not starting                                  | 3        |
| charging_on_grid             | CoS-gate only: grid/Powerwall charging detected, stopped                                    | 4        |
| trigger_failed / poll_failed | wake, command, verification, or poll error                                                  | 4 / 3    |
| gave_up                      | 5 consecutive failures                                                                      | 5        |
| auth_failed                  | token problem; re-run `tesla auth login`                                                    | 5        |
| max_runtime                  | `max_runtime_hours` reached before a trigger                                                | 4        |
| stopped                      | SIGINT/SIGTERM in resident mode                                                             | 3        |
| manual                       | after `car charge start` / `stop` / `limit`                                                 | 3        |

`on_start` sends one snapshot when the watcher starts, so you always know
the current state: on every process start for the resident loop, and for
`--once` timer runs when there is no prior run or the gap since the last
run exceeded two poll intervals (i.e. after a reboot, sleep, or service
stop — not on every 5-minute tick). The situation it reports also
suppresses the immediate duplicate condition message.

Condition notifications (wait_plug, charging, already_charging) are sent
only when the situation **changes**. Below-threshold polls silently
update the remembered situation, so the next threshold crossing notifies
again. The situation and last-run time are persisted in
`notify_state.json` beside `config.toml` (`tesla --config` prints the
directory), so it works the same for the resident loop and for per-run
systemd timer invocations — no repeated pushes while nothing changes.
Failure and lifecycle stages are events and are sent whenever they
happen. `--dry-run` sends nothing and does not change the remembered
situation. A failed publish is logged and ignored; it never affects the
watch loop.

## Tuning what you get

- `notify.min_priority` drops lower-value stages. For example,
  `tesla config set notify.min_priority 4` leaves only threshold,
  charging, trigger_failed, max_runtime, and the priority-5 failures.
- `notify.verbose true` ignores both the situation-change check and the
  cooldowns, so every stage is sent every run — handy while learning
  what the service does. Turn it back off for normal operation.
  `tesla notify reset` clears the remembered situation and cooldowns
  (useful to re-see a stage immediately, or to force the `on_start`
  snapshot on the next run).
- `notify.tags` and the per-stage tags render as emoji/text prefixes and
  are the easiest visual classifier (`battery`, `electric_plug`, `zap`,
  `warning`, `rotating_light`).

iPhone note: the ntfy iOS app has no per-priority channels and cannot
pause a subscription (Android can), so every ntfy alert looks and sounds
the same at the iOS level. Tune on the server with the settings above
rather than in the app; Android users additionally get per-priority
notification channels they can customize.

Verbose mode on a 5-minute timer would exceed ntfy.sh's 250 messages/day
(normal mode only sends when something changes) — use manual `--once`
runs while observing, or temporarily lengthen `OnUnitActiveSec`.
