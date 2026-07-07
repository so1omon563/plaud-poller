from plaud_poller.poll import (
    find_note_by_plaud_id,
    folder_names_from_filetags,
    move_note,
    resolve_note_path,
    speaker_names_from_segments,
    tags_from_folders,
    unique_destination,
)
from plaud_poller.render import extract_summary_markdown, flatten_outline, flatten_transcript, obsidian_tag, render_obsidian_note, slug_filename, summary_from_transsumm


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


def test_flatten_outline():
    text = flatten_outline([
        {"start_time": 0, "topic": "Kickoff"},
        {"start_time": 61000, "topic": "Next steps"},
    ])
    assert "- **00:00** — Kickoff" in text
    assert "- **01:01** — Next steps" in text


def test_slug_filename():
    assert slug_filename('A/B:C*D?E"F<G>H|I') == "A B C D E F G H I"


def test_obsidian_tag_from_plaud_folder():
    assert obsidian_tag("Work") == "work"
    assert obsidian_tag("Client Meetings") == "client-meetings"
    assert tags_from_folders(["Work", "Client Meetings", "Work"]) == ["work", "client-meetings"]


def test_folder_names_from_filetags():
    row = {"filetag_id_list": ["folder-1"]}
    detail = {"filetag_id_list": ["folder-1", "folder-2"]}
    filetags = {"folder-1": {"name": "Work"}, "folder-2": {"name": "Research"}}
    assert folder_names_from_filetags(row, detail, filetags) == ["Work", "Research"]


def test_speaker_names_from_segments_uses_plaud_labels():
    speakers = speaker_names_from_segments([
        {"speaker": "Jane Doe", "content": "Hello"},
        {"speaker": "Jane Doe", "content": "Again"},
        {"original_speaker": "Sam Example", "content": "Hi"},
    ])
    assert speakers == ["Jane Doe", "Sam Example"]


def test_render_note_keeps_id_out_of_visible_date_fields():
    summary = "> Date: 2026-07-07 07:00:42\n> Participants: [Jed]"
    note = render_obsidian_note(
        plaud_id="abc123",
        title="2026-01-15 Product Review: Search Improvements",
        metadata={"start_time": 1783429242000, "duration": 123, "edit_time": 1783429300000},
        transcript_md="Transcript",
        summary_md=summary,
        outline_md="- **00:00** — Kickoff",
        folder_names=["Work"],
        speakers=["Jane Doe", "Sam Example"],
        tags=["work"],
        include_transcript=False,
        include_outline=False,
    )
    assert note.startswith("---\n")
    assert 'plaud_id: "abc123"' in note
    assert 'title: "2026-01-15 Product Review: Search Improvements"' in note
    assert "plaud_updated_at:" in note
    assert "has_summary: true" in note
    assert "has_transcript: true" in note
    assert "has_outline: true" in note
    assert "tags:\n  - \"work\"" in note
    assert "plaud_folders:\n  - \"Work\"" in note
    assert "speakers:\n  - \"[[Jane Doe]]\"\n  - \"[[Sam Example]]\"" in note
    assert "recorded_date:" not in note
    assert "## Transcript" not in note
    assert "## Outline" not in note
    assert "## Summary" not in note
    assert "# 2026-01-15 Product Review: Search Improvements" not in note
    visible = note.split("---", 2)[2].strip()
    assert visible == summary


def test_render_note_can_include_outline():
    note = render_obsidian_note(
        plaud_id="abc123",
        title="2026-01-15 Product Review: Search Improvements",
        metadata={},
        transcript_md="",
        summary_md="Summary body",
        outline_md="- **00:00** — Kickoff",
        include_transcript=False,
        include_outline=True,
    )
    assert "## Outline" in note
    assert "- **00:00** — Kickoff" in note


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
