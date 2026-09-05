"""Parameter decoding for the PHP-compatible shim.

`get_option()` (worker_acceptor_light.php:2074-2082) is three lines and they matter:

    if(isset($_POST[$opt])){ return $_POST[$opt]; }
    if(isset($_GET[$opt])){ return $_GET[$opt]; }
    return $default;

POST wins over GET **per parameter**, not per request - a caller can pass `task` in the query
string and `content` in the body and both are read. That applies to `task` and `op` too, which
is why the dispatch decision itself has to go through this and not through FastAPI's usual
typed parameters.

`$_POST` is populated only for form-encoded and multipart bodies, so a JSON body reaches
nothing today. We accept JSON as well: refusing it would be bug-compatibility for its own
sake, and no current client sends it, so nothing observes the difference.
"""
import json
from typing import Any, Optional

from starlette.datastructures import FormData


class Params:
    def __init__(self, form: FormData | dict, query: Any):
        self._form = form
        self._query = query

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        value = self._form.get(name) if self._form is not None else None
        if value is not None:
            return value if isinstance(value, str) else str(value)
        value = self._query.get(name) if self._query is not None else None
        if value is not None:
            return value
        return default

    def keys(self) -> set[str]:
        keys = set(self._query.keys()) if self._query is not None else set()
        if self._form is not None:
            keys |= set(self._form.keys())
        return keys


async def read_params(request) -> Params:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    form: FormData | dict | None = None
    if content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
        form = await request.form()
    elif content_type == "application/json":
        try:
            body = json.loads(await request.body() or b"{}")
            form = body if isinstance(body, dict) else {}
        except ValueError:
            form = {}
    return Params(form, request.query_params)
