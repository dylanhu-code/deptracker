from deptracker.gold import summarize_gold_set



def test_label_summary_handles_empty_file(tmp_path) -> None:
    """Summarize a gold-set file that does not yet contain rows."""
    result = summarize_gold_set(tmp_path / "gold_set.jsonl")

    assert result == {
        "total_rows": 0,
        "status_counts": {},
        "source_counts": {},
    }
