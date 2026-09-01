"""Every local path a document points at must resolve.

The diagrams were published as committed images so GitHub's mermaid renderer
could not decide whether they show up. The first attempt wrote the path from the
repository root into every document, which is correct only for the ones sitting
at the root: `README.md` looked right and `docs/architecture.md` rendered a
broken-image icon next to the alt text.

That is the same shape as the documented-but-inert environment variables in
`test_env_contract.py`: a reference that reads fine and resolves to nothing.
Nothing failed, because nothing checked.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCUMENTS = sorted([*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md")])

#: `src="..."`, `srcset="..."` and markdown `](...)`, minus anything remote or
#: in-page.
REFERENCE = re.compile(r'(?:src|srcset)="([^"]+)"|\]\(([^)\s]+)\)')


def local_references(document: pathlib.Path) -> list[str]:
    found: list[str] = []
    for match in REFERENCE.finditer(document.read_text()):
        target = match.group(1) or match.group(2)
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        found.append(target.split("#")[0])
    return [t for t in found if t]


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
def test_every_local_reference_resolves(document: pathlib.Path) -> None:
    """Relative to the document, which is the whole point: a path that resolves
    from the repository root is not the same as one that resolves from here."""
    broken = [
        target
        for target in local_references(document)
        if not (document.parent / target).resolve().exists()
    ]
    assert not broken, f"{document.name} points at nothing: {broken}"


def test_every_committed_diagram_is_referenced() -> None:
    """An image nobody links is a file that outlived the diagram it came from."""
    referenced = {
        (document.parent / target).resolve()
        for document in DOCUMENTS
        for target in local_references(document)
    }
    orphans = [
        svg.name
        for svg in sorted((ROOT / "docs" / "img").glob("*.svg"))
        if svg.resolve() not in referenced
    ]
    assert not orphans, f"in docs/img and linked from nowhere: {orphans}"
