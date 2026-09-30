# Raspberry Pi setup

## One command on Raspberry Pi OS

On a Pi with Raspberry Pi OS Bookworm (Lite is fine), 64-bit on a Pi 5, Pi 4, or Zero 2 W:

```bash
curl -fsSL https://raw.githubusercontent.com/vedantparnaik/nervlynx/main/deploy/pi/install.sh | bash
```

It installs the GPIO, I2C, serial, and camera libraries from apt; creates `~/.venv` with
`--system-site-packages` so it can use them (Bookworm doesn't allow system-wide `pip`);
installs NervLynx into it and puts it on your `PATH`; turns on I2C and SPI; adds you to
the `gpio`, `i2c`, `spi`, `dialout`, `video`, and `audio` groups; lets your services start
at boot (`loginctl enable-linger`); and ends with `nervlynx doctor`. Reboot once
afterwards. Running it again upgrades NervLynx in place.

| Option | Does |
| --- | --- |
| `--with ai,voice,mesh` | Also install object detection (ONNX Runtime), on-robot voice (Vosk, espeak-ng), or the Zenoh mesh |
| `--hostname my-rover` | Rename the Pi so it is `my-rover.local` on the network |
| `--ref v0.3.0` | Install a particular branch, tag, or commit |
| `--source PATH` | Install from a checkout instead of GitHub |
| `--no-interfaces` | Leave the I2C and SPI settings alone |

Pass options through the pipe with `bash -s --`, for example
`curl -fsSL .../install.sh | bash -s -- --with ai --hostname my-rover`.

CI runs this script on Ubuntu for every change (where the Pi-only steps are skipped). It
has not yet been run on each Pi model.

## A ready-made SD card image

The `pi-image` workflow builds Raspberry Pi OS Lite (64-bit) with NervLynx already
installed, using [pi-gen](https://github.com/RPi-Distro/pi-gen) and the same installer
(`pi-gen/stage-nervlynx`). Run it from the repository's Actions tab (choose the NervLynx
version to put in it), or push a `v*` tag to attach the image to that release. Download
the `.img.xz` and flash it with [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
("Use custom"), and set the username, password, Wi-Fi, and SSH in Imager's OS
customisation: the image has SSH off and asks for a user on first boot otherwise.
