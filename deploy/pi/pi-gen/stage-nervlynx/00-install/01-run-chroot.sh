#!/bin/bash -e
# Runs inside the image: the same installer people run on a Pi, in image mode.

bash /opt/nervlynx-setup/install.sh --image --no-interfaces --user "${FIRST_USER_NAME}" --ref "$(cat /opt/nervlynx-setup/ref)"
rm -rf /opt/nervlynx-setup
