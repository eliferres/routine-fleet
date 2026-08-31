#!/bin/sh
# Stand-in for a real runner. Swap the echo for whatever executes a prompt file
# in your stack (an agent CLI, a python script, a curl to a queue).
set -eu
echo "[$FLEET_ROUTINE] slot $FLEET_SLOT — would execute $1"
