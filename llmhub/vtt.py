"""A byte-faithful port of the PHP's text_by_vtt() (worker_acceptor_light.php:1845-1859).

This is not "strip the timings from a WebVTT file" written afresh. Its output feeds exactly
one decision - whether a call has enough speech in it to be worth asking the model about -
and that decision is calibrated against a 48-character threshold that was tuned on THIS
function's output. Reimplementing it more sensibly would move the threshold silently.

Three behaviours are inherited deliberately, and each is tested:

  1. Line 0 is dropped unconditionally (`$line_count != 0`). In a real WebVTT that is the
     `WEBVTT` header, but the PHP does not check - it drops whatever is first.
  2. A cue-timing line is recognised as one containing " --> ", and dropped - EXCEPT when the
     marker sits at offset 0. `strpos()` returns 0 there and PHP's `==` makes `0 == false`
     true, so the line is kept. That is almost certainly not what anyone intended, but the
     threshold was calibrated with it in place.
  3. The split is on "\\n" alone, so a CRLF file keeps its "\\r" at the end of every line and
     those carriage returns are counted. Stripping them would shorten every measured
     transcript by one character per line.
"""

CUE_MARKER = " --> "


def text_by_vtt(vtt: str) -> str:
    """The speech of a vtt: no header line, no blank lines, no cue timings."""
    out: list[str] = []
    for index, line in enumerate(vtt.split("\n")):
        if index == 0 or line == "":
            continue
        # `strpos($line, " --> ") == false` in PHP: false (absent) or 0 (at the very start).
        position = line.find(CUE_MARKER)
        if position <= 0:
            out.append(line + "\n")
    return "".join(out)


def speech_chars(vtt: str) -> int:
    """What the short-transcript rule measures. The body handed to a worker is the WHOLE vtt
    with its timings (worker_acceptor_light.php:1509-1516); only this count ignores them."""
    return len(text_by_vtt(vtt))
