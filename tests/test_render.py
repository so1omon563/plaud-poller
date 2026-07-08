import os
from types import MethodType

from plaud_poller.api import PlaudAuthError, PlaudClient
from plaud_poller.config import load_settings
from plaud_poller.poll import (
    backup_note_before_overwrite,
    find_note_by_plaud_id,
    folder_names_from_filetags,
    format_counts,
    localize_markdown_images,
    move_note,
    preserve_existing_task_states,
    resolve_note_path,
    result_counts,
    speaker_names_from_segments,
    SyncResult,
    tags_from_folders,
    unique_destination,
)
from plaud_poller.privacy import install_hook
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


def test_localize_markdown_images_downloads_and_rewrites_to_obsidian_relative_path(tmp_path):
    class FakeClient:
        def fetch_presigned_bytes(self, url):
            assert url == "https://plaud.example/presigned-image"
            return b"\x89PNG\r\n\x1a\nimage-bytes"

    summary = "Before\n\n![PLAUD NOTE](permanent/account/mark/20260708_153956_5004367b.png)\n\nAfter"
    rewritten, assets = localize_markdown_images(
        summary,
        client=FakeClient(),
        download_path_mapping={
            "permanent/account/mark/20260708_153956_5004367b.png": "https://plaud.example/presigned-image"
        },
        obsidian_dir=tmp_path / "vault" / "Plaud",
        recording_id="74219dade0f05fbcd5fdd20426e7ca74",
    )

    expected_rel = "_attachments/plaud/74219dade0f05fbcd5fdd20426e7ca74/20260708_153956_5004367b.png"
    assert rewritten == f"Before\n\n![PLAUD NOTE]({expected_rel})\n\nAfter"
    assert assets == [tmp_path / "vault" / "Plaud" / expected_rel]
    assert assets[0].read_bytes() == b"\x89PNG\r\n\x1a\nimage-bytes"


def test_preserve_existing_task_states_keeps_checked_tasks_and_completion_dates():
    existing = """## Next Arrangements
- [x] Jed to start IESDO-2244 and coordinate with Kevin. ✅ 2026-07-08
- [ ] Confirm the release date.
"""
    generated = """## Next Arrangements
- [ ] Jed to start IESDO-2244 and coordinate with Kevin.
- [ ] Confirm the release date.
- [ ] New Plaud task.
"""

    assert preserve_existing_task_states(generated, existing) == """## Next Arrangements
- [x] Jed to start IESDO-2244 and coordinate with Kevin. ✅ 2026-07-08
- [ ] Confirm the release date.
- [ ] New Plaud task.
"""


def test_preserve_existing_task_states_preserves_duplicate_tasks_by_occurrence_order():
    existing = """- [x] Follow up with Kyle. ✅ 2026-07-08
- [ ] Follow up with Kyle.
"""
    generated = """- [ ] Follow up with Kyle.
- [ ] Follow up with Kyle.
"""

    assert preserve_existing_task_states(generated, existing) == """- [x] Follow up with Kyle. ✅ 2026-07-08
- [ ] Follow up with Kyle.
"""


def test_preserve_existing_task_states_ignores_materially_rewritten_tasks():
    existing = "- [x] Follow up with Kyle about DS refresh. ✅ 2026-07-08\n"
    generated = "- [ ] Follow up with Kyle about the release plan.\n"

    assert preserve_existing_task_states(generated, existing) == generated


def test_preserve_existing_task_states_keeps_generated_metadata_when_existing_has_no_metadata():
    existing = "- [ ] Follow up with Kyle.\n"
    generated = "- [ ] Follow up with Kyle. 📅 2026-07-21\n"

    assert preserve_existing_task_states(generated, existing) == generated


def test_preserve_task_state_config_defaults_true_and_can_be_disabled(tmp_path):
    env_keys = [
        "PLAUD_AUTHORIZATION",
        "PLAUD_TOKEN",
        "PLAUD_DATA_DIR",
        "PLAUD_RECORDINGS_DIR",
        "PLAUD_STATE_DB",
        "PLAUD_OBSIDIAN_DIR",
        "PLAUD_PRESERVE_TASK_STATE",
    ]
    old_env = {key: os.environ.get(key) for key in env_keys}
    try:
        for key in env_keys:
            os.environ.pop(key, None)
        (tmp_path / ".env").write_text("PLAUD_TOKEN=test-token\n", encoding="utf-8")
        assert load_settings(tmp_path).preserve_task_state is True

        os.environ.pop("PLAUD_TOKEN", None)
        (tmp_path / ".env").write_text("PLAUD_TOKEN=test-token\nPLAUD_PRESERVE_TASK_STATE=false\n", encoding="utf-8")
        assert load_settings(tmp_path).preserve_task_state is False
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_plaud_status_token_expired_raises_auth_error():
    client = PlaudClient.__new__(PlaudClient)

    def fake_request_text(self, path_or_url, *, method="GET", body=None, headers=None):
        return '{"status": -419, "msg": "workspace token expired"}'

    client._request_text = MethodType(fake_request_text, client)
    try:
        client.request_json("/file/simple/web")
    except PlaudAuthError as exc:
        assert "token expired" in str(exc)
    else:
        raise AssertionError("expired PLAUD status should raise PlaudAuthError")


def test_result_counts_and_format():
    counts = result_counts([
        SyncResult("new"),
        SyncResult("updated"),
        SyncResult("updated"),
        SyncResult("unchanged"),
    ])
    assert counts["new"] == 1
    assert counts["updated"] == 2
    assert counts["unchanged"] == 1
    assert "updated=2" in format_counts(counts)


def test_backup_note_before_overwrite(tmp_path):
    note = tmp_path / "Note.md"
    note.write_text("old", encoding="utf-8")
    backup = backup_note_before_overwrite(note, tmp_path / "_Archive")
    assert backup is not None
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "old"
    assert backup.parent == tmp_path / "_Archive"


def test_install_privacy_hook(tmp_path):
    repo = tmp_path / "repo"
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True)
    hook = install_hook(repo)
    assert hook == hooks / "pre-commit"
    assert hook.exists()
    assert "plaud_poller.privacy" in hook.read_text(encoding="utf-8")
    assert hook.stat().st_mode & 0o111
