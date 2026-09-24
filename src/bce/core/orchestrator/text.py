"""Deterministic text helpers shared by anchor finding, expansion and scoring.

Everything here is pure string processing (no model, no database): stop-word filtering for task
text, identifier-likeness heuristics (which tokens may be treated as *explicit* symbol references),
snake/camel splitting for partial name matches, and test-symbol detection from symbol ids / paths.
"""

from __future__ import annotations

import re

#: Unicode-aware so that Turkish task text tokenises as words ("toplantı", not "toplant" + a
#: dropped tail); identifiers are ASCII anyway and unaffected.
_WORD_RE = re.compile(r"[^\W\d]\w*")
_BACKTICK_RE = re.compile(r"`([^`\s]+)`")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_HAS_LOWER_UPPER_RE = re.compile(r"[a-z][A-Z]")
_DOTTED_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[.:]{1,2}[A-Za-z_][A-Za-z0-9_]*)+")

#: Minimum token length considered for lexical matching / task signals.
MIN_TOKEN_LEN = 3

#: Function words + generic task-description filler (English and Turkish). These never become
#: anchors or task signals on their own: they match thousands of docstrings and carry no code
#: meaning. Domain words ("error", "token", "period") are deliberately *not* listed - they are
#: legitimate lexical hints and are only down-weighted by term saturation in anchor finding.
_STOPWORDS_EN = """
the and not for with when where which that this these those from into onto over under than
then there their they them what while will would should could shall must can may might does
did done doing has have had having are was were been being its our your his her who whom how
why also any all some such each every both either neither only own same other another more
most less least very too just even still yet again ever never always often here now out off
via per but nor because although though after before during without within about above below
between against through instead rather like unlike since until upon along across around
behind beside besides except toward towards whether however otherwise already please thanks
thank let lets get gets got getting set sets put use uses used using make makes made making
need needs needed want wants wanted try tries tried seem seems one two three first second
last new old way ways thing things something anything nothing everything someone anyone
everyone etc
fix fixes fixed fixing bug bugs issue issues problem problems wrong correct correctly
incorrect incorrectly properly proper missing add adds added adding remove removes removed
removing change changes changed changing update updates updated updating implement implements
implemented implementing support supports supported ensure ensures sure check checks checked
checking handle handles handled handling improve improves improved refactor refactoring
cleanup clean currently expected actual actually behaviour behavior work works working broken
fail fails failed failing failure return returns returned returning show shows shown display
displays displayed appear appears code function functions method methods class classes file
files call calls called calling given gives give take takes taken many much few several
various different based related relevant possible impossible able unable allow allows allowed
avoid avoids prevent prevents well good bad better best worse worst true false none null nil
yes
"""

_STOPWORDS_TR = """
ve ile için icin bir bu şu su olan olarak gibi daha çok cok ama fakat ancak veya ya da de ki
mi mı mu mü ne nasıl nasil neden niçin nicin hangi her bazı bazi tüm tum bütün butun hiç hic
sonra önce once kadar göre gore değil degil var yok olur olmalı olmali olmuyor oluyor olsun
ise iken üzerinde uzerinde içinde icinde arasında arasinda hata hatası hatasi sorun sorunu
düzelt duzelt düzeltme duzeltme ekle ekleme kaldır kaldir değiştir degistir kontrol eksik
yanlış yanlis doğru dogru çalışmıyor calismiyor çalışıyor calisiyor gerekiyor gerekli lazım
lazim şekilde sekilde kısım kisim yer yeri kod fonksiyon sınıf sinif dosya satır satir değer
deger
"""

STOPWORDS: frozenset[str] = frozenset(_STOPWORDS_EN.split()) | frozenset(_STOPWORDS_TR.split())

_TEST_DIR_PARTS = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "testing", "e2e", "fixtures"}
)
_TEST_FILE_RE = re.compile(
    r"(^|[/\\.])("
    r"test_[^/\\]*"  # test_foo.py
    r"|[^/\\]*_test\.[a-z]+"  # foo_test.go / foo_test.py
    r"|[^/\\]*\.(test|spec)\.[a-z]+"  # foo.test.ts / foo.spec.js
    r"|[^/\\]*(Test|Tests|Spec|Specs)\.(java|cs|kt|scala|php|rb)"  # FooTest.java / FooTests.cs
    r"|conftest\.py"
    r")$",
)
#: ``test_x`` / ``should_x`` are tests wherever they live (pytest, rspec, JUnit-snake); the
#: CamelCase forms are only trusted when no path is known - ``TestBotTriggerPanel`` and
#: ``testConnection`` are production names, and real ``TestFoo`` / ``testFoo`` tests sit in
#: test directories or ``*Test.java`` files that the path rules already catch.
_TEST_NAME_RE = re.compile(r"^(test_|it_|should_|spec_)")
_TEST_NAME_CAMEL_RE = re.compile(r"^(test[A-Z]|Test[A-Z])")


def tokens(text: str) -> list[str]:
    """Identifier-like tokens (original case) in first-seen order, de-duplicated."""
    seen: list[str] = []
    for token in _WORD_RE.findall(text or ""):
        if token not in seen:
            seen.append(token)
    return seen


def _lower(token: str) -> str:
    """``str.lower`` with the Turkish dotted capital folded to plain ``i`` (``İptal`` -> ``iptal``).

    Python lowers ``İ`` to ``i`` + a combining dot, which matches neither the stop-word list nor
    the domain vocabulary; identifiers are ASCII so this cannot change them.
    """
    return token.replace("İ", "i").lower()


def is_stopword(token: str) -> bool:
    return _lower(token) in STOPWORDS


def content_tokens(text: str) -> list[str]:
    """Lower-cased tokens with stop-words and short tokens removed (lexical / task-signal input).

    Compound identifiers are kept whole *and* contribute their snake/camel parts, so a task that
    says ``diff_command`` also matches symbols named ``diff`` (deterministic order preserved).
    """
    out: list[str] = []
    for token in tokens(text):
        lowered = _lower(token)
        if len(lowered) < MIN_TOKEN_LEN or lowered in STOPWORDS:
            continue
        if lowered not in out:
            out.append(lowered)
        for part in split_identifier(token):
            if len(part) >= MIN_TOKEN_LEN and part not in STOPWORDS and part not in out:
                out.append(part)
    return out


#: Turkish domain words -> the English words code identifiers are written in. Task text from a
#: Turkish-speaking team says "toplantı" while the symbol is ``MeetingJoinService``; without this
#: the lexical channel has nothing to match and the task-signal feature (w2) is blind to the word.
#: Generic software / product vocabulary only - no repository-specific names. Keys are the bare
#: stem in both spellings (with and without diacritics); suffix stripping is handled by
#: :func:`_tr_stem`, which tries progressively shorter prefixes of the token.
_DOMAIN_TERMS_TR_EN: dict[str, tuple[str, ...]] = {
    "toplantı": ("meeting",),
    "toplanti": ("meeting",),
    "takvim": ("calendar",),
    "kullanıcı": ("user",),
    "kullanici": ("user",),
    "bildirim": ("notification", "notify"),
    "kayıt": ("record", "recording", "register"),
    "kayit": ("record", "recording", "register"),
    "özet": ("summary",),
    "ozet": ("summary",),
    "katılımcı": ("participant", "attendee"),
    "katilimci": ("participant", "attendee"),
    "katıl": ("join",),
    "katil": ("join",),
    "oturum": ("session",),
    "yetki": ("permission", "authorization"),
    "izin": ("permission",),
    "rol": ("role",),
    "ayar": ("setting", "config"),
    "rapor": ("report",),
    "arama": ("search", "call"),
    "sil": ("delete", "remove"),
    "güncelle": ("update",),
    "guncelle": ("update",),
    "liste": ("list",),
    "tarih": ("date",),
    "saat": ("time", "hour"),
    "zaman": ("time",),
    "süre": ("duration",),
    "sure": ("duration",),
    "dosya": ("file",),
    "eposta": ("email", "mail"),
    "posta": ("email", "mail"),
    "şifre": ("password",),
    "sifre": ("password",),
    "parola": ("password",),
    "giriş": ("login", "sign", "entry"),
    "giris": ("login", "sign", "entry"),
    "çıkış": ("logout", "exit"),
    "cikis": ("logout", "exit"),
    "hesap": ("account",),
    "abonelik": ("subscription",),
    "ödeme": ("payment",),
    "odeme": ("payment",),
    "fatura": ("invoice", "billing"),
    "mesaj": ("message",),
    "sohbet": ("chat",),
    "kanal": ("channel",),
    "ekip": ("team",),
    "takım": ("team",),
    "takim": ("team",),
    "etiket": ("tag", "label"),
    "durum": ("status", "state"),
    "kuyruk": ("queue",),
    "bağlantı": ("connection", "link"),
    "baglanti": ("connection", "link"),
    "yükle": ("upload", "load"),
    "yukle": ("upload", "load"),
    "indir": ("download",),
    "dil": ("language", "locale"),
    "çeviri": ("translation",),
    "ceviri": ("translation",),
    "ses": ("audio", "voice"),
    "görüntü": ("image", "video"),
    "goruntu": ("image", "video"),
    "ekran": ("screen",),
    "buton": ("button",),
    "düğme": ("button",),
    "dugme": ("button",),
    "menü": ("menu",),
    "menu": ("menu",),
    "sayfa": ("page",),
    "tablo": ("table",),
    "sütun": ("column",),
    "sutun": ("column",),
    "filtre": ("filter",),
    "sırala": ("sort", "order"),
    "sirala": ("sort", "order"),
    "önbellek": ("cache",),
    "onbellek": ("cache",),
    "veritabanı": ("database",),
    "veritabani": ("database",),
    "sunucu": ("server",),
    "istek": ("request",),
    "yanıt": ("response",),
    "yanit": ("response",),
    "uyarı": ("warning",),
    "uyari": ("warning",),
    "günlük": ("log", "daily"),
    "gunluk": ("log", "daily"),
    "yönetici": ("admin",),
    "yonetici": ("admin",),
    "panel": ("dashboard", "panel"),
    "tekrar": ("recurring", "retry", "repeat"),
    "tekrarlayan": ("recurring",),
    "seri": ("series",),
    "davet": ("invite", "invitation"),
    "ayrıl": ("leave",),
    "ayril": ("leave",),
    "başlat": ("start",),
    "baslat": ("start",),
    "durdur": ("stop",),
    "bitir": ("end", "finish"),
    "iptal": ("cancel",),
    "planla": ("schedule",),
    "zamanla": ("schedule",),
    "hatırlat": ("remind", "reminder"),
    "hatirlat": ("remind", "reminder"),
    "transkript": ("transcript",),
    "deşifre": ("transcript",),
    "desifre": ("transcript",),
    "konuşmacı": ("speaker",),
    "konusmaci": ("speaker",),
    "sahip": ("owner",),
    "organizatör": ("organizer",),
    "organizator": ("organizer",),
    "sağlayıcı": ("provider",),
    "saglayici": ("provider",),
    "servis": ("service",),
    "hizmet": ("service",),
    "kimlik": ("identity", "auth", "id"),
    "doğrulama": ("validation", "verification", "auth"),
    "dogrulama": ("validation", "verification", "auth"),
    "yenile": ("refresh", "renew"),
    "senkron": ("sync",),
    "eşitle": ("sync",),
    "esitle": ("sync",),
    "aktar": ("transfer", "export", "import"),
    "dışa": ("export",),
    "disa": ("export",),
    "içe": ("import",),
    "ice": ("import",),
    "sürüm": ("version",),
    "surum": ("version",),
    "şablon": ("template",),
    "sablon": ("template",),
    "gönder": ("send",),
    "gonder": ("send",),
    "al": ("get", "receive"),
    "oluştur": ("create",),
    "olustur": ("create",),
    "üye": ("member",),
    "uye": ("member",),
    "grup": ("group",),
    "sınır": ("limit",),
    "sinir": ("limit",),
    "kota": ("quota",),
    "ücret": ("price", "fee"),
    "ucret": ("price", "fee"),
    "indirim": ("discount",),
    "adres": ("address",),
    "telefon": ("phone",),
    "isim": ("name",),
    "başlık": ("title", "header"),
    "baslik": ("title", "header"),
    "açıklama": ("description",),
    "aciklama": ("description",),
    "içerik": ("content",),
    "icerik": ("content",),
    "ek": ("attachment",),
    "yorum": ("comment",),
    "onay": ("approval", "approve", "confirm"),
    "reddet": ("reject",),
    "beklemede": ("pending",),
    "aktif": ("active",),
    "pasif": ("inactive", "disabled"),
    "gizli": ("hidden", "private"),
    "genel": ("public", "general"),
    "varsayılan": ("default",),
    "varsayilan": ("default",),
    "zorunlu": ("required",),
    "boş": ("empty", "null"),
    "bos": ("empty", "null"),
    "sayı": ("count", "number"),
    "sayi": ("count", "number"),
    "toplam": ("total",),
    "ortalama": ("average",),
    "grafik": ("chart", "graph"),
    "istatistik": ("statistics", "stats"),
    "arşiv": ("archive",),
    "arsiv": ("archive",),
    "geçmiş": ("history",),
    "gecmis": ("history",),
    "gelecek": ("upcoming", "future"),
    "bugün": ("today",),
    "bugun": ("today",),
    "haftalık": ("weekly",),
    "haftalik": ("weekly",),
    "aylık": ("monthly",),
    "aylik": ("monthly",),
    "saatlik": ("hourly",),
}

#: Shortest stem we accept when stripping Turkish suffixes ("toplantılar", "toplantısını").
#: Shorter entries ("al", "ek", "ses", "sil") match exactly only: as prefixes they would be noise.
_TR_MIN_STEM = 4
#: What may follow a stem for the token to count as an inflected form of it: a chain of common
#: Turkish suffix morphemes (plural, possessive, case, passive, negation, tense, buffer letters).
#: This is what keeps English words off the map: "serial" / "series" / "listener" / "indirect"
#: start with "seri" / "liste" / "indir" but their tails are not Turkish suffixes.
_TR_SUFFIX_RE = re.compile(
    r"^(?:l[ae]r|[ıiuü]|s[ıiuü]|n[ıiuü]|[dt][ae]n?|[ae]|[ıiuü]n|m[ıiuü]z|n[ıiuü]z|l[ıiuü]k"
    r"|c[ıiuü]|l[ıiuü]|s[ıiuü]z|m[ae]k?|m[ıiuü]|[ıiuü]?yor|[dt][ıiuü]|m[ıiuü]ş|[ae]c[ae]k"
    r"|[ıiuü]l|[ıiuü]nc[ae]|ken|[ny])+$"
)


def _tr_stem(token: str) -> tuple[str, ...]:
    """English equivalents of a Turkish token, matching the longest known stem it starts with.

    Exact entries match as they are; a longer token matches an entry only when the remainder is
    a plausible Turkish suffix chain (:data:`_TR_SUFFIX_RE`).
    """
    if token in _DOMAIN_TERMS_TR_EN:
        return _DOMAIN_TERMS_TR_EN[token]
    for end in range(len(token) - 1, _TR_MIN_STEM - 1, -1):
        stem = token[:end]
        if stem in _DOMAIN_TERMS_TR_EN and _TR_SUFFIX_RE.match(token[end:]):
            return _DOMAIN_TERMS_TR_EN[stem]
    return ()


def domain_equivalents(token: str) -> list[str]:
    """English identifier words for a (possibly suffixed) Turkish domain word; ``[]`` otherwise."""
    return [w for w in _tr_stem(_lower(token)) if len(w) >= MIN_TOKEN_LEN]


def query_terms(text: str) -> list[str]:
    """:func:`content_tokens` plus the English equivalents of any Turkish domain words.

    This is what the lexical channel and the task-signal feature match against symbol names and
    docstrings; :func:`content_tokens` stays the plain tokenisation for statistics. Order is
    deterministic: each token is followed by its equivalents, duplicates dropped.
    """
    out: list[str] = []
    for token in content_tokens(text):
        if token not in out:
            out.append(token)
        for word in domain_equivalents(token):
            if word not in STOPWORDS and word not in out:
                out.append(word)
    return out


def phrase_terms(text: str) -> list[str]:
    """Adjacent content-word pairs of the task text (``"access token"``), in first-seen order.

    A pair is a far more precise lexical hint than either word: "token" saturates any pool on
    an auth-heavy codebase, "access token" names one concept. Only consecutive tokens of the
    original text form a pair (identifier parts and Turkish equivalents do not), each word must
    be a content token, and a pair whose words are identical is skipped.
    """
    words = [_lower(t) for t in tokens(text)]
    keep = [w for w in words if len(w) >= MIN_TOKEN_LEN and w not in STOPWORDS]
    # Pairs are formed over the *filtered* sequence, so "the access token" still yields
    # "access token" while "token, which" yields nothing.
    out: list[str] = []
    for left, right in zip(keep, keep[1:], strict=False):
        if left == right:
            continue
        pair = f"{left} {right}"
        if pair not in out:
            out.append(pair)
    return out


def split_identifier(name: str) -> list[str]:
    """snake_case / camelCase / PascalCase / dotted -> lower-cased parts (deterministic)."""
    if not name:
        return []
    parts: list[str] = []
    for chunk in re.split(r"[_\W]+", name):
        if not chunk:
            continue
        for piece in _CAMEL_RE.split(chunk):
            if piece:
                parts.append(_lower(piece))
    return parts


def looks_like_identifier(token: str) -> bool:
    """True when a token plausibly names a code symbol rather than an English/Turkish word.

    Accepted: snake_case, camelCase/PascalCase with an internal case change, tokens containing
    digits, and ALL_CAPS constants of length >= 4. Rejected: single lower-case or capitalised
    words ("check", "Period") - those go through the lexical channel instead.
    """
    if not token or len(token) < MIN_TOKEN_LEN:
        return False
    if _lower(token) in STOPWORDS:
        return False
    if "_" in token.strip("_"):
        return True
    if any(ch.isdigit() for ch in token):
        return True
    if _HAS_LOWER_UPPER_RE.search(token):
        return True
    return token.isupper() and len(token) >= 4


def explicit_references(text: str) -> list[tuple[str | None, str]]:
    """``(container, name)`` references in task text that may be resolved as symbol names.

    Backtick-quoted spans are always explicit (the author marked them as code). Dotted/scoped
    references (``Class.method``, ``pkg::name``) keep the qualifying segment alongside the last
    one, so ``MongoDbService.CancelAsync`` can be resolved inside that type instead of matching
    every ``CancelAsync`` in the graph; ``container`` is None for a bare token, which must pass
    :func:`looks_like_identifier`.

    A tail that was written qualified is not *also* offered bare: the author wrote
    ``MongoDbService.CancelAsync``, which is not evidence for the three other ``CancelAsync``
    methods in the repository, and admitting the bare form would hand back exactly the ambiguity
    the qualifier removed.
    """
    out: list[tuple[str | None, str]] = []
    seen: set[tuple[str | None, str]] = set()
    qualified_tails: set[str] = set()

    def add(container: str | None, name: str) -> None:
        if len(name) < MIN_TOKEN_LEN or _lower(name) in STOPWORDS:
            return
        if (container, name) not in seen:
            seen.add((container, name))
            out.append((container, name))

    def add_qualified(raw: str) -> None:
        parts = [p for p in re.split(r"[.:]+", raw.strip("()")) if p]
        if not parts or not _WORD_RE.fullmatch(parts[-1]):
            return
        container = parts[-2] if len(parts) >= 2 else None
        if container:
            qualified_tails.add(parts[-1])
        add(container, parts[-1])

    for quoted in _BACKTICK_RE.findall(text or ""):
        add_qualified(quoted)
    for dotted in _DOTTED_RE.findall(text or ""):
        add_qualified(dotted)
    for token in tokens(text):
        if token not in qualified_tails and looks_like_identifier(token):
            add(None, token)
    return out


_QUOTED_RE = re.compile(r"(?<![A-Za-z0-9_])(['\"])([^'\"\n]{3,80})\1(?![A-Za-z0-9_])")
_IDENT_ONLY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Backtick spans *with* spaces (``\`a?.b || c\```): a code expression the author pasted.
_BACKTICK_SPAN_RE = re.compile(r"`([^`\n]{3,160})`")
#: A whitespace-separated piece of a pasted expression that is code rather than an operator or
#: a placeholder: starts like an identifier, and contains a member access, call or underscore.
_CODE_PIECE_RE = re.compile(r"^[A-Za-z_$][\w$?.\[\]()'\"]*$")
#: Identifier-looking words shorter than this are abbreviations / prose, not a code mention.
_MENTION_MIN_LEN = 4

_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
#: ``src/utils/auth.ts``, ``./flask/app.py``, ``pkg\sub\Thing.cs`` - a slash-joined path ending in
#: a file name with an extension. Bare file names are covered by :data:`_CODE_FILE_RE`.
_PATH_MENTION_RE = re.compile(
    r"(?<![\w/\\.-])((?:[A-Za-z]:)?[/\\]?(?:[\w.-]+[/\\])+[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,5})"
    r"(?![\w/\\])"
)
#: A bare ``Layout.tsx`` / ``app.py`` is a file mention only with a source-code extension (any
#: ``.md`` / ``.json`` / ``.png`` word in prose is not something the graph indexes).
_CODE_FILE_RE = re.compile(
    r"(?<![\w/\\.-])([\w-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|java|kt|kts|scala|cs|fs|rb|rs"
    r"|php|swift|c|cc|cpp|cxx|h|hh|hpp|m|mm|vue|svelte|dart|lua|ex|exs|erl|hs|clj|sql))"
    r"(?![\w/\\])"
)
_PATH_STRIP_PREFIXES = ("./", ".\\", "/", "\\")


def file_mentions(text: str) -> list[str]:
    """Source-file paths a task text names, as repo-relative suffixes (first-seen order).

    ``src/utils/auth.ts``, ``./flask/app.py``, the ``File "/opt/app/flask/app.py"`` of a Python
    traceback and a bare ``Layout.tsx`` all count; URLs are removed first so ``github.com/x/y.js``
    is not a file. Leading ``./`` / ``/`` are dropped and backslashes normalised, so the result
    is matched as a *suffix* of indexed file ids (``repo:src/utils/auth.ts``) - an absolute
    traceback path still finds its file. Output documents (``docs/plan.md``) are kept when they
    carry a directory: whether they are indexed is the repository's call, not the parser's.
    """
    cleaned = _URL_RE.sub(" ", text or "")
    out: list[str] = []
    seen: set[str] = set()
    for match in list(_PATH_MENTION_RE.finditer(cleaned)) + list(_CODE_FILE_RE.finditer(cleaned)):
        path = match.group(1).replace("\\", "/")
        if len(path) > 2 and path[1] == ":" and path[2] == "/":
            path = path[2:]  # Windows drive
        while path.startswith(_PATH_STRIP_PREFIXES):
            path = path[2:] if path.startswith("./") else path[1:]
        path = path.strip("./")
        if not path or path.lower() in seen:
            continue
        seen.add(path.lower())
        out.append(path)
    return out


def mention_terms(text: str) -> list[tuple[str, str]]:
    """Code fragments of a task text that may appear *verbatim inside symbol bodies*.

    Returns ``(kind, text)`` pairs in first-seen order, de-duplicated:

    * ``("literal", "token")`` for a quoted string (``'token'``, ``"cortex.accessToken"``) - the
      body is searched for the string *with* quotes, so a literal is only matched as a literal;
    * ``("code", "error?.response?.data?.message")`` for a backtick span or a dotted / scoped
      reference (``queryClient.invalidateQueries``) - matched as a plain substring;
    * ``("ident", "localStorage")`` for an identifier-looking word (camelCase, snake_case,
      ALL_CAPS, digits) - matched as a whole word.

    This feeds the *usage* anchor source: "every place that reads the raw ``localStorage`` key"
    is answered by the symbols whose text contains ``localStorage``, whatever they are named.
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str) -> None:
        value = value.strip().strip(",;").lstrip(".")  # "...rest" spreads, "…and so on"
        if kind == "code" and _IDENT_ONLY_RE.match(value):
            kind = "ident"  # a backticked bare name is a whole-word match, not a substring
        if not value or (kind, value) in seen:
            return
        if kind == "ident" and (len(value) < _MENTION_MIN_LEN or _lower(value) in STOPWORDS):
            return
        if kind == "code" and len(value) < _MENTION_MIN_LEN + 1:
            return  # "e.g", "i.e": prose abbreviations, not code
        seen.add((kind, value))
        out.append((kind, value))

    for span in _BACKTICK_SPAN_RE.findall(text or ""):
        span = span.strip()
        if " " not in span:
            add("code", span.strip("()"))
            continue
        # A pasted expression: keep the code-looking pieces, drop operators and placeholders.
        for piece in span.split():
            piece = piece.strip("()").strip(",;")
            if _CODE_PIECE_RE.match(piece) and any(ch in piece for ch in "._(["):
                add("code", piece)
    for _, literal in _QUOTED_RE.findall(text or ""):
        add("literal", literal)
    for dotted in _DOTTED_RE.findall(text or ""):
        add("code", dotted)
    for token in tokens(text):
        if looks_like_identifier(token) and _IDENT_ONLY_RE.match(token):
            add("ident", token)
    return out


#: A quoted string in *code* that is a key rather than prose: one token of letters, digits and
#: ``_ . : / -`` (``'monitoring-executions'``, ``"cortex.accessToken"``, ``'/api/runs'``),
#: 4-64 characters, starting with a letter, underscore or slash. Relative import specifiers
#: (``'../utils/x'``) start with a dot and are excluded; a sentence has spaces and is excluded.
_KEY_LITERAL_RE = re.compile(r"""(['"])([A-Za-z_/][\w.:/-]{3,63})\1""")
#: ...and *namespaced*: a separator, a digit or a camelCase hump. A bare word in quotes
#: (``'undefined'``, ``'string'``, ``'dark'``, ``'GET'``, ``'runs'``) is a type name, an enum value
#: or a prop that unrelated symbols share by the dozen; a namespaced key names one thing.
_STRUCTURED_KEY_RE = re.compile(r"[-_./:0-9]|[a-z][A-Z]")


def body_literals(body: str, *, limit: int = 12) -> list[str]:
    """Namespaced string literals of a symbol's text, in document order, de-duplicated.

    Query keys, storage keys, route paths, event, config and i18n keys couple symbols that the
    graph does not connect - ``invalidateQueries({queryKey: ['monitoring-executions']})`` in a
    helper and ``useQuery({queryKey: ['monitoring-executions', page]})`` in a hook - and a change
    to one reaches the other. The impact anchor source looks up these literals in other bodies
    (``anchors._impact_anchors``). Bare words (``typeof x === 'undefined'``) are not keys and are
    skipped. At most ``limit`` distinct literals, the first ones in the text (deterministic).
    """
    out: list[str] = []
    for _, literal in _KEY_LITERAL_RE.findall(body or ""):
        if literal in out or not any(ch.isalpha() for ch in literal):
            continue
        if not _STRUCTURED_KEY_RE.search(literal):
            continue
        out.append(literal)
        if len(out) >= max(int(limit), 1):
            break
    return out


def explicit_candidates(text: str) -> list[str]:
    """Names of :func:`explicit_references`, de-duplicated in first-seen order."""
    out: list[str] = []
    for _, name in explicit_references(text):
        if name not in out:
            out.append(name)
    return out


def is_test_symbol(
    symbol_id: str | None, name: str | None = None, file_id: str | None = None
) -> bool:
    """Deterministic test detection from the file path, module path and symbol name.

    Works on symbol ids of the form ``<lang>::<module.path>::<Class>::<name>#<hash>`` as well as
    on ``repo:path/to/file.py`` file ids; any of the three inputs may be missing.
    """
    paths: list[str] = []
    if file_id:
        paths.append(file_id.split(":", 1)[1] if ":" in file_id else file_id)
    if symbol_id:
        segments = symbol_id.split("::")
        if len(segments) >= 2 and segments[1]:
            paths.append(segments[1].replace(".", "/"))
        if name is None and segments:
            name = segments[-1].split("#", 1)[0] or None
    for path in paths:
        norm = path.replace("\\", "/")
        parts = norm.split("/")
        if any(p.lower() in _TEST_DIR_PARTS for p in parts[:-1]):
            return True
        if _TEST_FILE_RE.search(parts[-1]) or _TEST_FILE_RE.search(norm):
            return True
        # Module-path form has no extension: "tests/test_api_ui" -> last part "test_api_ui".
        if parts[-1].startswith("test_") or parts[-1].endswith("_test"):
            return True
    if name and _TEST_NAME_RE.match(name):
        return True
    if name and not paths and _TEST_NAME_CAMEL_RE.match(name):
        return True
    return False
