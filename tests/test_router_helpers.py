from macbrow.router import _clean_value, _span_candidates, _span_confidence


def test_span_candidates_prefers_suffixes_then_inner_spans():
    spans = _span_candidates("send Constance a message saying hi there.")
    assert spans[0] == "send Constance a message saying hi there"
    assert "hi there" in spans  # suffix
    assert "Constance" in spans  # inner span
    assert len(spans) == len({s.lower() for s in spans})  # de-duplicated


def test_span_candidates_respects_budget():
    long = " ".join(f"w{i}" for i in range(40))
    # every suffix is always offered; inner spans fill the rest of the budget
    assert len(_span_candidates(long, max_candidates=60)) == 60
    assert len(_span_candidates(long, max_candidates=10)) == 40


def test_clean_value_normalises_spoken_urls():
    assert _clean_value(" github dot com ") == "github.com"
    assert _clean_value('"docs dot livekit dot io slash agents"') == "docs.livekit.io/agents"


def test_clean_value_strips_trailing_punctuation():
    assert _clean_value("trailer of love hypothesis.") == "trailer of love hypothesis"
    assert _span_candidates("play the trailer. Done.")[0] == "play the trailer. Done"


def test_span_confidence_counts_wordings_of_the_same_value():
    probs = {
        "a dynamite song": 0.42,
        "dynamite": 0.24,
        "dynamite song": 0.22,
        "play a dynamite song": 0.08,
        "__none__": 0.03,
    }
    assert round(_span_confidence("a dynamite song", probs), 2) == 0.96
    assert _span_confidence("butter", {"butter": 0.3, "dynamite": 0.6}) == 0.3  # a different value is not counted


def test_dictated_text_is_kept_as_said():
    # Found sending a test mail: the body lost its final period ("...Please ignore.").
    from macbrow.registry import ToolRegistry

    reg = ToolRegistry()
    for tool, arg in [
        ("type_here", "text"),
        ("notes_write", "text"),
        ("notes_create", "body"),
        ("show_notification", "message"),
    ]:
        assert next(a for a in reg.get(tool).args if a.name == arg).verbatim, (tool, arg)
    assert not next(a for a in reg.get("chrome_open").args if a.name == "query").verbatim
    assert _clean_value("github dot com.") == "github.com"  # names and URLs are still tidied


def test_dictation_keeps_its_closing_punctuation():
    from macbrow.router import _as_said

    said = "type Hello, this is a test mail. Please ignore."
    assert _as_said("Hello, this is a test mail. Please ignore", said) == "Hello, this is a test mail. Please ignore."
    assert _as_said("are you coming", "type are you coming?") == "are you coming?"
    assert _as_said("hello", "type hello, then press enter") == "hello"  # not at the end: nothing added
    assert _as_said("no punctuation", "type no punctuation") == "no punctuation"
    assert _as_said("not in it", "type something else.") == "not in it"
