from plaud_poller.poll import find_note_by_plaud_id, move_note, resolve_note_path, unique_destination
from plaud_poller.render import extract_summary_markdown, flatten_transcript, render_obsidian_note, slug_filename, summary_from_transsumm


def test_extract_summary_markdown_shapes():
    assert extract_summary_markdown("hello") == "hello"
    assert extract_summary_markdown('{"markdown":"# Hi"}') == "# Hi"
    assert extract_summary_markdown({"content": {"markdown": "nested"}}) == "nested"
    assert extract_summary_markdown({"ai_content": "ai"}) == "ai"


def test_summary_from_outline_fallback():
    md = summary_from_transsumm({"outline_result": [{"start_time": 61000, "topic": "Intro"}]})
    assert md is not None
    assert "## Topics" in md
    assert "01:01" in md
    assert "Intro" in md


def test_flatten_transcript():
    text = flatten_transcript([
        {"start_time": 0, "speaker": "A", "content": "Hello"},
        {"start_time": 65000, "original_speaker": "B", "content": "World"},
    ])
    assert "[00:00] A: Hello" in text
    assert "[01:05] B: World" in text


def test_slug_filename():
    assert slug_filename('A/B:C*D?E"F<G>H|I') == "A B C D E F G H I"


def test_render_note_keeps_id_out_of_visible_date_fields():
    summary = "> Date: 2026-07-07 07:00:42\n> Participants: [Jed]"
    note = render_obsidian_note(
        plaud_id="abc123",
        title="2026-01-15 Product Review: Search Improvements",
        metadata={"start_time": 1783429242000, "duration": 123},
        transcript_md="Transcript",
        summary_md=summary,
        include_transcript=False,
    )
    assert note.startswith("---\n")
    assert 'plaud_id: "abc123"' in note
    assert "recorded_date:" not in note
    assert "## Transcript" not in note
    assert "## Summary" not in note
    assert "# 2026-01-15 Product Review: Search Improvements" not in note
    visible = note.split("---", 2)[2].strip()
    assert visible == summary


def test_resolve_note_path_hides_plaud_id(tmp_path):
    assert resolve_note_path(tmp_path, "2026-01-15 Product Review: Search Improvements", "abc123") == tmp_path / "2026-01-15 Product Review Search Improvements.md"
    existing = tmp_path / "2026-01-15 Product Review Search Improvements.md"
    existing.write_text('---\nplaud_id: "other"\n---\n', encoding="utf-8")
    assert resolve_note_path(tmp_path, "2026-01-15 Product Review: Search Improvements", "abc123") == tmp_path / "2026-01-15 Product Review Search Improvements (2).md"
    assert resolve_note_path(tmp_path, "2026-01-15 Product Review: Search Improvements", "other") == existing


def test_find_and_move_note_by_plaud_id(tmp_path):
    old = tmp_path / "Old Title.md"
    old.write_text('---\nplaud_id: "abc123"\n---\nbody\n', encoding="utf-8")
    assert find_note_by_plaud_id(tmp_path, "abc123") == old
    moved = move_note(old, tmp_path / "New Title.md")
    assert moved == tmp_path / "New Title.md"
    assert not old.exists()
    assert moved.exists()


def test_unique_destination_adds_suffix(tmp_path):
    first = tmp_path / "Note.md"
    first.write_text("x", encoding="utf-8")
    assert unique_destination(first) == tmp_path / "Note (2).md"
