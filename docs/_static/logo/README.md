# rules_requirements logo

## Concept

This logo was designed for the `Studio-Fug/rules_requirements` project.

The project is fundamentally about **traceability** rather than simply storing or
displaying requirements. Its model connects things such as user needs,
requirements, mitigations, risks, tests, and verification evidence so that the
relationships between them can be audited and gaps can be detected.

The logo deliberately reduces that idea to the smallest useful graph.

## Why it looks like this

The mark contains three graph nodes arranged on three corners of a square:

    1  →  2
    ↑
    3

The directed path is therefore:

    3 → 1 → 2

This communicates a traceable chain of relationships without trying to encode the
entire project data model into a tiny icon.

### Lowercase “r”

The three-node arrangement and its two directed edges also form an abstract
lowercase **r**. This gives the graph a secondary identity tied to
`rules_requirements` without adding typography to the favicon.

### Green checks

Each node contains the same green checkmark. The checks communicate that the
objects in the graph are satisfied / verified, while keeping the graph itself as
the primary visual metaphor.

The checks use identical geometry and are centered consistently inside each node.

### Arrows

The two arrows represent explicit directed relationships between graph objects.
Their geometry, weight, arrowhead dimensions, and overlap with destination nodes
are intentionally consistent. The vertical relationship is visually the same
construction as the horizontal relationship, rotated 90 degrees.

### Favicon-first geometry

The design was intentionally kept extremely simple:

- three circles
- two directed edges
- three checkmarks
- black, white, and one green accent
- no text
- no gradients or fine detail

The nearly square arrangement gives the mark good utilization of a square favicon
canvas and allows its topology to remain legible at small sizes.

## Design evolution

Early concepts explored documents, checklists, larger graphs, closed loops,
layered structures, and an `RR` monogram. Those were rejected because they either
looked like generic requirements/compliance software or were too complicated for
favicon scale.

The design was progressively reduced to a single graph edge between verified
nodes, then expanded to three nodes to improve the square silhouette. Reorienting
the graph into the final `3 → 1 → 2` topology produced the lowercase-r shape and
gave the icon a project-specific identity without sacrificing simplicity.

## Source

`rules_requirements_logo.svg` is the canonical vector artwork. It is hand-authored
SVG geometry rather than an image trace.

The same geometry appears in:

- `rules_requirements_logo-dark.svg`: ink and paper swapped, for dark backgrounds.
- `python/rules_requirements/server/static/favicon.svg`: the favicon of the web
  editor and of this documentation. Its colours are classes in an embedded
  stylesheet, so it switches to the dark variant under
  `prefers-color-scheme: dark`. A unit test checks that it draws exactly the
  canonical shapes. Its view box is cropped to the art (`20 20 216 216`), so
  it is larger in a browser tab; the shapes are unchanged.
- The web editor's top bar shows `favicon.svg` itself, so it follows the colour
  scheme the same way.

The favicon follows the operating system's colour scheme, not the colour of the
browser's tab strip: with a dark tab strip on a light scheme (or the reverse)
its edges lose contrast, as the artwork itself would.
