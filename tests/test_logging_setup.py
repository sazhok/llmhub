"""logly 0.2.x formats every message unless told not to - a brace must log, not raise."""
import pytest

import logging_setup  # noqa: F401 - importing it is what switches the shared logger to raw
from logly import logger


@pytest.mark.parametrize("message", [
    "result ({'1': 2})",
    "set {'a', 'b'}",
    "braces {x}",
    "{}",
])
def test_braces_are_text(message):
    logger.info(message)


def test_exception_keeps_raw():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("job {'id': 1} failed")
