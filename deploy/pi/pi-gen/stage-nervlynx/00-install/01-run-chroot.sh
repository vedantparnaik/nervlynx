#!/bin/bash -e
# Runs inside the image: the same installer people run on a Pi, in image mode.

bash /tmp/nervlynx-install.sh --image --no-interfaces --user "${FIRST_USER_NAME}" --ref "$(cat /tmp/nervlynx-ref)"
rm -f /tmp/nervlynx-install.sh /tmp/nervlynx-ref
