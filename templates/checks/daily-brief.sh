#!/bin/sh
# A work check: is there anything for daily-brief to do right now?
#
# Print `nothing-to-do` and exit 0 to cancel the run. Anything else runs the
# routine, so you never have to be clever here: if this script breaks, crashes,
# hangs or is deleted, the routine simply runs as it always did.
#
# FLEET_ROUTINE, FLEET_SLOT and FLEET_RUNS are in the environment, so one script
# can answer for several routines.
set -u

QUEUE="${FLEET_QUEUE:-inbox}"

# Cancel only on a queue this script has seen and found empty. A missing,
# renamed or unreadable folder says nothing, so the routine runs: an empty
# answer from a check that could not look would skip every run forever.
if [ -d "$QUEUE" ] && listing=$(ls -A "$QUEUE") && [ -z "$listing" ]; then
  echo nothing-to-do
fi
exit 0
