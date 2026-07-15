import json
import os
import time
from types import MethodType
from urllib.parse import urlparse

from plaud_poller import auth
from plaud_poller.api import PlaudApiError, PlaudAuthError, PlaudClient
from plaud_poller.auth import BrowserWorkspaceSession, _scan_file_for_workspace_sessions, build_login_url
from plaud_poller.config import load_settings
from plaud_poller.poll import (
    append_sync_changelog,
    backup_note_before_overwrite,
    find_note_by_plaud_id,
    folder_names_from_filetags,
    format_counts,
    localize_markdown_images,
    move_note,
    note_change_fields,
    preserve_existing_image_link_styles,
    preserve_existing_task_states,
    process_recording,
    refresh_after_auth_error,
    should_silence_auth_cooldown,
    resolve_note_path,
    result_counts,
    speaker_names_from_segments,
    SyncResult,
    tags_from_folders,
    unique_destination,
)
from plaud_poller.privacy import install_hook
from plaud_poller.render import extract_summary_markdown, flatten_outline, flatten_transcript, obsidian_tag, render_obsidian_note, slug_filename, summary_from_transsumm
from plaud_poller.state import State


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


def test_note_change_fields_describes_semantic_frontmatter_and_summary_changes():
    previous = """---
title: Old title
duration_ms: 100
tags:
  - \"work\"
plaud_folders:
  - \"Work\"
has_summary: true
---
Old summary
"""
    current = """---
title: New title
duration_ms: 200
tags:
  - \"work\"
  - \"urgent\"
plaud_folders:
  - \"Work\"
  - \"Planning\"
has_summary: true
---
New summary
"""
    assert note_change_fields(
        previous,
        current,
        summary_changed=True,
        transcript_changed=False,
        include_outline=False,
    ) == ["title", "duration", "tags", "folders", "summary"]


def test_append_sync_changelog_records_compact_visible_update(tmp_path):
    append_sync_changelog(
        tmp_path,
        plaud_id="abc123def",
        note_path=tmp_path / "Meeting.md",
        action="updated",
        fields=["summary", "folders"],
    )
    lines = (tmp_path / "sync-changelog.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["plaud_id"] == "abc123def"
    assert event["note"] == "Meeting.md"
    assert event["action"] == "updated"
    assert event["fields"] == ["summary", "folders"]
    assert event["at"].endswith("+00:00")


def test_process_recording_logs_visible_duration_change(tmp_path):
    class FakeClient:
        duration = 100

        def file_detail(self, _recording_id):
            return {"file_name": "Meeting", "duration": self.duration}

    client = FakeClient()
    state = State(tmp_path / "state.sqlite")
    row = {"file_id": "abc123", "file_name": "Meeting", "duration": 100}
    kwargs = {
        "client": client,
        "state": state,
        "row": row,
        "data_dir": tmp_path / "data",
        "obsidian_dir": tmp_path / "vault",
        "download_audio": False,
        "include_transcript": False,
        "include_outline": False,
        "filetags": {},
        "backup_on_change": False,
        "backup_dir": tmp_path / "backups",
        "preserve_task_state": True,
        "dry_run": False,
    }
    try:
        assert process_recording(**kwargs).message == "new Plaud note: abc123 (new note)"
        client.duration = 200
        assert process_recording(**kwargs).message == "updated Plaud note: abc123 (duration)"
    finally:
        state.close()
    events = [json.loads(line) for line in (tmp_path / "data" / "sync-changelog.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(event["action"], event["fields"]) for event in events] == [
        ("new", ["new note"]),
        ("updated", ["duration"]),
    ]


def test_process_recording_restores_blank_note_from_cached_content_after_api_failure(tmp_path):
    rid = "abc123"

    class FakeClient:
        def file_detail(self, _recording_id):
            return {"file_name": "Meeting", "duration": 100, "is_trans": True, "is_summary": True, "content_list": []}

        def transcript_and_summary(self, _recording_id):
            raise PlaudApiError("temporary PLAUD content error")

    data_dir = tmp_path / "data"
    rec_dir = data_dir / "recordings" / rid
    rec_dir.mkdir(parents=True)
    (rec_dir / "summary.md").write_text("## Summary\nCached summary", encoding="utf-8")
    (rec_dir / "transcript.json").write_text(json.dumps([{ "speaker": "Alice", "text": "Cached transcript" }]), encoding="utf-8")
    (rec_dir / "outline.json").write_text("[]", encoding="utf-8")
    note_path = tmp_path / "vault" / "Meeting.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(f"---\nplaud_id: \"{rid}\"\nhas_summary: false\nhas_transcript: false\nhas_outline: false\n---\n", encoding="utf-8")
    state = State(tmp_path / "state.sqlite")
    state.upsert_seen(rid, title="Meeting", start_time=None, duration=100, metadata_hash="old", transcript_hash=None, summary_hash=None, note_hash="old", audio_downloaded=False, changed=False)
    try:
        result = process_recording(
            client=FakeClient(), state=state, row={"file_id": rid, "file_name": "Meeting", "is_trans": True, "is_summary": True},
            data_dir=data_dir, obsidian_dir=tmp_path / "vault", download_audio=False, include_transcript=False,
            include_outline=False, filetags={}, backup_on_change=False, backup_dir=tmp_path / "backups",
            preserve_task_state=True, dry_run=False,
        )
    finally:
        state.close()
    assert result.message == "restored Plaud note: abc123 (retained content after PLAUD API failure)"
    restored = note_path.read_text(encoding="utf-8")
    assert "has_summary: true" in restored
    assert "Cached summary" in restored


def test_process_recording_preserves_nonblank_note_when_plaud_content_api_fails(tmp_path):
    rid = "abc123"

    class FakeClient:
        def file_detail(self, _recording_id):
            return {"file_name": "Meeting", "duration": 100, "is_trans": True, "is_summary": True, "content_list": []}

        def transcript_and_summary(self, _recording_id):
            raise PlaudApiError("temporary PLAUD content error")

    note_path = tmp_path / "vault" / "Meeting.md"
    note_path.parent.mkdir(parents=True)
    original = f"---\nplaud_id: \"{rid}\"\nhas_summary: true\nhas_transcript: true\nhas_outline: false\n---\n## Summary\nExisting content\n"
    note_path.write_text(original, encoding="utf-8")
    state = State(tmp_path / "state.sqlite")
    state.upsert_seen(rid, title="Meeting", start_time=None, duration=100, metadata_hash="old", transcript_hash="old", summary_hash="old", note_hash="old", audio_downloaded=False, changed=False)
    try:
        result = process_recording(
            client=FakeClient(), state=state, row={"file_id": rid, "file_name": "Meeting", "is_trans": True, "is_summary": True},
            data_dir=tmp_path / "data", obsidian_dir=tmp_path / "vault", download_audio=False, include_transcript=False,
            include_outline=False, filetags={}, backup_on_change=False, backup_dir=tmp_path / "backups",
            preserve_task_state=True, dry_run=False,
        )
    finally:
        state.close()
    assert result.status == "kept"
    assert note_path.read_text(encoding="utf-8") == original


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


def test_preserve_existing_image_link_styles_keeps_obsidian_wiki_embeds():
    generated = "Before\n![PLAUD NOTE](_attachments/plaud/rec/20260708_153956_5004367b.png)\nAfter"
    existing = "Before\n![[20260708_153956_5004367b.png|PLAUD NOTE]]\nAfter"

    assert preserve_existing_image_link_styles(generated, existing) == existing


def test_preserve_existing_image_link_styles_leaves_new_images_alone():
    generated = "![PLAUD NOTE](_attachments/plaud/rec/new.png)\n"
    existing = "![[old.png|PLAUD NOTE]]\n"

    assert preserve_existing_image_link_styles(generated, existing) == generated


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


def test_build_login_url_encodes_return_url():
    assert build_login_url() == "https://web.plaud.ai/login?from_url=https%3A%2F%2Fweb.plaud.ai%2F"
    assert build_login_url("https://web.plaud.ai/some/path?q=1") == (
        "https://web.plaud.ai/login?from_url=https%3A%2F%2Fweb.plaud.ai%2Fsome%2Fpath%3Fq%3D1"
    )


def test_browser_workspace_session_scanner_reads_refresh_token(tmp_path):
    text = (
        'x pld_user:workspaceList\\v\\x01'
        '[{"workspaceId":"ws_123","domain":"https://api.plaud.ai","region":"aws:us-west-2",'
        '"workspaceToken":"old-token","expiresAt":1783533595505,'
        '"refreshToken":"refresh-token","refreshExpiresAt":1786039195505}]'
    )
    path = tmp_path / "000001.log"
    path.write_text(text, encoding="utf-8")

    sessions = _scan_file_for_workspace_sessions(path, "Chrome", "Default")

    assert sessions == [
        BrowserWorkspaceSession(
            browser="Chrome",
            profile="Default",
            workspace_id="ws_123",
            domain="https://api.plaud.ai",
            region="aws:us-west-2",
            workspace_token="old-token",
            expires_at_ms=1783533595505,
            refresh_token="refresh-token",
            refresh_expires_at_ms=1786039195505,
        )
    ]


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


def test_transcript_and_summary_accepts_plaud_status_one_success_only_for_that_endpoint():
    client = PlaudClient.__new__(PlaudClient)
    calls = []

    def fake_request_text(self, path_or_url, *, method="GET", body=None, headers=None):
        calls.append((path_or_url, method, body))
        return '{"status": 1, "msg": "success", "data_result": [{"speaker": "Example"}]}'

    client._request_text = MethodType(fake_request_text, client)
    try:
        client.request_json("/file/detail/example")
    except PlaudApiError as exc:
        assert "status 1" in str(exc)
    else:
        raise AssertionError("status 1 must remain an error for ordinary API endpoints")

    payload = client.transcript_and_summary("example")
    assert payload["status"] == 1
    assert payload["data_result"] == [{"speaker": "Example"}]
    assert calls[-1] == ("/ai/transsumm/example", "POST", b"{}")


def test_refresh_after_auth_error_respects_auto_refresh_flag(tmp_path):
    old = os.environ.get("PLAUD_AUTO_REFRESH_TOKEN")
    try:
        os.environ.pop("PLAUD_AUTO_REFRESH_TOKEN", None)
        refreshed, message = refresh_after_auth_error(tmp_path, tmp_path / "data")
        assert refreshed is False
        assert "disabled" in message
    finally:
        if old is None:
            os.environ.pop("PLAUD_AUTO_REFRESH_TOKEN", None)
        else:
            os.environ["PLAUD_AUTO_REFRESH_TOKEN"] = old


def test_refresh_after_auth_error_uses_bounded_browser_login_when_enabled(tmp_path):
    keys = [
        "PLAUD_AUTO_REFRESH_TOKEN",
        "PLAUD_AUTO_BROWSER_LOGIN",
        "PLAUD_AUTO_BROWSER_LOGIN_COOLDOWN_SECONDS",
    ]
    old_env = {key: os.environ.get(key) for key in keys}
    old_refresh = auth.refresh_env_token
    old_browser_login = auth.browser_login_refresh
    calls: list[dict] = []
    try:
        os.environ.update(
            {
                "PLAUD_AUTO_REFRESH_TOKEN": "true",
                "PLAUD_AUTO_BROWSER_LOGIN": "true",
                "PLAUD_AUTO_BROWSER_LOGIN_COOLDOWN_SECONDS": "21600",
            }
        )
        auth.refresh_env_token = lambda *_args, **_kwargs: (False, "no usable storage token")

        def fake_browser_login(_env_path, **kwargs):
            calls.append(kwargs)
            return True, "fresh PLAUD workspace token"

        auth.browser_login_refresh = fake_browser_login
        data_dir = tmp_path / "data"
        refreshed, message = refresh_after_auth_error(tmp_path, data_dir)
        assert refreshed is True
        assert "fresh PLAUD workspace token" in message
        assert calls == [{"timeout_seconds": 90, "interval_seconds": 5, "open_browser": True, "login_method": "generic"}]
        assert not (data_dir / ".browser-login-recovery.json").exists()
    finally:
        auth.refresh_env_token = old_refresh
        auth.browser_login_refresh = old_browser_login
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_refresh_after_auth_error_rate_limits_failed_browser_login(tmp_path):
    keys = ["PLAUD_AUTO_REFRESH_TOKEN", "PLAUD_AUTO_BROWSER_LOGIN", "PLAUD_AUTO_BROWSER_LOGIN_COOLDOWN_SECONDS"]
    old_env = {key: os.environ.get(key) for key in keys}
    old_refresh = auth.refresh_env_token
    old_browser_login = auth.browser_login_refresh
    try:
        os.environ.update(
            {
                "PLAUD_AUTO_REFRESH_TOKEN": "true",
                "PLAUD_AUTO_BROWSER_LOGIN": "true",
                "PLAUD_AUTO_BROWSER_LOGIN_COOLDOWN_SECONDS": "21600",
            }
        )
        auth.refresh_env_token = lambda *_args, **_kwargs: (False, "no usable storage token")
        auth.browser_login_refresh = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not open browser during cooldown"))
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / ".browser-login-recovery.json").write_text(json.dumps({"last_attempt_at": time.time()}), encoding="utf-8")
        refreshed, message = refresh_after_auth_error(tmp_path, data_dir)
        assert refreshed is False
        assert "cooldown active" in message
    finally:
        auth.refresh_env_token = old_refresh
        auth.browser_login_refresh = old_browser_login
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_scheduled_auth_cooldown_is_silent_but_manual_is_not():
    old = os.environ.get("PLAUD_SILENCE_AUTH_COOLDOWN")
    message = "no usable storage token; browser-login cooldown active (retry in 10s)"
    try:
        os.environ.pop("PLAUD_SILENCE_AUTH_COOLDOWN", None)
        assert should_silence_auth_cooldown(message) is False
        os.environ["PLAUD_SILENCE_AUTH_COOLDOWN"] = "true"
        assert should_silence_auth_cooldown(message) is True
    finally:
        if old is None:
            os.environ.pop("PLAUD_SILENCE_AUTH_COOLDOWN", None)
        else:
            os.environ["PLAUD_SILENCE_AUTH_COOLDOWN"] = old


def test_validate_token_does_not_recurse_on_region_redirect_cycle():
    calls: list[str] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status": -302, "data": {"domains": {"api": "https://api.plaud.ai"}}}'

    original = auth.urlopen
    try:
        def fake_urlopen(request, **_kwargs):
            calls.append(urlparse(request.full_url).hostname or "")
            return FakeResponse()

        auth.urlopen = fake_urlopen
        ok, region, error = auth.validate_token("synthetic-token", "aws:eu-central-1")
    finally:
        auth.urlopen = original

    assert ok is False
    assert region is None
    assert "redirected" in (error or "")
    assert calls == ["api-euc1.plaud.ai", "api.plaud.ai", "api-apse1.plaud.ai"]


def test_workspace_refresh_stops_redirect_cycle():
    calls: list[str] = []
    session = BrowserWorkspaceSession(
        browser="test",
        profile="Default",
        workspace_id="workspace",
        domain="https://api-euc1.plaud.ai",
        region="aws:eu-central-1",
        workspace_token=None,
        expires_at_ms=None,
        refresh_token="synthetic-token",
        refresh_expires_at_ms=None,
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status": -302, "data": {"domains": {"api": "https://api.plaud.ai"}}}'

    original = auth.urlopen
    try:
        def fake_urlopen(request, **_kwargs):
            calls.append(urlparse(request.full_url).hostname or "")
            return FakeResponse()

        auth.urlopen = fake_urlopen
        ok, token, region, error = auth.refresh_workspace_session(session)
    finally:
        auth.urlopen = original

    assert ok is False
    assert token is None
    assert region == "aws:us-west-2"
    assert error == "workspace refresh redirect loop detected"
    assert calls == ["api-euc1.plaud.ai", "api.plaud.ai"]


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
