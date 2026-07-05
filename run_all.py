#!/usr/bin/env python3
"""Run art2kicad on every artwork in artworks/, one at a time."""
import re, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARTWORKS = ROOT / "artworks"
TOOL = ROOT / "art2kicad" / "art2kicad.py"
OUT = ROOT / "outputs"

def parse_md(md: Path) -> dict:
    """Extract title, artist, license, source from a metadata .md file."""
    txt = md.read_text()
    def field(name):
        m = re.search(rf'\*\*{name}\*\*\s*\|\s*(.+?)\s*\|', txt)
        if not m:
            return ""
        val = m.group(1).strip()
        # extract URL from markdown link [text](url)
        lm = re.search(r'\((https?://[^)]+)\)', val)
        if lm:
            return lm.group(1)
        # strip backticks
        return val.strip('`')
    return {
        "slug": field("Slug"),
        "title": field("Title"),
        "artist": field("Artist"),
        "license": field("License"),
        "source": field("Source"),
    }

artworks = sorted(ARTWORKS.glob("*.jpg"))
print(f"Found {len(artworks)} artworks to process\n", flush=True)

ok, fail = 0, 0
for i, jpg in enumerate(artworks, 1):
    md = jpg.with_suffix(".md")
    if not md.exists():
        print(f"[{i}/{len(artworks)}] SKIP {jpg.name} (no .md)", flush=True)
        continue
    meta = parse_md(md)
    slug = meta["slug"] or jpg.stem
    print(f"[{i}/{len(artworks)}] {slug}: {meta['title']} — {meta['artist']}", flush=True)

    cmd = [
        sys.executable, str(TOOL), str(jpg),
        "-o", str(OUT), "-n", slug,
        "--title", meta["title"],
        "--artist", meta["artist"],
        "--license", meta["license"],
        "--source", meta["source"],
        "--width", "100",
        "--max-px", "800",
        "--render-3d",
    ]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    dt = time.time() - t0
    if r.returncode == 0:
        # print last 2 lines of stdout for the summary
        lines = [l for l in r.stdout.strip().split("\n") if l]
        for l in lines[-3:]:
            print(f"  {l}", flush=True)
        print(f"  done in {dt:.1f}s\n", flush=True)
        ok += 1
    else:
        print(f"  FAILED ({dt:.1f}s)", flush=True)
        print(f"  stderr: {r.stderr[-500:]}", flush=True)
        print(f"  stdout: {r.stdout[-500:]}", flush=True)
        print(flush=True)
        fail += 1

print(f"=== COMPLETE: {ok} ok, {fail} failed ===", flush=True)
