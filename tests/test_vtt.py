"""The short-transcript rule rests on this function, and a call it wrongly measures as empty
gets no checklist at all. Every case below is a behaviour of the PHP original
(worker_acceptor_light.php:1845-1859), including the two that look like bugs."""
from llmhub import vtt


def test_drops_the_header_line():
    assert vtt.text_by_vtt("WEBVTT\nhello\n") == "hello\n"


def test_drops_line_zero_whatever_it_is():
    # The PHP tests `$line_count != 0`, not whether the line says WEBVTT.
    assert vtt.text_by_vtt("hello\nworld\n") == "world\n"


def test_drops_cue_timing_lines_and_blanks():
    body = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nspoken\n\n"
    assert vtt.text_by_vtt(body) == "spoken\n"


def test_keeps_a_line_whose_marker_is_at_offset_zero():
    """`strpos(...) == false` is true for 0 as well as for false, so this line survives.

    Almost certainly not what anyone intended, but the 48-character threshold was calibrated
    with it in place, and "fixing" it would move the threshold silently.
    """
    assert vtt.text_by_vtt("WEBVTT\n --> 00:00:02.000\n") == " --> 00:00:02.000\n"


def test_counts_carriage_returns_of_a_crlf_file():
    """The split is on "\\n" alone, so every line of a CRLF file keeps its "\\r"."""
    assert vtt.speech_chars("WEBVTT\r\nspoken\r\n") == len("spoken\r\n")


def test_speech_chars_ignores_timings():
    body = ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nabc\n\n"
            "00:00:03.000 --> 00:00:04.000\ndef\n")
    # 8 characters of speech ("abc\ndef\n"), while the vtt itself is far longer - which is the
    # whole point: timings must not make an almost empty call look long enough to analyse.
    assert vtt.speech_chars(body) == 8
    assert len(body) > 60


def test_empty_input_is_zero():
    assert vtt.speech_chars("") == 0
    assert vtt.speech_chars("WEBVTT\n") == 0
