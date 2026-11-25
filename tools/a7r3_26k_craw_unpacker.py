#!/usr/bin/env python3
"""
Sony α7R III (ILCE-7RM3) Plan-A 2.6K 12-bit cRAW (encType=2) Stream Unpacker
============================================================================
Dedicated macOS / cross-platform post-processing tool for Plan A:
  - Sensor Readout : IMX251 driveMode 0x17 (23.976/29.97p) / 0x18 (25.00p),
                     SLVS-EC DataType 0x2c (True 12-bit ADC), Port 5 (RdcW0).
  - Hardware RDC   : encType = 2 (8 bit/px Sony ARW2 cRAW, 32-byte independent blocks).
  - Frame Geometry : 2656 x 1536 (4,079,616 B = 996 x 4096 B, 4K sector-aligned default)
                     or 2656 x 1542 (4,095,552 B uncropped native readout).
  - Container Spec : 4096-byte aligned `A7R3CRAW` v1 header at offset 0 (also auto-detects
                     headerless raw `.craw` streams).
  - Output         : `.cinemadng` bundle (16-bit Linear CinemaDNG sequence) ready to drag
                     directly into DaVinci Resolve with full Camera RAW controls.
"""

import argparse
import ctypes
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path

CRAW_MAGIC = b"A7R3CRAW"
CRAW_HEADER_SIZE = 4096
CRAW_HEADER_FMT = "<8sIIHHI4I4H4HI4h"  # 68 bytes packed inside 4096-byte page

# Calibrated CIE XYZ -> Camera RGB matrices for Sony ILCE-7RM3 (IMX251)
# Illuminant A (17) and D65 (21), scaled x10000
ILCE_7RM3_COLOR_MATRIX_1 = [
    (6640, 10000), (-1847, 10000), (-503, 10000),
    (-5238, 10000), (13010, 10000), (2474, 10000),
    (-993, 10000), (1673, 10000), (6527, 10000),
]
ILCE_7RM3_COLOR_MATRIX_2 = [
    (5991, 10000), (-1456, 10000), (-455, 10000),
    (-4764, 10000), (12485, 10000), (2533, 10000),
    (-707, 10000), (1405, 10000), (6701, 10000),
]

DEFAULT_Y_KNOTS = (8000, 10400, 12900, 14100)  # SubIFD tag 0x7010
DEFAULT_BLACK_LEVEL = (512, 512, 512, 512)    # SubIFD tag 0x7310
DEFAULT_WHITE_LEVEL = 16383                   # SubIFD tag 0xc61d
DEFAULT_WB_RGGB = (2500, 1024, 1024, 1660)    # SubIFD tag 0x7313 (~Daylight)

C_DECODER_SRC = r"""
#include <stdint.h>
#include <string.h>

void decode_sony_arw2_frame(
    const uint8_t *src,
    uint16_t *dst,
    int width,
    int height,
    const uint16_t *curve_lut2048
) {
    const int blocks_per_row = width >> 5; /* width / 32 */
    for (int row = 0; row < height; ++row) {
        const uint8_t *row_src = src + (size_t)row * (size_t)width;
        uint16_t *row_dst = dst + (size_t)row * (size_t)width;
        for (int blk = 0; blk < blocks_per_row; ++blk) {
            const uint8_t *b32 = row_src + (blk << 5);
            uint16_t *out32 = row_dst + (blk << 5);
            for (int sub = 0; sub < 2; ++sub) {
                uint64_t lo = 0, hi = 0;
                memcpy(&lo, b32 + (sub << 4), 8);
                memcpy(&hi, b32 + (sub << 4) + 8, 8);

                int pmax = (int)(lo & 0x7ffULL);
                int pmin = (int)((lo >> 11) & 0x7ffULL);
                int imax = (int)((lo >> 22) & 0xfULL);
                int imin = (int)((lo >> 26) & 0xfULL);

                int shl = 0;
                int diff = pmax - pmin;
                while (shl < 4 && (0x80 << shl) <= diff) {
                    shl++;
                }

                int bitpos = 30;
                for (int i = 0; i < 16; ++i) {
                    int val11;
                    if (i == imax) {
                        val11 = pmax;
                    } else if (i == imin) {
                        val11 = pmin;
                    } else {
                        uint32_t d;
                        if (bitpos <= 57) {
                            d = (uint32_t)((lo >> bitpos) & 0x7fULL);
                        } else if (bitpos >= 64) {
                            d = (uint32_t)((hi >> (bitpos - 64)) & 0x7fULL);
                        } else {
                            int low_bits = 64 - bitpos;
                            d = (uint32_t)((lo >> bitpos) | (hi << low_bits)) & 0x7fU;
                        }
                        bitpos += 7;
                        val11 = pmin + ((int)d << shl);
                        if (val11 > 2047) val11 = 2047;
                    }
                    out32[(i << 1) + sub] = curve_lut2048[val11];
                }
            }
        }
    }
}
"""


def build_11_to_14bit_lut(y_knots=DEFAULT_Y_KNOTS):
    """
    Build the exact 2048-entry 11-bit -> 14-bit piecewise-linear inverse companding LUT
    matching dcraw / LibRaw `sony_curve` reconstruction from SubIFD tag 0x7010.
    At dark black (val11 = 256 -> idx = 512), LUT outputs exact BlackLevel = 512.
    """
    sony_curve = [0] + [(int(k) >> 2) & 0xFFF for k in y_knots] + [4095]
    curve4096 = [0] * 4096
    for i in range(5):
        step = 1 << i
        for j in range(sony_curve[i] + 1, sony_curve[i + 1] + 1):
            curve4096[j] = curve4096[j - 1] + step
    return [min(16383, curve4096[v11 << 1]) for v11 in range(2048)]


def get_c_decoder():
    """Compile and load the native arm64/x86_64 C decoder right next to this script."""
    cc = shutil.which("clang") or shutil.which("cc") or shutil.which("gcc")
    if not cc:
        return None
    so_path = Path(__file__).resolve().parent / "lib_a7r3_arw2_dec.dylib"
    if not so_path.exists():
        c_path = so_path.with_suffix(".c")
        try:
            c_path.write_text(C_DECODER_SRC, encoding="utf-8")
            subprocess.run(
                [cc, "-O3", "-shared", "-fPIC", str(c_path), "-o", str(so_path)],
                check=True,
                capture_output=True,
            )
        except Exception:
            return None
        finally:
            if c_path.exists():
                c_path.unlink()
    try:
        lib = ctypes.CDLL(str(so_path))
        lib.decode_sony_arw2_frame.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint16),
        ]
        lib.decode_sony_arw2_frame.restype = None
        return lib
    except Exception:
        return None


def decode_frame_python(raw_bytes: bytes, width: int, height: int, lut):
    """Pure-Python fallback if no C compiler is installed."""
    out = bytearray(width * height * 2)
    mv = memoryview(raw_bytes)
    blocks_per_row = width >> 5
    for row in range(height):
        r_in = row * width
        r_out = row * width * 2
        for blk in range(blocks_per_row):
            b_off = r_in + (blk << 5)
            o_off = r_out + (blk << 6)
            for sub in range(2):
                bits = int.from_bytes(mv[b_off + (sub << 4) : b_off + ((sub + 1) << 4)], "little")
                pmax = bits & 0x7FF
                pmin = (bits >> 11) & 0x7FF
                imax = (bits >> 22) & 0xF
                imin = (bits >> 26) & 0xF
                shl = 0
                diff = pmax - pmin
                while shl < 4 and (0x80 << shl) <= diff:
                    shl += 1
                d_idx = 0
                for i in range(16):
                    if i == imax:
                        v11 = pmax
                    elif i == imin:
                        v11 = pmin
                    else:
                        d = (bits >> (30 + 7 * d_idx)) & 0x7F
                        d_idx += 1
                        v11 = pmin + (d << shl)
                        if v11 > 2047:
                            v11 = 2047
                    struct.pack_into("<H", out, o_off + (((i << 1) + sub) << 1), lut[v11])
    return bytes(out)


def pack_craw_header(
    width: int,
    height: int,
    fps_num: int = 24000,
    fps_den: int = 1001,
    iso: int = 400,
    shutter_den: int = 50,
    y_knots=DEFAULT_Y_KNOTS,
    black_level=DEFAULT_BLACK_LEVEL,
    white_level: int = DEFAULT_WHITE_LEVEL,
    wb_rggb=DEFAULT_WB_RGGB,
) -> bytes:
    """Build the 4096-byte sector-aligned `A7R3CRAW` v1 stream header."""
    hdr = bytearray(CRAW_HEADER_SIZE)
    struct.pack_into(
        CRAW_HEADER_FMT,
        hdr,
        0,
        CRAW_MAGIC,
        1,  # version = 1
        2,  # enc_type = 2 (8 bit/px Sony cRAW)
        width,
        height,
        width * height,
        fps_num,
        fps_den,
        iso,
        shutter_den,
        *y_knots,
        *black_level,
        white_level,
        *wb_rggb,
    )
    return bytes(hdr)


def unpack_craw_header(buf4096: bytes):
    """Parse 4096-byte `A7R3CRAW` header if present; return None for headerless streams."""
    if len(buf4096) < CRAW_HEADER_SIZE or buf4096[:8] != CRAW_MAGIC:
        return None
    vals = struct.unpack_from(CRAW_HEADER_FMT, buf4096, 0)
    return {
        "version": vals[1],
        "enc_type": vals[2],
        "width": vals[3],
        "height": vals[4],
        "frame_bytes": vals[5],
        "fps_num": vals[6],
        "fps_den": vals[7],
        "iso": vals[8],
        "shutter_den": vals[9],
        "y_knots_7010": list(vals[10:14]),
        "black_level": list(vals[14:18]),
        "white_level": vals[18],
        "wb_rggb": list(vals[19:23]),
    }


def parse_arw_metadata(arw_path: Path):
    """Extract SubIFD[0] strip geometry, SonyToneCurve, BlackLevel, WhiteLevel, and WB from an ILCE-7RM3 .ARW."""
    with open(arw_path, "rb") as f:
        hdr = f.read(1024 * 1024)
    if hdr[:2] != b"II":
        raise ValueError("Expected little-endian TIFF/ARW ('II').")
    ifd0_off = struct.unpack_from("<I", hdr, 4)[0]

    def read_ifd(off):
        cnt = struct.unpack_from("<H", hdr, off)[0]
        tags = {}
        for i in range(cnt):
            eoff = off + 2 + i * 12
            tag, typ, count, vraw = struct.unpack_from("<HHII", hdr, eoff)
            tags[tag] = (typ, count, vraw, eoff)
        return tags

    ifd0 = read_ifd(ifd0_off)
    sub_off = ifd0[0x014A][2]
    sub = read_ifd(sub_off)

    width = sub[0x0100][2]
    height = sub[0x0101][2]
    comp = sub[0x0103][2]
    strip_off = sub[0x0111][2]
    strip_len = sub[0x0117][2]
    white = sub.get(0xC61D, (3, 1, DEFAULT_WHITE_LEVEL, 0))[2]

    y_knots = struct.unpack_from("<4H", hdr, sub[0x7010][2]) if 0x7010 in sub else DEFAULT_Y_KNOTS
    black = struct.unpack_from("<4H", hdr, sub[0x7310][2]) if 0x7310 in sub else DEFAULT_BLACK_LEVEL
    wb = struct.unpack_from("<4h", hdr, sub[0x7313][2]) if 0x7313 in sub else DEFAULT_WB_RGGB

    return {
        "width": width,
        "height": height,
        "compression": comp,
        "strip_offset": strip_off,
        "strip_byte_count": strip_len,
        "y_knots": list(y_knots),
        "black_level": list(black),
        "white_level": int(white),
        "wb_rggb": list(wb),
        "sub_ifd_offset": sub_off,
        "exif_ifd_offset": ifd0[0x8769][2] if 0x8769 in ifd0 else None,
    }


def build_cinemadng_header(
    width: int,
    height: int,
    wb_rggb=DEFAULT_WB_RGGB,
    black_level=DEFAULT_BLACK_LEVEL,
    white_level=DEFAULT_WHITE_LEVEL,
    fps_num: int = 24000,
    fps_den: int = 1001,
) -> bytes:
    """Construct a DNG 1.4 / CinemaDNG header (pixel payload aligned at offset 4096)."""
    PIXEL_OFFSET = 4096
    strip_bytes = width * height * 2

    r_gain, g_gain, _, b_gain = wb_rggb
    as_shot_neutral = [
        (g_gain, max(1, r_gain)),
        (1024, 1024),
        (g_gain, max(1, b_gain)),
    ]

    entries = []
    extra = bytearray()

    def add_u16(tag, val):
        entries.append((tag, 3, 1, struct.pack("<H", val)))

    def add_u32(tag, val):
        entries.append((tag, 4, 1, struct.pack("<I", val)))

    def add_u16_arr(tag, vals):
        entries.append((tag, 3, len(vals), struct.pack(f"<{len(vals)}H", *vals)))

    def add_u32_arr(tag, vals):
        entries.append((tag, 4, len(vals), struct.pack(f"<{len(vals)}I", *vals)))

    def add_bytes(tag, typ, bvals):
        entries.append((tag, typ, len(bvals), bytes(bvals)))

    def add_str(tag, s):
        b = s.encode("ascii") + b"\x00"
        entries.append((tag, 2, len(b), b))

    def add_urationals(tag, pairs):
        entries.append((tag, 5, len(pairs), b"".join(struct.pack("<II", n, d) for n, d in pairs)))

    def add_srationals(tag, pairs):
        entries.append((tag, 10, len(pairs), b"".join(struct.pack("<ii", n, d) for n, d in pairs)))

    crop_x = 16 if width > 64 else 0
    crop_y = 8 if height > 32 else 0
    crop_w = width - 2 * crop_x
    crop_h = height - 2 * crop_y

    add_u32(0x00FE, 0)
    add_u32(0x0100, width)
    add_u32(0x0101, height)
    add_u16(0x0102, 16)
    add_u16(0x0103, 1)
    add_u16(0x0106, 32803)
    add_str(0x010F, "SONY")
    add_str(0x0110, "ILCE-7RM3")
    add_u32(0x0111, PIXEL_OFFSET)
    add_u16(0x0112, 1)
    add_u16(0x0115, 1)
    add_u32(0x0116, height)
    add_u32(0x0117, strip_bytes)
    add_u16(0x011C, 1)
    add_str(0x0131, "A7R3-PlanA-2.6K-cRAW")
    add_u16_arr(0x828D, [2, 2])
    add_bytes(0x828E, 1, [0, 1, 1, 2])
    add_bytes(0xC612, 1, [1, 4, 0, 0])
    add_bytes(0xC613, 1, [1, 1, 0, 0])
    add_str(0xC614, "Sony ILCE-7RM3")
    add_bytes(0xC616, 1, [0, 1, 1, 2])
    add_u16(0xC617, 1)
    add_u16_arr(0xC619, [2, 2])
    add_u16_arr(0xC61A, list(black_level))
    add_u32(0xC61D, white_level)
    add_u32_arr(0xC61F, [crop_x, crop_y])
    add_u32_arr(0xC620, [crop_w, crop_h])
    add_srationals(0xC621, ILCE_7RM3_COLOR_MATRIX_1)
    add_srationals(0xC622, ILCE_7RM3_COLOR_MATRIX_2)
    add_urationals(0xC628, as_shot_neutral)
    add_srationals(0xC62A, [(0, 100)])
    add_u16(0xC65A, 17)
    add_u16(0xC65B, 21)
    add_srationals(0xC764, [(fps_num, fps_den)])

    entries.sort(key=lambda x: x[0])

    ifd_off = 8
    extra_base = ifd_off + 2 + len(entries) * 12 + 4
    ifd_buf = bytearray(struct.pack("<H", len(entries)))
    for tag, typ, count, payload in entries:
        if len(payload) <= 4:
            ifd_buf += struct.pack("<HHI4s", tag, typ, count, payload.ljust(4, b"\x00"))
        else:
            if len(extra) & 1:
                extra += b"\x00"
            val_off = extra_base + len(extra)
            extra += payload
            ifd_buf += struct.pack("<HHII", tag, typ, count, val_off)
    ifd_buf += struct.pack("<I", 0)

    header = bytearray(b"II\x2a\x00" + struct.pack("<I", ifd_off)) + ifd_buf + extra
    header += b"\x00" * (PIXEL_OFFSET - len(header))
    return bytes(header)


def cmd_simulate(args):
    arw_path = Path(args.donor_arw)
    meta = parse_arw_metadata(arw_path)
    if meta["compression"] != 32767:
        raise ValueError(f"Donor ARW is not Sony cRAW (Compression={meta['compression']}, expected 32767).")

    src_w, src_h = meta["width"], meta["height"]
    out_w, out_h = args.width, args.height
    if out_w % 32 != 0 or out_h % 2 != 0:
        raise ValueError("Width must be a multiple of 32 (RDC block) and height must be even (Bayer pair).")

    with open(arw_path, "rb") as f:
        f.seek(meta["strip_offset"])
        full_strip = memoryview(f.read(meta["strip_byte_count"]))

    out_craw = Path(args.output)
    out_craw.parent.mkdir(parents=True, exist_ok=True)

    craw_hdr = pack_craw_header(
        width=out_w,
        height=out_h,
        y_knots=meta["y_knots"],
        black_level=meta["black_level"],
        white_level=meta["white_level"],
        wb_rggb=meta["wb_rggb"],
    )

    base_y = ((src_h - out_h) // 2) & ~1
    max_x_blocks = (src_w - out_w) // 32
    start_blk = max(0, (max_x_blocks // 2) - (args.frames // 2))

    t0 = time.perf_counter()
    with open(out_craw, "wb") as out_f:
        out_f.write(craw_hdr)
        for fn in range(args.frames):
            x_off = min(max_x_blocks, start_blk + fn) * 32
            for row in range(out_h):
                s_off = (base_y + row) * src_w + x_off
                out_f.write(full_strip[s_off : s_off + out_w])
    dt = time.perf_counter() - t0

    frame_bytes = out_w * out_h
    aligned_4k = "YES (exact 4096B page aligned)" if (frame_bytes % 4096 == 0) else f"NO (rem {frame_bytes % 4096}B)"
    rate_24p = (frame_bytes * 23.976) / (1000 * 1000)
    print(f"[simulate:PlanA] Wrote {args.frames} frames ({out_w}x{out_h}) + 4KB A7R3CRAW header -> {out_craw}")
    print(f"                 Frame size : {frame_bytes:,} B | 4K-aligned: {aligned_4k}")
    print(f"                 23.976p BW : {rate_24p:.2f} MB/s | Elapsed: {dt*1000:.1f} ms")


def cmd_unpack(args):
    craw_path = Path(args.input)
    with open(craw_path, "rb") as f:
        probe = f.read(CRAW_HEADER_SIZE)

    hdr_meta = unpack_craw_header(probe)
    data_offset = CRAW_HEADER_SIZE if hdr_meta else 0
    meta = hdr_meta or {}

    sidecar_path = craw_path.with_suffix(craw_path.suffix + ".json")
    if not meta and sidecar_path.exists():
        meta = json.loads(sidecar_path.read_text(encoding="utf-8"))

    if args.donor_arw:
        donor_meta = parse_arw_metadata(Path(args.donor_arw))
        meta.setdefault("y_knots_7010", donor_meta["y_knots"])
        meta.setdefault("black_level", donor_meta["black_level"])
        meta.setdefault("white_level", donor_meta["white_level"])
        meta.setdefault("wb_rggb", donor_meta["wb_rggb"])

    width = args.width or meta.get("width", 2656)
    height = args.height or meta.get("height", 1536)
    fps_num = meta.get("fps_num", 24000)
    fps_den = meta.get("fps_den", 1001)
    frame_bytes = width * height
    payload_bytes = craw_path.stat().st_size - data_offset
    total_frames = payload_bytes // frame_bytes
    if args.max_frames:
        total_frames = min(total_frames, args.max_frames)

    y_knots = meta.get("y_knots_7010", DEFAULT_Y_KNOTS)
    black_level = meta.get("black_level", DEFAULT_BLACK_LEVEL)
    white_level = meta.get("white_level", DEFAULT_WHITE_LEVEL)
    wb_rggb = meta.get("wb_rggb", DEFAULT_WB_RGGB)

    out_dir = Path(args.output_dir)
    if out_dir.suffix.lower() != ".cinemadng":
        out_dir = out_dir.with_name(out_dir.name + ".cinemadng")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem_prefix = out_dir.stem

    lut = build_11_to_14bit_lut(y_knots)
    dng_hdr = build_cinemadng_header(width, height, wb_rggb, black_level, white_level, fps_num, fps_den)
    c_lib = get_c_decoder()
    lut_arr = (ctypes.c_uint16 * 2048)(*lut) if c_lib else None
    out_buf = (ctypes.c_uint16 * (width * height))() if c_lib else None

    t0 = time.perf_counter()
    with open(craw_path, "rb") as f:
        f.seek(data_offset)
        for idx in range(total_frames):
            raw_frame = f.read(frame_bytes)
            if c_lib:
                c_lib.decode_sony_arw2_frame(raw_frame, out_buf, width, height, lut_arr)
                u16_bytes = memoryview(out_buf)
            else:
                u16_bytes = decode_frame_python(raw_frame, width, height, lut)

            out_file = out_dir / f"{stem_prefix}_{idx:06d}.dng"
            with open(out_file, "wb") as wf:
                wf.write(dng_hdr)
                wf.write(u16_bytes)

    dt = time.perf_counter() - t0
    accel = "Apple clang -O3 native C" if c_lib else "pure Python fallback"
    print(f"[unpack:PlanA] {total_frames} frames ({width}x{height}) -> {out_dir}")
    print(f"               Engine: {accel} | Time: {dt*1000:.1f} ms ({total_frames/max(dt,1e-6):.1f} fps)")


def main():
    ap = argparse.ArgumentParser(description="Sony ILCE-7RM3 Plan-A 2.6K 12-bit cRAW (encType=2) Stream Tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_sim = sub.add_parser("simulate", help="Generate a Plan-A .craw stream (with 4KB A7R3CRAW header) from a donor .ARW")
    sp_sim.add_argument("--donor-arw", required=True, help="Path to ILCE-7RM3 compressed .ARW file")
    sp_sim.add_argument("--output", required=True, help="Output .craw file path")
    sp_sim.add_argument("--width", type=int, default=2656, help="Width (default: 2656)")
    sp_sim.add_argument("--height", type=int, default=1536, help="Height (default: 1536 for exact 4KB SD sector alignment; 1542 for full OB)")
    sp_sim.add_argument("--frames", type=int, default=24, help="Frame count (default: 24)")
    sp_sim.set_defaults(func=cmd_simulate)

    sp_unp = sub.add_parser("unpack", help="Unpack a Plan-A .craw file into a DaVinci Resolve .cinemadng bundle")
    sp_unp.add_argument("--input", required=True, help="Input .craw file path")
    sp_unp.add_argument("--output-dir", required=True, help="Output .cinemadng bundle path")
    sp_unp.add_argument("--donor-arw", help="Optional donor .ARW override (only needed for headerless streams)")
    sp_unp.add_argument("--width", type=int, help="Width override for headerless streams")
    sp_unp.add_argument("--height", type=int, help="Height override for headerless streams")
    sp_unp.add_argument("--max-frames", type=int, help="Max frames to unpack")
    sp_unp.set_defaults(func=cmd_unpack)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
