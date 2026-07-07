from plaud_poller.render import extract_summary_markdown, flatten_transcript, slug_filename, summary_from_transsumm


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
