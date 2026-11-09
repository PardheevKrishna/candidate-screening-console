"""Prompt-injection and hidden-text detection for resumes (review evidence, never verdicts).

Public API:

* ``scan_text(text)``               -> findings for instruction-like text aimed at screeners or
                                       AI tools, and for invisible / bidi / tag unicode characters;
* ``scan_pdf_hidden_text(data)``    -> findings for PDF text a reader cannot see: white or
                                       transparent text on a white background, render mode 3 or
                                       7, text under 1 pt, text outside the page, hidden layers;
* ``strip_hidden(text, findings)``  -> ``text`` without the hidden snippets and invisible characters;
* ``summarize(findings)`` / ``reason_snippet(findings)`` -> reviewer-facing wording (risk.py).

A finding is ``{"kind": "instruction" | "invisible_chars" | "hidden_text", "snippet": str,
"detail": str}``. Nothing here raises for bad input and nothing is logged: snippets are
candidate data.

resume_parser uses the page-level API (``scan_page`` + ``neutralize``) while it extracts PDF
text, so every content stream is parsed once and hidden text never reaches parsing or scoring.
Precision guards: white text on a coloured shape or image is visible (template headers),
invisible text drawn over an image is the OCR text layer of a scanned resume, and sentences
that describe security work ("detects prompts such as 'ignore previous instructions'") are
mentions, not instructions.
"""
from __future__ import annotations

import io
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

__all__ = [
    "FINDING_KINDS",
    "scan_text",
    "scan_pdf_hidden_text",
    "strip_hidden",
    "strip_invisible",
    "remove_snippets",
    "summarize",
    "finding_summary",
    "reason_snippet",
    "looks_like_instruction",
    "flag_kind",
    "FLAG_KEYS",
    "FLAG_REASONS",
    "PageScan",
    "ScanBudget",
    "scan_page",
    "neutralize",
    "hidden_ocgs",
]

KIND_INSTRUCTION = "instruction"
KIND_INVISIBLE = "invisible_chars"
KIND_HIDDEN = "hidden_text"
FINDING_KINDS = (KIND_INSTRUCTION, KIND_INVISIBLE, KIND_HIDDEN)

SNIPPET_MAX = 300            # characters kept in a finding's snippet
MAX_FINDINGS = 12            # per scanned text / document
ZW_MIN = 5                   # zero-width characters needed for a finding (stray ones are common)
BIDI_MIN = 3                 # bidi embeddings / isolates needed (overrides count from the first)
HIDDEN_MIN_ALNUM = 4         # hidden runs shorter than this are removed but not reported
TINY_PT = 1.0                # effective glyph height below this (pt) is invisible
MIN_ALPHA = 0.05             # fill / stroke opacity at or below this is invisible
LIGHT_LUMA = 0.80            # only text at least this light is compared with its background
SAME_COLOUR_DIST = 0.10      # RGB distance under which text and background colour look the same
MAX_DOC_OPS = 400_000        # content-stream operators walked per document (time guard)
MAX_FORM_DEPTH = 6
MAX_PAINTED = 20_000         # painted areas remembered per page; beyond -> light text counts as visible
MAX_REMOVE_CHARS = 4000      # longest snippet used for text removal
# A page whose only text is invisible but which draws its content as an image or as vector outlines
# carries a text layer (OCR scans, "text to curves" exports): that text is the page, not hidden.
TEXT_LAYER_MAX_VISIBLE = 40      # visible glyphs on such a page at most
TEXT_LAYER_IMAGE_SHARE = 0.25    # ... and images covering this share of the page
TEXT_LAYER_MIN_FILLS = 200       # ... or at least this many filled vector paths

# ---------------------------------------------------------------------------
# Invisible characters
# ---------------------------------------------------------------------------
_ZW_CHARS = "\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff"
_BIDI_CHARS = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_BIDI_OVERRIDES = "\u202d\u202e"
_TAG_RE = re.compile("[\U000e0000-\U000e007f]")
_INVISIBLE_RE = re.compile(f"[{_ZW_CHARS}{_BIDI_CHARS}\U000e0000-\U000e007f]")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACES_RE = re.compile(r"[ \t\f\v\r]+")

# ---------------------------------------------------------------------------
# Instruction-like text: phrases addressed to an AI system or screener. Ordinary resume
# sentences ("Designed system prompts for a chatbot", "Built agents that act as an AI tutor",
# "Ranked the top candidates", "Acted as a teaching assistant") do not match.
# ---------------------------------------------------------------------------
_AI = (r"(?:ai|a\.i\.|llm|large\s+language\s+model|language\s+model|chat\s?bot|chat\s?gpt|gpt(?:-?\d+o?)?"
       r"|gemini|copilot|ats|applicant\s+tracking\s+system|resume\s+(?:screener|parser|reviewer|evaluator"
       r"|bot)|screening\s+(?:bot|tool|system|agent|model|software|algorithm)|recruit(?:er|ing|ment)\s+(?:bot|ai"
       r"|assistant|agent|model)|hiring\s+(?:bot|agent|model|ai|assistant)|automated\s+(?:screener|screening"
       r"|reviewer|system))")
_ROLE_NOUN = (r"(?:\s+(?:recruiter|screener|reviewer|evaluator|assistant|agent|model|system|tool|bot|program"
              r"|grader)s?)?")
_CLAUSE_END = (r"(?=\s*(?:[,.:;!?]|$)|\s+(?:and|that|who|which|tasked|designed|trained|reading|parsing|screening"
               r"|reviewing|evaluating|processing|analy[sz]ing|scanning)\b)")
_SELF = r"(?:this|my)\s+(?:candidate|applicant|resume|cv|profile|application|submission)"
_TOP = (r"(?:10\s*/\s*10|10\s+out\s+of\s+10|100\s*/\s*100|100\s*%|5\s*/\s*5|five\s+stars|(?:the\s+)?highest"
        r"(?:\s+possible)?\s+(?:score|rating|rank|marks|grade|points)|(?:the\s+)?maximum\s+(?:score|rating|marks"
        r"|points|grade)|a\s+perfect\s+(?:score|rating|10|fit|match)|(?:as\s+)?(?:a\s+)?(?:strong|perfect)\s+"
        r"(?:hire|fit|match)|as\s+(?:the\s+)?top\s+candidate|as\s+(?:an?\s+)?(?:excellent|outstanding|exceptional)"
        r"\b)")
_I, _IM = re.IGNORECASE, re.IGNORECASE | re.MULTILINE
_INSTRUCTION_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("override", "instruction override (e.g. 'ignore previous instructions')", re.compile(
        r"\b(?:ignore|disregard|forget)\s+(?:(?:all|any|the|your|my|these|those|of)\s+){0,3}(?:previous|prior"
        r"|above|earlier|preceding|foregoing|original|initial|system|other)\s+(?:instructions?|prompts?|directions?"
        r"|directives?|rules|guidelines|commands?|criteria)\b"
        r"|\b(?:override|bypass)\s+(?:(?:all|any|the|your|these|those)\s+){0,2}(?:previous|prior|above|earlier"
        r"|system)\s+(?:instructions?|prompts?)\b"
        r"|\b(?:ignore|disregard|forget)\s+(?:everything|anything|all)\s+(?:above|before|previously\s+(?:said"
        r"|stated|written))\b", _I)),
    ("role", "addresses an AI system directly", re.compile(
        r"\b(?:you\s+are|you're)\s+(?:now\s+)?(?:an?\s+|the\s+)?(?:(?:helpful|friendly|expert|senior|hr|hiring"
        r"|recruiting)\s+)*(?:assistant|" + _AI + ")" + _ROLE_NOUN + _CLAUSE_END
        + r"|(?:(?:^|[.!?:])\s*|\b(?:please|now|you)\s+)(?:act|behave)\s+as\s+(?:an?\s+|the\s+)?" + _AI
        + _ROLE_NOUN + _CLAUSE_END
        + r"|\bpretend\s+(?:to\s+be|you\s+are)\s+(?:an?\s+|the\s+)?" + _AI + r"\b"
        r"|\bif\s+you\s+are\s+(?:an?\s+|the\s+)?" + _AI + _ROLE_NOUN + _CLAUSE_END
        + r"|\bas\s+an?\s+(?:ai\s+)?(?:large\s+)?language\s+model\b"
        r"|\b(?:note|message|instructions?|attention|hey|hi|hello|dear)\s+(?:to\s+|for\s+)?(?:the\s+|any\s+"
        r"|all\s+)?" + _AI + r"(?:\s+(?:recruiters?|screeners?|reviewers?|readers?|systems?|tools?|models?"
        r"|agents?|assistants?))?\s*[:,]", _IM)),
    ("score", "asks for a top score or ranking", re.compile(
        r"\b(?:rate|score|rank|grade|mark|evaluate|assess|classify)\s+" + _SELF + r"\b[^.!?\n]{0,40}?" + _TOP
        + r"|\bgive\s+(?:" + _SELF + r"|him|her|them|me)\s+(?:a\s+|the\s+)?(?:highest|maximum|top|perfect|full"
        r"|best)\s+(?:score|rating|marks|points|rank|grade)"
        r"|\bgive\s+" + _SELF + r"\s+(?:a\s+)?(?:10\s*/\s*10|100\s*%)"
        r"|\bthis\s+(?:candidate|applicant|resume|cv)\s+(?:is|should\s+be|must\s+be|has\s+been)\s+(?:rated|scored"
        r"|ranked|graded|marked)\s+(?:as\s+)?(?:an?\s+|the\s+)?(?:10|100|five|5|highest|top|perfect|maximum|best"
        r"|first|#\s*1|number\s+one|excellent|outstanding|exceptional)\b", _I)),
    ("outcome", "dictates the screening outcome", re.compile(
        r"\b(?:must|should|need\s+to|have\s+to)\s+(?:always\s+)?(?:shortlist|select|hire|advance|approve|accept"
        r"|pass)\s+" + _SELF + r"\b"
        r"|\b(?:respond|reply|answer|output|return)\s+(?:only\s+with|with\s+only|with\s+[\"'“‘])\s*"
        r"[\"'“‘]?(?:yes|hire|strong\s+hire|selected|shortlisted|approved|accepted|qualified"
        r"|pass(?:ed)?)\b", _I)),
    ("system", "targets the system prompt", re.compile(
        r"\bsystem\s+(?:prompt|message|instructions?)\s*[:=]"
        r"|\b(?:ignore|disregard|forget|override|bypass|reveal|print|repeat|leak)\s+(?:\w+\s+){0,3}"
        r"system\s+(?:prompt|message|instructions?)\b"
        r"|\bnew\s+(?:system\s+)?instructions?\s*:", _I)),
    ("template", "chat-template control tokens", re.compile(
        r"<\|\s*[a-z_]{2,40}\s*\|>|\[/?INST\]|<</?SYS>>|^\s*#{2,}\s*(?:system|instructions?)\s*:?\s*$", _IM)),
    ("conceal", "asks to keep the instruction secret", re.compile(
        r"\bdo\s+not\s+(?:mention|reveal|disclose|repeat|reference|acknowledge|share)\s+(?:\w+\s+){0,3}"
        r"(?:this|these|the\s+above|the\s+following)\s+(?:instructions?|text|prompt|message|note|line"
        r"|request)\b", _I)),
]
# A sentence describing security work mentions an injection rather than performing one.
_MENTION_KINDS = frozenset({"override", "system"})
_DEFENSIVE_RE = re.compile(
    r"\b(?:detect\w*|prevent\w*|defen[cs]\w*|defend\w*|mitigat\w*|guardrail\w*|classif\w*|block\w*"
    r"|saniti[sz]\w*|red[\s-]?team\w*|benchmark\w*|robustness|adversarial|jailbreak\w*|prompt\s+injections?)\b",
    re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"[.!?•]|\n\s*\n")


def _probe(text: str) -> str:
    """Matching form of ``text``: NFKC, invisible characters removed, spaces collapsed (lines kept)."""
    text = unicodedata.normalize("NFKC", _INVISIBLE_RE.sub("", text))
    return _SPACES_RE.sub(" ", text)


def _clip(text: Any, n: int) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: max(0, n - 3)].rstrip() + "..."


def _sentence(text: str, start: int, end: int) -> str:
    lo = max(0, start - 300)
    cut = [m.end() for m in _SENTENCE_END_RE.finditer(text, lo, start)]
    s = cut[-1] if cut else lo
    m = _SENTENCE_END_RE.search(text, end, min(len(text), end + 300))
    e = m.end() if m else min(len(text), end + 300)
    return " ".join(text[s:e].split())


def _instruction_hits(text: str, *, hidden: bool = False) -> list[tuple[str, str]]:
    """(label, sentence) for every instruction-like passage; mentions in security work skipped."""
    probe = _probe(text)
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, label, rx in _INSTRUCTION_PATTERNS:
        for m in rx.finditer(probe):
            sentence = _sentence(probe, m.start(), m.end()) or " ".join(m.group().split())
            if not hidden and kind in _MENTION_KINDS and _DEFENSIVE_RE.search(sentence):
                continue
            key = sentence.casefold()
            if key in seen:
                continue
            seen.add(key)
            hits.append((label, sentence))
            if len(hits) >= MAX_FINDINGS:
                return hits
    return hits


def looks_like_instruction(text: str) -> bool:
    """True when ``text`` reads like an instruction aimed at an AI screener (no mention exemption)."""
    return bool(isinstance(text, str) and text and _instruction_hits(text, hidden=True))


def _visible_chars(text: str) -> str:
    """Invisible characters rendered as [U+XXXX] so a reviewer can see them."""
    return _INVISIBLE_RE.sub(lambda m: f"[U+{ord(m.group()):04X}]", text)


def _invisible_finding(text: str) -> dict[str, str] | None:
    if not _INVISIBLE_RE.search(text):
        return None
    body = text[1:] if text.startswith("\ufeff") else text   # a leading byte-order mark is harmless
    zw = sum(body.count(c) for c in _ZW_CHARS)
    bidi = sum(body.count(c) for c in _BIDI_CHARS)
    overrides = sum(body.count(c) for c in _BIDI_OVERRIDES)
    tags = _TAG_RE.findall(body)
    if not (tags or overrides or bidi >= BIDI_MIN or zw >= ZW_MIN):
        return None
    parts: list[str] = []
    snippet = ""
    if tags:
        decoded = "".join(chr(ord(c) - 0xE0000) for c in tags if 0x20 <= ord(c) - 0xE0000 < 0x7F)
        parts.append(f"{len(tags)} Unicode tag character(s) hiding text")
        snippet = decoded.strip()
        if snippet and _instruction_hits(snippet, hidden=True):
            parts.append("the hidden text reads like an instruction to AI screeners")
    if bidi:
        parts.append(f"{bidi} bidirectional control character(s)" + (" incl. overrides" if overrides else ""))
    if zw:
        parts.append(f"{zw} zero-width character(s)")
    if not snippet:
        first = _INVISIBLE_RE.search(body)
        i = first.start() if first else 0
        snippet = _visible_chars(body[max(0, i - 40):i + 80])
    return {"kind": KIND_INVISIBLE, "snippet": _clip(snippet, SNIPPET_MAX), "detail": "; ".join(parts)}


def scan_text(text: str) -> list[dict[str, str]]:
    """Findings for instruction-like text aimed at screeners / AI and for invisible characters."""
    if not isinstance(text, str) or not text:
        return []
    try:
        out: list[dict[str, str]] = []
        invisible = _invisible_finding(text)
        if invisible:
            out.append(invisible)
        for label, sentence in _instruction_hits(text):
            out.append({"kind": KIND_INSTRUCTION, "snippet": _clip(sentence, SNIPPET_MAX), "detail": label})
        return out[:MAX_FINDINGS]
    except Exception:  # noqa: BLE001 - a scanner bug must never stop parsing
        return []


# ---------------------------------------------------------------------------
# Removing hidden text
# ---------------------------------------------------------------------------
def strip_invisible(text: str) -> str:
    """``text`` without zero-width, bidi-control and Unicode tag characters."""
    return _INVISIBLE_RE.sub("", text) if isinstance(text, str) else ""


def _snippet_pattern(snippet: str) -> re.Pattern[str] | None:
    chars = [c for c in _INVISIBLE_RE.sub("", snippet) if not c.isspace()][:MAX_REMOVE_CHARS]
    if sum(c.isalnum() for c in chars) < 3:
        return None
    return re.compile(r"\s*".join(re.escape(c) for c in chars))


def remove_snippets(text: str, snippets: Iterable[str] | None) -> str:
    """Remove the first occurrence of each snippet; whitespace differences are tolerated."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    for snippet in list(snippets or [])[:200]:
        if not isinstance(snippet, str):
            continue
        rx = _snippet_pattern(snippet)
        m = rx.search(text) if rx else None
        if m:
            text = text[:m.start()] + " " + text[m.end():]
    return text


def strip_hidden(text: str, findings: Iterable[dict[str, Any]] | None = None) -> str:
    """``text`` without the snippets of ``hidden_text`` findings and without invisible characters."""
    snippets = [f.get("snippet", "") for f in findings or [] if isinstance(f, dict) and f.get("kind") == KIND_HIDDEN]
    return strip_invisible(remove_snippets(text, snippets))


# ---------------------------------------------------------------------------
# Reviewer wording
# ---------------------------------------------------------------------------
_PRIORITY = {KIND_INSTRUCTION: 1, KIND_HIDDEN: 2, KIND_INVISIBLE: 3}


def _rank(f: dict[str, Any]) -> tuple[int, int]:
    kind = str(f.get("kind"))
    instructive = kind == KIND_INSTRUCTION or (kind == KIND_HIDDEN and "instruction" in str(f.get("detail", "")))
    return (0 if instructive else 1, _PRIORITY.get(kind, 9))


def _no_pipes(text: Any) -> str:
    return str(text).replace("|", "/")


def finding_summary(f: dict[str, Any], width: int = 80) -> str:
    """One finding in words (no "|", so summaries can be " | "-joined)."""
    kind, detail = f.get("kind"), _no_pipes(f.get("detail", ""))
    snippet = _no_pipes(_clip(f.get("snippet", ""), width))
    if kind == KIND_HIDDEN:
        return f"Hidden text ({detail}): '{snippet}'"
    if kind == KIND_INSTRUCTION:
        return f"Instruction aimed at AI screeners ({detail}): '{snippet}'"
    if kind == KIND_INVISIBLE:
        return f"Invisible characters ({detail})" + (f": '{snippet}'" if snippet else "")
    return f"{detail}: '{snippet}'"


def summarize(findings: Iterable[dict[str, Any]] | None, limit: int = 6) -> str:
    """" | "-joined summaries (most serious first) for the injection_flags column."""
    items = sorted((f for f in findings or [] if isinstance(f, dict)), key=_rank)
    parts = [finding_summary(f) for f in items[:limit]]
    if len(items) > limit:
        parts.append(f"+{len(items) - limit} more")
    return " | ".join(parts)


def reason_snippet(findings: Iterable[dict[str, Any]] | None, width: int = 60) -> str:
    """Short snippet of the most serious finding (instructions first)."""
    items = sorted((f for f in findings or [] if isinstance(f, dict)), key=_rank)
    return _no_pipes(_clip(items[0].get("snippet", ""), width)) if items else ""


# Risk flag per resume: only the most serious kind counts, so one resume is never weighted twice.
FLAG_PROMPT_INJECTION = "prompt_injection"   # instruction aimed at AI screeners (visible or hidden)
FLAG_HIDDEN_TEXT = "hidden_text"             # text a reader cannot see (white, tiny, off-page, hidden layer)
FLAG_INVISIBLE_CHARS = "invisible_chars"     # only zero-width / bidi / tag characters
FLAG_KEYS = (FLAG_PROMPT_INJECTION, FLAG_HIDDEN_TEXT, FLAG_INVISIBLE_CHARS)
FLAG_REASONS = {
    FLAG_PROMPT_INJECTION: "Instruction aimed at AI screeners found in resume (prompt injection)",
    FLAG_HIDDEN_TEXT: "Hidden text in resume (invisible to readers)",
    FLAG_INVISIBLE_CHARS: "Unusual invisible characters in resume (possible obfuscation or copy-paste artefact)",
}


def flag_kind(findings: Iterable[dict[str, Any]] | None) -> str | None:
    """The risk flag for one resume: prompt_injection when any finding reads like an instruction to AI
    screeners (visible, hidden or encoded in invisible characters), else hidden_text, else
    invisible_chars; None without findings."""
    items = [f for f in findings or [] if isinstance(f, dict)]
    if not items:
        return None
    if any(f.get("kind") == KIND_INSTRUCTION or "instruction" in str(f.get("detail", "")) for f in items):
        return FLAG_PROMPT_INJECTION
    if any(f.get("kind") == KIND_HIDDEN for f in items):
        return FLAG_HIDDEN_TEXT
    return FLAG_INVISIBLE_CHARS


def flag_headline(injection_flags: Any) -> str:
    """Reviewer headline for an injection_flags summary (see summarize()); "" when there is none."""
    text = str(injection_flags or "").strip()
    if not text:
        return ""
    if text.startswith("Instruction aimed at AI screeners") or "instruction" in text.casefold():
        return FLAG_REASONS[FLAG_PROMPT_INJECTION]
    if "Hidden text (" in text:
        return FLAG_REASONS[FLAG_HIDDEN_TEXT]
    return FLAG_REASONS[FLAG_INVISIBLE_CHARS]


# ===========================================================================
# PDF hidden text
# ===========================================================================
@dataclass
class ScanBudget:
    """Operators that may still be walked for one document (shared across its pages)."""

    ops_left: int = MAX_DOC_OPS


@dataclass
class PageScan:
    """Hidden-text evidence for one page.

    ``hidden_ops`` are indices into the page's own operator list (``neutralize`` blanks them);
    ``texts`` holds every hidden run in full and ``form_texts`` the runs inside form XObjects,
    which cannot be blanked per page and are removed from the extracted text instead.
    """

    findings: list[dict[str, str]] = field(default_factory=list)
    hidden_ops: list[int] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    form_texts: list[str] = field(default_factory=list)


_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_TEXT_SHOW = frozenset({b"Tj", b"TJ", b"'", b'"'})
_FILL_PAINT = frozenset({b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"})
_ALL_PAINT = _FILL_PAINT | {b"S", b"s", b"n"}
_PATH_OPS = frozenset({b"m", b"l", b"c", b"v", b"y", b"re", b"h"})
_REASON_RENDER = "invisible render mode"
_LAYER_REASONS = frozenset({_REASON_RENDER, "white text on a white background", "text colour matches the background",
                            "transparent text"})
_WHITE = (1.0, 1.0, 1.0)


def _mul(m: tuple[float, ...], n: tuple[float, ...]) -> tuple[float, ...]:
    """m x n for PDF affine matrices [a b c d e f] (row-vector convention)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _apply(m: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
    return x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _nums(operands: Any, k: int) -> tuple[float, ...] | None:
    if not isinstance(operands, list) or len(operands) < k:
        return None
    try:
        vals = tuple(float(x) for x in operands[:k])
    except (TypeError, ValueError):
        return None
    return vals if all(math.isfinite(v) for v in vals) else None


def _resolve(obj: Any) -> Any:
    try:
        return obj.get_object() if obj is not None and hasattr(obj, "get_object") else obj
    except Exception:  # noqa: BLE001
        return None


def _lookup(resources: Any, category: str, name: Any) -> Any:
    try:
        table = _resolve(resources.get(category)) if resources is not None else None
        return _resolve(table.get(name)) if table is not None else None
    except Exception:  # noqa: BLE001
        return None


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _intersect(a: tuple[float, ...] | None, b: tuple[float, ...] | None) -> tuple[float, ...] | None:
    if a is None:
        return b
    if b is None:
        return a
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (x0, y0, x1, y1) if x0 <= x1 and y0 <= y1 else (x0, y0, x0, y0)


def _contains(box: tuple[float, ...], x: float, y: float) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _overlaps(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _to_rgb(space: str, comps: list[float] | tuple[float, ...]) -> tuple[float, float, float] | None:
    vals = [min(1.0, max(0.0, _num(c))) for c in comps]
    if space == "gray" and len(vals) >= 1:
        return vals[0], vals[0], vals[0]
    if space == "rgb" and len(vals) >= 3:
        return vals[0], vals[1], vals[2]
    if space == "cmyk" and len(vals) >= 4:
        c, m, y, k = vals[:4]
        return 1 - min(1.0, c + k), 1 - min(1.0, m + k), 1 - min(1.0, y + k)
    if space == "sep" and len(vals) >= 1:
        return _WHITE if vals[0] <= MIN_ALPHA else None   # tint 0 = no ink; other tints: unknown colour
    return None


def _luma(rgb: tuple[float, float, float]) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _dist(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


class _GState:
    __slots__ = ("ctm", "fill", "stroke", "fill_space", "stroke_space", "fill_alpha", "stroke_alpha",
                 "font", "font_type3", "font_bytes", "size", "render", "hscale", "leading", "rise", "clip")

    def __init__(self) -> None:
        self.ctm: tuple[float, ...] = _IDENTITY
        self.fill: tuple[float, float, float] | None = (0.0, 0.0, 0.0)
        self.stroke: tuple[float, float, float] | None = (0.0, 0.0, 0.0)
        self.fill_space = "gray"
        self.stroke_space = "gray"
        self.fill_alpha = 1.0
        self.stroke_alpha = 1.0
        self.font: Any = None
        self.font_type3 = False
        self.font_bytes = 1
        self.size = 0.0
        self.render = 0
        self.hscale = 1.0
        self.leading = 0.0
        self.rise = 0.0
        self.clip: tuple[float, ...] | None = None

    def copy(self) -> "_GState":
        new = _GState.__new__(_GState)
        for name in _GState.__slots__:
            setattr(new, name, getattr(self, name))
        return new


@dataclass
class _Event:
    reasons: list[str]
    point: tuple[float, float]
    op_index: int | None          # page-level operator index; None inside form XObjects
    decode: Callable[[], str]


def _font_decoder(font: Any) -> Callable[[bytes], str]:
    """bytes -> text for one font dictionary, the way pypdf's text extraction decodes it."""
    encoding: Any = "latin-1"
    cmap: dict[Any, Any] = {}
    try:
        from pypdf.generic._font import Font  # noqa: PLC0415 - pypdf internals, guarded

        parsed = Font.from_font_resource(font)
        encoding, cmap = parsed.encoding, parsed.character_map
    except Exception:  # noqa: BLE001
        try:
            from pypdf._cmap import get_encoding  # noqa: PLC0415

            encoding, cmap = get_encoding(font)
        except Exception:  # noqa: BLE001
            encoding, cmap = "latin-1", {}

    def decode(raw: bytes) -> str:
        try:
            if isinstance(encoding, str):
                try:
                    text = raw.decode(encoding, "surrogatepass")
                except (UnicodeDecodeError, LookupError):
                    text = raw.decode("latin-1", "replace")
            else:
                text = "".join(encoding.get(b, chr(b)) for b in raw)
            text = "".join(str(cmap.get(ch, ch)) for ch in text)
        except Exception:  # noqa: BLE001
            text = raw.decode("latin-1", "replace")
        return _CTRL_RE.sub(" ", _SURROGATE_RE.sub("\ufffd", text))

    return decode


def _raw(s: Any) -> bytes:
    if isinstance(s, bytes):
        return bytes(s)
    if isinstance(s, str):
        try:
            return s.get_original_bytes()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return s.encode("latin-1", "replace")
    return b""


def _payload(op: bytes, operands: Any) -> Any:
    if not isinstance(operands, list) or not operands:
        return None
    if op == b'"':
        return operands[2] if len(operands) >= 3 else None
    return operands[0]


def _is_string(item: Any) -> bool:
    return isinstance(item, bytes) or (isinstance(item, str) and not item.startswith("/"))


def _payload_text(payload: Any, decode: Callable[[bytes], str]) -> str:
    if isinstance(payload, list):
        parts: list[str] = []
        for item in payload:
            if _is_string(item):
                parts.append(decode(_raw(item)))
            elif _num(item) <= -250:           # a large kerning gap is a word space
                parts.append(" ")
        return "".join(parts)
    return decode(_raw(payload)) if _is_string(payload) else ""


def _glyph_count(payload: Any, per_glyph: int) -> int:
    items = payload if isinstance(payload, list) else [payload]
    n = sum(len(_raw(x)) for x in items if _is_string(x))
    return max(1, n // max(1, per_glyph))


def hidden_ocgs(reader: Any) -> frozenset[int]:
    """Object numbers of optional-content groups (layers) that are off by default."""
    try:
        props = _resolve(_resolve(reader.trailer["/Root"]).get("/OCProperties"))
        if props is None:
            return frozenset()
        default = _resolve(props.get("/D")) or {}

        def ids(arr: Any) -> set[int]:
            return {ref.idnum for ref in (_resolve(arr) or []) if hasattr(ref, "idnum")}

        if str(default.get("/BaseState", "/ON")) == "/OFF":
            return frozenset(ids(props.get("/OCGs")) - ids(default.get("/ON")))
        return frozenset(ids(default.get("/OFF")))
    except Exception:  # noqa: BLE001
        return frozenset()


def _page_box(page: Any) -> tuple[float, float, float, float] | None:
    try:
        box = page.cropbox
        x0, y0, x1, y1 = float(box.left), float(box.bottom), float(box.right), float(box.top)
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    except Exception:  # noqa: BLE001
        return None


class _Walker:
    """Minimal content-stream interpreter: graphics state, text state and painted areas."""

    def __init__(self, reader: Any, page_box: tuple[float, ...] | None, off_ocgs: frozenset[int],
                 budget: ScanBudget) -> None:
        self.reader = reader
        self.page_box = page_box
        self.off_ocgs = off_ocgs
        self.budget = budget
        self.gs = _GState()
        self.stack: list[_GState] = []
        self.tm: tuple[float, ...] = _IDENTITY
        self.tlm: tuple[float, ...] = _IDENTITY
        self.mc: list[bool] = []                 # marked-content stack: inside a hidden layer?
        self.path: list[tuple[float, float]] = []
        self.pending_clip = False
        self.painted: list[tuple[tuple[float, ...], tuple[float, float, float] | None]] = []
        self.images: list[tuple[float, ...]] = []
        self.complex = False
        self.events: list[_Event | None] = []
        self.forms: list[int] = []
        self.decoders: dict[int, tuple[Any, Callable[[bytes], str]]] = {}
        self.visible_glyphs = 0
        self.fills = 0

    # ---- helpers ---------------------------------------------------------
    def _decoder(self, font: Any) -> Callable[[bytes], str]:
        if font is None:
            return lambda raw: _CTRL_RE.sub(" ", raw.decode("latin-1", "replace"))
        hit = self.decoders.get(id(font))
        if hit is None or hit[0] is not font:      # the font object is kept alive with its decoder
            hit = self.decoders[id(font)] = (font, _font_decoder(font))
        return hit[1]

    def _paint(self, box: tuple[float, ...] | None, colour: tuple[float, float, float] | None) -> None:
        if box is None:
            return
        if len(self.painted) >= MAX_PAINTED:
            self.complex = True
            return
        self.painted.append((_intersect(box, self.gs.clip), colour))

    def _unit_square(self) -> tuple[float, ...]:
        m = self.gs.ctm
        return _bbox([_apply(m, 0, 0), _apply(m, 1, 0), _apply(m, 0, 1), _apply(m, 1, 1)])

    def _background(self, x: float, y: float) -> tuple[float, float, float] | None | bool:
        """Colour painted under (x, y): rgb, None when unknown (image / shading), False for the page."""
        for box, colour in reversed(self.painted):
            if _contains(box, x, y):
                return colour
        return False

    def _colour_space(self, resources: Any, name: Any, depth: int = 0) -> tuple[str, list[float]]:
        """(family, initial components) of a colour space name."""
        key = str(name)
        if key in ("/DeviceGray", "/G", "/CalGray"):
            return "gray", [0.0]
        if key in ("/DeviceRGB", "/RGB", "/CalRGB"):
            return "rgb", [0.0, 0.0, 0.0]
        if key in ("/DeviceCMYK", "/CMYK"):
            return "cmyk", [0.0, 0.0, 0.0, 1.0]
        if key == "/Pattern":
            return "pattern", []
        obj = _lookup(resources, "/ColorSpace", name)
        if obj is None or depth > 2:
            return "unknown", []
        if isinstance(obj, str):
            return self._colour_space(resources, obj, depth + 1)
        try:
            head = str(_resolve(obj[0]))
            if head == "/ICCBased":
                n = int(_num(_resolve(obj[1]).get("/N"), 0))
                return {1: ("gray", [0.0]), 3: ("rgb", [0.0, 0.0, 0.0]),
                        4: ("cmyk", [0.0, 0.0, 0.0, 1.0])}.get(n, ("unknown", []))
            if head == "/CalRGB":
                return "rgb", [0.0, 0.0, 0.0]
            if head == "/CalGray":
                return "gray", [0.0]
            if head == "/Separation":
                return "sep", [1.0]
        except Exception:  # noqa: BLE001
            pass
        return "unknown", []

    def _set_colour(self, stroke: bool, space: str, comps: list[float] | tuple[float, ...]) -> None:
        rgb = _to_rgb(space, comps)
        if stroke:
            self.gs.stroke_space, self.gs.stroke = space, rgb
        else:
            self.gs.fill_space, self.gs.fill = space, rgb

    def _layer_hidden(self, resources: Any, operands: Any) -> bool:
        """BDC /OC <props>: content in an optional-content group that is off by default."""
        if not self.off_ocgs or not isinstance(operands, list) or len(operands) < 2 or str(operands[0]) != "/OC":
            return False
        prop = operands[1]
        if isinstance(prop, str):
            table = _resolve(resources.get("/Properties")) if resources is not None else None
            prop = table.raw_get(prop) if table is not None and hasattr(table, "raw_get") and prop in table else None
        return self._ocg_hidden(prop)

    def _ocg_hidden(self, ref: Any) -> bool:
        if ref is None:
            return False
        if hasattr(ref, "idnum"):
            if ref.idnum in self.off_ocgs:
                return True
        obj = _resolve(ref)
        if not hasattr(obj, "get") or str(obj.get("/Type", "")) != "/OCMD":
            return False
        groups = obj.raw_get("/OCGs") if hasattr(obj, "raw_get") and "/OCGs" in obj else None
        resolved = _resolve(groups)
        refs = list(resolved) if isinstance(resolved, list) else [groups]
        refs = [r for r in refs if hasattr(r, "idnum")]
        return bool(refs) and all(r.idnum in self.off_ocgs for r in refs)

    # ---- text --------------------------------------------------------------
    def _td(self, tx: float, ty: float) -> None:
        a, b, c, d, e, f = self.tlm
        self.tlm = (a, b, c, d, tx * a + ty * c + e, tx * b + ty * d + f)
        self.tm = self.tlm

    def _show(self, op: bytes, operands: Any, op_index: int | None) -> None:
        gs = self.gs
        if op in (b"'", b'"'):
            self._td(0.0, -gs.leading)
        payload = _payload(op, operands)
        if payload is None:
            return
        m = _mul(self.tm, gs.ctm)
        fs, rise = gs.size, gs.rise
        ox, oy = m[4] + rise * m[2], m[5] + rise * m[3]
        reasons: list[str] = []
        if self.mc and self.mc[-1]:
            reasons.append("hidden layer")
        mode = gs.render
        if mode in (3, 7):
            reasons.append(_REASON_RENDER)
        elif not gs.font_type3:
            v = abs(fs) * math.hypot(m[2], m[3])
            h = abs(fs) * gs.hscale * math.hypot(m[0], m[1])
            if v < TINY_PT or h < TINY_PT / 2:
                reasons.append(f"text {v:.2g} pt high")
        if self.page_box is not None and not _contains(self.page_box, ox, oy):
            w = _glyph_count(payload, gs.font_bytes) * 0.6 * fs * gs.hscale
            corners = [_apply(m, 0, rise), _apply(m, w, rise), _apply(m, 0, rise + fs), _apply(m, w, rise + fs)]
            if not _overlaps(_bbox(corners), self.page_box):
                reasons.append("outside the page")
        if mode not in (3, 7):
            colour_reason = self._colour_reason(mode, m, ox, oy, fs)
            if colour_reason:
                reasons.append(colour_reason)
        if not reasons:
            self.visible_glyphs += _glyph_count(payload, gs.font_bytes)
            if self.events and self.events[-1] is not None:
                self.events.append(None)
            return
        decode = self._decoder(gs.font)
        self.events.append(_Event(reasons, (ox, oy), op_index, lambda: _payload_text(payload, decode)))

    def _colour_reason(self, mode: int, m: tuple[float, ...], ox: float, oy: float, fs: float) -> str | None:
        """Why the painted colour(s) of this text are invisible, or None when the text shows."""
        gs = self.gs
        sides = []
        if mode in (0, 2, 4, 6):
            sides.append((gs.fill, gs.fill_alpha))
        if mode in (1, 2, 5, 6):
            sides.append((gs.stroke, gs.stroke_alpha))
        found = ""
        for colour, alpha in sides:
            if alpha <= MIN_ALPHA:
                found = found or "transparent text"
                continue
            if colour is None or _luma(colour) < LIGHT_LUMA or self.complex:
                return None
            bg = self._background(ox + 0.3 * fs * m[2], oy + 0.3 * fs * m[3])
            if bg is None:                         # image / shading underneath: visible
                return None
            bg_rgb = _WHITE if bg is False else bg
            if _dist(colour, bg_rgb) >= SAME_COLOUR_DIST:
                return None
            white = _luma(colour) >= 0.95 and _luma(bg_rgb) >= 0.95
            found = found or ("white text on a white background" if white else "text colour matches the background")
        return found or None

    # ---- walking -------------------------------------------------------------
    def walk(self, ops: list[tuple[Any, bytes]], resources: Any, *, page_level: bool, depth: int = 0) -> None:
        for index, item in enumerate(ops):
            if self.budget.ops_left <= 0:
                return
            self.budget.ops_left -= 1
            try:
                operands, op = item
                self._op(op, operands, resources, index if page_level else None, depth)
            except Exception:  # noqa: BLE001 - one odd operator must not stop the scan
                continue

    def _op(self, op: bytes, operands: Any, resources: Any, op_index: int | None, depth: int) -> None:
        gs = self.gs
        if op in _TEXT_SHOW:
            self._show(op, operands, op_index)
        elif op == b"Td" or op == b"TD":
            v = _nums(operands, 2)
            if v:
                if op == b"TD":
                    gs.leading = -v[1]
                self._td(v[0], v[1])
        elif op == b"Tm":
            v = _nums(operands, 6)
            if v:
                self.tm = self.tlm = v
        elif op == b"T*":
            self._td(0.0, -gs.leading)
        elif op == b"BT":
            self.tm = self.tlm = _IDENTITY
        elif op == b"Tf":
            if isinstance(operands, list) and len(operands) >= 2:
                font = _lookup(resources, "/Font", operands[0])
                gs.font, gs.size = font, _num(operands[1])
                subtype = str(font.get("/Subtype", "")) if hasattr(font, "get") else ""
                gs.font_type3 = subtype == "/Type3"
                gs.font_bytes = 2 if subtype == "/Type0" else 1
        elif op == b"Tr":
            gs.render = int(_num(operands[0] if operands else 0))
        elif op == b"Tz":
            gs.hscale = _num(operands[0] if operands else 100.0, 100.0) / 100.0
        elif op == b"TL":
            gs.leading = _num(operands[0] if operands else 0.0)
        elif op == b"Ts":
            gs.rise = _num(operands[0] if operands else 0.0)
        elif op == b"q":
            self.stack.append(gs.copy())
        elif op == b"Q":
            if self.stack:
                self.gs = self.stack.pop()
        elif op == b"cm":
            v = _nums(operands, 6)
            if v:
                gs.ctm = _mul(v, gs.ctm)
        elif op in (b"g", b"G"):
            v = _nums(operands, 1)
            if v:
                self._set_colour(op == b"G", "gray", v)
        elif op in (b"rg", b"RG"):
            v = _nums(operands, 3)
            if v:
                self._set_colour(op == b"RG", "rgb", v)
        elif op in (b"k", b"K"):
            v = _nums(operands, 4)
            if v:
                self._set_colour(op == b"K", "cmyk", v)
        elif op in (b"cs", b"CS"):
            if operands:
                space, initial = self._colour_space(resources, operands[0])
                self._set_colour(op == b"CS", space, initial)
        elif op in (b"sc", b"scn", b"SC", b"SCN"):
            stroke = op in (b"SC", b"SCN")
            items = operands if isinstance(operands, list) else []
            pattern = any(isinstance(x, str) for x in items)
            space = "pattern" if pattern else (gs.stroke_space if stroke else gs.fill_space)
            self._set_colour(stroke, space, [_num(x) for x in items if not isinstance(x, str)])
        elif op == b"gs":
            ext = _lookup(resources, "/ExtGState", operands[0]) if operands else None
            if hasattr(ext, "get"):
                if "/ca" in ext:
                    gs.fill_alpha = _num(ext.get("/ca"), 1.0)
                if "/CA" in ext:
                    gs.stroke_alpha = _num(ext.get("/CA"), 1.0)
        elif op in _PATH_OPS:
            self._path(op, operands)
        elif op == b"W" or op == b"W*":
            self.pending_clip = True
        elif op in _ALL_PAINT:
            box = _bbox(self.path) if self.path else None
            if op in _FILL_PAINT and gs.fill_alpha > MIN_ALPHA:
                self.fills += 1
                self._paint(box, gs.fill)
            if self.pending_clip and box is not None:
                gs.clip = _intersect(gs.clip, box)
            self.pending_clip = False
            self.path = []
        elif op == b"Do":
            self._do(operands, resources, depth)
        elif op == b"INLINE IMAGE":
            box = self._unit_square()
            self.images.append(box)
            self._paint(box, None)
        elif op == b"sh":
            self._paint(gs.clip or self.page_box, None)
        elif op == b"BDC" or op == b"BMC":
            parent = bool(self.mc and self.mc[-1])
            self.mc.append(parent or (op == b"BDC" and self._layer_hidden(resources, operands)))
        elif op == b"EMC":
            if self.mc:
                self.mc.pop()

    def _path(self, op: bytes, operands: Any) -> None:
        m = self.gs.ctm
        if op == b"re":
            v = _nums(operands, 4)
            if v:
                x, y, w, h = v
                self.path.extend([_apply(m, x, y), _apply(m, x + w, y), _apply(m, x, y + h), _apply(m, x + w, y + h)])
        elif op != b"h":
            v = [_num(x) for x in (operands or [])]
            for i in range(0, len(v) - 1, 2):
                self.path.append(_apply(m, v[i], v[i + 1]))

    def _do(self, operands: Any, resources: Any, depth: int) -> None:
        if not operands:
            return
        xobj = _lookup(resources, "/XObject", operands[0])
        if not hasattr(xobj, "get"):
            return
        subtype = str(xobj.get("/Subtype", ""))
        if subtype == "/Image":
            box = self._unit_square()
            self.images.append(box)
            self._paint(box, None)
            return
        if subtype != "/Form" or depth >= MAX_FORM_DEPTH or id(xobj) in self.forms:
            return
        from pypdf.generic import ContentStream  # noqa: PLC0415

        matrix = _nums(list(_resolve(xobj.get("/Matrix")) or []), 6) or _IDENTITY
        form_resources = _resolve(xobj.get("/Resources")) or resources
        hidden = False
        if hasattr(xobj, "raw_get") and "/OC" in xobj:
            hidden = self._ocg_hidden(xobj.raw_get("/OC"))
        ops = ContentStream(xobj, self.reader, "bytes").operations
        saved = (self.gs, self.stack, self.tm, self.tlm, self.path, self.pending_clip)
        self.gs = self.gs.copy()
        self.gs.ctm = _mul(matrix, self.gs.ctm)
        self.stack, self.path, self.pending_clip = [], [], False
        self.mc.append(bool(self.mc and self.mc[-1]) or hidden)
        mc_depth = len(self.mc)
        self.forms.append(id(xobj))
        try:
            self.walk(ops, form_resources, page_level=False, depth=depth + 1)
        finally:
            self.forms.pop()
            del self.mc[mc_depth - 1:]
            self.gs, self.stack, self.tm, self.tlm, self.path, self.pending_clip = saved

    # ---- results -------------------------------------------------------------
    def _text_layer_page(self) -> bool:
        """No visible text, but the page is drawn as images / vector outlines: its invisible text is
        the page's text layer (OCR scan, "text to curves" export) rather than hidden text."""
        if self.visible_glyphs > TEXT_LAYER_MAX_VISIBLE or self.page_box is None:
            return False
        x0, y0, x1, y1 = self.page_box
        area = max((x1 - x0) * (y1 - y0), 1.0)
        covered = 0.0
        for box in self.images:
            clip = _intersect(box, self.page_box)
            if clip is not None:
                covered += max(0.0, clip[2] - clip[0]) * max(0.0, clip[3] - clip[1])
        return covered >= TEXT_LAYER_IMAGE_SHARE * area or self.fills >= TEXT_LAYER_MIN_FILLS

    def finish(self, page_no: int) -> PageScan:
        """Group consecutive hidden events into runs; OCR / text layers of drawn pages are visible."""
        scan = PageScan()
        runs: list[list[_Event]] = []
        current: list[_Event] = []
        layer_page = self._text_layer_page()
        for ev in self.events:
            if ev is not None and ev.reasons == [_REASON_RENDER] and any(
                    _contains(box, *ev.point) for box in self.images):
                ev = None                              # invisible OCR layer over a scanned page image
            if ev is not None and layer_page and set(ev.reasons) <= _LAYER_REASONS:
                ev = None                              # the only text of a page drawn as image / outlines
            if ev is None or (current and (current[-1].op_index is None) != (ev.op_index is None)):
                if current:
                    runs.append(current)
                current = []
            if ev is not None:
                current.append(ev)
        if current:
            runs.append(current)
        for run in runs:
            text = " ".join(" ".join(ev.decode() for ev in run).split())
            in_form = run[0].op_index is None
            scan.hidden_ops.extend(ev.op_index for ev in run if ev.op_index is not None)
            if not text:
                continue
            scan.texts.append(text)
            if in_form:
                scan.form_texts.append(text)
            if sum(ch.isalnum() for ch in text) < HIDDEN_MIN_ALNUM or len(scan.findings) >= MAX_FINDINGS:
                continue
            reasons = list(dict.fromkeys(r for ev in run for r in ev.reasons))
            words = len(text.split())
            detail = f"page {page_no}: {', '.join(reasons)}; {words} word{'s' if words != 1 else ''}"
            if _instruction_hits(text, hidden=True):
                detail += "; reads like an instruction to AI screeners"
            scan.findings.append({"kind": KIND_HIDDEN, "snippet": _clip(text, SNIPPET_MAX), "detail": detail})
        return scan


def scan_page(page: Any, ops: list[tuple[Any, bytes]], reader: Any = None, page_no: int = 1, *,
              budget: ScanBudget | None = None, off_ocgs: frozenset[int] | None = None) -> PageScan:
    """Hidden-text evidence for one pypdf page whose parsed operators are ``ops``.

    ``ops`` must come from ``ContentStream(contents, reader, "bytes")`` so string operands stay
    raw bytes. pypdf's decode limits are the caller's (resume_parser._pdf_limits); a form
    XObject that breaks them is skipped.
    """
    try:
        resources = _resolve(page.get("/Resources"))
    except Exception:  # noqa: BLE001
        resources = None
    walker = _Walker(reader, _page_box(page), off_ocgs or frozenset(), budget or ScanBudget())
    walker.walk(ops, resources, page_level=True)
    return walker.finish(page_no)


def neutralize(ops: list[tuple[Any, bytes]], indices: Iterable[int]) -> None:
    """Blank the strings shown by the operators at ``indices`` (text positioning is kept)."""
    from pypdf.generic import ArrayObject, ByteStringObject  # noqa: PLC0415

    for i in sorted(set(indices)):
        try:
            operands, op = ops[i]
            if op == b"TJ" and operands:
                operands[0] = ArrayObject()
            elif op in (b"Tj", b"'") and operands:
                operands[0] = ByteStringObject(b"")
            elif op == b'"' and len(operands) >= 3:
                operands[2] = ByteStringObject(b"")
        except (IndexError, TypeError, ValueError):
            continue


def scan_pdf_hidden_text(pdf_bytes: bytes, *, max_pages: int | None = None) -> list[dict[str, str]]:
    """Findings for invisible text in a PDF, bounded by the resume parser's safety limits."""
    if not isinstance(pdf_bytes, (bytes, bytearray)) or b"%PDF-" not in bytes(pdf_bytes[:1024]):
        return []
    from pypdf import PdfReader  # noqa: PLC0415
    from pypdf.generic import ContentStream  # noqa: PLC0415

    from .resume_parser import PDF_MAX_PAGES, _pdf_limits  # noqa: PLC0415 - import here avoids a cycle

    findings: list[dict[str, str]] = []
    limit = PDF_MAX_PAGES if max_pages is None else max_pages
    try:
        with _pdf_limits():
            reader = PdfReader(io.BytesIO(bytes(pdf_bytes)), strict=False)
            if reader.is_encrypted:
                reader.decrypt("")
            budget, off = ScanBudget(), hidden_ocgs(reader)
            for page_no, page in enumerate(reader.pages, start=1):
                if page_no > limit or budget.ops_left <= 0:
                    break
                contents = _resolve(page.get("/Contents"))
                if contents is None:
                    continue
                ops = ContentStream(contents, reader, "bytes").operations
                findings.extend(scan_page(page, ops, reader, page_no, budget=budget, off_ocgs=off).findings)
    except Exception:  # noqa: BLE001 - unreadable or oversized PDFs keep the findings found so far
        pass
    return findings[:MAX_FINDINGS]
