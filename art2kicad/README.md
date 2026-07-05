# art2kicad — turn a picture into a PCB

`art2kicad` converts a raster image (JPEG/PNG) into a self-contained **KiCad 10
project** whose PCB depicts the artwork using only manufacturable board
finishes — no ink, no components, no nets:

| board material            | KiCad layer(s)         | image tone         |
|---------------------------|------------------------|--------------------|
| solder mask (background)  | *(covered area)*       | darkest band       |
| bare copper / ENIG metal  | `F.Cu` + `F.Mask`      | mid band           |
| exposed FR4 substrate     | `F.Mask` only          | lightest band      |
| silkscreen ink            | `F.SilkS` (edge ribbons) | image structural edges |

The result is a real, fab-able board: gerbers plot cleanly and DRC is essentially
clean (only art-feature width/spacing advisories).

## Pipeline

```
image ──► grayscale + auto-level ──► 3 luminance bands
   │                                       │
   │              (copper, fr4, lit)       │
   │                                       ▼
   │                            PIL morphology (close/open)
   │                                       │
   │                                       ▼
   │         ┌─────────────────┐  potrace -b geojson  (vectorize, drop specks)
   │         │ Sobel edge mask │       │
   │         │ (for silk=edges)│       ▼
   │         └────────┬────────┘  keyhole-bridge holes → single simple polygons
   │                  │                       │
   │                  ▼                       ▼
   │           potrace -b geojson     px→mm scale, Y-flip to KiCad up-axis
   │                  │                       │
   ▼                  ▼                       ▼
  preview PNG  ◄── synthetic render    gr_poly on F.Cu / F.Mask / F.SilkS
                                        + Edge.Cuts outline
                                        + empty schematic
                                        + minimal .kicad_pro
```

Why each tool:

* **PIL + numpy** — grayscale, auto-level, band thresholding, morphological
  cleanup (open/close), and the synthetic appearance render.
* **potrace** (`-b geojson`) — vectorizes each binary mask into polygons with
  holes. GeoJSON gives flat coordinates (no Béziers to flatten), and
  `--turdsize` drops sub-feature specks at the vector level.
* **Python** — parses GeoJSON, merges holes into each outer ring with the
  zero-width "keyhole" bridge technique (KiCad `gr_poly` cannot represent
  holes directly), scales/flips coordinates, and writes the KiCad S-expression
  files.
* **KiCad (`kicad-cli`)** — optional: loads the board, runs DRC, and exports
  gerbers / preview PDF. Also required: the `.kicad_pcb` filename **must**
  match the `.kicad_pro` filename, and a `.kicad_sch` must sit alongside, or
  `kicad-cli` refuses to load the board.

## Requirements

```bash
python3 -c "import PIL, numpy; print('ok')"
potrace --version        # 1.16
kicad-cli --version      # 10.0+ (optional, for --pdf / --gerber)
```

## Usage

```bash
python3 art2kicad.py <image.{jpg,png}> -o <out-dir> -n <project-name> \
    --title "..." --artist "..." --license "..." --source "..." \
    --width 100 --render --pdf --gerber
```

Outputs (in `<out-dir>/<name>/`):

* `<name>.kicad_pro`, `<name>.kicad_sch`, `<name>.kicad_pcb` — the project
* `<name>-render3d.png` — real 3D render via `kicad-cli pcb render` (`--render-3d`)
* `<name>-preview.pdf` — `kicad-cli` layer plot (`--pdf`)
* `gerbers/` — `F_Cu`, `F_Mask`, `F_SilkS`, `Edge_Cuts` + job file (`--gerber`)

### Key options

| option | default | meaning |
|--------|---------|---------|
| `--width` | 100 | board width in mm; height follows aspect ratio |
| `--height` | *(auto)* | override height in mm |
| `--t1` / `--t2` | *(auto)* | luminance thresholds 0–255 (dark→bg, mid→copper, light→fr4) |
| `--p1` / `--p2` | 35 / 72 | percentiles for auto thresholds |
| `--invert` | off | invert luminance first (use for negatives) |
| `--silk` | `edges` | `none` / `edges` (Sobel edge ribbons, default) / `outline` (lit-region outlines) / `contour` (per-band) |
| `--silk-width` | 0.15 | silkscreen stroke/ribbon width in mm |
| `--silk-edge-blur` | 1.5 | edges mode: Gaussian blur σ before Sobel |
| `--silk-edge-pct` | 90 | edges mode: keep edges above this magnitude percentile (higher=fewer edges) |
| `--silk-edge-turd` | 20 | edges mode: potrace turdsize for silk (breaks up connected edge networks) |
| `--min-feature-mm` | 0.25 | drives potrace turdsize (speck removal) |
| `--close-px` / `--open-px` | 3 / 2 | morphological cleanup kernel (px) |
| `--opttolerance` | 0.6 | potrace simplification (higher = fewer nodes) |
| `--max-px` | 800 | downsample longest side before processing (speed) |
| `--mask-color`/`--copper-color`/`--fr4-color`/`--silk-color` | black/#b87333/#d9c89a/white | render colors only |
| `--render-3d` | off | render a real 3D view of the PCB via `kicad-cli pcb render` |
| `--render-3d-size` | 800 | 3D render image size in px (higher may fail headless) |
| `--render-3d-side` | top | 3D camera side: top/bottom/left/right/front/back |
| `--render-3d-rotate` | *(none)* | 3D board rotation, e.g. `-45,0,45` for isometric |

### Tuning a new image

Each image wants its own thresholds. Workflow:

1. Run once with `--render` (fast, no KiCad needed) and inspect
   `<name>-appearance.png`.
2. If too much / too little copper, change `--t1`. If too much / too little
   exposed FR4, change `--t2`. Use `--p1`/`--p2` to retune both from
   percentiles.
3. If the board is too busy, raise `--opttolerance` and `--min-feature-mm`.
4. If fine highlights vanish, lower `--open-px` to 1 and raise `--p2`.
5. If silk edges are too busy, raise `--silk-edge-pct` (e.g. 92–95) or `--silk-edge-turd`.
6. If silk edges are too sparse, lower `--silk-edge-pct` (e.g. 85) or `--silk-edge-blur`.
7. Re-run with `--pdf --gerber` to produce fabrication output.

## How the 4-tone mapping reads on a finished board

Recommended finish for art boards: **black solder mask + ENIG + white silk**.

* black mask = the darkest tone (background)
* gold ENIG on exposed copper = the mid tone (metal)
* tan FR4 through mask openings = the lightest tone
* white silk edge ribbons = crisp structural detail from the source image

## Licensing notes

`art2kicad` only embeds license/attribution text you pass via `--license`
`--source` `--artist` `--title` into board metadata and an optional silkscreen
corner block. It is your responsibility to:

* verify the source image is public-domain or CC-licensed,
* honour attribution (CC-BY) and share-alike (CC-BY-SA) terms on the
  *derivative* design files you publish,
* avoid NC-licensed images if you intend to sell boards.

## Repo layout

```
art2kicad/
  art2kicad.py     # the tool (single file)
  README.md        # this file
outputs/
  <name>/
    <name>.kicad_pro / .kicad_sch / .kicad_pcb
    <name>-appearance.png   <name>-comparison.png
    <name>-preview.pdf      gerbers/
```
