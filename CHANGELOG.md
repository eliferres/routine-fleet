# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added
- Installable with `pipx install git+https://github.com/eliferres/routine-fleet`, which puts a `routine-fleet` command on your PATH; `routine-fleet --version` prints the version.

### Changed
- CI tests on Python 3.9, 3.11 and 3.13 (was 3.12).
- The README leads with Install, then what the tool checks, the walkthrough, and a new Exit codes table listing 0, 1, 2 and 3.
- Renamed fleet.py to routine_fleet.py, so an install cannot shadow another package named `fleet`. Generated crontab lines now call routine_fleet.py; regenerate your block.

### Fixed
- `crontab` run as the installed command with no `--install-dir` now schedules `routine-fleet` itself, instead of a source file an installed user does not have; a clone or an `--install-dir` still names `routine_fleet.py`.
- Error lines now start with the name the tool was invoked as (`routine-fleet` installed, `routine_fleet.py` from a clone) instead of the old `fleet` name.
- `run` with a state directory it cannot create now exits 2 with one line naming the directory and the cause, instead of failing later on a missing run-log file.
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
