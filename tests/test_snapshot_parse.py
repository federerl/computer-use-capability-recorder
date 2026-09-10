"""Snapshot parsing, without a browser.

Fixtures are the shapes a real ARIA snapshot produces: bare scalars, single-key
mappings, metadata keys, free text, and nested containment.
"""

from __future__ import annotations

import pytest
import yaml

from src.surface.base import Observation
from src.surface.snapshot import parse_frame, render

STOPPAY = """
- link "Profile":
  - /url: /members/100482/detail
- heading "Place Stop Payment" [level=3]
- paragraph: BARNES, ROSALIND | Checking ****4821
- table:
  - rowgroup:
    - row "Check Number":
      - cell "Check Number"
      - cell:
        - textbox
    - row "Reason -- select --":
      - cell "Reason"
      - cell "-- select --":
        - combobox:
          - option "Lost check"
    - row "Submit Stop Payment":
      - cell
      - cell "Submit Stop Payment":
        - button "Submit Stop Payment"
"""

RESULTS = """
- table:
  - row "100482 BARNES, R. Checking ****4821 View":
    - cell "100482"
    - cell "View":
      - link "View"
  - row "100482 BARNES, R. Savings ****9930 View":
    - cell "100482"
    - cell "View":
      - link "View"
"""


def nodes(src: str, frame: str = "detail"):
    return parse_frame(yaml.safe_load(src), frame)


def test_roles_and_names_parsed():
    got = {(n.role, n.name) for n in nodes(STOPPAY)}
    assert ("heading", "Place Stop Payment") in got
    assert ("button", "Submit Stop Payment") in got
    assert ("textbox", "") in got, "the unnamed control must still be captured"


def test_metadata_keys_are_not_nodes():
    assert not any(n.role.startswith("/") for n in nodes(STOPPAY))


def test_layout_wrappers_dropped_without_losing_containment():
    ns = nodes(STOPPAY)
    assert not any(n.role == "rowgroup" for n in ns)
    textbox = next(n for n in ns if n.role == "textbox")
    assert ("row", "Check Number") in textbox.ancestors, \
        "dropping the rowgroup must not sever the row relationship"


def test_unnamed_control_finds_its_scope():
    textbox = next(n for n in nodes(STOPPAY) if n.role == "textbox")
    assert textbox.name == ""
    assert textbox.nearest_scope() == ("row", "Check Number")


def test_scope_is_the_innermost_named_ancestor():
    combobox = next(n for n in nodes(STOPPAY) if n.role == "combobox")
    # The cell is named too, but a cell is not a scoping role; the row is.
    assert combobox.nearest_scope() == ("row", "Reason -- select --")


def test_refs_only_on_addressable_nodes():
    ns = nodes(STOPPAY)
    assert all(n.ref for n in ns if n.role in ("textbox", "button", "combobox", "link"))
    assert all(not n.ref for n in ns if n.role in ("heading", "table", "row"))


def test_refs_are_unique_across_frames():
    a = parse_frame(yaml.safe_load(RESULTS), "main", start_ref=0)
    highest = max(int(n.ref[1:]) for n in a if n.ref)
    b = parse_frame(yaml.safe_load(STOPPAY), "detail", start_ref=highest)
    refs = [n.ref for n in (*a, *b) if n.ref]
    assert len(refs) == len(set(refs))


def test_identical_links_differ_only_by_row():
    ns = nodes(RESULTS, frame="main")
    links = [n for n in ns if n.role == "link"]
    assert len(links) == 2
    assert {n.name for n in links} == {"View"}
    scopes = [n.nearest_scope() for n in links]
    assert scopes[0] != scopes[1], "row scope is the only thing that separates them"


def test_free_text_captured_but_not_targetable():
    ns = parse_frame(yaml.safe_load("- text: Session expires in 2 minutes"), "main")
    text = next(n for n in ns if n.role == "text")
    assert "Session expires" in text.text
    assert not text.ref and not text.actionable


def test_render_is_compact_and_shows_refs():
    obs = Observation(nodes=nodes(STOPPAY), frames=["detail"])
    out = render(obs)
    assert out.startswith("frame:detail")
    assert "[e" in out
    assert "Submit Stop Payment" in out
    # Structure is kept, but cheaply.
    assert len(out.splitlines()) < 25


@pytest.mark.parametrize("raw,expect", [
    ('- button "Save [draft]"', ("button", "Save [draft]")),
    ("- separator", ("separator", "")),
    ('- heading "Totals" [level=2]', ("heading", "Totals")),
])
def test_key_shapes(raw, expect):
    n = parse_frame(yaml.safe_load(raw), "main")[0]
    assert (n.role, n.name) == expect
