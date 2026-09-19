---
paths:
  - "README.md"
  - "LICENSE"
  - "LICENSE-*.md"
  - "content/**"
  - "docs/**"
---

# Licensing facts (do not drift)

Three licenses split by kind of material: code is MIT (`LICENSE`), original creative content is CC BY-NC 4.0 (`LICENSE-CONTENT.md`), and game content is OGL 1.0a (`LICENSE-OGL.md`). The README's "Licensing" section is the human-facing statement of the split.

- **Never describe this app as compatible with, designed for, or an implementation of *Old-School Essentials*.** The OSE SRD declares the names "Necrotic Gnome" and "Old-School Essentials" to be Product Identity. OGL §7 bars using Product Identity "including as an indication as to compatibility" without a separate trademark agreement. That agreement, Necrotic Gnome's Third-Party License, is deliberately **not** taken. It is scoped to products "designed for use with" OSE, and it would require this repo to state "Requires Old-School Essentials". That statement is false for a self-contained game that needs no rulebook. Say "the classic 1981 B/X rules" instead. osrlib's own README states that no claim of compatibility is made, and this repo's README makes none either.
- **Naming the trademark in an *attribution* context is fine and is compelled.** OGL §6 requires reproducing the Section 15 chain, which names the OSE SRD outright. What matters is whether the use is attribution or marketing, not the string itself. That is why `content/README.md`'s "not Necrotic Gnome's catalogue" (a disclaimer) stays, and why "SRD" and "B/X" anywhere in docstrings and comments are fine. "SRD" is not Product Identity.
- **osr-web transmits Open Game Content it does not contain, and that is what makes it an OGL licensee.** The source tree replicates no SRD prose. However, `server/app.py` sends `spell.intro` to the browser in its `learnable` payload, and that is SRD-derived text that osrlib's own packaged license designates as Open Game Content (OGC). OGL §1(c) counts "publicly display, transmit" as distribution. Anyone hosting a public instance is a distributor. The README says so. Don't "simplify" `LICENSE-OGL.md` away on the theory that the repo contains no SRD text.
- **`LICENSE-OGL.md` follows osrlib's *packaged* copy (`src/osrlib/data/LICENSE-OGL.md`), not its root one.** The root copy designates `srd/`, a directory that does not exist here. It also omits the "osrlib System Reference Document data compilation © 2026 Marsh Macy" Section 15 entry, which is the entry covering the osrlib compilation osr-web transmits. `LICENSE-OGL.md`'s header is osr-web's own OGL §8 designation (which portions are Open Game Content and which are not). Shipping the license text alone does not satisfy §8.
- **osr-web contributes no original Open Game Content**, so Section 15 gets no osr-web entry. Adding one would designate *The Cold Vein*'s game-mechanical elements as OGC and open them to commercial reuse under an irrevocable license, contradicting the CC BY-NC choice. If that ever changes, the designation header and the Section 15 entry must change together.
- **New adventure content stays original** and references osrlib's OGL SRD catalogs by id only. That is the standing rule in `content/README.md`, and it has a licensing reason behind it as well as an authorial one.
