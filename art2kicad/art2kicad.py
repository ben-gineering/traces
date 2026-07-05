#!/usr/bin/env python3
"""
art2kicad — convert a raster image (jpg/png) into a KiCad project whose PCB
depicts the artwork using only manufacturable PCB finishes:

    * solder mask            -> the darkest "background" tone
    * bare copper / ENIG     -> a mid tone (exposed metal)
    * exposed FR4            -> the lightest tone (mask opening, no copper)
    * silkscreen             -> line detail / outlines

The output is a self-contained KiCad 10 project:

    <name>.kicad_pro   project settings (minimal, valid)
    <name>.kicad_sch   empty schematic
    <name>.kicad_pcb   board outline + graphic polygons on F.Cu / F.Mask / F.SilkS

Pipeline:
    1.  PIL loads the image; grayscale + auto-level + denoise.
    2.  Two luminance thresholds split the image into three bands:
            band 0  (dark)     -> masked background
            band 1  (mid)      -> exposed metal   (F.Cu + F.Mask)
            band 2  (light)    -> exposed FR4       (F.Mask only)
        An optional silkscreen outline is derived from a band boundary.
    3.  Each band mask is cleaned (morphological open/close) and written PGM.
    4.  potrace (-b geojson) vectorizes each mask; its turdsize drops specks.
    5.  GeoJSON polygons (which carry holes) are merged into single simple
        polygons via the zero-width "keyhole" bridge technique — KiCad's
        gr_poly cannot represent holes directly.
    6.  Pixel coordinates are scaled to mm and Y-flipped to KiCad's up-axis.
    7.  gr_poly elements are emitted on the appropriate layers; a rectangular
        Edge.Cuts outline closes the board.

Requires: python3 (PIL + numpy), potrace, and (optionally) kicad-cli for export.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps

# --------------------------------------------------------------------------- #
# Point / polygon helpers
# --------------------------------------------------------------------------- #

Pt = tuple[float, float]


def dist2(a: Pt, b: Pt) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def ring_area(ring: list[Pt]) -> float:
    """Signed area of a polygon ring (Shoelace)."""
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def ensure_ccw(ring: list[Pt]) -> list[Pt]:
    return ring if ring_area(ring) > 0 else ring[::-1]


def ensure_cw(ring: list[Pt]) -> list[Pt]:
    return ring if ring_area(ring) < 0 else ring[::-1]


def bridge_hole(outer: list[Pt], hole: list[Pt]) -> list[Pt]:
    """
    Merge a hole into an outer ring using a zero-width "keyhole" bridge.
    Returns a single simple polygon (list of (x,y)).
    """
    # find closest pair of vertices between outer and hole
    best = (math.inf, 0, 0)
    for i, p in enumerate(outer):
        for j, q in enumerate(hole):
            d = dist2(p, q)
            if d < best[0]:
                best = (d, i, j)
    _, i, j = best
    # path: outer[0..i] + hole[j..] + hole[0..j] + outer[i..]
    return (
        outer[: i + 1]
        + hole[j:]
        + hole[: j + 1]
        + outer[i:]
    )


def merge_holes(outer: list[Pt], holes: list[list[Pt]]) -> list[Pt]:
    """Merge all holes into the outer ring, one keyhole bridge at a time."""
    ring = ensure_ccw(outer)
    for h in holes:
        h = ensure_cw(h)
        ring = bridge_hole(ring, h)
    return ring


# --------------------------------------------------------------------------- #
# Image decomposition
# --------------------------------------------------------------------------- #

def load_gray(path: Path) -> Image.Image:
    im = Image.open(path).convert("L")
    return im


def auto_level(im: Image.Image) -> Image.Image:
    arr = np.asarray(im, dtype=np.float32)
    lo, hi = np.percentile(arr, [0.5, 99.5])
    if hi <= lo:
        return im
    arr = np.clip((arr - lo) / (hi - lo) * 255.0, 0, 255)
    return Image.fromarray(arr.astype(np.uint8))


def percentile_thresholds(arr: np.ndarray, p_lo: float, p_hi: float) -> tuple[int, int]:
    t_lo = int(np.percentile(arr, p_lo))
    t_hi = int(np.percentile(arr, p_hi))
    if t_hi <= t_lo:
        t_hi = min(255, t_lo + 1)
    return t_lo, t_hi


def clean_mask(im_bw: Image.Image, close_px: int, open_px: int) -> Image.Image:
    """
    Morphological cleanup using PIL filters (no scipy needed).
      close = dilate then erode   (fills tiny gaps / cracks)
      open  = erode then dilate  (drops specks)
    """
    if close_px >= 2:
        k = close_px if close_px % 2 == 1 else close_px + 1
        im_bw = im_bw.filter(ImageFilter.MaxFilter(k))
        im_bw = im_bw.filter(ImageFilter.MinFilter(k))
    if open_px >= 2:
        k = open_px if open_px % 2 == 1 else open_px + 1
        im_bw = im_bw.filter(ImageFilter.MinFilter(k))
        im_bw = im_bw.filter(ImageFilter.MaxFilter(k))
    return im_bw


def make_band_masks(
    gray: Image.Image,
    t_lo: int,
    t_hi: int,
    close_px: int,
    open_px: int,
) -> dict[str, Image.Image]:
    """
    Return dict of binary (uint8, 255=on) masks:
        'copper'  -> pixels in (t_lo, t_hi]   -> F.Cu + F.Mask  (exposed metal)
        'fr4'     -> pixels  > t_hi            -> F.Mask only     (exposed substrate)
        'lit'     -> pixels  > t_lo            -> union, used for silk outlines
    The silkscreen layer is derived later as stroked vector outlines (cleaner
    than a noisy raster gradient), so no 'silk' mask is produced here.
    """
    arr = np.asarray(gray, dtype=np.int32)
    copper = (arr > t_lo) & (arr <= t_hi)
    fr4 = arr > t_hi
    lit = arr > t_lo

    masks: dict[str, Image.Image] = {}
    masks["copper"] = clean_mask(Image.fromarray((copper * 255).astype(np.uint8)), close_px, open_px)
    masks["fr4"] = clean_mask(Image.fromarray((fr4 * 255).astype(np.uint8)), close_px, open_px)
    masks["lit"] = clean_mask(Image.fromarray((lit * 255).astype(np.uint8)), close_px, open_px)
    return masks


# --------------------------------------------------------------------------- #
# Vectorization via potrace
# --------------------------------------------------------------------------- #

@dataclass
class GeoPolygon:
    outer: list[Pt]
    holes: list[list[Pt]] = field(default_factory=list)


def potrace_geojson(bw_path: Path, turdsize: int, opttolerance: float, alphamax: float) -> list[GeoPolygon]:
    """
    Run potrace in geojson backend and parse the polygons.
    Returns list of GeoPolygon (each with an outer ring and zero or more holes).
    """
    with tempfile.NamedTemporaryFile(suffix=".geojson", delete=False) as tf:
        out = Path(tf.name)
    try:
        cmd = [
            "potrace", "-b", "geojson",
            "-t", str(turdsize),
            "-O", str(opttolerance),
            "-a", str(alphamax),
            "--tight",
            "-o", str(out),
            str(bw_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"potrace failed: {r.stderr or r.stdout}")
        data = json.loads(out.read_text())
    finally:
        out.unlink(missing_ok=True)

    polys: list[GeoPolygon] = []
    for feat in data.get("features", []):
        geom = feat.get("geometry", {})
        if geom.get("type") == "Polygon":
            coords = geom["coordinates"]
            polys.append(GeoPolygon(outer=coords[0], holes=coords[1:]))
        elif geom.get("type") == "MultiPolygon":
            for poly in geom["coordinates"]:
                polys.append(GeoPolygon(outer=poly[0], holes=poly[1:]))
    return polys


# --------------------------------------------------------------------------- #
# Coordinate transform & KiCad emission
# --------------------------------------------------------------------------- #

def transform_ring(ring: list[Pt], scale: float, h_px: int, ox: float = 0.0, oy: float = 0.0) -> list[Pt]:
    """Pixel coords -> KiCad mm coords. Y is flipped (KiCad y is up).
    (ox, oy) is the bottom-left offset of the art region in mm."""
    return [(ox + x * scale, oy + (h_px - y) * scale) for x, y in ring]


def fmt(x: float) -> str:
    # trim trailing zeros but keep reasonable precision
    return f"{x:.4f}".rstrip("0").rstrip(".")


def emit_gr_poly(pts: list[Pt], layer: str, uid: str, stroke: float | None = None) -> list[str]:
    """
    Emit a gr_poly. If stroke is None -> filled solid shape (width ~0).
    If stroke is a number -> unfilled outline with that stroke width (mm).
    """
    if len(pts) < 3:
        return []
    lines = ["\t(gr_poly"]
    lines.append("\t\t(pts")
    row = "\t\t\t"
    n = 0
    for x, y in pts:
        row += f"(xy {fmt(x)} {fmt(y)}) "
        n += 1
        if n % 8 == 0:
            lines.append(row.rstrip())
            row = "\t\t\t"
    if row.strip():
        lines.append(row.rstrip())
    lines.append("\t\t)")
    if stroke is None:
        lines.append("\t\t(stroke (width -0.000001) (type solid))")
        lines.append("\t\t(fill yes)")
    else:
        lines.append(f"\t\t(stroke (width {fmt(stroke)}) (type solid))")
        lines.append("\t\t(fill no)")
    lines.append(f'\t\t(layer "{layer}")')
    lines.append(f'\t\t(uuid "{uid}")')
    lines.append("\t)")
    return lines


def emit_gr_line(x1: float, y1: float, x2: float, y2: float, layer: str, width: float, uid: str) -> str:
    return (
        f'\t(gr_line (start {fmt(x1)} {fmt(y1)}) (end {fmt(x2)} {fmt(y2)}) '
        f'(stroke (width {fmt(width)}) (type default)) (layer "{layer}") (uuid "{uid}"))'
    )


def emit_gr_text(text: str, x: float, y: float, layer: str, size: float, uid: str, angle: float = 0) -> list[str]:
    # KiCad stores multi-line text with the two-character escape sequence \n,
    # not a real newline. Escape backslashes and quotes, then encode newlines.
    esc = text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return [
        f'\t(gr_text "{esc}"',
        f'\t\t(at {fmt(x)} {fmt(y)} {fmt(angle) if angle else 0})',
        f'\t\t(layer "{layer}")',
        f'\t\t(uuid "{uid}")',
        f'\t\t(effects (font (size {fmt(size)} {fmt(size)}) (thickness {fmt(size*0.15)})))',
        "\t)",
    ]


# --------------------------------------------------------------------------- #
# KiCad project file templates
# --------------------------------------------------------------------------- #

SCH_TEMPLATE = """(kicad_sch
	(version 20250114)
	(generator "art2kicad")
	(generator_version "10.0")
	(uuid "{sch_uuid}")
	(paper "A4")
	(title_block
		(title "{title}")
		(date "{date}")
		(rev "1")
		(company "art2kicad")
		(comment 1 "License: {license}")
		(comment 2 "Source: {source}")
	)
	(lib_symbols)
	(symbol_instances)
)
"""

PRO_TEMPLATE = r"""{
  "board": {
    "design_settings": {
      "defaults": {
        "board_outline_line_width": 0.05,
        "copper_line_width": 0.2,
        "copper_text_size_h": 1.5,
        "copper_text_size_v": 1.5,
        "copper_text_thickness": 0.3,
        "courtyard_line_width": 0.05,
        "fab_line_width": 0.1,
        "fab_text_size_h": 1.0,
        "fab_text_size_v": 1.0,
        "fab_text_thickness": 0.15,
        "other_line_width": 0.1,
        "other_text_size_h": 1.0,
        "other_text_size_v": 1.0,
        "other_text_thickness": 0.15,
        "silk_line_width": 0.12,
        "silk_text_size_h": 1.0,
        "silk_text_size_v": 1.0,
        "silk_text_thickness": 0.12
      }
    },
    "layer_presets": []
  },
  "boards": [],
  "cvpcb": { "equivalence_files": [] },
  "libraries": { "pinned_footprint_libs": [], "pinned_symbol_libs": [] },
  "meta": { "filename": "{name}.kicad_pro", "version": 2 },
  "net_settings": { "classes": [ { "bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25, "diff_pair_width": 0.2, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1, "name": "Default", "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)", "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3, "wire_width": 6 } ], "meta": { "version": 4 }, "net_colors": null, "netclass_assignments": null, "netclasses": [] },
  "pcbnew": { "last_paths": { "gencad": "", "idf": "", "netlist": "", "plot": "", "pos_files": "", "specctra_dsn": "", "step": "", "svg": "", "vrml": "" }, "page_layout_descr_file": "" },
  "schematic": { "annotate_start_num": 0, "bom_export_filename": "", "bom_fmt_presets": [], "bom_fmt_settings": { "field_delimiter": ",", "keep_line_breaks": false, "keep_tabs": false, "name": "CSV", "ref_delimiter": ",", "ref_range_delimiter": "", "string_delimiter": "\"" }, "bom_presets": [], "bom_settings": { "exclude_dnp": false, "fields_ordered": [], "filter_string": "", "group_symbols": true, "name": "Grouped By Value", "sort_asc": true, "sort_field": "Reference" }, "connection_grid_size": 50.0, "drawing": { "dashed_lines_dash_length_ratio": 12.0, "dashed_lines_gap_length_ratio": 3.0, "default_line_thickness": 6.0, "default_text_size": 50.0, "field_names": [], "intersheets_ref_own_page": false, "intersheets_ref_prefix": "", "intersheets_ref_short": false, "intersheets_ref_show": false, "intersheets_ref_suffix": "", "junction_size_choice": 3, "label_size_ratio": 0.375, "operating_point_overlay_i_precision": 3, "operating_point_overlay_i_range": "~A", "operating_point_overlay_v_precision": 3, "operating_point_overlay_v_range": "~V", "pin_symbol_size": 25.0, "text_offset_ratio": 0.15 }, "legacy_lib_dir": "", "legacy_lib_list": [], "meta": { "version": 1 }, "net_format_name": "", "page_layout_descr_file": "", "plot_directory": "", "spice_current_sheet_as_root": false, "spice_external_command": "spice \"%I\"", "spice_model_current_sheet_as_root": true, "spice_save_all_currents": false, "spice_save_all_dissipations": false, "spice_save_all_voltages": false, "subpart_first_id": 65, "subpart_internal_separator": 0 }, "sheets": [ [ "{sch_uuid}", "" ] ], "text_variables": {} }
}
"""

LAYERS_BLOCK = """\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(9 "F.Adhes" user "F.Adhesive")
\t\t(11 "B.Adhes" user "B.Adhesive")
\t\t(13 "F.Paste" user)
\t\t(15 "B.Paste" user)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(7 "B.SilkS" user "B.Silkscreen")
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(17 "Dwgs.User" user "User.Drawings")
\t\t(19 "Cmts.User" user "User.Comments")
\t\t(21 "Eco1.User" user "User.Eco1")
\t\t(23 "Eco2.User" user "User.Eco2")
\t\t(25 "Edge.Cuts" user)
\t\t(27 "Margin" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t\t(29 "B.CrtYd" user "B.Courtyard")
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t\t(39 "User.1" user)
\t\t(41 "User.2" user)
\t\t(43 "User.3" user)
\t\t(45 "User.4" user)
\t\t(47 "User.5" user)
\t\t(49 "User.6" user)
\t\t(51 "User.7" user)
\t\t(53 "User.8" user)
\t\t(55 "User.9" user)
\t)"""

PCB_HEADER = """(kicad_pcb
\t(version 20260206)
\t(generator "art2kicad")
\t(generator_version "10.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(title_block
\t\t(title "{title}")
\t\t(date "{date}")
\t\t(rev "1")
\t\t(company "art2kicad")
\t\t(comment 1 "License: {license}")
\t\t(comment 2 "Source: {source}")
\t)
"""

SETUP_BLOCK = """\t(setup
\t\t(pad_to_mask_clearance 0)
\t\t(allow_soldermask_bridges_in_footprints no)
\t\t(tenting (front yes) (back yes))
\t)
"""


def render_appearance(
    out_png: Path,
    gray: Image.Image,
    masks: dict[str, Image.Image],
    board_w: float,
    board_h: float,
    edge_offset: float,
    mask_color: str,
    copper_color: str,
    fr4_color: str,
    silk_color: str,
    silk_mode: str,
    silk_width_mm: float,
    scale: float,
) -> Path:
    """
    Composite a synthetic 'how the finished board will look' PNG directly from
    the raster masks. This is a fast, KiCad-independent fidelity check.
    Layering (back to front):
        mask_color  (solder mask background)
        fr4_color   where fr4 mask opening (exposed substrate)
        copper_color where copper mask opening (exposed metal)
        silk_color   outline strokes on top
    """
    iw, ih = gray.size
    # work at processing resolution; output ~3x for a crisper image
    out = Image.new("RGB", (iw * 3, ih * 3), mask_color)
    canvas = Image.new("RGB", (iw, ih), mask_color)
    px = canvas.load()

    fr4_arr = np.asarray(masks["fr4"].convert("L")) > 0
    cu_arr = np.asarray(masks["copper"].convert("L")) > 0

    fr4_rgb = Image.new("RGB", (iw, ih), fr4_color)
    cu_rgb = Image.new("RGB", (iw, ih), copper_color)

    # paint FR4 where fr4 opening (and not copper — copper takes precedence)
    canvas = Image.composite(canvas, fr4_rgb, Image.fromarray(fr4_arr.astype(np.uint8) * 255))
    # paint copper where copper opening (overrides fr4 in overlap, though bands are disjoint)
    canvas = Image.composite(canvas, cu_rgb, Image.fromarray(cu_arr.astype(np.uint8) * 255))

    # silkscreen outlines: rasterize the lit/contour mask boundary as thin white lines
    if silk_mode in ("outline", "contour"):
        key = "lit" if silk_mode == "outline" else None
        if silk_mode == "contour":
            # union boundary of copper and fr4 = boundary of lit too
            key = "lit"
        u = np.asarray(masks[key].convert("L")) > 0
        uimg = Image.fromarray(u.astype(np.uint8) * 255)
        dil = np.asarray(uimg.filter(ImageFilter.MaxFilter(3)), dtype=np.int16)
        ero = np.asarray(uimg.filter(ImageFilter.MinFilter(3)), dtype=np.int16)
        edge = (dil - ero) > 0
        # thicken to ~silk_width in px
        px_w = max(1, int(round(silk_width_mm / scale)))
        edge_img = Image.fromarray(edge.astype(np.uint8) * 255)
        if px_w > 1:
            edge_img = edge_img.filter(ImageFilter.MaxFilter(px_w if px_w % 2 == 1 else px_w + 1))
        silk_rgb = Image.new("RGB", (iw, ih), silk_color)
        canvas = Image.composite(canvas, silk_rgb, edge_img)

    # crop to board outline (edge_offset inset in mm -> px)
    eo_px = max(0, int(round(edge_offset / scale)))
    # The board is LARGER than the art by edge_offset on every side (a margin of
    # bare mask color). Add that margin rather than cropping the art.
    if eo_px > 0:
        bordered = Image.new("RGB", (iw + 2 * eo_px, ih + 2 * eo_px), mask_color)
        bordered.paste(canvas, (eo_px, eo_px))
        canvas = bordered
    canvas = canvas.resize((canvas.width * 3, canvas.height * 3), Image.LANCZOS)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png)
    return out_png


# --------------------------------------------------------------------------- #
# Main driver
# --------------------------------------------------------------------------- #

def deterministic_uuid(seed: str) -> str:
    # stable UUIDs derived from a string so reruns are reproducible-ish
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def build_pcb(
    name: str,
    title: str,
    date: str,
    license_str: str,
    source: str,
    board_w: float,
    board_h: float,
    edge_offset: float,
    graphics: list[str],
) -> str:
    sch_uuid = deterministic_uuid(f"{name}-sch")
    parts = [PCB_HEADER.format(title=title, date=date, license=license_str, source=source)]
    parts.append(LAYERS_BLOCK)
    parts.append(SETUP_BLOCK)
    parts.append('\t(net 0 "")')
    # board outline = outer rectangle (0,0)-(board_w, board_h).
    # Artwork is mapped to the inner region, so copper clears the edge.
    x0, y0 = 0.0, 0.0
    x1, y1 = board_w, board_h
    edge_pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for k in range(4):
        a = edge_pts[k]
        b = edge_pts[(k + 1) % 4]
        parts.append(emit_gr_line(a[0], a[1], b[0], b[1], "Edge.Cuts", 0.05,
                                  deterministic_uuid(f"{name}-edge-{k}")))
    parts.extend(graphics)
    parts.append("\t(embedded_fonts no)")
    parts.append(")")
    return "\n".join(parts) + "\n"


def write_project(out_dir: Path, name: str, pcb_text: str, title: str, date: str,
                  license_str: str, source: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    sch_uuid = deterministic_uuid(f"{name}-sch")
    (out_dir / f"{name}.kicad_pcb").write_text(pcb_text)
    (out_dir / f"{name}.kicad_sch").write_text(SCH_TEMPLATE.format(
        sch_uuid=sch_uuid, title=title, date=date, license=license_str, source=source))
    # The .kicad_pro JSON contains literal { } braces, so use plain substitution.
    pro = (PRO_TEMPLATE
           .replace('"{name}.kicad_pro"', f'"{name}.kicad_pro"')
           .replace('"{sch_uuid}"', f'"{sch_uuid}"'))
    (out_dir / f"{name}.kicad_pro").write_text(pro)


def run_export(out_dir: Path, name: str, want_pdf: bool, want_gerber: bool) -> None:
    pcb = out_dir / f"{name}.kicad_pcb"
    if want_pdf:
        pdf = out_dir / f"{name}-preview.pdf"
        layers = "F.Cu,F.Mask,F.SilkS,Edge.Cuts"
        r = subprocess.run(
            ["kicad-cli", "pcb", "export", "pdf", "--output", str(pdf),
             "--layers", layers, "--mode-single", "--include-border-title",
             str(pcb)],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  [warn] pdf export failed: {r.stderr.strip() or r.stdout.strip()}", file=sys.stderr)
        else:
            print(f"  wrote {pdf}")
    if want_gerber:
        gdir = out_dir / "gerbers"
        r = subprocess.run(
            ["kicad-cli", "pcb", "export", "gerbers", "-o", str(gdir),
             "--layers", "F.Cu,F.Mask,F.SilkS,Edge.Cuts", str(pcb)],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  [warn] gerber export failed: {r.stderr.strip() or r.stdout.strip()}", file=sys.stderr)
        else:
            print(f"  wrote gerbers in {gdir}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="art2kicad",
        description="Convert a raster image into a KiCad PCB-art project.",
    )
    ap.add_argument("image", type=Path, help="input JPEG/PNG image")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("build"),
                   help="output directory (default: ./build)")
    ap.add_argument("-n", "--name", type=str, default=None,
                   help="project base name (default: input filename stem)")
    ap.add_argument("--title", default="", help="artwork title (metadata)")
    ap.add_argument("--artist", default="", help="artist (metadata)")
    ap.add_argument("--license", default="", help="license string (metadata)")
    ap.add_argument("--source", default="", help="source URL (metadata)")

    # board geometry
    ap.add_argument("--width", type=float, default=100.0,
                   help="board width in mm (default 100); height derived from aspect ratio")
    ap.add_argument("--height", type=float, default=None,
                   help="board height in mm (overrides aspect-ratio-derived height)")
    ap.add_argument("--edge-offset", type=float, default=2.0,
                   help="inset of Edge.Cuts outline from board extent (mm)")

    # tonal split
    ap.add_argument("--t1", type=int, default=None,
                   help="low threshold 0-255 (dark->background boundary). auto if unset")
    ap.add_argument("--t2", type=int, default=None,
                   help="high threshold 0-255 (copper->fr4 boundary). auto if unset")
    ap.add_argument("--p1", type=float, default=35.0,
                   help="percentile for auto t1 (default 35)")
    ap.add_argument("--p2", type=float, default=72.0,
                   help="percentile for auto t2 (default 72)")
    ap.add_argument("--invert", action="store_true",
                   help="invert the luminance before splitting")

    # silk
    ap.add_argument("--silk", choices=["none", "outline", "contour"], default="outline",
                   help="silkscreen source: outline=stroked outlines of lit regions, "
                        "contour=stroked outlines of each band, none (default outline)")
    ap.add_argument("--silk-width", type=float, default=0.15,
                   help="silkscreen outline stroke width in mm (default 0.15)")
    ap.add_argument("--silk-color", default="white", help="(render only) silkscreen color")
    ap.add_argument("--mask-color", default="black", help="(render only) solder mask color")
    ap.add_argument("--copper-color", default="#b87333", help="(render only) exposed copper color")
    ap.add_argument("--fr4-color", default="#d9c89a", help="(render only) exposed FR4 color")

    # manufacturing-aware cleanup
    ap.add_argument("--min-feature-mm", type=float, default=0.25,
                   help="approx. minimum feature size in mm (drives morphology + potrace turdsize)")
    ap.add_argument("--close-px", type=int, default=3,
                   help="morphological close kernel px (gaps/cracks)")
    ap.add_argument("--open-px", type=int, default=2,
                   help="morphological open kernel px (specks; potrace turdsize also drops specks)")
    ap.add_argument("--opttolerance", type=float, default=0.6,
                   help="potrace curve optimization tolerance (higher=fewer nodes)")
    ap.add_argument("--alphamax", type=float, default=1.0,
                   help="potrace corner threshold (1=default, 0=polygonal)")

    # downsampling for speed (image is large)
    ap.add_argument("--max-px", type=int, default=1000,
                   help="downsample longest side to N px before processing (0=no resample)")

    # export
    ap.add_argument("--pdf", action="store_true", help="run kicad-cli to export a preview PDF")
    ap.add_argument("--gerber", action="store_true", help="run kicad-cli to export gerbers")
    ap.add_argument("--render", action="store_true",
                   help="render a synthetic board-appearance PNG (no KiCad needed)")
    ap.add_argument("--keep-masks", action="store_true", help="keep intermediate PNG/PGM masks")

    args = ap.parse_args(argv)

    name = args.name or args.image.stem
    out_dir = args.outdir / name
    date = "2026-07-05"

    print(f"[art2kicad] {args.image} -> {out_dir}/{name}.kicad_pcb")

    # ---- load & preprocess ----
    gray = load_gray(args.image)
    if args.max_px and max(gray.size) > args.max_px:
        w, h = gray.size
        s = args.max_px / max(w, h)
        gray = gray.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        print(f"  downsampled to {gray.size}")
    gray = auto_level(gray)
    if args.invert:
        gray = ImageOps.invert(gray)
    arr = np.asarray(gray, dtype=np.int32)

    if args.t1 is None or args.t2 is None:
        t_lo_a, t_hi_a = percentile_thresholds(arr, args.p1, args.p2)
        t_lo = args.t1 if args.t1 is not None else t_lo_a
        t_hi = args.t2 if args.t2 is not None else t_hi_a
    else:
        t_lo, t_hi = args.t1, args.t2
    if t_hi <= t_lo:
        t_hi = min(255, t_lo + 1)
    print(f"  thresholds: t1={t_lo}  t2={t_hi}  (silk={args.silk})")

    # ---- board geometry from aspect ratio ----
    # `--width` is the OUTER board width. The artwork is mapped to the inner
    # region [edge_offset, board_w-edge_offset], preserving aspect ratio; the
    # Edge.Cuts rectangle is the outer board footprint. This keeps copper away
    # from the board edge (clears the copper_edge_clearance DRC rule).
    iw, ih = gray.size
    eo = args.edge_offset
    art_w = max(1.0, args.width - 2 * eo)
    if args.height is not None:
        board_w, board_h = args.width, args.height
        art_h = max(1.0, board_h - 2 * eo)
    else:
        art_h = art_w * (ih / iw)
        board_w = args.width
        board_h = art_h + 2 * eo
    scale = art_w / iw  # mm per pixel (art region)
    print(f"  board: {board_w:.2f} x {board_h:.2f} mm  (art {art_w:.2f} x {art_h:.2f} mm, margin {eo} mm)  "
          f"(scale {scale*1000:.1f} um/px)")

    # ---- make masks ----
    masks = make_band_masks(gray, t_lo, t_hi, args.close_px, args.open_px)
    if args.keep_masks:
        (out_dir).mkdir(parents=True, exist_ok=True)

    # ---- vectorize & emit ----
    # potrace turdsize: remove islands whose pixel-area < turdsize.
    # translate min-feature to a pixel area (roughly (min_feature/scale)^2).
    px_per_mm = 1.0 / scale
    min_feat_px = max(2, int(args.min_feature_mm * px_per_mm))
    turd = max(2, int((min_feat_px * min_feat_px) / 4))
    print(f"  min-feature {args.min_feature_mm}mm ~ {min_feat_px}px -> potrace turdsize={turd}")

    graphics: list[str] = []

    def vectorize(mask_key: str) -> tuple[list[GeoPolygon], Path | None]:
        bw = masks[mask_key]
        if args.keep_masks:
            (out_dir / f"{mask_key}.png").save(bw.convert("L"))
        pgm = out_dir / f"{mask_key}.pgm" if args.keep_masks else Path(tempfile.mkstemp(suffix=".pgm")[1])
        # potrace traces BLACK (0) pixels, so invert: band becomes black, background white.
        ImageOps.invert(bw.convert("L")).save(pgm)
        polys = potrace_geojson(pgm, turd, args.opttolerance, args.alphamax)
        if not args.keep_masks:
            pgm.unlink(missing_ok=True)
        return polys, (pgm if args.keep_masks else None)

    def emit_filled(mask_key: str, kicad_layers: list[str], label: str) -> int:
        polys, _ = vectorize(mask_key)
        count = 0
        for idx, gp in enumerate(polys):
            merged = transform_ring(merge_holes(gp.outer, gp.holes), scale, ih, eo, eo)
            if len(merged) < 3:
                continue
            for layer in kicad_layers:
                graphics.extend(emit_gr_poly(
                    merged, layer,
                    deterministic_uuid(f"{name}-{label}-{layer}-{idx}")))
            count += 1
        print(f"  {label:7s} -> {count} polygon(s) on {','.join(kicad_layers)}")
        return count

    def emit_outline(mask_key: str, layer: str, width_mm: float, label: str) -> int:
        polys, _ = vectorize(mask_key)
        count = 0
        for idx, gp in enumerate(polys):
            merged = transform_ring(merge_holes(gp.outer, gp.holes), scale, ih, eo, eo)
            if len(merged) < 3:
                continue
            graphics.extend(emit_gr_poly(
                merged, layer,
                deterministic_uuid(f"{name}-{label}-{layer}-{idx}"),
                stroke=width_mm))
            count += 1
        print(f"  {label:7s} -> {count} outline(s) on {layer} (stroke {width_mm}mm)")
        return count

    # exposed metal: copper AND matching mask opening
    emit_filled("copper", ["F.Cu", "F.Mask"], "copper")
    # exposed FR4: mask opening only (no copper underneath)
    emit_filled("fr4", ["F.Mask"], "fr4")
    # silkscreen: clean vector outlines of the lit (non-background) regions
    if args.silk == "outline":
        emit_outline("lit", "F.SilkS", args.silk_width, "silk")
    elif args.silk == "contour":
        emit_outline("copper", "F.SilkS", args.silk_width, "silk-cu")
        emit_outline("fr4", "F.SilkS", args.silk_width, "silk-fr4")
    # silk == none: nothing

    # optional attribution silkscreen text in the corner
    if args.license or args.artist:
        txt_lines = []
        if args.title: txt_lines.append(args.title)
        if args.artist: txt_lines.append(args.artist)
        if args.license: txt_lines.append(args.license)
        txt = "\n".join(txt_lines)
        sz = max(1.0, board_h * 0.012)
        graphics.extend(emit_gr_text(
            txt, board_w - 2, board_h - 2 - sz, "F.SilkS", sz,
            deterministic_uuid(f"{name}-attr")))

    # ---- build & write project ----
    pcb_text = build_pcb(name, args.title or name, date, args.license, args.source,
                         board_w, board_h, args.edge_offset, graphics)
    write_project(out_dir, name, pcb_text, args.title or name, date,
                  args.license, args.source)
    print(f"  wrote {out_dir}/{name}.kicad_pro/.kicad_sch/.kicad_pcb")

    # ---- optional synthetic appearance render ----
    if args.render:
        render_png = render_appearance(
            out_dir / f"{name}-appearance.png", gray, masks,
            board_w, board_h, edge_offset=args.edge_offset,
            mask_color=args.mask_color, copper_color=args.copper_color,
            fr4_color=args.fr4_color, silk_color=args.silk_color,
            silk_mode=args.silk, silk_width_mm=args.silk_width, scale=scale)
        print(f"  wrote {render_png}")

    # ---- optional export ----
    if args.pdf or args.gerber:
        run_export(out_dir, name, args.pdf, args.gerber)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
