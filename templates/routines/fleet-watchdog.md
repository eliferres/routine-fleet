# fleet-watchdog

Run `routine_fleet.py report`. If the header is not `ALL CLEAR`, deliver the report to
the owner of every flagged routine — mail, chat, ticket, whatever is read.

Then run `routine_fleet.py parity` against each machine that must carry this fleet, and
deliver any difference the same way.
