# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added
- Installable with `pipx install git+https://github.com/eliferres/routine-fleet`, which puts a `routine-fleet` command on your PATH; `routine-fleet --version` prints the version.

### Changed
- CI tests on Python 3.9, 3.11 and 3.13 (was 3.12).
- Renamed fleet.py to routine_fleet.py, so an install cannot shadow another package named `fleet`. Generated crontab lines now call routine_fleet.py; regenerate your block.

### Fixed
- `run` with a state directory it cannot create now exits 2 with one line naming the directory and the cause, instead of failing later on a missing run-log file.
- The README opener says each thing once; it had two paragraphs describing the same four parts.

## [1.1.0](https://github.com/eliferres/routine-fleet/releases/tag/v1.1.0) - 2026-09-03

### Added
- Added a terminal demo to the README's first screen, showing fleet.py refusing to run a routine twice in the same slot, then the watchdog report flagging a missed run and a rotted routine.
- Added macos-latest to the CI matrix alongside ubuntu-latest.

### Changed
- Added full type hints to all 29 functions and methods in fleet.py, with no behavior change.

## [1.0.0](https://github.com/eliferres/routine-fleet/releases/tag/v1.0.0) - 2026-08-31

First public release.
