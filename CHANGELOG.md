# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added
- Work checks: a routine can name a `work_check` script that answers "anything to do?" before the run. A check cancels the run by printing `nothing-to-do` and exiting 0, at the cost of the script; anything else fails open and runs the routine, including a crashed interpreter's exit 1, GNU `timeout`'s own 125, an empty script's silent 0, and any other output. `work_check_seconds` (default 5) caps how long a check may take, and `--ignore-work-check` overrides it for one run.
- `SKIPPED` in the watchdog report for a run its work check cancelled, with the number of slots skipped since the routine last really ran. The count is printed, not alerted on: `SKIPPED` does not flag the report or change its exit code.
- Late runs are refused: a run starting more than `replay_window_minutes` (default 120) after its slot is a scheduler catching up on a slot it missed, so `run` exits 3 without running it and the watchdog still reports the slot as `MISSED`. Set the window per roster or per routine, 0 turns the rule off, and `--allow-late` runs a deliberate catch-up.
- Installable with `pipx install git+https://github.com/eliferres/routine-fleet`, which puts a `routine-fleet` command on your PATH; `routine-fleet --version` prints the version.

### Changed
- `run` now refuses a run that starts more than two hours after its slot, which 1.1.0 ran. An existing roster gets the rule on upgrade: set `replay_window_minutes` above your largest `grace_minutes`, or to 0 to keep the old behavior.
- CI tests on Python 3.9, 3.11 and 3.13 (was 3.12).
- The README leads with Install, then what the tool checks, the walkthrough, and a new Exit codes table listing 0, 1, 2 and 3.
- Renamed fleet.py to routine_fleet.py, so an install cannot shadow another package named `fleet`. Generated crontab lines now call routine_fleet.py; regenerate your block.

### Fixed
- A run-log line that parses as JSON but carries no `slot` is now counted as unreadable, like any other damaged line. `report` used to abort with a traceback on it.
- The demo transcript and picture now cover all five walkthrough commands: the roster lint and the parity check had no recorded receipt.
- The demo picture test now replays whole transcript entries in order, so a dropped or reordered row in the picture fails the suite.
- The documented exit codes now match the tool: `run` passes the routine's own exit code through, and reports 127 when the routine cannot be started.
- `crontab` with no `--install-dir` now schedules a file that exists: this module's own path from a clone, or the installed command's resolved path when installed. It used to name `routine_fleet.py` beside the roster, which is usually nothing at all.
- Error lines now start with the name the tool was invoked as (`routine-fleet` installed, `routine_fleet.py` from a clone) instead of the old `fleet` name.
- `run` with a directory it cannot create now exits 2 with one line naming that exact directory, marker or state, and the cause, instead of failing later on a missing run-log file.
- The demo transcript and picture show the real exit code of the refused twin (3, was recorded as 1) and a placeholder marker path instead of a real temp folder; a test now replays the transcript and fails if either drifts.
- The README opener says each thing once; it had two paragraphs describing the same four parts.

## [1.1.0](https://github.com/eliferres/routine-fleet/releases/tag/v1.1.0) - 2026-09-03

### Added
- Added a terminal demo to the README's first screen, showing fleet.py refusing to run a routine twice in the same slot, then the watchdog report flagging a missed run and a rotted routine.
- Added macos-latest to the CI matrix alongside ubuntu-latest.

### Changed
- Added full type hints to all 29 functions and methods in fleet.py, with no behavior change.

## [1.0.0](https://github.com/eliferres/routine-fleet/releases/tag/v1.0.0) - 2026-08-31

First public release.
