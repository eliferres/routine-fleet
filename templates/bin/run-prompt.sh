#!/bin/sh
# One runner for the whole fleet: the roster passes the prompt path as $1, and
# the guard exports FLEET_ROUTINE and FLEET_SLOT for logging or idempotency keys.
# Replace the body with the command that actually executes a prompt in your stack.
set -eu
echo "[$FLEET_ROUTINE] slot $FLEET_SLOT — executing $1"
