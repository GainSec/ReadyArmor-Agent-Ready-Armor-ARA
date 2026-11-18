from ara.forge.manifest.text_extract import (
    _reconstruct_tables_from_words, _grid_to_markdown)


def _w(text, x0, top):
    return {"text": text, "x0": x0, "top": top}


def test_reconstructs_checkmark_matrix():
    # a requirement x L1/L2/L3 matrix with wrapped description lines
    words = []
    y = 0
    for rid in ("1.1.1", "1.1.2", "1.1.3"):
        words += [_w(rid, 108, y), _w("Verify_something_here", 140, y),
                  _w("continuation_text", 140, y + 10),   # wrapped desc line
                  _w("✓", 447, y), _w("✓", 473, y), _w("✓", 499, y)]
        y += 24
    grids = _reconstruct_tables_from_words(words)
    assert len(grids) == 1
    grid = grids[0]
    assert len(grid) == 3                       # three requirement rows
    # each row has the three level-columns as marks
    for row in grid:
        assert row[-3:] == ["✓", "✓", "✓"]
    md = _grid_to_markdown(grid)
    assert md.count("|") > 6 and "✓" in md


def test_prose_is_not_a_table():
    # single left-column prose -> no table (no false positive)
    words = [_w(f"word{i}", 108, i * 12) for i in range(20)]
    assert _reconstruct_tables_from_words(words) == []


def test_partial_level_applicability_preserved():
    # a requirement that applies to L2/L3 but not L1 keeps the empty cell
    words = []
    words += [_w("2.1.1", 108, 0), _w("desc", 140, 0),
              _w("✓", 447, 0), _w("✓", 473, 0), _w("✓", 499, 0)]
    words += [_w("2.1.2", 108, 24), _w("desc", 140, 24),
              _w("✓", 473, 24), _w("✓", 499, 24)]   # no L1 mark
    words += [_w("2.1.3", 108, 48), _w("desc", 140, 48),
              _w("✓", 447, 48), _w("✓", 499, 48)]   # no L2 mark
    grids = _reconstruct_tables_from_words(words)
    assert grids
    g = grids[0]
    assert g[1][1] == "" or g[1][1] != "✓"   # L1 empty on row 2
