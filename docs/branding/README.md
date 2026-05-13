# neon-legion branding kit

![Hero banner](hero-banner.svg)

This directory contains the vector branding kit for `neon-legion`. The assets are pure SVG: no JavaScript, no external fonts, no external images, and no binary image weight.

The visual system is built around the **agent command mark**: an angular `NL` monogram, five agent nodes, a human approval accent, and ledger rails. The goal is product identity first, cyberpunk mood second.

## Assets

| File | Intended use |
|---|---|
| `hero-banner.svg` | README header banner, 1500 x 500. |
| `social-card.svg` | OpenGraph/Twitter/Telegram preview card, 1200 x 630. |
| `logo.svg` | Square command mark and favicon/avatar source, 512 x 512. |
| `divider.svg` | Subtle README section divider, 1500 x 72. |
| `neon-legion-flow.dot` / `.svg` | English agent conveyor diagram for the public README. |
| `neon-legion-flow.ru.dot` / `.svg` | Russian agent conveyor diagram for Russian architecture docs. |

## Previews

![Social card](social-card.svg)

![Square logo](logo.svg)

![Pipeline divider](divider.svg)

![Agent conveyor](neon-legion-flow.svg)

## Design Rules

1. Keep the mark readable at small sizes: no body copy inside the core logo.
2. Use neon accents as state signals, not decoration.
3. Show real product concepts: command, plan, build, review, track.
4. Keep public assets scrubbed: no personal paths, hostnames, tokens, or live session text.

## Palette

| Color | Role |
|---|---|
| `#020617` | Obsidian background. |
| `#00D4FF` | Command cyan for primary lines and agent nodes. |
| `#64FFDA` | Ledger green for savings, completion, and tracked state. |
| `#FFB020` | Human approval amber. |
| `#FF2EC4` | Audit magenta for review/warning accents. |
| `#F8FAFC` | Near-white typography and secondary lines. |
| `#7C3AED` | Optional low-opacity depth glow only. |

## Swapping Assets

Replace the SVG file in this directory with another self-contained SVG using the same filename. GitHub README links use relative paths, so replacing `hero-banner.svg` or `logo.svg` updates the rendered preview without changing Markdown.

Recommended root README integration:

```md
![neon-legion local AI command banner](docs/branding/hero-banner.svg)
```

## License

This branding kit is MIT-licensed under the same license as the repository. You may replace, fork, remix, or discard it freely.
