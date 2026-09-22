# ACE Instrument ADL-200A — serial protocol

Reverse-engineered from `ADL2Pro.exe` (the vendor PC software) and **confirmed on
real hardware**. The application is a C++/MFC program distributed inside an
InstallFactory installer; the command templates are plain ASCII string constants in
its `.rdata` section, and the framing was validated by live request/response.

## Link layer

- **RS-232, 38400 baud, 8-N-1**, no flow control.
- Request/response, **polled** (the logger never sends unsolicited data).
- ASCII text, no checksum.

## Framing

```
Request : >>?1,<ID>,<CMD>;\r
Response: <<!01,<ID>,<CMD>:<data>;\r
```

- `<ID>` — Logger ID (1 on the reference unit).
- Requests end with `;` then **CR (0x0D)** — no LF.
- A single request may produce **many** response lines (each CR-terminated), e.g.
  `GCC` and `MEA` return one line per channel.
- The `CONNECT` handshake seen in the app is **not required** — `G*`/`MEA` work
  directly.

## Commands

`G…` = get/read (safe). `S…`, `RUN`, `FMT`, `RST` = **change state — do not send blindly.**

| Cmd | Meaning | Example reply |
|-----|---------|---------------|
| `GTT` | get time | `<<!01,01,GTT:01:22:54;` |
| `GDD` | get date | `<<!01,01,GDD:01/01/01;` |
| `GMU` | (unit/flags) | `<<!01,01,GMU:0000;` |
| `GMC` | (mode/config) | `<<!01,01,GMC:000000;` |
| `GCC` | channel config, 16 lines | `<<!01,01,GCC:04,1,1,1,0020,6;` |
| `GAM` / `GAT` / `GOV` | misc getters | `GAM:01;` `GAT:00,00;` `GOV:01;` |
| `MEA` | **measure all channels** (~26 s) | see below |
| `SDD` / `STT` | set date / time | *(write)* |
| `SCC`/`SMS`/`SAM`/`SAT`/`SOV`/`SMM`/`MES` | set config | *(write)* |
| `SVE` `ZIG` `LSD` `RUN` `FMT` `RST` | save / zigbee / run / **format** / reset | *(stateful/destructive)* |

## MEA — measurement

`>>?1,1,MEA;\r` triggers a full sweep. Vibrating-wire channels take time, so the
complete answer arrives over **~26 seconds** as 32 lines (16 channels × 2 types):

```
<<!01,<ID>,MES:<CH>,<TYPE>,<yy/mm/dd>,<hh:mm:ss>,<VALUE>;\r
```

- `<CH>` — channel `01`..`16`.
- `<TYPE>` — `01` = primary reading (VW digits/frequency), `06` = secondary
  (temperature).
- `<VALUE>` — zero-padded integer.

Live example (channel 04 has a sensor; the other channels are open):

```
<<!01,01,MES:04,01,01/01/01,01:23:57,0000166635;
<<!01,01,MES:04,06,01/01/01,01:23:57,0000003007;
```

Convert `VALUE` to engineering units with the sensor's calibration (VW: digits →
gauge units; temperature: counts → °C).
