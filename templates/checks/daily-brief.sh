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

if [ ! -d "$QUEUE" ] || [ -z "$(ls -A "$QUEUE" 2>/dev/null)" ]; then
  echo nothing-to-do
fi
exit 0
