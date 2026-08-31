# fleet-watchdog

Run `fleet.py report` against the roster and read the output. If the header is
not `ALL CLEAR`, send the whole report to the owner of every flagged routine.

The watchdog is a roster entry like any other, so its own silence is caught by
the next run of itself — and by the parity check, which sees it disappear from
the scheduler.
