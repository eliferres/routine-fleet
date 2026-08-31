# routine-fleet

Scheduled AI routines rot silently. One stops firing and nobody notices for
three weeks; another fires twice and double-writes the same report; an account
migration quietly drops half the fleet. Every one of those failures is invisible
because a job that does not run produces no output to look at.

This is the smallest honest fix: one roster as the source of truth, a run guard
that refuses a second run in the same slot, a watchdog that says per routine
whether the last due slot actually happened, and a parity check that diffs the
live scheduler against the roster. One Python file, standard library only.

![ci](https://github.com/eliferres/routine-fleet/actions/workflows/ci.yml/badge.svg)

## Quick start

```bash
git clone https://github.com/eliferres/routine-fleet.git
cd routine-fleet
python3 fleet.py --roster demo/fleet.json validate    # zero dependencies, Python 3.9+
```

That lints the shipped demo fleet. The [walkthrough](#walkthrough) below runs
the whole pattern — guard, twin refusal, watchdog, parity — offline, from this
fresh clone, in five commands.

To adopt it: copy `templates/` next to `fleet.py`, replace the example routines
with yours, then `python3 fleet.py crontab --install-dir /opt/fleet` and paste
the block into your scheduler.

## The four pieces

**One canonical roster.** `fleet.json` holds every routine's name, cron
schedule, prompt or script path, run command, and owner. Everything else derives
from it: the crontab block is generated from it, the watchdog iterates it, the
parity check diffs against it. A routine that is not in the roster does not
exist — that is the whole point of having one.

**A run guard the scheduler actually invokes.** Cron calls `fleet.py run
<name>`, never the routine directly. The guard resolves which scheduled slot
`now` belongs to, claims a marker file for that slot with an exclusive create,
and refuses loudly if the slot is already claimed. Two schedulers racing the
same minute cannot both win it. Start and completion — with the exit code — go
to a run log.

**A watchdog that checks the checkers.** `fleet.py report` reads only the roster
and the run log. For each routine it finds the last slot that is past its grace
window and asks whether that slot completed. Silence, non-zero exits, refused
twins, and rot (a roster entry whose prompt file no longer exists) each get a
named line and a loud header. The watchdog is itself a roster entry, so its own
silence shows up in its next report and in every parity check.

**A parity check for fleets that must exist twice.** Two machines, two accounts,
a staging box: `fleet.py parity` reads the live scheduler through an adapter —
`crontab` or a generic `json` export — and prints exactly what is missing,
extra, or drifted. Fleet-managed cron lines carry a `# fleet:<name>` tag, so
other people's cron entries are left alone.

## The roster format, verbatim

Every field the roster accepts. Unknown keys are a validation error, so the
roster cannot quietly grow a field nothing reads:

```jsonc
{
  "version": 1,
  "timezone": "UTC",          // documentation only; slots use the local clock
  "grace_minutes": 30,        // fleet-wide default before a late slot is "missed"
  "watchdog": "fleet-watchdog",  // the routine that runs `report`; checked last
  "routines": [
    {
      "name": "daily-brief",           // [a-z0-9][a-z0-9._-]*, unique in the fleet
      "schedule": "30 7 * * *",        // 5-field cron: * , - / and 0-7 for Sunday
      "runs": "routines/daily-brief.md",  // prompt or script, relative to the roster
      "command": ["bin/run-prompt.sh", "{path}"],  // optional; {path} = resolved runs
      "owner": "you@example.com",      // who gets called when this line goes red
      "grace_minutes": 120             // optional per-routine override
    }
  ]
}
```

With no `command`, the guard executes `runs` directly. Every run gets
`FLEET_ROUTINE`, `FLEET_SLOT`, and `FLEET_RUNS` in its environment;
`FLEET_SLOT` doubles as an idempotency key downstream.

## Walkthrough

Five commands against the shipped demo fleet: three routines plus the watchdog,
one healthy, one that went silent, one whose prompt file was deleted. Two
scratch state directories keep the repo clean — the guard writes to one, the
canned run log is copied into the other.

```bash
export FLEET_GUARD="$(mktemp -d)" FLEET_STATE="$(mktemp -d)"
cp demo/state/run-log.jsonl "$FLEET_STATE/"
```

**1. Lint the roster.**

```bash
python3 fleet.py --roster demo/fleet.json validate
# OK demo/fleet.json: roster is well formed.
```

**2. Run a routine through the guard.**

```bash
python3 fleet.py --roster demo/fleet.json --state "$FLEET_GUARD" \
  --now 2026-03-02T08:05 run daily-standup-brief
# [daily-standup-brief] slot 2026-03-02T08:00 — would execute .../daily-standup-brief.md
```

**3. Try to run it again in the same slot.** Exits 3, says why, and does not
run the routine.

```bash
python3 fleet.py --roster demo/fleet.json --state "$FLEET_GUARD" \
  --now 2026-03-02T08:47 run daily-standup-brief
# !!! TWIN REFUSED: daily-standup-brief already ran in slot 2026-03-02T08:00.
#     Refusing to run it twice. Clear <marker path> to force a re-run.
```

**4. Run the watchdog.** Exits 1 whenever anything needs a human.

```bash
python3 fleet.py --roster demo/fleet.json --state "$FLEET_STATE" \
  --now 2026-03-02T09:00 report
```

```text
FLEET REPORT  2026-03-02 09:00  4 routines  roster fleet.json
!!! ATTENTION: 2 of 4 routines flagged

  OK         daily-standup-brief      slot 2026-03-02 08:00
  MISSED     nightly-link-sweep       slot 2026-03-02 02:00  no run recorded
  ROTTED     weekly-access-audit      routines/weekly-access-audit.md is missing
  OK         fleet-watchdog           slot 2026-03-01 09:30  (watchdog, checked last)
```

**5. Check parity against the scheduler.** The shipped example crontab has
drifted three ways; the untagged backup job in it is correctly ignored.

```bash
python3 fleet.py --roster demo/fleet.json parity \
  --adapter crontab --source demo/crontab.example
```

```text
PARITY  roster fleet.json  vs  crontab demo/crontab.example
!!! 3 difference(s)

  DRIFTED  nightly-link-sweep       roster "0 2 * * *", scheduler "0 3 * * *"
  MISSING  weekly-access-audit      roster "0 6 * * 1", not scheduled here
  EXTRA    legacy-cleanup           scheduler "0 5 * * *", not in the roster
```

Omit `--source` and the crontab adapter reads the live `crontab -l` instead.

## What is in the box

| Path | Role |
|---|---|
| `fleet.py` | The whole tool: `validate`, `run`, `report`, `parity`, `crontab`. |
| `templates/fleet.json` | The roster to copy and edit. |
| `templates/routines/` | Prompt files written so their output is checkable. |
| `templates/bin/run-prompt.sh` | One runner for the fleet; swap the body for your stack. |
| `templates/crontab.example` | Generated by `fleet.py crontab`, tags and all. |
| `demo/` | A four-routine fleet with canned history: healthy, silent, rotted. |
| `tests/test_fleet.py` | Real rosters in temp dirs, injected timestamps, no mocks. |

## What the watchdog enforces

Five states, each guarding a way a fleet actually dies:

1. `MISSED` — the last due slot has no run at all. This is the failure that
   costs weeks, because silence looks exactly like success.
2. `FAILED` / `INCOMPLETE` — the run started and either exited non-zero or never
   recorded a completion. A crashed routine still produces no output; without
   the log it is indistinguishable from a missed slot.
3. `ROTTED` — the roster points at a prompt or script that no longer exists.
   Catches the rename nobody propagated.
4. Refused twins — the guard did its job, but something invoked the routine
   twice in one slot, and that cause is still there.
5. Unreadable run-log lines are counted, never skipped. The log is the only
   evidence there is, so a corrupt log must not look like a healthy one.

`report` exits 1 on any of these, which is what turns it into an alert: cron
mails the output, or the watchdog prompt reads the header and escalates to the
owner named in the roster.

## Why a roster, not a scheduler UI

Cron, launchd, systemd timers, and every hosted scheduler will happily tell you
what is scheduled. None of them tell you what was *supposed* to be scheduled.
The roster is the declared intent, in version control, reviewable in a diff; the
scheduler is just one deployment of it, and parity is the check that the two
still agree. That inversion is what survives an account migration: the fleet is
a file, and rebuilding it elsewhere is one generated crontab block away.

Keeping the guard in front of every routine is the same move applied to time.
The scheduler knows when it fired; only the marker knows which slot that firing
belonged to, and only a slot identity makes "did this already run?" answerable.

## Limitations

- The watchdog reads the log the guards write, so a routine invoked outside
  `fleet.py run` is invisible to it. Bypassing the guard is the one failure this
  pattern cannot see.
- Cron expressions are evaluated for the previous expected slot only, not full
  history. A routine that missed four slots and then recovered reports `OK`.
- Slots use the local naive clock. A DST shift or a machine in another timezone
  moves slot boundaries, and `timezone` in the roster is documentation, not
  enforcement.
- The slot search walks back at most 62 days, so a cadence rarer than monthly
  reports `NO-SLOT` rather than a real answer.
- Parity sees only entries tagged `# fleet:<name>`. An untagged hand-edited cron
  line running a fleet routine reads as absent.
- `L`, `W`, `#`, `@reboot`, and other non-standard cron extensions are rejected
  by `validate` rather than approximated.

## License

MIT
