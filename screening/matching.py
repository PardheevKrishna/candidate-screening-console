"""Resume <-> TA candidate linkage.

* ``match_resumes(ta, parsed, cfg)`` -> one row per parsed resume: file_hash, file_name,
  candidate_id (str or NaN), match_method (S.MATCH_*), match_confidence (0..1), note.
* ``link_candidates(ta, parsed, matches, cfg)`` -> (base, parsed_by_candidate, issues).

Priority (first hit wins): TA resume_file == file name (1.0) -> TA candidate_id token in the
file name (1.0) -> resume email == TA email (0.99) -> TA candidate_id in the resume text
(0.97) -> phone last-10 (0.95) -> unique fuzzy name (token_sort_ratio >= 92, margin >= 5;
score/100 x 0.9). Every exact tier is a dict lookup built once; fuzzy matching only runs
for leftovers against a prebuilt choices list. When several resumes reach one candidate the
highest confidence wins (then more words, newer "_v2"/"(2)" version, file name); the rest
become MATCH_DUPLICATE. Notes never contain names, emails or phone numbers.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz.utils import default_process

from . import schema as S
from .config import AppConfig
from .ingest import canonicalize_spellings, clean_email, clean_name, normalize_candidate_id
from .models import ParsedResume

STAGE = "match"
FILE_HASH = "file_hash"
FILE_NAME = "file_name"
NOTE = "note"
MATCH_COLUMNS = [FILE_HASH, FILE_NAME, S.CANDIDATE_ID, S.MATCH_METHOD, S.MATCH_CONFIDENCE, NOTE]
LINK_COLUMNS = [S.RESUME_FILE, S.RESUME_HASH, S.HAS_RESUME, S.MATCH_METHOD, S.MATCH_CONFIDENCE]

CONFIDENCE = {
    S.MATCH_TA_FILE: 1.0,
    S.MATCH_ID_FILENAME: 1.0,
    S.MATCH_EMAIL: 0.99,
    S.MATCH_ID_TEXT: 0.97,
    S.MATCH_PHONE: 0.95,
    S.MATCH_RESUME_ONLY: 1.0,
}
SHARED_CONTACT_CONFIDENCE = 0.9   # email/phone shared by several TA candidates, resolved by name
NAME_MIN_SCORE = 92.0             # rapidfuzz token_sort_ratio
NAME_MIN_MARGIN = 5.0             # best - second best
NAME_CONFIDENCE_SCALE = 0.9
MIN_NUMERIC_ID_DIGITS = 5         # all-digit IDs shorter than this are never matched as tokens ("Resume (12)")
MAX_ID_SEGMENTS = 4
MAX_TEXT_SCAN_CHARS = 200_000
IDENTITY_CONFLICT = "identity conflict"
NO_ASSESSMENT_FLAG = "no_assessment"

_COMPOUND_RE = re.compile(r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*")
_SEGMENT_SPLIT_RE = re.compile(r"[-_/.]")
_DIGIT_RE = re.compile(r"\d")
_COUNTER_RE = re.compile(r"\s*\(\d{1,3}\)\s*$")
_VERSION_RE = re.compile(r"(?:^|[^a-z0-9])v(?:er(?:sion)?)?[\s_.-]?(\d{1,3})$|\((\d{1,3})\)$", re.I)
_FILENAME_NOISE = frozenset({
    "resume", "resumes", "cv", "curriculum", "vitae", "biodata", "profile", "final", "updated", "latest",
    "new", "copy", "draft", "version", "ver", "rev", "doc", "docx", "pdf", "txt", "file", "my", "the", "of",
    "and", "candidate", "application", "campus", "placement", "fresher", "scan", "scanned", "signed",
    "page", "img", "image", "sde", "swe", "intern", "internship",
})


# ---------------------------------------------------------------------------
# Index over the TA feed (built once per call)
# ---------------------------------------------------------------------------
def _id_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _usable_id_key(key: str) -> bool:
    """IDs findable as tokens: >= 3 chars, contain a digit, and all-digit IDs need >= 5 digits."""
    return len(key) >= 3 and bool(_DIGIT_RE.search(key)) and (not key.isdigit() or len(key) >= MIN_NUMERIC_ID_DIGITS)


def _name_tokens(name: Any) -> list[str]:
    """Lowercase alphabetic tokens, initials and digits dropped ("K. Asha Menon" -> [asha, menon])."""
    if not isinstance(name, str):
        return []
    return [t for t in default_process(name).split() if len(t) > 1 and t.isalpha()]


def _digits10(value: Any) -> str | None:
    """Last 10 digits of a phone value (str / int / integral float); None if fewer digits."""
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if value is None or isinstance(value, bool):
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits[-10:] if len(digits) >= 10 else None


@dataclass
class _TAIndex:
    id_keys: dict[str, str] = field(default_factory=dict)       # alnum key -> candidate_id
    max_segments: int = 2
    files: dict[str, list[str]] = field(default_factory=dict)   # lower file name / stem -> ids
    emails: dict[str, list[str]] = field(default_factory=dict)
    phones: dict[str, list[str]] = field(default_factory=dict)
    names: dict[str, list[str]] = field(default_factory=dict)   # sorted-token key -> ids
    compact: dict[str, list[str]] = field(default_factory=dict)  # tokens joined (both orders) -> ids
    name_keys: dict[str, str] = field(default_factory=dict)     # id -> sorted-token key
    choices: list[str] = field(default_factory=list)            # unique name keys (fuzzy search space)


def _add(index: dict[str, list[str]], key: str | None, cid: str) -> None:
    if key:
        bucket = index.setdefault(key, [])
        if cid not in bucket:
            bucket.append(cid)


def _column(ta: pd.DataFrame, name: str) -> list[Any]:
    return ta[name].tolist() if name in ta.columns else [None] * len(ta)


def _build_index(ta: pd.DataFrame) -> _TAIndex:
    idx = _TAIndex()
    key_owner: dict[str, str | None] = {}
    rows = zip(_column(ta, S.CANDIDATE_ID), _column(ta, S.RESUME_FILE), _column(ta, S.EMAIL),
               _column(ta, S.PHONE), _column(ta, S.NAME))
    for raw_id, resume_file, email, phone, name in rows:
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        cid = raw_id
        key = _id_key(cid)
        if _usable_id_key(key):
            key_owner[key] = cid if key_owner.get(key, cid) == cid else None  # ambiguous keys disabled
            segments = len(re.findall(r"[A-Za-z0-9]+", cid))
            idx.max_segments = max(idx.max_segments, min(MAX_ID_SEGMENTS, segments))
        if isinstance(resume_file, str) and resume_file.strip():
            base = re.split(r"[\\/]", resume_file.strip())[-1].lower()
            _add(idx.files, base, cid)
            _add(idx.files, Path(base).stem, cid)
        _add(idx.emails, clean_email(email), cid)
        _add(idx.phones, _digits10(phone), cid)
        tokens = _name_tokens(name)
        if len(tokens) >= 2:
            sorted_key = " ".join(sorted(tokens))
            idx.name_keys[cid] = sorted_key
            _add(idx.names, sorted_key, cid)
            _add(idx.compact, "".join(tokens), cid)
            _add(idx.compact, "".join(reversed(tokens)), cid)
    idx.id_keys = {k: v for k, v in key_owner.items() if v is not None}
    idx.choices = list(idx.names)
    return idx


# ---------------------------------------------------------------------------
# Per-resume signals
# ---------------------------------------------------------------------------
def _id_hits(text: str, idx: _TAIndex) -> list[str]:
    """TA candidate IDs appearing as tokens (any of - _ / . between parts), in order of appearance."""
    hits: list[str] = []
    if not idx.id_keys:
        return hits
    for m in _COMPOUND_RE.finditer(text):
        token = m.group(0)
        if not _DIGIT_RE.search(token):
            continue
        segs = _SEGMENT_SPLIT_RE.split(token.upper())
        for size in range(min(len(segs), idx.max_segments), 0, -1):
            for start in range(len(segs) - size + 1):
                cid = idx.id_keys.get("".join(segs[start:start + size]))
                if cid and cid not in hits:
                    hits.append(cid)
    return hits


def name_from_filename(file_name: str, split_camel: bool = True) -> str | None:
    """Best-effort person name from a resume file name ("Asha_Menon_Resume.docx" -> "asha menon").

    split_camel turns "AshaMenon_CV" into "asha menon" (callers also try it off: "McDonald").
    """
    stem = _COUNTER_RE.sub("", Path(str(file_name)).stem)
    if split_camel:
        stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)
    parts = [p for p in re.split(r"[\s_.]+", stem) if p and not _DIGIT_RE.search(p)]  # drop IDs, v2, years
    tokens = [t.lower() for p in parts for t in re.findall(r"[A-Za-z]+", p)]
    tokens = [t for t in tokens if len(t) > 1 and t not in _FILENAME_NOISE]
    return " ".join(tokens) or None


def _version(file_name: str) -> int:
    m = _VERSION_RE.search(Path(file_name).stem.strip())
    return int(m.group(1) or m.group(2)) if m else 0


@dataclass
class _Match:
    file_hash: str
    file_name: str
    candidate_id: str | None = None
    method: str = S.MATCH_NONE
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
    words: int = 0
    version: int = 0

    def row(self) -> dict[str, Any]:
        return {FILE_HASH: self.file_hash, FILE_NAME: self.file_name, S.CANDIDATE_ID: self.candidate_id,
                S.MATCH_METHOD: self.method, S.MATCH_CONFIDENCE: round(float(self.confidence), 4),
                NOTE: S.SEP_NOTES.join(self.notes)}


def _name_queries(p: ParsedResume) -> list[str]:
    queries = []
    for raw in (p.name_guess, name_from_filename(p.file_name), name_from_filename(p.file_name, split_camel=False)):
        tokens = _name_tokens(raw)
        if tokens and " ".join(tokens) not in queries:
            queries.append(" ".join(tokens))
    return queries


def _name_score(query: str, cid: str, idx: _TAIndex) -> float:
    key = idx.name_keys.get(cid)
    return float(fuzz.token_sort_ratio(query, key)) if key else 0.0


def _pick(cids: list[str], p: ParsedResume, idx: _TAIndex) -> tuple[str, bool]:
    """Single owner of a shared contact: best name similarity, then TA order. (cid, was_ambiguous)."""
    if len(cids) == 1:
        return cids[0], False
    queries = _name_queries(p)
    best = max(cids, key=lambda c: (max((_name_score(q, c, idx) for q in queries), default=0.0), -cids.index(c)))
    return best, True


def _match_name(p: ParsedResume, idx: _TAIndex) -> tuple[str, float] | None:
    """Unique name match: exact sorted-token / compact key, else fuzzy (>= 92, margin >= 5)."""
    best: tuple[str, float] | None = None
    for query in _name_queries(p):
        tokens = query.split()
        exact = idx.names.get(" ".join(sorted(tokens))) if len(tokens) >= 2 else idx.compact.get(query)
        if exact is not None:
            found = (exact[0], 100.0) if len(exact) == 1 else None
        elif len(tokens) >= 2 or len(query) >= 6:
            found = None
            res = process.extract(" ".join(sorted(tokens)), idx.choices, scorer=fuzz.token_sort_ratio,
                                  processor=None, limit=2, score_cutoff=NAME_MIN_SCORE - NAME_MIN_MARGIN)
            if res and res[0][1] >= NAME_MIN_SCORE and (len(res) < 2 or res[0][1] - res[1][1] >= NAME_MIN_MARGIN):
                owners = idx.names[res[0][0]]
                found = (owners[0], float(res[0][1])) if len(owners) == 1 else None
        else:
            found = None
        if found and (best is None or found[1] > best[1]):
            best = found
    return best


def _match_one(p: ParsedResume, idx: _TAIndex) -> _Match:
    file_name = Path(str(p.file_name)).name
    out = _Match(p.file_hash, file_name, words=int(p.word_count or 0), version=_version(file_name))
    stem = Path(file_name).stem
    owners = idx.files.get(file_name.lower()) or idx.files.get(stem.lower()) or []
    name_hits = _id_hits(stem.replace(" ", "_"), idx)
    email_ids = _unique(cid for e in _emails(p) for cid in idx.emails.get(e, []))
    phone_ids = _unique(cid for ph in _phones(p) for cid in idx.phones.get(ph, []))

    def settle(cid: str, method: str, confidence: float | None = None) -> None:
        out.candidate_id, out.method = cid, method
        out.confidence = CONFIDENCE[method] if confidence is None else confidence

    if len(owners) == 1:
        settle(owners[0], S.MATCH_TA_FILE)
    elif len(owners) > 1:
        out.notes.append("TA feed lists this file for several candidates")
    if out.candidate_id is None and name_hits:
        settle(name_hits[0], S.MATCH_ID_FILENAME)
    if len(name_hits) > 1:
        out.notes.append(f"several candidate IDs in file name ({S.SEP_LIST.join(name_hits)})")
    if out.candidate_id is None and email_ids:
        cid, shared = _pick(email_ids, p, idx)
        settle(cid, S.MATCH_EMAIL, SHARED_CONTACT_CONFIDENCE if shared else None)
        if shared:
            out.notes.append(f"resume email matches several candidates ({S.SEP_LIST.join(email_ids)})")
    if out.candidate_id is None:
        guess = normalize_candidate_id(p.candidate_id_guess)
        text_hits = [idx.id_keys[_id_key(guess)]] if guess and _id_key(guess) in idx.id_keys else []
        text_hits += [c for c in _id_hits((p.text or "")[:MAX_TEXT_SCAN_CHARS], idx) if c not in text_hits]
        if text_hits:
            settle(text_hits[0], S.MATCH_ID_TEXT)
    if out.candidate_id is None and phone_ids:
        cid, shared = _pick(phone_ids, p, idx)
        settle(cid, S.MATCH_PHONE, SHARED_CONTACT_CONFIDENCE if shared else None)
    if out.candidate_id is None:
        named = _match_name(p, idx)
        if named:
            # Name-only evidence is weak (common names collide). Reaching this point means the
            # resume's email/phone matched nobody; if the resume HAS one and the TA candidate
            # also has one on record, they disagree -> likely a different person. Don't link a
            # possibly-wrong resume; leave it for a recruiter to confirm.
            has_email = any(named[0] in ids for ids in idx.emails.values())
            has_phone = any(named[0] in ids for ids in idx.phones.values())
            if (_emails(p) and has_email) or (_phones(p) and has_phone):
                out.notes.append(f"possible name match {named[0]} not linked: resume email/phone "
                                 "differ from the TA record - confirm manually")
            else:
                settle(named[0], S.MATCH_NAME, round(named[1] / 100.0 * NAME_CONFIDENCE_SCALE, 4))

    cid = out.candidate_id
    if cid is None:
        out.notes.append("no TA identifier matched")
    else:
        if email_ids and cid not in email_ids:
            out.notes.append(f"{IDENTITY_CONFLICT}: resume email belongs to {email_ids[0]}")
        if out.method == S.MATCH_TA_FILE and name_hits and cid not in name_hits:
            out.notes.append(f"{IDENTITY_CONFLICT}: file name carries candidate ID {name_hits[0]}")
        if phone_ids and cid not in phone_ids and out.method != S.MATCH_PHONE:
            out.notes.append(f"resume phone matches {phone_ids[0]}")
    return out


def _mark_duplicates(results: list[_Match]) -> None:
    """Several resumes -> one candidate: keep the best, the others become MATCH_DUPLICATE."""
    groups: dict[str, list[_Match]] = {}
    for r in results:
        if r.candidate_id is not None and r.method not in (S.MATCH_NONE, S.MATCH_DUPLICATE):
            groups.setdefault(r.candidate_id, []).append(r)
    for cid, group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: (-r.confidence, -r.words, -r.version, r.file_name.lower(), r.file_hash))
        for r in group[1:]:
            r.method = S.MATCH_DUPLICATE
            r.notes.append(f"duplicate of {group[0].file_name} for {cid}")


def _sorted_resumes(parsed: dict[str, ParsedResume]) -> list[ParsedResume]:
    """Deterministic processing order regardless of parse (thread) completion order."""
    return sorted(parsed.values(), key=lambda p: (str(p.file_name).lower(), str(p.file_name), str(p.file_hash)))


def _filename_id(file_name: str, pattern: re.Pattern[str] | None) -> str | None:
    """cfg.processing.candidate_id_pattern on the file stem ("_" read as a separator), ignoring
    noise like "Resume2026" / "CV-2025"."""
    if pattern is None:
        return None
    for m in pattern.finditer(Path(file_name).stem.replace("_", " ")):
        letters = re.sub(r"[^A-Za-z].*$", "", m.group(0)).lower()  # leading letters: "CV2026" -> "cv"
        if letters not in _FILENAME_NOISE:
            return normalize_candidate_id(m.group(0))
    return None


def _resume_only_matches(resumes: list[ParsedResume], cfg: AppConfig) -> list[_Match]:
    """One candidate per resume: ID guess (or file-name ID) -> same email as another resume -> RES-<hash8>.

    Resumes sharing an ID guess merge (-> duplicates) unless their emails are disjoint, in which
    case they are kept as separate people rather than silently dropping one.
    """
    try:
        pattern: re.Pattern[str] | None = re.compile(cfg.processing.candidate_id_pattern)
    except (re.error, TypeError):
        pattern = None
    by_email: dict[str, str] = {}
    guess_emails: dict[str, set[str]] = {}
    results: list[_Match] = []
    pending: list[tuple[_Match, ParsedResume]] = []
    for p in resumes:
        file_name = Path(str(p.file_name)).name
        r = _Match(p.file_hash, file_name, method=S.MATCH_RESUME_ONLY, confidence=CONFIDENCE[S.MATCH_RESUME_ONLY],
                   words=int(p.word_count or 0), version=_version(file_name))
        results.append(r)
        emails = set(_emails(p))
        guess = normalize_candidate_id(p.candidate_id_guess) or _filename_id(file_name, pattern)
        known = guess_emails.get(guess or "")
        if guess and known and emails and not known & emails:
            r.notes.append(f"ID {guess} is also on a resume with a different email; kept as a separate candidate")
            guess = None
        if guess:
            r.candidate_id = guess
            guess_emails.setdefault(guess, set()).update(emails)
            for email in sorted(emails):
                by_email.setdefault(email, guess)
        else:
            pending.append((r, p))
    for r, p in pending:  # resumes without an ID: same email => same person
        emails = _emails(p)
        known = next((by_email[e] for e in emails if e in by_email), None)
        r.candidate_id = known or f"RES-{str(p.file_hash)[:8].upper()}"
        for e in emails:
            by_email.setdefault(e, r.candidate_id)
    return results


def _emails(p: ParsedResume) -> list[str]:
    return _unique(e for e in (clean_email(x) for x in (p.emails or [])) if e)


def _phones(p: ParsedResume) -> list[str]:
    return _unique(d for d in (_digits10(x) for x in (p.phones or [])) if d)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def match_resumes(ta: pd.DataFrame | None, parsed: dict[str, ParsedResume], cfg: AppConfig) -> pd.DataFrame:
    """Link every parsed resume to at most one TA candidate (see module docstring)."""
    resumes = _sorted_resumes(parsed or {})
    if ta is None:
        results = _resume_only_matches(resumes, cfg)
    else:
        idx = _build_index(ta)
        results = [_match_one(p, idx) for p in resumes]
    _mark_duplicates(results)
    frame = pd.DataFrame([r.row() for r in results], columns=MATCH_COLUMNS)
    for c in (FILE_HASH, FILE_NAME, S.CANDIDATE_ID, S.MATCH_METHOD, NOTE):
        frame[c] = frame[c].astype("str")
    frame[S.MATCH_CONFIDENCE] = frame[S.MATCH_CONFIDENCE].astype("float64")
    return frame


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------
def _issue(severity: str, issue: str, candidate_id: str | None = None, file_name: str | None = None) -> dict[str, Any]:
    return {"severity": severity, "stage": STAGE, "issue": issue, "candidate_id": candidate_id, "file_name": file_name}


def _text_or_none(value: Any) -> str | None:
    return " ".join(value.split()) or None if isinstance(value, str) else None


def _number_or_nan(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _winners(matches: pd.DataFrame, parsed: dict[str, ParsedResume], valid_ids: set[str] | None,
             issues: list[dict[str, Any]]) -> dict[str, _Match]:
    """candidate_id -> linked resume, re-validating the (possibly user-edited) matches table."""
    linked: dict[str, list[_Match]] = {}
    for rec in matches.to_dict("records"):
        file_hash = _text_or_none(rec.get(FILE_HASH)) or ""
        file_name = _text_or_none(rec.get(FILE_NAME)) or ""
        cid = _text_or_none(rec.get(S.CANDIDATE_ID))
        method = _text_or_none(rec.get(S.MATCH_METHOD)) or S.MATCH_NONE
        note = rec.get(NOTE) if isinstance(rec.get(NOTE), str) else ""
        p = parsed.get(file_hash)
        if p is None:
            issues.append(_issue("warning", "Match row refers to a resume that was not parsed; skipped.",
                                 cid, file_name or None))
            continue
        if IDENTITY_CONFLICT in note:
            issues.append(_issue("warning", f"Possible identity conflict ({note}); verify the resume owner.",
                                 cid, file_name))
        if method == S.MATCH_DUPLICATE:
            issues.append(_issue("info", "Duplicate resume for this candidate; another file was linked.",
                                 cid, file_name))
            continue
        if method == S.MATCH_NONE or cid is None:
            issues.append(_issue("warning", "Resume could not be linked to any TA candidate.", None, file_name))
            continue
        if valid_ids is not None and cid not in valid_ids:
            issues.append(_issue("warning", "Resume matched an ID that is not in the TA feed; ignored.",
                                 cid, file_name))
            continue
        confidence = rec.get(S.MATCH_CONFIDENCE)
        confidence = float(confidence) if isinstance(confidence, (int, float)) and not math.isnan(confidence) else 0.0
        linked.setdefault(cid, []).append(_Match(file_hash, file_name or Path(str(p.file_name)).name, cid, method,
                                                 confidence, words=int(p.word_count or 0),
                                                 version=_version(file_name)))
    winners: dict[str, _Match] = {}
    for cid, group in linked.items():
        group.sort(key=lambda r: (-r.confidence, -r.words, -r.version, r.file_name.lower(), r.file_hash))
        winners[cid] = group[0]
        for r in group[1:]:
            issues.append(_issue("info", "Duplicate resume for this candidate; another file was linked.",
                                 cid, r.file_name))
    return winners


def _ta_base(ta: pd.DataFrame, winners: dict[str, _Match], parsed_any: bool,
             issues: list[dict[str, Any]]) -> pd.DataFrame:
    base = ta.reset_index(drop=True).copy()
    ids = base[S.CANDIDATE_ID] if S.CANDIDATE_ID in base.columns else pd.Series([None] * len(base), dtype="str")
    ids = ids.astype("str")
    won = {cid: m for cid, m in winners.items()}
    base[S.RESUME_FILE] = pd.Series([won[c].file_name if c in won else "" for c in ids], dtype="str")
    base[S.RESUME_HASH] = pd.Series([won[c].file_hash if c in won else "" for c in ids], dtype="str")
    base[S.HAS_RESUME] = pd.Series([c in won for c in ids], dtype="bool")
    base[S.MATCH_METHOD] = pd.Series([won[c].method if c in won else "" for c in ids], dtype="str")
    base[S.MATCH_CONFIDENCE] = pd.Series([won[c].confidence if c in won else 0.0 for c in ids], dtype="float64")

    missing = [c for c in ids.tolist() if c not in won]
    if missing and not parsed_any:
        issues.append(_issue("info", f"No resumes provided; all {len(missing):,} TA candidates are scored "
                                     f"without resume data."))
    elif missing:
        issues.append(_issue("info", f"{len(missing):,} of {len(ids):,} TA candidates have no matching resume."))
        listed = dict(zip(ids.tolist(), _column(ta.reset_index(drop=True), S.RESUME_FILE)))
        for cid in missing:
            listed_file = isinstance(listed.get(cid), str)
            hint = " (the TA feed names a resume file that was not uploaded)" if listed_file else ""
            issues.append(_issue("info", f"No resume matched this candidate{hint}.", cid))
    tail = {S.DATA_FLAGS} | {c for c in base.columns if c.startswith("extra_")}
    head = [c for c in base.columns if c not in tail and c not in LINK_COLUMNS]
    return base[head + LINK_COLUMNS + [c for c in base.columns if c in tail]]


def _resume_only_base(winners: dict[str, _Match], parsed: dict[str, ParsedResume],
                      issues: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for cid, m in sorted(winners.items(), key=lambda kv: (kv[1].file_name.lower(), kv[0])):
        p = parsed[m.file_hash]
        edu = p.education if isinstance(p.education, dict) else {}
        email = next(iter(_emails(p)), None)
        phone = next(iter(_phones(p)), None)
        cgpa = _number_or_nan(edu.get("cgpa"))
        year = _number_or_nan(edu.get("grad_year"))
        rows.append({
            S.CANDIDATE_ID: cid, S.EMAIL: email, S.PHONE: phone,
            S.NAME: clean_name(p.name_guess) or clean_name(name_from_filename(m.file_name)),
            S.COLLEGE: _text_or_none(edu.get("college")), S.DEGREE: _text_or_none(edu.get("degree")),
            S.BRANCH: _text_or_none(edu.get("branch")),
            S.GRAD_YEAR: int(year) if 1980 <= year <= 2100 else None,
            S.CGPA: cgpa if 0 < cgpa <= 10 else np.nan,
            S.HR_SCORE: np.nan, S.MAX_SCORE: np.nan, S.HR_PCT: np.nan,
            S.EMAIL_DOMAIN: email.split("@")[-1] if email else None,
            S.RESUME_FILE: m.file_name, S.RESUME_HASH: m.file_hash, S.HAS_RESUME: True,
            S.MATCH_METHOD: S.MATCH_RESUME_ONLY, S.MATCH_CONFIDENCE: CONFIDENCE[S.MATCH_RESUME_ONLY],
            S.DATA_FLAGS: NO_ASSESSMENT_FLAG,
        })
    columns = [S.CANDIDATE_ID, S.NAME, S.EMAIL, S.PHONE, S.COLLEGE, S.DEGREE, S.BRANCH, S.GRAD_YEAR, S.CGPA,
               S.HR_SCORE, S.MAX_SCORE, S.HR_PCT, S.EMAIL_DOMAIN, *LINK_COLUMNS, S.DATA_FLAGS]
    base = pd.DataFrame(rows, columns=columns)
    for c in (S.CANDIDATE_ID, S.NAME, S.EMAIL, S.PHONE, S.COLLEGE, S.DEGREE, S.BRANCH, S.EMAIL_DOMAIN,
              S.RESUME_FILE, S.RESUME_HASH, S.MATCH_METHOD, S.DATA_FLAGS):
        base[c] = base[c].astype("str")
    for c in (S.COLLEGE, S.DEGREE, S.BRANCH):
        base[c] = canonicalize_spellings(base[c])
    base[S.GRAD_YEAR] = base[S.GRAD_YEAR].astype("Int64")
    for c in (S.CGPA, S.HR_SCORE, S.MAX_SCORE, S.HR_PCT, S.MATCH_CONFIDENCE):
        base[c] = base[c].astype("float64")
    base[S.HAS_RESUME] = base[S.HAS_RESUME].astype("bool")
    issues.append(_issue("info", f"Resume-only mode: {len(base):,} candidate(s) created from resumes "
                                 f"(no assessment scores)."))
    return base


def link_candidates(ta: pd.DataFrame | None, parsed: dict[str, ParsedResume],
                    matches: pd.DataFrame, cfg: AppConfig
                    ) -> tuple[pd.DataFrame, dict[str, ParsedResume], list[dict[str, Any]]]:
    """Attach the linked resume to every candidate.

    TA mode: base = TA rows + resume_file, resume_hash, has_resume, match_method,
    match_confidence (unlinked: "", "", False, "", 0.0). Resume-only mode (ta is None):
    one candidate per linked resume with name/email/phone/education from the resume and
    NaN assessment scores (data_flags "no_assessment"). Never raises on bad rows: problems
    become issues ({"severity","stage","issue","candidate_id","file_name"}).
    """
    parsed = parsed or {}
    issues: list[dict[str, Any]] = []
    if matches is None or not len(matches):
        matches = pd.DataFrame(columns=MATCH_COLUMNS)
    valid_ids = None
    if ta is not None:
        valid_ids = set(ta[S.CANDIDATE_ID].dropna().astype(str)) if S.CANDIDATE_ID in ta.columns else set()
    winners = _winners(matches, parsed, valid_ids, issues)
    if ta is None:
        base = _resume_only_base(winners, parsed, issues)
    else:
        base = _ta_base(ta, winners, bool(parsed), issues)
    parsed_by_candidate = {cid: parsed[m.file_hash] for cid, m in winners.items()}
    return base, parsed_by_candidate, issues
