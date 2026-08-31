# fleet-watchdog

Run `fleet.py report`. If the header is not `ALL CLEAR`, deliver the report to
the owner of every flagged routine — mail, chat, ticket, whatever is read.

Then run `fleet.py parity` against each machine that must carry this fleet, and
deliver any difference the same way.
