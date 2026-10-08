"""System One client: one decision request to Jev or to a decider server.

TypeSafe's Jev (over the OpenRouter Decisions API or TypeSafe's own ``/v1/systemone``) and the
open-weight decider models (``decider.serve``, ``POST /v1/systemone``) share one wire format. One
request carries a ``state`` (arbitrary JSON the model reads) and a set of typed ``questions``
about it; the answer is one typed value per question with a probability - no generated text:

- ``noul``   -> ``{"noul": p}``                         probability the statement holds
- ``score``  -> ``{"score": x, "probabilities": ...}``   position on an ordered scale (index 0..n-1)
- ``choice`` -> ``{"choice": key, "probabilities": ...}``

All questions of one request are answered in parallel; the response cost is input tokens only.
The transport is injectable so the selector can be tested without the network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, NamedTuple

#: ``transport(url, body, headers, timeout) -> parsed JSON``.
Transport = Callable[[str, bytes, dict[str, str], float], dict[str, Any]]


class JevError(RuntimeError):
    """The decision request failed (network, HTTP status, or a malformed answer)."""


class Decision(NamedTuple):
    answers: dict[str, Any]
    usage: dict[str, Any]
    #: The model name the server reports having answered with (``""`` when it reports none).
    served_model: str


def _urllib_transport(
    url: str, body: bytes, headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise JevError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JevError(f"request failed: {exc}") from exc
    except ValueError as exc:  # json
        raise JevError(f"malformed response: {exc}") from exc


class JevClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        url: str,
        timeout: float,
        transport: Transport | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.url = url
        self.timeout = timeout
        self._transport = transport or _urllib_transport

    def decide(self, state: dict[str, Any], questions: dict[str, Any]) -> Decision:
        """Answers keyed like ``questions``, usage and the served model. Raises :class:`JevError`."""
        body = json.dumps(
            {"model": self.model, "state": state, "questions": questions}, ensure_ascii=False
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Title": "bce context selector"}
        # A self-hosted decider server takes no key; sending an empty bearer would only confuse
        # a proxy in front of it.
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = self._transport(self.url, body, headers, self.timeout)
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            raise JevError("response carries no answers")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return Decision(answers, usage, str(data.get("model") or ""))
