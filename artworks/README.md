# Example artworks

A collection of famous public-domain and Creative-Commons artworks from
[Wikimedia Commons](https://commons.wikimedia.org), suitable for testing
`art2kicad`. Each image has a matching `.md` metadata file with full
attribution, license, and source links.

## Artworks (20 images)

| Slug | Title | Artist | Year | License |
|------|-------|--------|------|---------|
| `starry_night` | The Starry Night | Vincent van Gogh | 1889 | Public domain |
| `the_scream` | The Scream | Edvard Munch | 1893 | Public domain |
| `mona_lisa` | Mona Lisa | Leonardo da Vinci | 1503 | Public domain |
| `girl_with_a_pearl_earring` | Girl with a Pearl Earring | Johannes Vermeer | 1665 | Public domain |
| `the_great_wave` | The Great Wave off Kanagawa | Katsushika Hokusai | 1831 | Public domain |
| `birth_of_venus` | The Birth of Venus | Sandro Botticelli | 1485 | Public domain |
| `the_night_watch` | The Night Watch | Rembrandt van Rijn | 1642 | Public domain |
| `wanderer_above_the_sea_of_fog` | Wanderer above the Sea of Fog | Caspar David Friedrich | 1818 | Public domain |
| `the_kiss_klimt` | The Kiss | Gustav Klimt | 1908 | Public domain |
| `liberty_leading_the_people` | Liberty Leading the People | Eugène Delacroix | 1830 | Public domain |
| `whistlers_mother` | Whistler's Mother | James McNeill Whistler | 1871 | Public domain |
| `the_card_players` | The Card Players | Paul Cézanne | 1893 | Public domain |
| `impression_sunrise` | Impression, Sunrise | Claude Monet | 1872 | Public domain |
| `third_of_may_1808` | The Third of May 1808 | Francisco Goya | 1814 | Public domain |
| `saturn_devouring_his_son` | Saturn Devouring His Son | Francisco Goya | 1823 | Public domain |
| `arnolfini_portrait` | The Arnolfini Portrait | Jan van Eyck | 1434 | Public domain |
| `death_of_marat` | The Death of Marat | Jacques-Louis David | 1793 | Public domain |
| `black_square` | Black Square | Kazimir Malevich | 1915 | CC BY-SA 4.0 |
| `aa78` | AA78 | Zdzisław Beksiński | 1978 | CC BY-SA 3.0 |
| `untitled_1984` | Untitled | Zdzisław Beksiński | 1984 | CC BY-SA 3.0 |

## Generating boards

```bash
# e.g. generate a board from The Starry Night
python3 ../art2kicad/art2kicad.py artworks/starry_night.jpg \
    -o outputs -n starry_night \
    --title "The Starry Night" --artist "Vincent van Gogh" \
    --license "Public domain" \
    --source "https://commons.wikimedia.org/wiki/File:VanGogh-starry_night_ballance1.jpg" \
    --width 100 --render --pdf --gerber
```

## Licensing

All artworks are free for commercial use:

- **17 paintings** are in the **public domain** (artist deceased > 100 years;
  Wikimedia's policy: faithful reproductions of 2D PD works are also PD).
- **1 painting** (`black_square`) is licensed **CC BY-SA 4.0** — commercial
  use is permitted, but you must credit the photographer (Wikimedia user
  "shakko") and release derivatives under the same license.
- **2 paintings** (`aa78`, `untitled_1984`) by Zdzisław Beksiński are licensed
  **CC BY-SA 3.0** — commercial use permitted with attribution and share-alike.
