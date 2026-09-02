"""A 400-name universe lives in a file, not a yml list."""
from __future__ import annotations
from src.core.config import StrategyConfig

def test_file_names_append_to_the_inline_list_without_duplicates(tmp_path):
    f = tmp_path / "u.txt"; f.write_text("# comment\nAA\nAAL\nKO\n")
    cfg = StrategyConfig(sixfold={"universe": ["KO", "PEP"], "universe_file": str(f)})
    assert cfg.sixfold_universe == ["KO", "PEP", "AA", "AAL"]

def test_the_shipped_sp400_file_is_complete_and_clean():
    from pathlib import Path
    names = [ln.strip() for ln in Path("config/universe_sp400.txt").read_text().splitlines() if ln.strip()]
    assert len(names) == 400 and len(set(names)) == 400
    assert all(n.replace("-", "").isalpha() and n.isupper() for n in names)

def test_an_unreadable_file_fails_loudly_not_silently():
    import pytest
    cfg = StrategyConfig(sixfold={"universe_file": "/nonexistent/x.txt"})
    with pytest.raises(RuntimeError):
        cfg.sixfold_universe

def test_unset_means_the_inline_list_only():
    cfg = StrategyConfig(sixfold={"universe": ["KO"]})
    assert cfg.sixfold_universe == ["KO"]
