# adl200a-mqtt

A small, robust Linux **systemd service** that polls an **ACE Instrument ADL-200A**
geotechnical data logger over RS-232 and publishes the readings to a local **MQTT**
broker as JSON.

The ADL-200A speaks an undocumented, proprietary ASCII protocol. It was
reverse-engineered from the vendor's `ADL2Pro.exe` and confirmed against real
hardware — see **[PROTOCOL.md](PROTOCOL.md)** for the full command set.

## What it does

- Opens the serial port (persistent, auto-reconnecting).
- Every `ADL200A_INTERVAL` seconds sends `MEA` (measure all channels, ~26 s).
- Parses the per-channel `MES` frames and publishes JSON to MQTT.
- Logs to `stdout` (journald under systemd).

## Hardware

The logger's host port is **RS-232, 38400 8N1**. Two tested setups:

| Host | Adapter | Serial device | Notes |
|------|---------|---------------|-------|
| x86 PC (e.g. Dell Wyse) | Moxa UPort 1150 (USB↔RS-232) | `/dev/ttyUSB0` | needs `moxa-1150.fw` (`firmware-misc-nonfree`) + the `mxu11x0` driver; the service sets RS-232 mode automatically |
| ARM SBC | native SoC UART wired to an RS-232 transceiver | `/dev/ttyS0`, … | works out of the box |

> RS-232 is point-to-point — the logger talks to one host at a time.

## Install

```sh
sudo ./install.sh
sudo editor /etc/default/adl200a        # set ADL200A_PORT to your serial device
sudo systemctl enable --now adl200a
```

`install.sh` installs the deps (`python3-serial`, `python3-paho-mqtt`, `mosquitto`),
copies the files to `/opt/adl200a`, drops the config at `/etc/default/adl200a`, and
registers the service.

## Configuration — `/etc/default/adl200a`

| Var | Default | Meaning |
|-----|---------|---------|
| `ADL200A_PORT` | `/dev/ttyUSB0` | serial device the logger is on |
| `ADL200A_BAUD` | `38400` | baud rate |
| `ADL200A_ID` | `1` | Logger ID |
| `ADL200A_INTERVAL` | `60` | seconds between measurement cycles |
| `ADL200A_MQTT_HOST` / `_PORT` | `127.0.0.1` / `1883` | MQTT broker |
| `ADL200A_TOPIC` | `adl200a` | MQTT topic prefix |

## Usage

```sh
# live data
mosquitto_sub -h 127.0.0.1 -t 'adl200a/#' -v
# service status / logs
systemctl status adl200a ; journalctl -u adl200a -f
# one-off command (stop the service first, the port is exclusive)
systemctl stop adl200a && python3 /opt/adl200a/ace.py MEA
```

### MQTT topics

- `adl200a/ch/<NN>` — one message per channel:
  ```json
  {"channel": 4, "values": {"t01": 87081, "t06": 2956},
   "dev_date": "01/01/01", "dev_time": "01:29:16",
   "timestamp": "2026-09-22T17:15:29+00:00"}
  ```
- `adl200a/measurement` — the whole 16-channel cycle in one message.

`t01` is the primary reading (vibrating-wire digits/frequency); `t06` is the
secondary (temperature). Scale to engineering units with your sensor calibration.

## Files

- `adl200a_mqtt.py` — the daemon
- `ace.py` — CLI to send one ACE command (`ace.py GTT`, `ace.py MEA`, …)
- `sniffer.py` — generic raw-hex serial probe
- `prestart.sh` — sets RS-232 mode on a Moxa UPort 1150 (no-op elsewhere)
- `adl200a.service` / `adl200a.default` — systemd unit + config template
- `install.sh` — installer

## License

MIT — see [LICENSE](LICENSE).
