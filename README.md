# traces — pictures as printed circuit boards

`traces` is a small toolkit for turning public-domain / Creative-Commons
artworks into manufacturable PCB art: boards whose image is built entirely
from FR4, bare copper / ENIG, solder mask, and silkscreen — no ink, no
components, no nets.

The result is a real, fab-able KiCad project: gerbers plot cleanly and DRC
is essentially clean (only art-feature advisories).

## Repository layout

```
traces/
├── art2kicad/        the generator (single-file Python tool + its own README)
├── testdata/         sample CC-BY-SA images (Zdzisław Beksiński)
├── examples/         (reserved for example configs / sample boards)
├── outputs/          generated projects (gitignored)
├── tools/            (reserved for auxiliary scripts)
├── README.md         this file
└── .gitignore
```

## The generator: `art2kicad`

See [`art2kicad/README.md`](art2kicad/README.md) for full documentation.

### 4-tone PCB palette

| board material            | KiCad layer(s)         | image tone         |
|---------------------------|------------------------|--------------------|
| solder mask (background)  | *(covered area)*       | darkest band       |
| bare copper / ENIG metal  | `F.Cu` + `F.Mask`      | mid band           |
| exposed FR4 substrate     | `F.Mask` only          | lightest band      |
| silkscreen ink            | `F.SilkS` (edge ribbons) | image structural edges |

### Pipeline

```
image ──► PIL: grayscale + auto-level + 3-band threshold + morphology
              │
              ▼
        potrace -b geojson   (vectorize each band, drop specks)
              │
              ▼
        keyhole-bridge holes → single simple polygons
              │
              ▼
        gr_poly on F.Cu / F.Mask / F.SilkS  +  Edge.Cuts outline
        + empty schematic + minimal .kicad_pro
```

### Requirements

```bash
python3 -c "import PIL, numpy; print('ok')"
potrace --version        # 1.16
kicad-cli --version      # 10.0+ (optional, for --pdf / --gerber)
```

### Quick start

```bash
python3 art2kicad/art2kicad.py testdata/AA78_by_Zdzislaw_Beksinski_1978.jpg \
    -o outputs -n aa78 \
    --title "AA78" --artist "Zdzisław Beksiński (1978)" \
    --license "CC BY-SA 3.0" \
    --source "https://commons.wikimedia.org/wiki/File:AA78_by_Zdzislaw_Beksinski_1978.jpg" \
    --width 100 --render --pdf --gerber
```

Produces, in `outputs/aa78/`:

- `aa78.kicad_pro` / `aa78.kicad_sch` / `aa78.kicad_pcb` — the KiCad project
- `aa78-appearance.png` — synthetic "how the finished board looks" render
- `aa78-preview.pdf` — `kicad-cli` layer plot
- `gerbers/` — `F_Cu`, `F_Mask`, `F_SilkS`, `Edge_Cuts` + job file

### Tuning a new image

1. Run once with `--render` (fast, no KiCad) and inspect `*-appearance.png`.
2. Too much/little copper → adjust `--t1` (or `--p1`). Too much/little exposed
   FR4 → adjust `--t2` (or `--p2`). Use `--invert` for negatives.
3. Too busy → raise `--opttolerance` and `--min-feature-mm`.
4. Silk edges too busy → raise `--silk-edge-pct` (e.g. 92–95) or `--silk-edge-turd`.
5. Fine highlights vanishing → `--open-px 1`, raise `--p2`.
6. Re-run with `--pdf --gerber` for fabrication output.

See [`art2kicad/README.md`](art2kicad/README.md) for the full option list.

## Recommended board finish

For art boards: **black solder mask + ENIG + white silk**.

- black mask = darkest tone (background)
- gold ENIG on exposed copper = mid tone (metal)
- tan FR4 through mask openings = lightest tone
- white silk outlines = crisp detail/edges

## Licensing notes

`art2kicad` only embeds the license/attribution text you pass via `--license`
`--source` `--artist` `--title` into board metadata and an optional silkscreen
corner block. It is your responsibility to:

- verify the source image is public-domain or CC-licensed,
- honour attribution (CC-BY) and share-alike (CC-BY-SA) terms on the
  *derivative* design files you publish,
- avoid NC-licensed images if you intend to sell boards.

The sample images in `testdata/` are © Zdzisław Beksiński, licensed
[CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) via
[Muzeum Historyczne w Sanoku](https://commons.wikimedia.org/wiki/Category:Paintings_by_Zdzisław_Beksiński_in_Muzeum_Historyczne_w_Sanoku),
sourced from Wikimedia Commons.
