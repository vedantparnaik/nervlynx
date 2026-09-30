#!/bin/bash -e
# Runs on the build host: stage the installer in the image and switch on I2C and SPI.
# (Not in /tmp: pi-gen mounts a fresh tmpfs there inside the chroot.)

install -d "${ROOTFS_DIR}/opt/nervlynx-setup"
install -m 755 files/install.sh "${ROOTFS_DIR}/opt/nervlynx-setup/install.sh"
install -m 644 files/ref "${ROOTFS_DIR}/opt/nervlynx-setup/ref"

CONFIG="${ROOTFS_DIR}/boot/firmware/config.txt"
[ -f "${CONFIG}" ] || CONFIG="${ROOTFS_DIR}/boot/config.txt"
grep -q '^dtparam=i2c_arm=on' "${CONFIG}" || echo 'dtparam=i2c_arm=on' >> "${CONFIG}"
grep -q '^dtparam=spi=on' "${CONFIG}" || echo 'dtparam=spi=on' >> "${CONFIG}"
grep -q '^i2c-dev' "${ROOTFS_DIR}/etc/modules" || echo 'i2c-dev' >> "${ROOTFS_DIR}/etc/modules"
