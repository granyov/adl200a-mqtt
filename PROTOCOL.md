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

`>>?1,1,MEA;\r` triggers a sweep of the channels **enabled in `GCC`** — not all
16. Vibrating-wire channels take time: measured on a live unit, about 5–6 s per
enabled channel in total (2 channels ≈ 6 s, 13 channels ≈ 66–71 s). Each channel
answers with one line per enabled input type:

```
<<!01,<ID>,MES:<CH>,<TYPE>,<yy/mm/dd>,<hh:mm:ss>,<VALUE>;\r
```

- `<CH>` — channel `01`..`16`.
- `<TYPE>` — input type code, see the table in *Channel configuration*:
  `01` = VW frequency, `06` = thermistor (ohms), and so on.
- `<VALUE>` — zero-padded integer.

Live example (channel 04 has a sensor; the other channels are open):

```
<<!01,01,MES:04,01,01/01/01,01:23:57,0000166635;
<<!01,01,MES:04,06,01/01/01,01:23:57,0000003007;
```

Convert `VALUE` to engineering units with the sensor's calibration (VW: digits →
gauge units; temperature: counts → °C).

## Channel configuration — `GCC` / `SCC`

Recovered from the vendor `ADL2Pro.exe` (v2.70; the real binary sits deflated
inside the InstallFactory installer under `Src\ADL2Pro.exe`). The mapping below
comes from the "Logger Setting" dialog (`CChannelSettingDlg`): its init code
fills the combo boxes, the *Send setting* handler turns the selections into
per-channel numbers, and the command builder formats them into `SCC`. The
dialog's *Reading set channels* path applies the exact inverse mapping, and
the stock configuration read from a real unit (`02,1,1,1,0020,6`) decodes to
the dialog defaults, so both directions agree.

```
>>?1,<ID>,GCC;                       -> 16 x <<!01,<ID>,GCC:<CH>,f2,f3,f4,f5,f6;
>>?1,<ID>,SCC:<CH>,f2,f3,f4,f5,f6;   -> <<!01,<ID>,ACK;
```

| Field | Meaning | Values |
|-------|---------|--------|
| `CH` | channel | 1..16 |
| `f2` | channel in use | 1 if VW or analog is on, else 0 |
| `f3` | VW input on | 1 / 0 |
| `f4` | VW sweep band | 0 = off, 1..4 = Sweep 01..04 (table below) |
| `f5` | VW constant / serial count | **20** when VW is on (hard-coded by the vendor); for a serial sensor — number of values it returns; else 0 |
| `f6` | analog input type | 0 = off, codes below |

A disabled channel is `SCC:<CH>,0,0,0,0,0`.

**VW sweep bands** (manual, "VW Channel Setup"; default Sweep 01, unit Hz):

| `f4` | Band |
|------|------|
| 1 | 450 – 1200 Hz |
| 2 | 800 – 2000 Hz |
| 3 | 1400 – 3500 Hz |
| 4 | 2400 – 6000 Hz |

The band must contain the sensor's whole working frequency span from its
calibration sheet. Earth pressure cell 1910 s/n 587968 runs 2991.4 Hz (zero)
to 2280.4 Hz (3.5 kgf/cm²) → **Sweep 03**. Sweep 04 would lose everything above
~2.97 kgf/cm² (below 2400 Hz); Sweep 01 does not cover the cell at all.

**Analog type codes** (`f6`). The same code is the `<TYPE>` field of the `MES`
frames the channel produces, so a channel set to VW + Thermistor 3K reports
`TYPE 01` (VW) and `TYPE 06` (thermistor).

| Code | Dialog item | Combo index |
|------|-------------|-------------|
| 1 | *(VW reading — not an analog type, only appears as `MES` TYPE)* | — |
| 2 | mV | 1 |
| 3 | Bridge | 2 |
| 4 | Serial sensor (multi-value, count in `f5`) | 5 |
| 5 | Resistance | 3 |
| 6 | Thermistor 3K | 0 |
| 7 | RTD | 4 |

Vendor rule: **Bridge and Serial sensor cannot share a channel with VW** — the
dialog forces the sweep to 0 when either is selected.

Typical channel for a VW sensor with a thermistor: `SCC:<CH>,1,1,<sweep>,20,6`.

## Logger-level settings (same dialog)

| Command | Field | Dialog control |
|---------|-------|----------------|
| `SAT:<h>,<m>` / `GAT` | measuring interval, hours and minutes | *Measuring interval* `[ ] Hour [ ] Minute` |
| `SAM:<n>` / `GAM` | interval mode, 1 = Fixed, 2 = Periodic | combo next to the interval |
| `SOV:<n>` / `GOV` | when memory is full, 0 = Stop recording, 1 = Overwrite oldest data | *When the logger memory is full* |

These govern the logger's **own** recording to its internal memory and run
independently of `MEA` polling. A unit read on 2026-09-30 answered `GAT:00,01`,
`GAM:01`, `GOV:01` — recording every minute, Fixed, overwrite.

The vendor software does **not** convert readings to engineering units: there is
no thermistor curve in the binary. VW readings are frequency in Hz with two
implied decimals (`0000294720` = 2947.20 Hz); thermistor readings are ohms.
