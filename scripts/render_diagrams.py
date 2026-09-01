"""Render every mermaid block to a committed image, and link to it.

GitHub renders mermaid client side, and when that renderer fails it shows
"Unable to render rich display" where the architecture diagram should be. It is
not a syntax problem: on the day this was written every diagram on GitHub failed
the same way, including a two-node one and the ones in mermaid's own repository,
while the same sources rendered under mermaid 10.9 and 11 locally.

The headline diagram of a public repository should not depend on a third party's
client-side renderer being healthy. Each block becomes a light and a dark SVG,
linked through `<picture>` so it follows the reader's theme, with the mermaid
source kept underneath in a `<details>` block: still the source of truth, still
diffable, and still rendered by anything that can.

    make diagrams        # after editing any diagram

Needs node, which is why it is a target rather than part of the test suite.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "img"
#: document -> one (slug, alt) per mermaid block, in the order they appear.
#: Named rather than numbered so a committed file says what it shows, and so
#: inserting a diagram does not silently rename the images of the ones after it.
DOCUMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "README.md": (
        ("architecture", "The five security layers, the MCP server and the domain backend"),
    ),
    "README.es.md": (
        ("arquitectura", "Las cinco capas de seguridad, el servidor MCP y el backend de dominio"),
    ),
    "docs/architecture.md": (
        ("layers", "MCP client, MCP server with its five layers, and the domain backend"),
        (
            "domain-model",
            "Entity relationships: clinic, professional, patient, slot, appointment, charge",
        ),
        ("appointment-states", "The appointment state machine and its legal transitions"),
    ),
    "docs/architecture.es.md": (
        ("capas", "Cliente MCP, servidor MCP con sus cinco capas, y el backend de dominio"),
        ("modelo-dominio", "Relaciones: clinica, profesional, paciente, cupo, cita, cargo"),
        ("estados-cita", "La maquina de estados de una cita y sus transiciones legales"),
    ),
}

#: The marker that lets this script find and replace its own output on a rerun.
OPEN = "<!-- diagram:{slug} -->"
CLOSE = "<!-- /diagram:{slug} -->"

BLOCK = re.compile(r"```mermaid\n(?P<body>.*?)```", re.S)
RENDERED = re.compile(r"<!-- diagram:(?P<slug>[a-z0-9_-]+) -->.*?<!-- /diagram:(?P=slug) -->", re.S)


def render(body: str, slug: str) -> None:
    """One SVG per theme, transparent so both sit on the reader's background."""
    source = IMAGES / f"{slug}.mmd"
    source.write_text(body)
    for theme, suffix in (("default", ""), ("dark", "-dark")):
        # A fixed argument list, no shell, and the only variable parts are
        # paths this script just built.
        subprocess.run(  # noqa: S603
            [
                "/usr/bin/env",
                "npx",
                "-y",
                "-p",
                "@mermaid-js/mermaid-cli",
                "mmdc",
                "-i",
                str(source),
                "-o",
                str(IMAGES / f"{slug}{suffix}.svg"),
                "-t",
                theme,
                "-b",
                "transparent",
            ],
            check=True,
            capture_output=True,
        )
        _pin_size(IMAGES / f"{slug}{suffix}.svg")
    source.unlink()


def _pin_size(svg: pathlib.Path) -> None:
    """Give the SVG a real width and height, taken from its own viewBox.

    mermaid emits `width="100%"` and leaves the size to a stylesheet, which is
    right inside a page and wrong inside an `<img>`: with no intrinsic size the
    browser falls back to a placeholder, and the diagram arrived on GitHub at
    138x150 pixels. The viewBox already carries the real dimensions.
    """
    text = svg.read_text()
    box = re.search(r'viewBox="0 0 ([0-9.]+) ([0-9.]+)"', text)
    if box is None:
        return
    width, height = round(float(box.group(1))), round(float(box.group(2)))
    text = text.replace('width="100%"', f'width="{width}" height="{height}"', 1)
    svg.write_text(text)


def figure(slug: str, body: str, alt: str) -> str:
    """A themed image, with the source underneath rather than replaced by it."""
    return (
        f"{OPEN.format(slug=slug)}\n"
        "<picture>\n"
        f'  <source media="(prefers-color-scheme: dark)" srcset="docs/img/{slug}-dark.svg">\n'
        f'  <img alt="{alt}" src="docs/img/{slug}.svg">\n'
        "</picture>\n\n"
        "<details>\n<summary>Diagram source</summary>\n\n"
        f"```mermaid\n{body}```\n\n"
        "</details>\n"
        f"{CLOSE.format(slug=slug)}"
    )


def main() -> int:
    IMAGES.mkdir(parents=True, exist_ok=True)
    for name, named in DOCUMENTS.items():
        path = ROOT / name
        text = path.read_text()
        # A rerun works on the source inside `<details>`, so the images stay in
        # step with the diagram rather than drifting away from it.
        blocks = list(BLOCK.finditer(text))
        if len(blocks) != len(named):
            raise SystemExit(
                f"{name} has {len(blocks)} diagrams and {len(named)} names. "
                "Name the new one in DOCUMENTS so its image says what it shows."
            )
        for index, match in enumerate(reversed(blocks)):
            slug, alt = named[len(blocks) - 1 - index]
            body = match.group("body")
            render(body, slug)
            replacement = figure(slug, body, alt)
            wrapper = RENDERED.search(text)
            start, end = match.start(), match.end()
            if wrapper and wrapper.start() < start < wrapper.end():
                start, end = wrapper.start(), wrapper.end()
            text = text[:start] + replacement + text[end:]
        path.write_text(text)
        print(f"  {name}: {len(blocks)} diagrama(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
