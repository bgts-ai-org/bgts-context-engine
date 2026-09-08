"""i18n tests: locale resolution + translation, and the determinism boundary (payload neutral)."""

from bce.core.i18n import Translator, normalize_locale, resolve_locale


def test_normalize_drops_region():
    assert normalize_locale("tr-TR") == "tr"
    assert normalize_locale("en_US") == "en"
    assert normalize_locale(None) is None


def test_resolve_priority_request_over_header():
    loc = resolve_locale(requested="tr", accept_language="en-US,en;q=0.9", supported=("en", "tr"))
    assert loc == "tr"


def test_resolve_falls_back_to_accept_language():
    loc = resolve_locale(requested=None, accept_language="tr-TR,tr;q=0.9,en;q=0.5", supported=("en", "tr"))
    assert loc == "tr"


def test_resolve_falls_back_to_default():
    loc = resolve_locale(requested="de", accept_language="fr-FR", supported=("en", "tr"), default="en")
    assert loc == "en"


def test_translation_differs_by_locale():
    tr = Translator(supported=("en", "tr"), default="en")
    en = tr.translate("tool.references.count", "en", count=3, name="x")
    turkish = tr.translate("tool.references.count", "tr", count=3, name="x")
    assert en != turkish
    assert "3" in en and "3" in turkish


def test_missing_param_degrades_gracefully():
    tr = Translator(supported=("en", "tr"), default="en")
    # No params supplied; should not raise, returns template with placeholders intact.
    out = tr.translate("tool.references.count", "en")
    assert "{count}" in out


def test_unknown_key_returns_key():
    tr = Translator(supported=("en", "tr"), default="en")
    assert tr.translate("does.not.exist", "en") == "does.not.exist"


def test_payload_is_locale_invariant():
    """The deterministic payload must not depend on locale; only the message does (P1)."""
    tr = Translator(supported=("en", "tr"), default="en")

    def tool_output(locale: str) -> dict:
        payload = {"symbol_id": "python::p::::run#abcd", "references": 3}
        message = tr.translate("tool.references.count", locale, count=3, name="run")
        return {"payload": payload, "message": message}

    en = tool_output("en")
    turkish = tool_output("tr")
    assert en["payload"] == turkish["payload"]  # byte-identical payload
    assert en["message"] != turkish["message"]  # only the message is localized
