# Freeing the serial UART on a Raspberry Pi 3

The HeaterMeter board connects to the Pi's GPIO UART. By default Raspberry Pi OS
uses that UART for a login console, and on the Pi 3 the good hardware UART
(PL011) is attached to the onboard Bluetooth, leaving the GPIO header with the
lesser "mini-UART" whose baud rate drifts with the CPU clock. Both need fixing.

## 1. Disable the serial login console, enable the UART

Easiest via `raspi-config`:

```
sudo raspi-config
#   Interface Options -> Serial Port
#     "Would you like a login shell over serial?"  -> No
#     "Would you like the serial port hardware enabled?" -> Yes
```

## 2. Put the good UART on the GPIO header (recommended on Pi 3)

Edit the firmware config (Bookworm: `/boot/firmware/config.txt`; older:
`/boot/config.txt`) and add:

```
# Move Bluetooth off the PL011 so /dev/serial0 is the stable hardware UART
dtoverlay=disable-bt
enable_uart=1
```

Then disable the BT-modem service so it does not grab the port:

```bash
sudo systemctl disable hciuart
```

Reboot. After this, `/dev/serial0` is a symlink to `/dev/ttyAMA0` (the PL011)
and runs at a rock-steady 38400.

## 3. Verify

```bash
ls -l /dev/serial0
pip install pyserial
python3 tools/hmmonitor.py /dev/serial0
```

You should see `$HMSU` status lines decode cleanly. If you instead see garbage or
bad checksums, you are probably still on the mini-UART (skip step 2 at your own
risk) or at the wrong baud.

## Notes

- Add your user to the `dialout` group to access the port without sudo:
  `sudo usermod -aG dialout $USER` (re-login after).
- If you keep Bluetooth, `/dev/serial0` points at the mini-UART; the existing
  firmware works around its small FIFO, and this host's `serial_io` reads in
  chunks too, but the PL011 path is more reliable. Prefer step 2.
