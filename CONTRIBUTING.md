# Contributing

Welcome things:

- New parity adapters (launchd, systemd timers, a hosted scheduler's API),
  each with a test that feeds it a real export.
- New watchdog states, with the failure they catch named in one line.
- Fixes to anything the README claims that turns out not to be true.

Ground rules: `fleet.py` stays standard-library only and stays one file, the
roster stays plain JSON, and every change keeps
`python3 -m unittest discover -s tests` green. Tests use real rosters in temp
directories and injected timestamps: no mocks, and nothing that needs cron
installed. Structural proposals belong in an issue before a PR; the pattern is
deliberately small, and most feature ideas are better as forks.
