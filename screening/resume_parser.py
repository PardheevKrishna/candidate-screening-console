"""Resume ingestion: safe upload/ZIP expansion, text extraction and structured parsing.

Flow: ``expand_uploads`` (in-memory ZIP expansion with size / member / zip-bomb guards)
-> ``parse_resumes`` (thread pool, de-duplicated by content hash) -> ``parse_resume``
(``extract_text`` + ``parse_resume_text``).

Nothing here raises for a bad file. Upload problems become DataQualityReport-style
issue dicts; extraction / parsing problems become ``ParsedResume.parse_status``,
``parse_error`` and ``warnings``. No resume content is ever logged.
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from . import schema as S
from . import skills
from .config import ProcessingConfig
from .models import ParsedResume, ResumeDoc

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "ARCHIVE_EXTENSIONS",
    "PRESENT_DATE",
    "expand_uploads",
    "load_resume_folder",
    "extract_text",
    "parse_resume_text",
    "parse_resume",
    "parse_resumes",
]

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt"}
ARCHIVE_EXTENSIONS = {".zip"}
PRESENT_DATE = date(2026, 11, 1)   # what "Present" / "Current" means in date ranges
MIN_TEXT_CHARS = 30                # less extracted text than this -> parse_status "empty"
ZIP_MAX_RATIO = 200.0              # uncompressed / compressed above this -> zip-bomb guard
PDF_FALLBACK_CHARS = 100           # pypdf output shorter than this -> retry with pdfplumber

_MB = 1024 * 1024
_STAGE_UPLOAD = "upload"
_JUNK_FILE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
_OCR_HINT = "No extractable text: the PDF looks image-only/scanned - OCR is required"

ProgressCallback = Callable[[int, int, str], None]


# ===========================================================================
# Uploads, ZIP archives and folders
# ===========================================================================
def _issue(severity: str, issue: str, file_name: str | None) -> dict[str, Any]:
    return {"severity": severity, "stage": _STAGE_UPLOAD, "issue": issue,
            "candidate_id": None, "file_name": file_name}


def _basename(name: str) -> str:
    """Basename of a path from any OS ('a\\b/c.pdf' -> 'c.pdf')."""
    raw = str(name or "").replace("\\", "/")
    return PurePosixPath(raw).name or raw.strip("/")


def _is_junk_name(name: str) -> bool:
    return name.startswith((".", "~$")) or name.lower() in _JUNK_FILE_NAMES


def _unsupported_message(ext: str) -> str:
    if ext in {".doc", ".rtf", ".odt", ".pages"}:
        return f"Unsupported document format '{ext}' - save it as .docx or PDF"
    supported = ", ".join(sorted(SUPPORTED_EXTENSIONS | ARCHIVE_EXTENSIONS))
    return f"Unsupported file type '{ext or '(none)'}' skipped (supported: {supported})"


def expand_uploads(docs: list[ResumeDoc], cfg: ProcessingConfig) -> tuple[list[ResumeDoc], list[dict[str, Any]]]:
    """Expand ZIP archives in memory and filter uploads down to parseable resumes.

    Returns (docs, issues). Unsupported or oversized files are skipped with an issue;
    file names are reduced to their basename. Never raises.
    """
    out: list[ResumeDoc] = []
    issues: list[dict[str, Any]] = []
    max_file = cfg.max_file_mb * _MB
    for doc in docs or []:
        name = _basename(getattr(doc, "file_name", ""))
        content = getattr(doc, "content", b"") or b""
        ext = Path(name).suffix.lower()
        if not name or _is_junk_name(name):
            issues.append(_issue("info", "Skipped hidden/system file", name or None))
        elif ext in ARCHIVE_EXTENSIONS:
            out.extend(_expand_zip(name, content, cfg, issues))
        elif ext not in SUPPORTED_EXTENSIONS:
            issues.append(_issue("warning", _unsupported_message(ext), name))
        elif len(content) > max_file:
            issues.append(_issue("warning", f"File is larger than {cfg.max_file_mb:g} MB "
                                            f"({len(content) / _MB:.1f} MB) - skipped", name))
        else:
            out.append(doc if doc.file_name == name else ResumeDoc(name, content, doc.source))
    return out, issues


def _expand_zip(archive: str, content: bytes, cfg: ProcessingConfig,
                issues: list[dict[str, Any]]) -> list[ResumeDoc]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
        infos = zf.infolist()
    except Exception as exc:  # BadZipFile, truncated, not a zip at all, ...
        issues.append(_issue("error", f"Could not open ZIP archive ({type(exc).__name__})", archive))
        return []

    out: list[ResumeDoc] = []
    source = f"zip:{archive}"
    max_file = cfg.max_file_mb * _MB
    max_total = cfg.max_zip_total_mb * _MB
    total_bytes = 0
    members = 0
    with zf:
        for info in infos:
            path = info.filename.replace("\\", "/")
            parts = [p for p in path.split("/") if p not in ("", ".", "..")]
            if info.is_dir() or path.endswith("/") or not parts:
                continue
            name = parts[-1]
            if any(p == "__MACOSX" or p.startswith(".") for p in parts) or _is_junk_name(name):
                continue
            ext = Path(name).suffix.lower()
            if ext in ARCHIVE_EXTENSIONS:
                issues.append(_issue("info", f"Nested ZIP inside '{archive}' was not expanded", name))
                continue
            if ext not in SUPPORTED_EXTENSIONS:
                issues.append(_issue("info", f"Skipped unsupported file inside '{archive}': "
                                             f"{_unsupported_message(ext)}", name))
                continue
            members += 1
            if members > cfg.max_zip_members:
                issues.append(_issue("warning", f"'{archive}' has more than {cfg.max_zip_members:,} resumes; "
                                                "the rest were skipped", archive))
                break
            if info.flag_bits & 0x1:
                issues.append(_issue("warning", f"Encrypted file inside '{archive}' skipped", name))
                continue
            if info.file_size > max_file:
                issues.append(_issue("warning", f"File is larger than {cfg.max_file_mb:g} MB - skipped", name))
                continue
            if info.file_size and info.file_size / max(info.compress_size, 1) > ZIP_MAX_RATIO:
                issues.append(_issue("warning", "Suspicious compression ratio (possible zip bomb) - skipped", name))
                continue
            if total_bytes + info.file_size > max_total:
                issues.append(_issue("warning", f"'{archive}' exceeds {cfg.max_zip_total_mb:g} MB uncompressed; "
                                                "the remaining files were skipped", archive))
                break
            try:
                with zf.open(info) as fh:
                    data = fh.read(int(max_file) + 1)  # bounded even if the header lies
            except Exception as exc:
                issues.append(_issue("warning", f"Could not read file from '{archive}' ({type(exc).__name__})", name))
                continue
            if len(data) > max_file:
                issues.append(_issue("warning", f"File is larger than {cfg.max_file_mb:g} MB - skipped", name))
                continue
            total_bytes += len(data)
            out.append(ResumeDoc(name, data, source))
    return out


def load_resume_folder(path: str | Path) -> list[ResumeDoc]:
    """Read resumes (and .zip archives) under ``path`` recursively, sorted by relative path.

    Hidden files/folders and ``__MACOSX`` are skipped; unreadable files are ignored.
    A path to a single file returns just that file. Missing paths return [].
    """
    root = Path(path)
    if root.is_file():
        candidates = [(root.name, root)]
    elif root.is_dir():
        candidates = [(p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file()]
    else:
        return []
    allowed = SUPPORTED_EXTENSIONS | ARCHIVE_EXTENSIONS
    docs: list[ResumeDoc] = []
    for rel, p in sorted(candidates, key=lambda rp: (rp[0].lower(), rp[0])):
        parts = rel.split("/")
        if any(part.startswith(".") or part == "__MACOSX" for part in parts) or _is_junk_name(p.name):
            continue
        if p.suffix.lower() not in allowed:
            continue
        try:
            docs.append(ResumeDoc(p.name, p.read_bytes(), "folder"))
        except OSError:
            continue
    return docs


# ===========================================================================
# Text extraction
# ===========================================================================
def extract_text(doc: ResumeDoc) -> tuple[str, list[str]]:
    """(raw text, warnings) for a .pdf / .docx / .txt resume. Never raises.

    Read failures are reported as the first warning and yield "".
    """
    text, warnings, error = _extract(doc)
    return text, ([error] if error else []) + warnings


def _sniff_kind(content: bytes, ext: str) -> str:
    """Trust the file signature over the extension for PDF <-> DOCX mix-ups."""
    is_pdf = b"%PDF-" in content[:1024]
    if ext == ".docx" and is_pdf:
        return ".pdf"
    if ext == ".pdf" and not is_pdf and content.startswith(b"PK\x03\x04"):
        return ".docx"
    return ext


def _extract(doc: ResumeDoc) -> tuple[str, list[str], str | None]:
    """(text, warnings, error). ``error`` is set only when the file could not be read."""
    try:
        content = doc.content or b""
        ext = Path(doc.file_name or "").suffix.lower()
    except Exception:
        return "", [], "Invalid document object"
    if not content:
        return "", ["File is empty (0 bytes)"], None
    if ext not in SUPPORTED_EXTENSIONS:
        return "", [], _unsupported_message(ext)
    kind = _sniff_kind(content, ext)
    try:
        if kind == ".pdf":
            return _pdf_text(content)
        if kind == ".docx":
            return _docx_text(content)
        return _decode_text(content), [], None
    except Exception as exc:  # defensive: extractor bugs must not escape
        return "", [], f"Could not extract text ({type(exc).__name__})"


def _pdf_text(content: bytes) -> tuple[str, list[str], str | None]:
    if b"%PDF-" not in content[:1024]:
        # Checked here so pypdf never logs "invalid pdf header: <first bytes of the file>".
        return "", [], "Not a valid PDF file (missing %PDF header) - the file may be corrupted or mislabelled"
    warnings: list[str] = []
    error: str | None = None
    text = ""
    links: list[str] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return "", [], "PDF is password-protected"
        pages: list[str] = []
        for i, page in enumerate(reader.pages, start=1):
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                warnings.append(f"Could not extract text from PDF page {i}")
            links.extend(_pdf_page_links(page))
        text = "\n".join(pages)
    except Exception as exc:
        error = f"Could not read PDF ({type(exc).__name__}) - the file may be corrupted"

    if len(text.strip()) < PDF_FALLBACK_CHARS:
        alt = _pdfplumber_text(content)
        ours, theirs = _alnum_count(text), _alnum_count(alt)
        if theirs > ours and (ours < 30 or theirs > 1.5 * ours):
            text, error = alt, None
    if not text.strip() and error is None:
        warnings.append(_OCR_HINT)
    return _append_links(text, links), warnings, (error if not text.strip() else None)


def _alnum_count(text: str) -> int:
    return sum(ch.isalnum() for ch in _CID_RE.sub("", text))


def _pdfplumber_text(content: bytes) -> str:
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception:
        return ""


def _pdf_page_links(page: Any) -> list[str]:
    """URIs of link annotations (resumes often hide GitHub/LinkedIn URLs behind link text)."""
    out: list[str] = []
    try:
        annots = page.get("/Annots")
        annots = annots.get_object() if hasattr(annots, "get_object") else annots
        for ref in annots or []:
            annot = ref.get_object()
            action = annot.get("/A")
            action = action.get_object() if hasattr(action, "get_object") else action
            uri = action.get("/URI") if action else None
            if isinstance(uri, bytes):
                uri = uri.decode("latin-1", "ignore")
            if uri:
                out.append(str(uri))
    except Exception:
        pass
    return out[:50]


def _append_links(text: str, links: list[str]) -> str:
    """Append link targets that do not already appear in the visible text."""
    lower = text.lower()
    extra: list[str] = []
    for link in links:
        link = link.strip()
        bare = re.sub(r"^(?:https?://|mailto:)", "", link, flags=re.IGNORECASE).rstrip("/").lower()
        if bare and bare not in lower and link not in extra:
            extra.append(re.sub(r"^mailto:", "", link, flags=re.IGNORECASE))
    return text + ("\n\nHyperlinks\n" + "\n".join(extra) if extra else "")


def _docx_text(content: bytes) -> tuple[str, list[str], str | None]:
    try:
        import docx
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        from docx.oxml.ns import qn

        document = docx.Document(io.BytesIO(content))
    except Exception as exc:
        return "", [], f"Could not read DOCX ({type(exc).__name__}) - the file may be corrupted"

    warnings: list[str] = []
    head: list[str] = []
    body: list[str] = []
    boxes: list[str] = []
    foot: list[str] = []
    seen_hf: set[str] = set()
    try:
        for section in document.sections:
            for part, bucket in ((section.first_page_header, head), (section.header, head),
                                 (section.footer, foot), (section.first_page_footer, foot)):
                if part.is_linked_to_previous:  # no own definition; reading it would create one
                    continue
                lines: list[str] = []
                _docx_blocks(part, lines)
                block = "\n".join(lines).strip()
                if block and block not in seen_hf:
                    seen_hf.add(block)
                    bucket.extend(lines)
    except Exception:
        warnings.append("Could not read DOCX headers/footers")
    try:
        _docx_blocks(document, body)
    except Exception:
        warnings.append("Part of the DOCX body could not be read")
    try:  # text boxes (common in template resumes) are invisible to python-docx's API
        seen_box: set[str] = set()
        for txbx in document.element.body.iter(qn("w:txbxContent")):
            for p in txbx.iter(qn("w:p")):
                t = "".join(node.text or "" for node in p.iter(qn("w:t"))).strip()
                if t and t not in seen_box:
                    seen_box.add(t)
                    boxes.append(t)
    except Exception:
        pass
    links: list[str] = []
    try:
        for rel in document.part.rels.values():
            if rel.reltype == RT.HYPERLINK and rel.is_external:
                links.append(rel.target_ref)
    except Exception:
        pass
    text = "\n".join(head + body + boxes + foot)
    return _append_links(text, links), warnings, None


def _docx_blocks(container: Any, out: list[str]) -> None:
    """Paragraphs and tables in document order; list paragraphs get a bullet glyph."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for block in container.iter_inner_content():
        if isinstance(block, Paragraph):
            t = block.text or ""
            out.append(f"• {t.strip()}" if t.strip() and _is_list_paragraph(block) else t)
        elif isinstance(block, Table):
            _docx_table(block, out)


def _is_list_paragraph(p: Any) -> bool:
    """Numbered/bulleted paragraph. Reads the raw style id: ``Paragraph.style`` resolves
    through the styles part and costs ~1 ms per paragraph."""
    try:
        ppr = p._p.pPr
        if ppr is not None and ppr.numPr is not None:
            return True
        return "list" in (p._p.style or "").lower()
    except Exception:
        return False


def _docx_table(table: Any, out: list[str]) -> None:
    """Short single-line cells of a row -> one 'a | b | c' line; otherwise cell by cell."""
    seen: list[Any] = []  # merged cells repeat the same <w:tc>; compare by identity
    for row in table.rows:
        try:
            cells = row.cells
        except Exception:
            continue
        row_lines: list[list[str]] = []
        for cell in cells:
            tc = cell._tc
            if any(tc is s for s in seen):
                continue
            seen.append(tc)
            lines: list[str] = []
            _docx_blocks(cell, lines)
            lines = [ln for ln in lines if ln.strip()]
            if lines:
                row_lines.append(lines)
        if len(row_lines) >= 2 and all(len(c) == 1 and "\n" not in c[0] and len(c[0]) <= 120 for c in row_lines):
            out.append(" | ".join(c[0].strip() for c in row_lines))
        else:
            for c in row_lines:
                out.extend(c)


def _decode_text(content: bytes) -> str:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return content.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1", errors="replace")  # pragma: no cover - latin-1 never fails


# ===========================================================================
# Text normalisation
# ===========================================================================
# Private-use code points that Symbol/Wingdings fonts use for bullets -> real bullet.
_PUA_BULLETS = (0xF0B7, 0xF0A7, 0xF076, 0xF0D8, 0xF0FC, 0xF0E8, 0xF0A8, 0xF06E, 0xF0AE, 0xF0FB, 0xF02D, 0xF0A2)
_TRANSLATE = {
    **dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None),
    **{cp: "•" for cp in _PUA_BULLETS},
    0x7F: "•",   # DEL / cp1252 0x95: how several PDF font encodings surface the bullet glyph
    0x95: "•",
}
_PUA_RE = re.compile("[\ue000-\uf8ff]")
_CID_RE = re.compile(r"\(cid:\d+\)")   # pdfminer's placeholder for glyphs without a unicode mapping
_LINEBREAK_RE = re.compile(r"\r\n?|[\f\v\x1c-\x1e\x85\u2028\u2029]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_COLUMN_GAP_RE = re.compile(r"\t+| {3,}")
_SPACES_RE = re.compile(r"[ \t]+")
_MULTI_PIPE_RE = re.compile(r"(?:\s*\|\s*){2,}")
_GLYPHS = "•●▪‣◦⁃∙·⦁○◆◇■□►▶➢➤⮚✓✔❖♦★☆✦⚫⬤→➔"
_GLYPH_PIPE_RE = re.compile(rf"^([{_GLYPHS}*>–—-])\s*\|\s*")


def _normalize_text(raw: str) -> str:
    """NFKC, strip invisible/icon characters, unify line breaks, collapse spaces.

    Tabs and wide gaps (3+ spaces, typical of multi-column layouts and DOCX tab
    stops) become ' | ' so column boundaries survive. Line breaks are kept; runs of
    blank lines collapse to one.
    """
    text = unicodedata.normalize("NFKC", raw or "").translate(_TRANSLATE)
    text = _CID_RE.sub("•", _PUA_RE.sub(" ", text))
    text = _LINEBREAK_RE.sub("\n", text)
    text = _CTRL_RE.sub(" ", text)
    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = _COLUMN_GAP_RE.sub(" | ", raw_line)
        line = _MULTI_PIPE_RE.sub(" | ", _SPACES_RE.sub(" ", line)).strip(" |")
        line = _GLYPH_PIPE_RE.sub(r"\1 ", line)
        if line.startswith("\ufffd"):  # undecodable glyph opening a line is almost always a bullet
            line = "•" + line[1:]
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


# ===========================================================================
# Section detection
# ===========================================================================
_SECTION_HEADINGS: dict[str, tuple[str, ...]] = {
    "summary": (
        "summary", "profile", "objective", "objectives", "career objective", "profile summary",
        "professional summary", "career summary", "professional objective", "about me",
        "personal statement", "executive summary", "career profile", "professional profile",
    ),
    "education": (
        "education", "academic qualifications", "academic qualification", "academics", "qualifications",
        "qualification", "educational qualifications", "educational qualification", "education details",
        "academic details", "academic background", "educational background", "academic profile",
        "scholastic profile", "education and training", "academic record", "education and certifications",
    ),
    "skills": (
        "skills", "technical skills", "core competencies", "tech stack", "key skills", "skill set", "skillset",
        "skills summary", "technical expertise", "technical proficiency", "technical proficiencies",
        "areas of expertise", "it skills", "computer skills", "programming skills", "software skills",
        "technical skills and tools", "skills and tools", "skills and interests", "skills and abilities",
        "core skills", "professional skills", "relevant skills", "soft skills", "technologies",
        "tools and technologies", "technical summary", "competencies", "expertise", "tools",
    ),
    "projects": (
        "projects", "academic projects", "key projects", "personal projects", "project experience",
        "project work", "major projects", "mini projects", "notable projects", "technical projects",
        "selected projects", "relevant projects", "project details", "academic project", "projects undertaken",
        "capstone project", "final year project", "side projects", "project", "projects and research",
    ),
    "experience": (
        "experience", "work experience", "internships", "internship experience", "professional experience",
        "employment", "internship", "employment history", "work history", "industry experience",
        "relevant experience", "experience and internships", "internships and training", "industrial training",
        "internship and training", "training and internships", "industrial experience", "research experience",
        "professional background",
    ),
    "certifications": (
        "certifications", "certifications and courses", "licenses and certifications", "courses", "certificates",
        "certification", "certifications and trainings", "online courses", "trainings", "training",
        "courses and certifications", "professional certifications", "certifications and achievements",
        "moocs", "licenses",
    ),
    "achievements": (
        "achievements", "awards", "honors", "honours", "accomplishments", "extra curricular", "extracurricular",
        "extra curricular activities", "extracurricular activities", "co curricular activities",
        "awards and achievements", "honors and awards", "honours and awards", "achievements and awards",
        "achievements and activities", "activities", "positions of responsibility", "position of responsibility",
        "leadership", "leadership experience", "hackathons", "competitions", "scholarships",
        "awards and recognition", "recognition",
    ),
    "other": (
        "hobbies", "interests", "hobbies and interests", "languages", "languages known", "personal details",
        "personal information", "personal profile", "personal data", "declaration", "references", "strengths",
        "additional information", "volunteering", "volunteer experience", "publications", "links", "profiles",
        "online profiles", "social links", "hyperlinks", "contact", "contact information", "contact details",
        "other information", "miscellaneous", "extra information", "address",
    ),
}
# Generic words that are headings only when standing alone outside entry-style sections
# ("Tech Stack:" inside a project, "Training" inside experience, "Courses" in education).
_WEAK_HEADINGS = {
    "profile", "tools", "technologies", "competencies", "expertise", "tech stack", "courses", "training",
    "trainings", "activities", "leadership", "recognition", "project", "internship", "languages", "links",
    "profiles", "contact", "address", "interests", "strengths", "licenses",
}
_ENTRY_SECTIONS = {"projects", "experience", "education"}


def _norm_heading(s: str) -> str:
    s = s.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


_HEADING_KEY: dict[str, str] = {
    _norm_heading(h): key for key, heads in _SECTION_HEADINGS.items() for h in heads
}
_HEADING_DECOR_RE = re.compile(rf"^[\s#*=_~{_GLYPHS}:|>\-–—]+|[\s#*=_~{_GLYPHS}:|<\-–—]+$")
_HEADING_NUM_RE = re.compile(r"^(?:\d{1,2}|[ivx]{1,4})[.)]\s+", re.IGNORECASE)
_SPACED_CAPS_RE = re.compile(r"(?:[A-Za-z] )+[A-Za-z]")
_INLINE_HEADING_RE = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z &/'-]{1,45}?)\s*(?::|\s[|–—-]\s|\|)\s*(?P<rest>\S.*)$"
)


def _heading(line: str, current: str | None) -> tuple[str, str, str] | None:
    """(section_key, heading label, rest of line) if ``line`` starts a section."""
    if not line:
        return None
    if len(line) <= 80:
        core = _HEADING_NUM_RE.sub("", _HEADING_DECOR_RE.sub("", line))
        if _SPACED_CAPS_RE.fullmatch(core):
            core = core.replace(" ", "")  # "E D U C A T I O N"
        norm = _norm_heading(core)
        key = _HEADING_KEY.get(norm)
        if key and len(norm.split()) <= 6:
            if norm in _WEAK_HEADINGS and current in _ENTRY_SECTIONS:
                return None
            return key, core, ""
    m = _INLINE_HEADING_RE.match(line)
    if m:
        label = _norm_heading(m.group("label"))
        key = _HEADING_KEY.get(label)
        if key and label not in _WEAK_HEADINGS and not (key == "skills" and current in {"projects", "experience"}):
            return key, m.group("label").strip(), m.group("rest").strip()
    words = line.split()  # "TECHNICAL SKILLS Python, Java" (heading glued to text in PDFs)
    for n in (3, 2, 1):
        prefix = " ".join(words[:n])
        if len(words) > n and prefix.isupper() and len(prefix) >= 4 and not words[n].isupper():
            key = _HEADING_KEY.get(_norm_heading(prefix))
            if key and _norm_heading(prefix) not in _WEAK_HEADINGS:
                return key, prefix, " ".join(words[n:])
    return None


Block = tuple[str | None, str, list[str]]   # (section key or None for the header, label, lines)


def _split_blocks(lines: list[str]) -> list[Block]:
    blocks: list[Block] = [(None, "", [])]
    for line in lines:
        h = _heading(line, blocks[-1][0])
        if h:
            key, label, rest = h
            blocks.append((key, label, [rest] if rest else []))
        else:
            blocks[-1][2].append(line)
    return blocks


def _is_prose(line: str) -> bool:
    return len(line.split()) >= 8 and "@" not in line and not _URL_RE.search(line)


def _sections_from_blocks(blocks: list[Block]) -> dict[str, str]:
    acc: dict[str, list[str]] = {}
    has_headings = len(blocks) > 1
    for key, _label, lines in blocks:
        content = [ln for ln in lines if ln]
        if not content:
            continue
        if key is None:  # text before the first heading: contact block (+ untitled summary)
            prose = [ln for ln in content if _is_prose(ln)] if has_headings else []
            if sum(len(ln.split()) for ln in prose) >= 15:
                acc.setdefault("summary", []).extend(prose)
                content = [ln for ln in content if ln not in prose]
            key = "other"
        acc.setdefault(key, []).extend(content)
    return {k: "\n".join(acc[k]) for k in S.SECTION_KEYS if k in acc}


# ===========================================================================
# Line classification, bullets and entries (projects / experience)
# ===========================================================================
_BULLET_RE = re.compile(rf"^(?P<g>[{_GLYPHS}]|[-*>–—](?=\s)|o(?=\s+\S))\s*")
_ACTION_VERB_RE = re.compile(
    r"^(?:developed|built|designed|implemented|created|led|managed|worked|collaborated|engineered|wrote|"
    r"automated|optimi[sz]ed|improved|integrated|deployed|trained|analy[sz]ed|researched|conducted|achieved|"
    r"reduced|increased|migrated|configured|maintained|tested|debugged|refactored|architected|launched|"
    r"delivered|contributed|assisted|participated|performed|prepared|handled|utili[sz]ed|used|applied|"
    r"established|coordinated|organi[sz]ed|mentored|presented|authored|published|won|secured|enhanced|"
    r"streamlined|scraped|visuali[sz]ed|cleaned|modell?ed|fine-tuned|containeri[sz]ed|set|ran|made|helped|"
    r"learned|gained|explored|studied|responsible|achieving|building|developing|designing|creating)\b",
    re.IGNORECASE,
)
_META_RE = re.compile(
    r"^(?:tech(?:nolog(?:y|ies))?(?:\s+(?:stack|used))?|tools(?:\s+used)?|stack|built\s+with|environment|"
    r"skills(?:\s+used)?|languages?|frameworks?|links?|github|demo|live|url|repo(?:sitory)?|duration|role|"
    r"team\s+size|guide|mentor|client|domain|platform|key\s+skills)\s*[:\-–|]",
    re.IGNORECASE,
)
_TITLE_SMALL_WORDS = {"of", "and", "the", "for", "in", "on", "using", "with", "a", "an", "to", "at", "via",
                      "&", "-", "|", "by", "from", "based"}


def _line_kind(line: str) -> str:
    """'title' | 'bullet' | 'meta' for a glyph-less line inside projects / experience."""
    words = line.split()
    if _META_RE.match(line):
        return "meta"
    if len(words) >= 4 and _ACTION_VERB_RE.match(line):
        return "bullet"
    if _has_range(line) or " | " in line or line.endswith(":"):
        return "title"
    if len(words) >= 5 and line.endswith("."):
        return "bullet"
    alpha = [w for w in words if w[:1].isalpha()]
    if len(words) <= 14 and alpha:
        caps = sum(1 for w in alpha if w[0].isupper() or w.lower() in _TITLE_SMALL_WORDS)
        if caps / len(alpha) >= 0.6:
            return "title"
    return "bullet" if len(words) >= 7 else "meta"


_DANGLING_END_RE = re.compile(
    r"(?:[,&/+-]|\b(?:with|and|or|of|to|for|in|on|by|the|a|an|using|from|at|as|into|via|across|over|"
    r"through|than|that|which|while|including|such))$",
    re.IGNORECASE,
)


def _is_continuation(line: str, previous: str) -> bool:
    """A wrapped bullet: starts lower-case, or the previous line stops mid-sentence."""
    if not line:
        return False
    return line[0].islower() or line[0] in "(&,;:)" or bool(_DANGLING_END_RE.search(previous))


Classified = list[tuple[str, str, str | None]]   # (kind, text, glyph)


def _classify(lines: list[str], rich: bool) -> Classified:
    """Tag lines as bullet / title / meta / text / blank; merge wrapped bullet lines.

    ``rich`` (projects, experience): glyph-less sentence-like lines count as bullets.
    """
    out: Classified = []
    for line in lines:
        if not line:
            out.append(("blank", "", None))
            continue
        m = _BULLET_RE.match(line)
        if m:
            text = line[m.end():].strip()
            if text:
                out.append(("bullet", text, m.group("g")))
            continue
        prev_kind, prev_text = (out[-1][0], out[-1][1]) if out else ("", "")
        if prev_kind == "bullet" and _is_continuation(line, prev_text):
            out[-1] = ("bullet", f"{prev_text} {line}", out[-1][2])
        elif not rich:
            out.append(("text", line, None))
        elif prev_kind == "meta" and prev_text.endswith(":"):
            out.append(("meta", line, None))   # value of a "Tech Stack:" label on its own line
        else:
            out.append((_line_kind(line), line, None))
    return out


def _new_entry(header: list[str] | None = None) -> dict[str, Any]:
    header = list(header or [])
    return {"header": header, "bullets": [], "meta": [], "lines": list(header), "closed": False}


def _group_entries(classified: Classified) -> list[dict[str, Any]]:
    """Title lines open an entry; bullets / meta lines attach to the current one."""
    entries: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for kind, text, _glyph in classified:
        if kind == "blank":
            if cur is not None and (cur["bullets"] or cur["meta"]):
                cur["closed"] = True
            continue
        if kind == "title":
            new_dated = _has_range(text) and any(_has_range(h) for h in cur["header"]) if cur else False
            if cur is None or cur["bullets"] or cur["meta"] or cur["closed"] or len(cur["header"]) >= 4 or new_dated:
                cur = _new_entry()
                entries.append(cur)
            cur["header"].append(text)
        else:
            if cur is None or cur["closed"]:
                cur = _new_entry()
                entries.append(cur)
            cur["bullets" if kind == "bullet" else "meta"].append(text)
        cur["lines"].append(text)
    return entries


def _split_title_only(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One-line projects ("Chatbot | Python" on consecutive lines) -> one entry per line."""
    out: list[dict[str, Any]] = []
    for e in entries:
        if e["bullets"] or e["meta"] or len(e["header"]) < 2:
            out.append(e)
            continue
        for line in e["header"]:
            if out and out[-1] is not e and _is_date_only(line) and out[-1]["header"]:
                out[-1]["header"].append(line)
                out[-1]["lines"].append(line)
            else:
                out.append(_new_entry([line]))
    return out


def _regroup_bullets(classified: Classified, section: str) -> list[dict[str, Any]]:
    """Entries for sections written entirely as bullets (no title lines)."""
    bullets = [(t, g) for k, t, g in classified if k == "bullet"]
    glyphs = list(dict.fromkeys(g for _, g in bullets if g))

    def is_title(text: str, glyph: str | None) -> bool:
        if section != "projects":
            return bool(_has_range(text) or _INTERN_RE.search(text))
        if len(glyphs) >= 2:  # two bullet levels: the first-seen glyph marks project titles
            return glyph == glyphs[0]
        return _line_kind(text) == "title"

    titled = any(is_title(t, g) for t, g in bullets)
    entries: list[dict[str, Any]] = []
    for t, g in bullets:
        if titled and is_title(t, g):
            entries.append(_new_entry([t]))
        elif not titled and section == "projects":
            entries.append(_new_entry([t]))   # each bullet is a one-line project
        else:
            if not entries:
                entries.append(_new_entry())
            entries[-1]["bullets"].append(t)
            entries[-1]["lines"].append(t)
    if entries:
        entries[-1]["meta"].extend(t for k, t, _ in classified if k == "meta")
    return entries


def _entries(lines: list[str], section: str) -> list[dict[str, Any]]:
    entries = _group_entries(_classify(lines, rich=True))
    if entries and not any(e["header"] for e in entries):
        entries = _regroup_bullets(_classify(lines, rich=True), section) or entries
    elif section == "projects":
        entries = _split_title_only(entries)
    return [e for e in entries if e["lines"]]


_TITLE_SPLIT_RE = re.compile(r"\s+[|–—]\s+|\s+-\s+|:\s+|\s+\(")


def _clean_title(line: str) -> str:
    line = _URL_RE.sub(" ", line)
    line = _RANGE_RE.sub(" ", _SAME_YEAR_RANGE_RE.sub(" ", line))
    head = _TITLE_SPLIT_RE.split(line, maxsplit=1)[0]
    head = " ".join(head.split()).strip(" |-–—:,;()")
    if len(head.split()) > 12:
        head = " ".join(head.split()[:12])
    return head[:120]


# ===========================================================================
# Dates & experience
# ===========================================================================
_MONTH_NUM = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
              "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_DATE_TOKEN = (rf"(?:{_MON}\s*[',’]?\s*(?:\d{{4}}|\d{{2}})(?!\d)"
               r"|(?:0?[1-9]|1[0-2])\s*[/.-]\s*(?:19|20)\d{2}"
               r"|(?:19|20)\d{2}\s*[/.-]\s*(?:0?[1-9]|1[0-2])(?!\d)"
               r"|(?:19|20)\d{2})")
_PRESENT = r"(?:present|current(?:ly)?|now|till\s+(?:date|now)|to\s+date|ongoing|today)"
_RANGE_RE = re.compile(
    rf"(?<![\w/])(?P<start>{_DATE_TOKEN})\s*(?:-|–|—|~|→|to|till|until)\s*(?P<end>{_DATE_TOKEN}|{_PRESENT})(?![\w/])",
    re.IGNORECASE,
)
_PRESENT_RE = re.compile(_PRESENT, re.IGNORECASE)
_SAME_YEAR_RANGE_RE = re.compile(  # "Jun - Aug 2025"
    rf"(?<![\w/])(?P<smon>{_MON})\s*(?:-|–|—|to)\s*(?P<emon>{_MON})\s*[',’]?\s*(?P<year>(?:19|20)\d{{2}})(?!\d)",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"(?<![\w.])(?P<n>\d{1,2}(?:\.\d)?)\s*\+?\s*-?\s*(?P<unit>months?|mos?|weeks?|wks?|years?|yrs?)\b",
    re.IGNORECASE,
)
_INTERN_RE = re.compile(r"\bintern(?:ship|ships|s|ed)?\b", re.IGNORECASE)
_ROLE_RE = re.compile(
    r"\b(?:intern(?:ship)?|engineer|developer|analyst|trainee|associate|scientist|consultant|manager|lead|"
    r"designer|researcher|assistant|fellow|programmer|architect|tester|specialist|executive|officer|"
    r"apprentice|volunteer|member|coordinator|head|founder|freelancer?)\b",
    re.IGNORECASE,
)
_ORG_RE = re.compile(
    r"\b(?:pvt|private|ltd|limited|inc|llp|llc|corp(?:oration)?|technologies|technology|solutions|labs?|"
    r"systems|software|services|infotech|consultancy|consulting|bank|university|institute|college|"
    r"foundation|ventures|group|company|industries|digital|analytics|networks)\b",
    re.IGNORECASE,
)
_HEADER_SPLIT_RE = re.compile(r"\s*(?:\||–|—|\s-\s|,|\bat\b|@|\(|\))\s*", re.IGNORECASE)


def _has_range(text: str) -> bool:
    return bool(_RANGE_RE.search(text) or _SAME_YEAR_RANGE_RE.search(text))


def _is_date_only(text: str) -> bool:
    rest = _SAME_YEAR_RANGE_RE.sub("", _RANGE_RE.sub("", text))
    return _has_range(text) and not any(c.isalnum() for c in rest)


def _date_token(tok: str) -> tuple[int, int | None] | None:
    tok = tok.strip().lower()
    m = re.fullmatch(rf"({_MON})\s*[',’]?\s*(\d{{4}}|\d{{2}})", tok)
    if m:
        year = int(m.group(2))
        year = year + 2000 if year < 100 else year
        month: int | None = _MONTH_NUM[m.group(1)[:3]]
    elif m := re.fullmatch(r"(\d{1,2})\s*[/.-]\s*(\d{4})", tok):
        year, month = int(m.group(2)), int(m.group(1))
    elif m := re.fullmatch(r"(\d{4})\s*[/.-]\s*(\d{1,2})", tok):
        year, month = int(m.group(1)), int(m.group(2))
    elif m := re.fullmatch(r"(\d{4})", tok):
        year, month = int(m.group(1)), None
    else:
        return None
    if not 1990 <= year <= 2040 or (month is not None and not 1 <= month <= 12):
        return None
    return year, month


def _interval(start: tuple[int, int | None], end: tuple[int, int | None] | None) -> tuple[int, int] | None:
    """Half-open month-index interval [start, end). ``end`` None means Present.

    An explicit end month is inclusive ("Jun 2025 - Aug 2025" = 3 months); Present is
    PRESENT_DATE itself ("May 2024 - Present" = 30 months). Year-only ranges count
    whole years ("2023 - 2024" = 12 months).
    """
    sy, sm = start
    s_idx = sy * 12 + (sm or 1) - 1
    if end is None:
        e_idx = PRESENT_DATE.year * 12 + PRESENT_DATE.month - 1
    else:
        ey, em = end
        if em is not None:
            e_idx = ey * 12 + em
        elif sm is None:
            e_idx = ey * 12 if ey > sy else s_idx + 12
        else:
            e_idx = ey * 12 + 12
    if e_idx <= s_idx or e_idx - s_idx > 600:
        return None
    return s_idx, e_idx


def _find_interval(text: str) -> tuple[tuple[int, int], str] | None:
    """First parseable date range in ``text`` -> (interval, raw text)."""
    for m in _RANGE_RE.finditer(text):
        start = _date_token(m.group("start"))
        is_present = _PRESENT_RE.fullmatch(m.group("end").strip()) is not None
        end = None if is_present else _date_token(m.group("end"))
        if start is None or (end is None and not is_present):
            continue
        iv = _interval(start, end)
        if iv:
            return iv, m.group().strip()
    m = _SAME_YEAR_RANGE_RE.search(text)
    if m:
        year = int(m.group("year"))
        iv = _interval((year, _MONTH_NUM[m.group("smon").lower()[:3]]), (year, _MONTH_NUM[m.group("emon").lower()[:3]]))
        if iv:
            return iv, m.group().strip()
    return None


def _duration_months(text: str) -> float | None:
    m = _DURATION_RE.search(text)
    if not m:
        return None
    n, unit = float(m.group("n")), m.group("unit").lower()
    months = n * 12 if unit.startswith(("y", "yr")) else n / 4.345 if unit.startswith("w") else n
    return round(months, 1) if 0 < months <= 120 else None


def _title_org(header: list[str]) -> tuple[str, str]:
    parts: list[str] = []
    for line in header[:3]:
        line = _URL_RE.sub(" ", _DURATION_RE.sub(" ", _RANGE_RE.sub(" ", _SAME_YEAR_RANGE_RE.sub(" ", line))))
        for p in _HEADER_SPLIT_RE.split(line):
            p = p.strip(" -–—:|,()")
            if len(p) >= 2 and any(c.isalpha() for c in p):
                parts.append(p)
    if not parts:
        return "", ""
    title = next((p for p in parts if _ROLE_RE.search(p)), parts[0])
    rest = [p for p in parts if p is not title]
    org = next((p for p in rest if _ORG_RE.search(p)), rest[0] if rest else "")
    return title[:120], org[:120]


def _parse_experiences(blocks: list[Block]) -> tuple[list[dict[str, Any]], float]:
    """Experience entries + total months (overlapping date ranges counted once)."""
    experiences: list[dict[str, Any]] = []
    intervals: list[tuple[int, int]] = []
    loose_months = 0.0
    for key, label, lines in blocks:
        if key != "experience":
            continue
        intern_block = _norm_heading(label).startswith("intern")
        for e in _entries(lines, "experience"):
            text = "\n".join(e["lines"])
            found = _find_interval(" | ".join(e["header"])) or _find_interval(text)
            if found:
                (s_idx, e_idx), raw = found
                months: float | None = float(e_idx - s_idx)
                intervals.append((s_idx, e_idx))
            else:
                raw, months = "", _duration_months(text)
                loose_months += months or 0.0
            title, org = _title_org(e["header"] or e["lines"][:1])
            experiences.append({
                "title": title, "org": org, "text": text, "months": months,
                "is_internship": intern_block or bool(_INTERN_RE.search(text)), "dates": raw,
            })
    total = loose_months + sum(e_idx - s_idx for s_idx, e_idx in _merge_intervals(intervals))
    return experiences, float(total)


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _parse_projects(blocks: list[Block]) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for key, _label, lines in blocks:
        if key != "projects":
            continue
        for e in _entries(lines, "projects"):
            text = "\n".join(e["lines"])
            source = e["header"][0] if e["header"] else (e["bullets"] or e["lines"])[0]
            repos = [u for u in _github_urls(text) if u.count("/") >= 2]
            projects.append({
                "title": _clean_title(source), "text": text, "bullets": list(e["bullets"]),
                "tech": skills.extract_skills(text), "github": repos[0] if repos else None,
            })
    return projects


# ===========================================================================
# Education & certifications
# ===========================================================================
_DEGREES: list[tuple[str, int, str]] = [   # (label, rank, regex); higher rank wins
    ("Ph.D", 4, r"\bph\.?\s?d\b|doctor\s+of\s+philosophy"),
    ("M.Tech", 3, r"\bm\.?\s?tech\b|master\s+of\s+technology"),
    ("M.E.", 3, r"(?-i:\bM\.\s?E\b\.?)|master\s+of\s+engineering"),
    ("MCA", 3, r"\bm\.?\s?c\.?\s?a\b|master\s+of\s+computer\s+applications?"),
    ("M.Sc", 3, r"\bm\.?\s?sc\b|master\s+of\s+science|(?-i:\bM\.S\.)"),
    ("MBA", 3, r"\bm\.?\s?b\.?\s?a\b|master\s+of\s+business\s+administration"),
    ("M.Com", 3, r"\bm\.?\s?com\b|master\s+of\s+commerce"),
    ("B.Tech", 2, r"\bb\.?\s?tech\b|bachelor\s+of\s+technology"),
    ("B.E.", 2, r"(?-i:\bB\.?\s?E\b\.?)(?![\w-])|bachelor\s+of\s+engineering"),
    ("BCA", 2, r"\bb\.?\s?c\.?\s?a\b|bachelor\s+of\s+computer\s+applications?"),
    ("B.Sc", 2, r"\bb\.?\s?sc\b|bachelor\s+of\s+science|(?-i:\bB\.S\.)"),
    ("BBA", 2, r"\bb\.?\s?b\.?\s?a\b|bachelor\s+of\s+business\s+administration"),
    ("B.Com", 2, r"\bb\.?\s?com\b|bachelor\s+of\s+commerce"),
    ("B.Arch", 2, r"\bb\.?\s?arch\b|bachelor\s+of\s+architecture"),
    ("B.Pharm", 2, r"\bb\.?\s?pharm(?:a|acy)?\b|bachelor\s+of\s+pharmacy"),
    ("Diploma", 1, r"\bdiploma\b"),
]
_DEGREE_RES = [(label, rank, re.compile(rx, re.IGNORECASE)) for label, rank, rx in _DEGREES]
_BRANCHES: list[tuple[str, re.Pattern[str]]] = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in [
    ("Artificial Intelligence and Data Science",
     r"\bai\s?(?:&|and|-)?\s?ds\b|artificial\s+intelligence\s+(?:&|and)\s+data\s+science|\baids\b"),
    ("Artificial Intelligence and Machine Learning",
     r"\bai\s?(?:&|and|/|-)?\s?ml\b|artificial\s+intelligence\s+(?:&|and)\s+machine\s+learning|\baiml\b"),
    ("Computer Science and Engineering",
     r"computer\s+science|\bc\.?s\.?e\b|\bcs\b|computer\s+engineering|\bcomps?\b"),
    ("Information Technology", r"information\s+technology|(?-i:\bIT\b)|\bi\.t\.(?!\w)"),
    ("Electronics and Communication Engineering",
     r"electronics\s+(?:&|and)\s+(?:tele\s?)?communications?|\be\.?c\.?e\b|\bentc\b|\be\s?&\s?tc\b"),
    ("Electrical and Electronics Engineering", r"electrical\s+(?:&|and)\s+electronics|\beee\b"),
    ("Electronics and Instrumentation Engineering",
     r"electronics\s+(?:&|and)\s+instrumentation|\beie\b|instrumentation"),
    ("Electrical Engineering", r"electrical"),
    ("Electronics Engineering", r"electronics"),
    ("Mechanical Engineering", r"mechanical|\bmech\b"),
    ("Civil Engineering", r"\bcivil\b"),
    ("Chemical Engineering", r"\bchemical\b"),
    ("Aerospace Engineering", r"aero(?:space|nautical)"),
    ("Biotechnology", r"bio\s?-?\s?tech(?:nology)?\b"),
    ("Data Science", r"data\s+science"),
    ("Computer Applications", r"computer\s+applications?"),
    ("Software Engineering", r"software\s+engineering"),
    ("Mathematics", r"mathematics|\bmaths?\b"),
    ("Statistics", r"statistics"),
    ("Physics", r"physics"),
    ("Chemistry", r"chemistry"),
    ("Commerce", r"commerce"),
]]
_COLLEGE_RE = re.compile(
    r"\b(?:university|universiti|institute|institution|college|academy|polytechnic|vidyapeeth|vidyapith|"
    r"iit|nit|iiit|bits|vit|srm|jntu|dtu|nsut|nmims|kiit|lpu|amity|manipal|coep|pes|rvce|bmsce|msrit|"
    r"sastra|iisc|iiest)\b|school\s+of\s+(?:engineering|technology|computing|computer)",
    re.IGNORECASE,
)
_SCHOOL_RE = re.compile(
    r"\b(?:high\s+school|public\s+school|senior\s+secondary|higher\s+secondary|secondary\s+school|"
    r"junior\s+college|ssc|hsc|sslc|cbse|icse|isc|class\s*(?:x|xii|10|12)(?:th)?|10th|12th|xth|xiith|"
    r"intermediate|matriculation|puc|pre[-\s]university|vidyalaya|board)\b",
    re.IGNORECASE,
)
_COLLEGE_SPLIT_RE = re.compile(r"\s+[|–—]\s+|\s+-\s+|,\s*|\(|\)|\s\|\s")
_CGPA_LABEL_RE = re.compile(
    r"\b(?:c\.?\s?g\.?\s?p\.?\s?a|s\.?\s?g\.?\s?p\.?\s?a|g\.?\s?p\.?\s?a|cpi|spi)\b\.?\s*(?:[:=\-–]|of|is)?\s*"
    r"(?P<v>\d{1,3}(?:\.\d{1,3})?)\s*(?:(?:/|out\s+of)\s*(?P<s>\d{1,3}(?:\.\d+)?))?",
    re.IGNORECASE,
)
_CGPA_TRAILING_RE = re.compile(
    r"(?<![\d.])(?P<v>\d{1,2}(?:\.\d{1,3})?)\s*(?:/\s*(?P<s>10|4|5)(?:\.0+)?)?\s*"
    r"(?:c\.?g\.?p\.?a|g\.?p\.?a|cpi)\b",
    re.IGNORECASE,
)
_FRACTION_RE = re.compile(r"(?<![\d.])(?P<v>\d{1,2}(?:\.\d{1,3})?)\s*/\s*(?P<s>10|4)(?:\.0+)?(?![\d.])")
_PERCENT_RE = re.compile(
    r"(?<![\d.])(?P<v>\d{2,3}(?:\.\d{1,2})?)\s*%|"
    r"\b(?:percentage|aggregate|marks)\b\s*[:\-–]?\s*(?P<w>\d{2,3}(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _to_ten_point(value: float, scale: float | None) -> float | None:
    if scale is None:
        if value <= 4.0:
            scale = 4.0
        elif value <= 10.0:
            scale = 10.0
        elif value <= 100.0:
            scale = 100.0
        else:
            return None
    if scale <= 0 or value < 0 or value > scale:
        return None
    return round(value * 10.0 / scale, 2)


def _labelled_cgpa(text: str) -> float | None:
    for rx in (_CGPA_LABEL_RE, _CGPA_TRAILING_RE):
        for m in rx.finditer(text):
            v = _to_ten_point(float(m.group("v")), float(m.group("s")) if m.group("s") else None)
            if v:
                return v
    return None


def _parse_cgpa(edu_lines: list[str], degree_idx: int | None, full_text: str) -> float | None:
    """10-point CGPA. Education section first (labelled, then "x/10", then a degree-level
    percentage - never a 10th/12th one), then a labelled CGPA anywhere in the resume."""
    edu_text = "\n".join(edu_lines)
    v = _labelled_cgpa(edu_text)
    if v:
        return v
    for m in _FRACTION_RE.finditer(edu_text):
        v = _to_ten_point(float(m.group("v")), float(m.group("s")))
        if v:
            return v
    order = list(range(len(edu_lines)))
    if degree_idx is not None:
        order.sort(key=lambda i: (abs(i - degree_idx) > 1, abs(i - degree_idx)))
    for i in order:
        if _SCHOOL_RE.search(edu_lines[i]):
            continue
        for m in _PERCENT_RE.finditer(edu_lines[i]):
            v = _to_ten_point(float(m.group("v") or m.group("w")), 100.0)
            if v:
                return v
    return _labelled_cgpa(full_text)


def _parse_branch(line: str, after: str, next_line: str, degree: str | None) -> str | None:
    for source in (after, line, next_line):
        for name, rx in _BRANCHES:
            if rx.search(source):
                return name
    if degree in {"MCA", "BCA"}:
        return "Computer Applications"
    raw = re.split(r"[,|(\[]|\s[-–—]\s|\d", re.sub(r"^\s*(?:in|of|-|–|:|\()\s*", "", after, flags=re.IGNORECASE))[0]
    raw = " ".join(raw.split()).strip(" .-–:")
    if 3 <= len(raw) <= 60 and raw.replace(" ", "").replace("&", "").isalpha() and not _COLLEGE_RE.search(raw):
        return raw.title() if raw.islower() or raw.isupper() else raw
    return None


def _college_from_line(line: str) -> str | None:
    for seg in _COLLEGE_SPLIT_RE.split(line):
        seg = seg.strip(" .-–—:|")
        if _COLLEGE_RE.search(seg) and not _SCHOOL_RE.search(seg):
            seg = re.split(r"\b(?:from|at)\b", seg, flags=re.IGNORECASE)[-1] if re.search(
                r"\b(?:b\.?\s?tech|b\.?e|degree|bachelor|master)\b.*\b(?:from|at)\b", seg, re.IGNORECASE) else seg
            seg = _YEAR_RE.sub(" ", seg)
            seg = " ".join(seg.split()).strip(" .-–—:|")
            if len(seg) >= 3:
                return seg[:120]
    return None


def _parse_education(sections: dict[str, str], full_text: str) -> dict[str, Any]:
    edu: dict[str, Any] = {"degree": None, "branch": None, "college": None, "cgpa": None, "grad_year": None}
    lines = [ln for ln in sections.get("education", "").split("\n") if ln.strip()]
    if not lines:  # no Education heading: use lines around any degree mention
        all_lines = [ln for ln in full_text.split("\n") if ln.strip()]
        for i, ln in enumerate(all_lines):
            if any(rx.search(ln) for _, _, rx in _DEGREE_RES):
                lines.extend(all_lines[i:i + 3])
        lines = list(dict.fromkeys(lines))

    best: tuple[int, int, int, str, re.Match[str]] | None = None  # (-rank, line idx, pos, label, match)
    for idx, ln in enumerate(lines):
        for label, rank, rx in _DEGREE_RES:
            m = rx.search(ln)
            if m:
                cand = (-rank, idx, m.start(), label, m)
                if best is None or cand[:3] < best[:3]:
                    best = cand
    degree_idx: int | None = None
    if best is not None:
        _, degree_idx, _, label, m = best
        line = lines[degree_idx]
        nxt = lines[degree_idx + 1] if degree_idx + 1 < len(lines) else ""
        edu["degree"] = label
        edu["branch"] = _parse_branch(line, line[m.end():], nxt, label)
    else:
        for ln in lines:
            for name, rx in _BRANCHES[:6]:
                if rx.search(ln):
                    edu["branch"] = name
                    break
            if edu["branch"]:
                break

    order = list(range(len(lines)))
    if degree_idx is not None:
        order.sort(key=lambda i: (i < degree_idx, abs(i - degree_idx)))
    for i in order:
        college = _college_from_line(lines[i])
        if college:
            edu["college"] = college
            break

    edu["cgpa"] = _parse_cgpa(lines, degree_idx, full_text)
    years = [int(y) for y in _YEAR_RE.findall("\n".join(lines)) if 2000 <= int(y) <= 2040]
    edu["grad_year"] = max(years) if years else None
    return edu


_CERT_WORD_RE = re.compile(r"\bcertifi(?:ed|cation|cations)\b", re.IGNORECASE)


def _clean_item(text: str) -> str:
    m = _BULLET_RE.match(text)
    text = text[m.end():] if m else text
    text = " ".join(text.split()).strip(" |-–—:;,")
    return text[:200] if len(text) >= 3 and any(c.isalpha() for c in text) else ""


def _parse_certifications(blocks: list[Block], lines: list[str]) -> list[str]:
    items: list[str] = []
    cert_blocks = [b for b in blocks if b[0] == "certifications"]
    if cert_blocks:
        for _key, _label, block_lines in cert_blocks:
            for kind, text, _g in _classify(block_lines, rich=False):
                if kind != "blank":
                    items.extend(_clean_item(part) for part in text.split(";"))
    else:
        for line in lines:
            if _CERT_WORD_RE.search(line) and not _heading(line, None):
                items.append(_clean_item(line))
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out[:30]


# ===========================================================================
# Entities: e-mail, phone, URLs, GitHub, LinkedIn, name, candidate id
# ===========================================================================
_EMAIL_RE = re.compile(  # lookbehind: one start per token (no quadratic backtracking)
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
_PHONE_IN_RE = re.compile(
    r"(?<![\w+])(?:(?:\+|00)\s?91[\s.-]?|91[\s.-]?(?=[6-9])|0(?=[6-9]))?\(?"
    r"(?:[6-9]\d{4}\)?[\s.-]?\d{5}|[6-9]\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{4})(?!\d)"
)
_PHONE_INTL_RE = re.compile(r"(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?!\d)")
_URL_CHARS = r"[^\s<>\"'|,;()\[\]{}]"
_URL_RE = re.compile(
    rf"(?:https?://|www\.){_URL_CHARS}+|"
    rf"(?<![\w.@/-])(?:[a-z]{{2,3}}\.)?(?:github\.com|gitlab\.com|linkedin\.com|bitbucket\.org|leetcode\.com|"
    rf"kaggle\.com|hackerrank\.com|codechef\.com|codeforces\.com|medium\.com|behance\.net)/{_URL_CHARS}+",
    re.IGNORECASE,
)
_GITHUB_RE = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?github\.com/(?P<user>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))"
    r"(?:/(?P<repo>[A-Za-z0-9._-]{1,100}))?",
    re.IGNORECASE,
)
_GITHUB_RESERVED = {
    "orgs", "settings", "features", "topics", "about", "sponsors", "marketplace", "explore", "login", "join",
    "pricing", "collections", "trending", "apps", "enterprise", "site", "contact", "security", "notifications",
    "pulls", "issues", "search", "new", "readme", "customer-stories", "team",
}
_LINKEDIN_RE = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:[a-z]{2,3}\.|www\.)?linkedin\.com/(?P<kind>in|pub)/(?P<slug>[A-Za-z0-9_%-]+)",
    re.IGNORECASE,
)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def _emails(text: str) -> list[str]:
    return _dedupe([m.group().lower().rstrip(".") for m in _EMAIL_RE.finditer(text)])


def _phones(text: str) -> list[str]:
    found: list[str] = []
    for m in _PHONE_IN_RE.finditer(text):
        found.append(re.sub(r"\D", "", m.group())[-10:])
    for m in _PHONE_INTL_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 10 <= len(digits) <= 13:
            found.append(digits[-10:])
    return _dedupe([p for p in found if len(p) == 10])


def _urls(text: str) -> list[str]:
    found = [m.group().rstrip(".:!?") for m in _URL_RE.finditer(text)]
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        if u.lower() not in seen:
            seen.add(u.lower())
            out.append(u)
    return out


def _github_urls(text: str) -> list[str]:
    out: list[str] = []
    for m in _GITHUB_RE.finditer(text):
        user = m.group("user").lower()
        if user in _GITHUB_RESERVED:
            continue
        repo = (m.group("repo") or "").lower().rstrip(".")
        repo = repo[:-4] if repo.endswith(".git") else repo
        out.append(f"github.com/{user}/{repo}" if repo else f"github.com/{user}")
    return _dedupe(out)


def _linkedin_urls(text: str) -> list[str]:
    return _dedupe([f"linkedin.com/{m.group('kind').lower()}/{m.group('slug').lower()}"
                    for m in _LINKEDIN_RE.finditer(text)])


_NOT_NAME_WORDS = {
    "resume", "curriculum", "vitae", "cv", "biodata", "bio", "data", "profile", "contact", "details", "information",
    "info", "email", "mail", "phone", "mobile", "tel", "address", "linkedin", "github", "portfolio", "website",
    "objective", "summary", "engineer", "engineering", "developer", "development", "student", "intern",
    "internship", "analyst", "scientist", "designer", "manager", "consultant", "programmer", "graduate",
    "undergraduate", "fresher", "technology", "technologies", "institute", "university", "college", "school",
    "skills", "skill", "projects", "project", "experience", "education", "personal", "page", "of", "the", "and",
    "for", "with", "in", "at", "to", "computer", "science", "software", "web", "full", "stack", "candidate",
    "name", "applicant", "application", "india", "b.tech", "btech", "m.tech", "mtech", "career", "work",
}
_NAME_LABEL_RE = re.compile(r"^(?:full\s+)?(?:candidate\s+)?name\s*[:\-–]\s*", re.IGNORECASE)
_NAME_SPLIT_RE = re.compile(r"\s*[|•·]\s*|\s+[–—]\s+|,\s*|\s{2,}")


def _looks_like_name(s: str) -> bool:
    tokens = s.split()
    if not 2 <= len(tokens) <= 4:
        return False
    for t in tokens:
        if not t[0].isalpha() or not all(c.isalpha() or c in ".'’-" for c in t):
            return False
        if t.lower().strip(".") in _NOT_NAME_WORDS:
            return False
    return sum(c.isalpha() for c in s) >= 4 and max(len(t.strip(".")) for t in tokens) >= 2


def _tidy_name(s: str) -> str:
    s = " ".join(s.split())
    return s.title() if s.isupper() or s.islower() else s


_FILE_NOISE_WORDS = {
    "resume", "cv", "final", "updated", "update", "new", "latest", "copy", "document", "doc", "docx", "pdf",
    "file", "scan", "scanned", "my", "profile", "biodata", "curriculum", "vitae", "candidate", "of", "and",
}


def _name_from_filename(file_name: str, id_rx: re.Pattern[str] | None,
                        candidate_id: str | None = None) -> str | None:
    stem = Path(file_name).stem
    if candidate_id:
        stem = re.sub(re.escape(candidate_id), " ", stem, flags=re.IGNORECASE)
    stem = stem.replace("_", " ")
    if id_rx is not None:
        stem = id_rx.sub(" ", stem)
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)  # AshaMenon -> Asha Menon
    words = [t for t in re.split(r"[\s.\-+()\[\],]+", stem)
             if t.isalpha() and len(t) > 1 and t.lower() not in _FILE_NOISE_WORDS]
    if not words or len(words) > 4:
        return None
    return " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in words)


def _guess_name(lines: list[str], file_name: str, id_rx: re.Pattern[str] | None,
                candidate_id: str | None = None) -> str | None:
    checked = 0
    for line in lines:
        if not line:
            continue
        checked += 1
        if checked > 12:
            break
        if _heading(line, None):
            continue
        candidate = _NAME_SPLIT_RE.split(_NAME_LABEL_RE.sub("", line), maxsplit=1)[0].strip(" .:-")
        if "@" not in candidate and not any(c.isdigit() for c in candidate) and _looks_like_name(candidate):
            return _tidy_name(candidate)
    return _name_from_filename(file_name, id_rx, candidate_id)


_ID_LABEL_RE = re.compile(
    r"\b(?:candidate|applicant|application|registration|reg|roll|enrol(?:l)?ment|hackerrank|student|"
    r"employee|test|assessment)\s*(?:id|no|number|code|#)|\bcand(?:idate)?\s*id\b",
    re.IGNORECASE,
)
_YEARISH_ID_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}$")
_GENERIC_FILE_PREFIXES = {"resume", "cv", "final", "updated", "file", "doc", "scan", "img", "image", "copy",
                          "page", "version", "draft", "document", "photo", "new"}


@lru_cache(maxsize=32)
def _id_regexes(pattern: str | None) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
    if pattern is None:
        pattern = ProcessingConfig().candidate_id_pattern
    if not pattern:
        return None, None
    try:
        return re.compile(pattern), re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None, None


def _guess_candidate_id(file_name: str, lines: list[str], id_pattern: str | None) -> str | None:
    """Candidate ID via ``id_pattern``: file name first, then labelled text lines, then the header."""
    rx, rx_i = _id_regexes(id_pattern)
    if rx is None or rx_i is None:
        return None
    stem = Path(file_name).stem

    def generic(found: str) -> bool:   # "CV_2024", "resume-2023": not candidate IDs
        return re.sub(r"[^a-z]", "", found.lower()) in _GENERIC_FILE_PREFIXES

    # "_" is a regex word character, so "CAND-00012_Asha" defeats \b; same-length probes keep offsets.
    probes = (stem, stem.replace("_", " "), stem.replace("_", "-"))
    for regex, strict in ((rx, False), (rx_i, True)):
        for probe in probes:
            m = regex.search(probe)
            found = stem[m.start():m.end()].strip() if m else ""
            if found and not generic(found) and not (strict and _YEARISH_ID_RE.search(found)):
                return found.upper()
    for line in lines:
        if _ID_LABEL_RE.search(line):
            m = rx.search(line)
            if m and m.group().strip():
                return m.group().strip().upper()
    for line in [ln for ln in lines if ln][:15]:
        for m in rx.finditer(line):
            if m.group().strip() and not _YEARISH_ID_RE.search(m.group()):
                return m.group().strip().upper()
    return None


# ===========================================================================
# Parsing entry points
# ===========================================================================
def parse_resume_text(text: str, file_name: str = "", file_hash: str = "",
                      id_pattern: str | None = None) -> ParsedResume:
    """Structure raw resume text into a ParsedResume. Never raises.

    ``id_pattern`` defaults to ProcessingConfig.candidate_id_pattern ("" disables ID guessing).
    """
    file_name = _basename(file_name)
    try:
        return _parse_text(text or "", file_name, file_hash or "", id_pattern)
    except Exception as exc:  # defensive: one odd resume must never stop a batch
        return _failed(file_name, file_hash or "", f"Parser error ({type(exc).__name__})", [], id_pattern)


def _parse_text(raw: str, file_name: str, file_hash: str, id_pattern: str | None) -> ParsedResume:
    text = _normalize_text(raw)
    lines = text.split("\n") if text else []
    rx, _ = _id_regexes(id_pattern)
    pr = ParsedResume(file_name=file_name, file_hash=file_hash, text=text, word_count=len(text.split()))
    pr.emails = _emails(text)
    pr.phones = _phones(text)
    pr.urls = _urls(text)
    pr.github_urls = _github_urls(text)
    pr.linkedin_urls = _linkedin_urls(text)
    pr.candidate_id_guess = _guess_candidate_id(file_name, lines, id_pattern)

    if len(text.strip()) < MIN_TEXT_CHARS:
        pr.parse_status = "empty"
        pr.parse_error = f"Little or no extractable text ({len(text.strip())} characters)"
        pr.name_guess = _name_from_filename(file_name, rx, pr.candidate_id_guess)
        return pr

    blocks = _split_blocks(lines)
    pr.sections = _sections_from_blocks(blocks)
    if len(blocks) == 1:
        pr.warnings.append("No section headings recognised; the whole text is stored under 'other'")
    pr.name_guess = _guess_name(lines, file_name, rx, pr.candidate_id_guess)

    evidence = skills.skill_evidence(pr.sections)
    pr.skills = list(evidence)
    pr.skill_evidence = evidence

    pr.projects = _parse_projects(blocks)
    pr.experiences, pr.total_experience_months = _parse_experiences(blocks)
    pr.bullets = [  # document order; sentence-like lines count as bullets in projects / experience
        bullet
        for key, _label, block_lines in blocks
        for kind, bullet, _glyph in _classify(block_lines, rich=key in {"projects", "experience"})
        if kind == "bullet"
    ]

    pr.education = _parse_education(pr.sections, text)
    pr.certifications = _parse_certifications(blocks, lines)
    pr.project_count = len(pr.projects)
    pr.internship_count = sum(1 for e in pr.experiences if e["is_internship"])
    return pr


def _failed(file_name: str, file_hash: str, error: str, warnings: list[str],
            id_pattern: str | None) -> ParsedResume:
    rx, _ = _id_regexes(id_pattern)
    candidate_id = _guess_candidate_id(file_name, [], id_pattern)
    return ParsedResume(
        file_name=file_name, file_hash=file_hash, parse_status="error", parse_error=error,
        warnings=list(warnings), name_guess=_name_from_filename(file_name, rx, candidate_id),
        candidate_id_guess=candidate_id,
    )


def parse_resume(doc: ResumeDoc, id_pattern: str | None = None) -> ParsedResume:
    """Extract and parse one resume file. Never raises (status "ok" | "empty" | "error")."""
    file_name = _basename(getattr(doc, "file_name", "") or "")
    try:
        file_hash = doc.sha256
    except Exception:
        file_hash = ""
    try:
        text, warnings, error = _extract(doc)
        if error:
            return _failed(file_name, file_hash, error, warnings, id_pattern)
        parsed = parse_resume_text(text, file_name, file_hash, id_pattern)
        parsed.warnings = _dedupe(warnings + parsed.warnings)
        if parsed.parse_status == "empty" and warnings:
            parsed.parse_error = warnings[0]
        return parsed
    except Exception as exc:  # defensive
        return _failed(file_name, file_hash, f"Unexpected parser error ({type(exc).__name__})", [], id_pattern)


def parse_resumes(docs: list[ResumeDoc], max_workers: int = 8,
                  progress_cb: ProgressCallback | None = None,
                  id_pattern: str | None = None) -> dict[str, ParsedResume]:
    """Parse resumes in a thread pool -> {file_hash: ParsedResume} in input order.

    Identical files (same sha256) are parsed once; the first file name wins and the
    others are noted in its warnings. ``progress_cb(done, total, file_name)`` runs on
    the calling thread once per unique file.
    """
    unique: dict[str, ResumeDoc] = {}
    duplicates: dict[str, list[str]] = {}
    for doc in docs or []:
        try:
            digest = doc.sha256
        except Exception:
            continue
        if digest in unique:
            duplicates.setdefault(digest, []).append(_basename(doc.file_name))
        else:
            unique[digest] = doc
    total = len(unique)
    results: dict[str, ParsedResume] = {}
    workers = max(1, min(int(max_workers or 1), total or 1))
    if workers == 1:
        for done, (digest, doc) in enumerate(unique.items(), start=1):
            results[digest] = parse_resume(doc, id_pattern)
            if progress_cb is not None:
                progress_cb(done, total, results[digest].file_name)
    else:
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="resume-parse")
        try:
            futures = {executor.submit(parse_resume, doc, id_pattern): digest for digest, doc in unique.items()}
            for done, future in enumerate(as_completed(futures), start=1):
                digest = futures[future]
                try:
                    results[digest] = future.result()
                except Exception as exc:  # parse_resume never raises; belt and braces
                    results[digest] = _failed(_basename(unique[digest].file_name), digest,
                                              f"Unexpected parser error ({type(exc).__name__})", [], id_pattern)
                if progress_cb is not None:
                    progress_cb(done, total, results[digest].file_name)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    for digest, names in duplicates.items():
        results[digest].warnings.append("Identical file also uploaded as: " + ", ".join(names))
    return {digest: results[digest] for digest in unique}
