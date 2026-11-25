# Sony α7R III (BIONZ X / "Astra") 10-bit Audit & 2.6K 12-bit cRAW Direct Recording Research

**[中文主页 -> README.md](README.md)**

> **TL;DR Verdict**:
> 1. **10-bit YCbCr MP4 internal video is physically impossible on BIONZ X ("Astra")**: Instruction-level static reverse engineering of `ILCE-7RM3` v3.10 (`AV-CAM.BIN`, `bonobo.bin`, `dfe_app.bin`, `vcd_firmware.bin`) proves the real-time video scalers (`RCH`/`RCV` at `0xf3880000`/`0xf3980000`) physically lack 10-bit DCE block-compression silicon (`caps & 0x800 == 0`), the streaming pipeline runs on an 8-bit `RC_IP_TYPE_IP` bus, and `Imola` H.264 video channels are locked to `8-bit 4:2:0` (applying equally to α7 III and ZV-E10 Gen 1).
> 2. **Bypassing the 8-bit video ISP to record `2.6K 12-bit cRAW` directly to UHS-II SD is 100% viable in silicon and bandwidth**: The front-end `DFE` (`CamAif` + `CamRdc`) features a native single-port **`2656 × 1536` (16:9 full-frame width, true 12-bit ADC `0x2c` readout + Sony `encType=2` 8 bit/px cRAW)** path backed by **autonomous 3D hardware ring-buffer DMA (`outer_repeat = 0xffff`)**. At `23.976p`, the stream requires **`97.81 MB/s`** — well within mainstream 256GB UHS-II V60 / V90 sustained write speeds — while keeping `SYNC0` EVF/LCD LiveView and PDAF autofocus active!

This repository publishes all **6 instruction-level architectural RE reports** plus a **100+ fps Apple Silicon C-accelerated `A7R3CRAW → CinemaDNG` unpacker** verified in DaVinci Resolve Studio 21.

---

## Key Architectural Findings

### 1. Why Traditional 10-bit Video Cannot Be Patched In ([Report 05](docs/05-R3-3.10-Bonobo-DFE-and-RCCT-10bit-DCE.md))
- **`bonobo.bin` (`172 KB`)** is a Big-Endian **Renesas H8S/2600 MCU firmware** managing 5-axis IBIS (`Bear`), shutter, and lens comms over 34-byte `0x39` serial frames — not an ISP coprocessor.
- **`dfe_app.bin` / `dfe_dat.bin`** are ARMv7 TCM microcode (`0xfe1e0800`) handling sensor timing, PDAF stripping, and **Bayer RAW RDC compression** prior to `CamCore`.
- Across all 14 `RC`/`DST` engines (`EXEC[0..13]` at `0x10bb56c`), only the single distortion engine `DST` (`0xf3500000`) has `caps & 0x800 != 0` (`submode=6/7/8`), whereas real-time video scalers `RCH`/`RCV` (`EXEC[5..10]`) are 8-bit only, and `Imola VCD` cannot ingest `DST`'s private compressed format.

### 2. The Viable Bypass Architecture: Plan A `2.6K 12-bit cRAW` ([Report 06](docs/06-R3-3.10-2.6K-cRAW-Pipeline-Blueprint.md))
- **Native Sensor Mode**: `IMX251` `driveMode = 0x17 / 0x18` (`SgcParam @ 0x10ffff4`, SLVS-EC DataType `0x2c` = `RAW12`) outputs single-port `2656 × 1542` on `Port 5` (`0x83cddc`, whose factory `default_encType` at `+0x1c` is already `2` = 8 bit/px Sony ARW2 cRAW) while feeding `2688 × 1529` to `SYNC0` for uninterrupted LiveView and autofocus.
- **Autonomous 3D Ring-Buffer DMA**: `'CamAif:KickRepeat is unsupported.'` (`0x1d070a`) triggers *only* when `numFrames <= 1`. With `numFrames >= 2` (e.g., a 64-frame `249 MiB` ring allocated from `DmmMemPool 0x2b4` via `0x7600c8`), `SetTransferRing` (`0x70a42a`) programs hardware negative wrap-around (`mls`) and `outer_repeat = 0xffff` (48+ hours of zero-CPU-ISR autonomous capture).
- **Zero-Copy 4K Sector Alignment (`A7R3CRAW` v1)**: Cropping 6 optical-black lines via `CamRdc` (`0xfc098000`) yields **`2656 × 1536` (`4,079,616 B = 996 × 4096 B`)**. Prepending a `4096 B` self-contained `A7R3CRAW` header aligns every single frame to 4 KiB page and SDMMC ADMA2 sector boundaries for `FileWriteDirect` (`0x34e9c8`).

---

## Try the `100+ fps` CinemaDNG Pipeline Right Now

Using any compressed `.ARW` photo shot on an `ILCE-7RM3`, you can simulate a 24-frame `2.6K` hardware `encType=2` stream and unpack it into a DaVinci Resolve `.cinemadng` clip (zero Python dependencies; auto-compiles native C via `clang -O3`):

```bash
# 1. Slice a bit-exact 24-frame 2656x1536 encType=2 stream from a real ILCE-7RM3 compressed .ARW
python3 tools/a7r3_26k_craw_unpacker.py simulate \
  --donor-arw /path/to/DSC00001.ARW \
  --output ./MOV00001.CRAW

# 2. Unpack into a calibrated 16-bit linear CinemaDNG bundle (~100+ fps on Apple Silicon)
python3 tools/a7r3_26k_craw_unpacker.py unpack \
  --input ./MOV00001.CRAW \
  --output-dir ./MOV00001.cinemadng
```

Drag the **`MOV00001.cinemadng` folder** straight into DaVinci Resolve's Media Pool to grade with full Camera RAW controls (`ColorMatrix1/2`, `AsShotNeutral`, `BlackLevel = 512`, `WhiteLevel = 16383`).

---

## Call for Collaborators

All static RTOS pipeline addresses, MMIO layouts, and post-processing tools are complete. We welcome collaboration from researchers experienced with **Sony Gen-3 `Astra` SoC / `Jiritsu` Linux ↔ RTOS (`AV-CAM`) IPC** who have **sacrificial/bench `ILCE-7RM3` or `ILCE-7M3` mainboards with hardware UART access** to test non-destructive RAM hooks and thermal/SD endurance.

---

## Legal & Safety Disclaimer

This repository contains **zero proprietary Sony firmware binaries, dumps, or keys**, and provides **no firmware signature bypass, exploit, or flashing instructions**. Never attempt to flash modified images onto a production camera. Released under the [MIT License](LICENSE).
