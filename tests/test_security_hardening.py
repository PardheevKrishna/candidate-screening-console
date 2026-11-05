"""Security hardening: decompression-bomb guards for resume files, sanitising of LLM-written
Markdown, the default listen address, and the LLM key name the UI tests use.

Every crafted file is generated in tmp_path at test time; nothing touches the network.
"""
from __future__ import annotations

import base64
import binascii
import io
import re
import struct
import threading
import time
import tomllib
import tracemalloc
import zipfile
import zlib
from pathlib import Path
from typing import Any

import docx
import pypdf
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from screening import resume_parser as RP
from screening.config import LLMConfig, ProcessingConfig
from screening.models import ResumeDoc
from ui import theme

ROOT = Path(__file__).resolve().parent.parent
FAST = 5.0                      # seconds a crafted file may take at most
MB = 1024 * 1024
ZWSP = "\u200b"
TEXT_PAGE = (b"BT /F1 12 Tf 72 720 Td 16 TL (Sample Candidate) Tj T* "
             b"(sample.candidate@example.com | 9123456789) Tj T* "
             b"(TECHNICAL SKILLS) Tj T* (Python, Java, SQL, Docker and Kubernetes) Tj ET")
RESUME_LINES = [
    "Sample Candidate",
    "sample.candidate@example.com | 9123456789",
    "EDUCATION",
    "B.E. Electronics and Communication Engineering, PQR College of Engineering, 2020 - 2024",
    "TECHNICAL SKILLS",
    "Languages: C, C++, Java, Python",
    "PROJECTS",
    "IoT Weather Station | Arduino, Python",
    "• Collected sensor data from 20 nodes and visualised it with Grafana.",
]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _deflate_repeated(unit: bytes, total: int) -> bytes:
    """zlib stream of ``unit`` repeated up to ``total`` bytes, compressed in 1 MB steps."""
    deflater = zlib.compressobj(9)
    block = unit * max(1, MB // len(unit))
    parts: list[bytes] = []
    left = total
    while left > 0:
        piece = block[:left]
        parts.append(deflater.compress(piece))
        left -= len(piece)
    parts.append(deflater.flush())
    return b"".join(parts)


@pytest.fixture(scope="module")
def flate_bomb() -> bytes:
    """A Flate stream that inflates to 100 MB of content-stream operators (~100 KB compressed)."""
    return _deflate_repeated(b"q Q\n", 100 * MB)


def _stream(data: bytes, filters: bytes = b"/FlateDecode") -> bytes:
    return b"<< /Length %d /Filter %s >>\nstream\n%s\nendstream" % (len(data), filters, data)


def _pdf(contents: list[bytes], extra: tuple[bytes, ...] = ()) -> bytes:
    """Minimal valid PDF: page i draws the (already Flate-compressed) content stream contents[i];
    ``extra`` objects are added after the pages, listed in the xref but referenced by nothing."""
    kids = b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(len(contents)))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(contents)),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, data in enumerate(contents):
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (5 + 2 * i))
        objects.append(_stream(data))
    objects.extend(extra)
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    out.write(b"".join(b"%010d 00000 n \n" % offset for offset in offsets))
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def _reportlab_pdf(pages: list[list[str]]) -> bytes:
    """PDF the way most tests build resumes (reportlab, ASCII85 + Flate content streams)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lines in pages:
        y = 800
        for line in lines:
            c.drawString(50, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


def _docx_bytes() -> bytes:
    d = docx.Document()
    d.add_paragraph("Sample Candidate")
    d.add_paragraph("sample.candidate@example.com | 9988776655")
    d.add_heading("Technical Skills", level=1)
    for skill in ("Python", "SQL", "Machine Learning"):
        d.add_paragraph(skill, style="List Bullet")
    d.add_heading("Projects", level=1)
    d.add_paragraph("Fraud Detection Engine")
    d.add_paragraph("Built an XGBoost model that flags risky card transactions.", style="List Bullet")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _rezip(source: bytes, *, replace: dict[str, Any] | None = None, add: dict[str, Any] | None = None,
           method: int = zipfile.ZIP_DEFLATED) -> bytes:
    """Copy a DOCX, swapping or adding parts. A part given as an iterator of chunks is
    streamed into the archive, so huge parts never sit in memory."""
    replace, add = replace or {}, add or {}
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as src, zipfile.ZipFile(out, "w", method) as dst:
        names = [info.filename for info in src.infolist()]
        for name in names + [n for n in add if n not in names]:
            data = replace.get(name, add.get(name))
            if data is None:
                dst.writestr(name, src.read(name))
            elif isinstance(data, bytes):
                dst.writestr(name, data)
            else:
                with dst.open(name, "w") as fh:
                    for chunk in data:
                        fh.write(chunk)
    return out.getvalue()


def _repeated(unit: bytes, total: int):
    """Whole copies of ``unit`` adding up to at least ``total`` bytes, in ~1 MB chunks."""
    per_chunk = max(1, MB // len(unit))
    copies = -(-total // len(unit))
    for done in range(0, copies, per_chunk):
        yield unit * min(per_chunk, copies - done)


def _document_xml(total: int):
    """word/document.xml of at least ``total`` bytes: one line of resume text, repeated."""
    yield (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>')
    yield from _repeated(b"<w:p><w:r><w:t>Python developer with SQL and Java experience</w:t></w:r></w:p>", total)
    yield b"</w:body></w:document>"


def _patch_declared_size(data: bytes, member: str, size: int) -> bytes:
    """Make the ZIP central directory claim ``size`` uncompressed bytes for ``member``."""
    buf = bytearray(data)
    pos = buf.find(b"PK\x01\x02")
    while pos >= 0:
        name_len = struct.unpack_from("<H", buf, pos + 28)[0]
        if bytes(buf[pos + 46:pos + 46 + name_len]) == member.encode():
            struct.pack_into("<I", buf, pos + 24, size)
            return bytes(buf)
        pos = buf.find(b"PK\x01\x02", pos + 46)
    raise AssertionError(f"{member} not in the ZIP directory")


def _from_disk(tmp_path: Path, name: str, data: bytes) -> ResumeDoc:
    path = tmp_path / name
    path.write_bytes(data)
    return ResumeDoc(path.name, path.read_bytes(), "folder")


def _timed_parse(doc: ResumeDoc):
    start = time.perf_counter()
    parsed = RP.parse_resume(doc)
    return parsed, time.perf_counter() - start


@pytest.fixture
def fallback_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Records every run of the pdfplumber / pdfminer fallback (which still runs normally)."""
    calls: list[int] = []
    real = RP._pdfplumber_text

    def spy(content: bytes) -> str:
        calls.append(len(content))
        return real(content)

    monkeypatch.setattr(RP, "_pdfplumber_text", spy)
    return calls


# ---------------------------------------------------------------------------
# PDF: pypdf limits, page cap and the fallback gate
# ---------------------------------------------------------------------------
def test_pdf_with_100mb_flate_content_stream_is_refused_quickly(tmp_path, flate_bomb, fallback_calls):
    doc = _from_disk(tmp_path, "CAND-00042_Bomb.pdf", _pdf([flate_bomb]))
    assert len(doc.content) < 200_000                 # ~100 KB on disk, 100 MB when inflated
    parsed, elapsed = _timed_parse(doc)
    assert elapsed < FAST, elapsed
    assert parsed.parse_status == "error"
    assert "safety limits" in parsed.parse_error and "decompression bomb" in parsed.parse_error
    assert parsed.candidate_id_guess == "CAND-00042"
    assert fallback_calls == []                       # pdfminer never sees the file
    text, warnings = RP.extract_text(doc)
    assert text == "" and warnings[0] == parsed.parse_error


def test_pdf_keeps_pages_read_before_a_bomb_page(tmp_path, flate_bomb, fallback_calls):
    doc = _from_disk(tmp_path, "mixed.pdf", _pdf([zlib.compress(TEXT_PAGE), flate_bomb, zlib.compress(TEXT_PAGE)]))
    parsed, elapsed = _timed_parse(doc)
    assert elapsed < FAST, elapsed
    assert parsed.parse_status == "ok" and parsed.phones == ["9123456789"]
    assert {"Python", "Java", "SQL"} <= set(parsed.skills)
    assert any(w.startswith("PDF page 2 exceeds the safety limits") for w in parsed.warnings)
    assert fallback_calls == []


def test_bomb_that_pypdf_never_opens_still_blocks_the_fallback(tmp_path, flate_bomb, fallback_calls):
    """The blank page gives pypdf no text and no error; the unreferenced bomb stream is
    exactly what pdfminer could inflate without limits, so the fallback must not run."""
    data = _pdf([zlib.compress(b"q Q")], extra=(_stream(flate_bomb),))
    parsed, elapsed = _timed_parse(_from_disk(tmp_path, "scan.pdf", data))
    assert elapsed < FAST, elapsed
    assert parsed.parse_status == "empty" and "OCR" in parsed.parse_error
    assert fallback_calls == []
    assert RP._pdf_fallback_allowed(_pdf([zlib.compress(b"q Q")]), 1)   # same file without the bomb


def test_pdf_over_page_limit_is_refused(tmp_path, fallback_calls):
    pages = [[f"Page {i + 1}", "Sample Candidate - Python, Java, SQL and Docker"] for i in range(RP.PDF_MAX_PAGES + 1)]
    parsed, elapsed = _timed_parse(_from_disk(tmp_path, "portfolio.pdf", _reportlab_pdf(pages)))
    assert elapsed < FAST, elapsed
    assert parsed.parse_status == "error"
    assert parsed.parse_error == "PDF has 51 pages - files longer than 50 pages are not read as resumes"
    assert fallback_calls == []
    parsed, _ = _timed_parse(_from_disk(tmp_path, "long_but_ok.pdf", _reportlab_pdf(pages[:-1])))
    assert parsed.parse_status == "ok"


def test_fallback_counts_page_objects_when_pypdf_cannot():
    one, many = _reportlab_pdf([RESUME_LINES]), _reportlab_pdf([["x"]] * (RP.PDF_MAX_PAGES + 1))
    assert RP._pdf_fallback_allowed(one, None)
    assert not RP._pdf_fallback_allowed(many, None)
    assert RP._pdf_fallback_allowed(many, 3)          # pypdf's own count wins when it has one


def test_normal_pdfs_still_parse_and_still_get_the_fallback(tmp_path, fallback_calls):
    parsed, _ = _timed_parse(_from_disk(tmp_path, "resume.pdf", _reportlab_pdf([RESUME_LINES])))
    assert parsed.parse_status == "ok" and parsed.phones == ["9123456789"]
    assert {"C", "C++", "Java", "Python", "Arduino", "Grafana"} <= set(parsed.skills)

    image_only = io.BytesIO()
    c = canvas.Canvas(image_only, pagesize=A4)
    c.rect(100, 400, 300, 200, fill=1)
    c.save()
    fallback_calls.clear()
    parsed, _ = _timed_parse(_from_disk(tmp_path, "scan.pdf", image_only.getvalue()))
    assert parsed.parse_status == "empty" and "OCR" in parsed.parse_error
    assert len(fallback_calls) == 1                   # ASCII85 + Flate streams pass the pre-check


@pytest.mark.parametrize("extra, allowed", [
    (_stream(base64.a85encode(zlib.compress(b"q Q")) + b"~>", b"[/ASCII85Decode /FlateDecode]"), True),
    (_stream(binascii.hexlify(b"q Q") + b">", b"/AHx"), True),
    (_stream(b"\xff\xd8\xff\xe0 jpeg data", b"/DCTDecode"), True),          # images are never inflated
    (b"<< /Length 12 >>\nstream\nplain stream\nendstream", True),
    (b"<< /Length 99999999999 >>\nstream\nabc\nendstream", False),          # declared /Length over the cap
    (_stream(b"\x80\x0b\x60\x50", b"/LZWDecode"), False),                  # filters that are not checked
    (_stream(b"\x00a\x80", b"/RunLengthDecode"), False),
    (_stream(zlib.compress(b"q Q"), b"9 0 R"), False),                     # indirect /Filter
    (_stream(b"x\x9c" + bytes(range(256)) * 4), False),                    # corrupt zlib data
    (_stream(zlib.compress(b"q Q" * 500)[:-8]), False),                    # zlib stream cut short
], ids=["a85+flate", "ahx", "dct", "no-filter", "length", "lzw", "runlength", "indirect", "corrupt", "truncated"])
def test_fallback_precheck_filters(extra, allowed):
    assert RP._pdf_fallback_allowed(_pdf([zlib.compress(b"q Q")], extra=(extra,)), 1) is allowed


@pytest.mark.parametrize("wrap", ["escaped-name", "a85-wrapped"])
def test_fallback_precheck_sees_through_name_escapes_and_filter_chains(flate_bomb, wrap):
    extra = (_stream(flate_bomb, b"/Fl#61teDecode") if wrap == "escaped-name"
             else _stream(base64.a85encode(flate_bomb) + b"~>", b"[/A85 /Fl]"))
    start = time.perf_counter()
    assert not RP._pdf_fallback_allowed(_pdf([zlib.compress(b"q Q")], extra=(extra,)), 1)
    assert time.perf_counter() - start < FAST


def test_pypdf_limits_apply_only_to_the_reading_thread():
    default = pypdf.get_configuration().zlib_maximum_output_length
    seen: dict[str, int] = {}
    with RP._pdf_limits():
        seen["inside"] = pypdf.get_configuration().zlib_maximum_output_length
        other = threading.Thread(target=lambda: seen.setdefault(
            "other thread", pypdf.get_configuration().zlib_maximum_output_length))
        other.start()
        other.join()
    seen["after"] = pypdf.get_configuration().zlib_maximum_output_length
    assert seen == {"inside": min(default, RP.PDF_MAX_DECODED_MB * MB), "other thread": default, "after": default}


def test_parallel_batch_with_bombs_and_normal_resumes(tmp_path, flate_bomb):
    docs = [_from_disk(tmp_path, "bomb1.pdf", _pdf([flate_bomb])),
            _from_disk(tmp_path, "resume.pdf", _reportlab_pdf([RESUME_LINES])),
            _from_disk(tmp_path, "bomb2.pdf", _pdf([flate_bomb, flate_bomb])),
            _from_disk(tmp_path, "resume.docx", _docx_bytes())]
    start = time.perf_counter()
    out = RP.parse_resumes(docs, max_workers=4)
    assert time.perf_counter() - start < FAST
    assert [p.parse_status for p in out.values()] == ["error", "ok", "error", "ok"]
    assert pypdf.get_configuration().zlib_maximum_output_length == pypdf.Configuration().zlib_maximum_output_length


# ---------------------------------------------------------------------------
# DOCX zip-bomb guard
# ---------------------------------------------------------------------------
def test_docx_with_40mb_document_xml_is_refused_before_python_docx(tmp_path, monkeypatch):
    data = _rezip(_docx_bytes(), replace={"word/document.xml": _document_xml(40 * MB)})
    doc = _from_disk(tmp_path, "CAND-00077_Bomb.docx", data)
    assert len(doc.content) < 2 * MB
    monkeypatch.setattr(docx, "Document", lambda *a, **k: pytest.fail("python-docx must not open a zip bomb"))
    parsed, elapsed = _timed_parse(doc)
    assert elapsed < FAST, elapsed
    assert parsed.parse_status == "error"
    assert parsed.parse_error == "DOCX unpacks to more than 30 MB (possible zip bomb) - not read"
    assert parsed.candidate_id_guess == "CAND-00077"


@pytest.mark.parametrize("kind, message", [
    ("parts", "DOCX has more than 2,000 internal parts (possible zip bomb) - not read"),
    ("ratio", "Suspicious compression ratio inside the DOCX (possible zip bomb) - not read"),
    ("method", "DOCX uses an unsupported compression method - not read"),
])
def test_docx_directory_guards(tmp_path, kind, message):
    base = _docx_bytes()
    if kind == "parts":
        data = _rezip(base, add={f"customXml/item{i}.xml": b"<a/>" for i in range(RP.DOCX_MAX_PARTS)})
    elif kind == "ratio":
        data = _rezip(base, add={"word/media/blank.bin": _repeated(b"\0", 2 * MB)})   # > 1 MB at ~1000:1
    else:
        data = _rezip(base, method=zipfile.ZIP_BZIP2)                      # inflated without any bound
    parsed, elapsed = _timed_parse(_from_disk(tmp_path, "crafted.docx", data))
    assert elapsed < FAST, elapsed
    assert (parsed.parse_status, parsed.parse_error) == ("error", message)


def test_docx_small_parts_may_compress_well(tmp_path):
    data = _rezip(_docx_bytes(), add={"word/media/blank.bin": _repeated(b"\0", MB // 2)})   # ~1000:1, only 0.5 MB
    parsed, _ = _timed_parse(_from_disk(tmp_path, "resume.docx", data))
    assert parsed.parse_status == "ok" and parsed.emails == ["sample.candidate@example.com"]
    assert {"Python", "SQL", "Machine Learning", "XGBoost"} <= set(parsed.skills)


def test_docx_with_lying_zip_directory_is_inflated_in_bounded_steps(tmp_path):
    """The directory claims 1,000 bytes for a part that inflates to 200 MB: ZipFile.read()
    (what python-docx calls) would inflate all of it before noticing; the guard reads
    1 MB at a time and stops at the declared size."""
    data = _rezip(_docx_bytes(), replace={"word/document.xml": _document_xml(200 * MB)})
    doc = _from_disk(tmp_path, "liar.docx", _patch_declared_size(data, "word/document.xml", 1000))
    tracemalloc.start()
    try:
        parsed, elapsed = _timed_parse(doc)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert elapsed < FAST, elapsed
    assert peak < 32 * MB, peak
    assert parsed.parse_status == "error" and parsed.parse_error.startswith("Could not read DOCX")


def test_zip_upload_skips_members_with_unbounded_compression():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", b"Aaa Person\nSkills: Python", compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("b.txt", b"Bbb Person\nSkills: SQL", compress_type=zipfile.ZIP_BZIP2)
        zf.writestr("c.txt", b"Ccc Person\nSkills: Java", compress_type=zipfile.ZIP_LZMA)
    out, issues = RP.expand_uploads([ResumeDoc("batch.zip", buf.getvalue())], ProcessingConfig())
    assert [d.file_name for d in out] == ["a.txt"]
    assert sorted(i["file_name"] for i in issues if "unsupported compression method" in i["issue"]) == ["b.txt", "c.txt"]


# ---------------------------------------------------------------------------
# LLM Markdown: nothing may fetch a URL or render as a link
# ---------------------------------------------------------------------------
def _unescaped(text: str, char: str) -> int:
    """How many ``char`` in Markdown source are not backslash-escaped."""
    return sum(1 for m in re.finditer(rf"\\[\s\S]|{re.escape(char)}", text) if m.group() == char)


def test_safe_markdown_inline_images_and_links_keep_only_their_text():
    text = ('## Brief\n![x](https://evil.example/p?d=1) see [site](https://evil.example "t") and '
            "[![logo](https://evil.example/a.png)](https://evil.example/b), "
            "![chart [v2]](https://evil.example/p_(1).png) ![t](<https://evil.example/s p.png>) "
            "[wrapped\nlink](//evil.example/w)")
    out = theme.safe_markdown(text)
    assert out == "## Brief\nx see site and logo, chart \\[v2] t wrapped\nlink"
    assert _unescaped(out, "[") == 0


def test_safe_markdown_reference_style_links_images_and_definitions():
    text = ("Pixel ![beacon][img] and ![shortcut] plus [docs][Site] and [Site][] and [site].\n"
            "Plain [1][2] and [note] stay text.\n\n"
            '[img]: https://evil.example/i.png?d=secret "x"\n'
            "   [SITE]: <https://evil.example/s>\n"
            "[shortcut]: https://evil.example/s.png\n"
            "[1]: see appendix\n")
    out = theme.safe_markdown(text)
    assert out == ("Pixel beacon and shortcut plus docs and Site and site.\n"
                   "Plain \\[1]\\[2] and \\[note] stay text.\n\n"
                   "\\[1]: see appendix\n")
    assert "evil.example" not in out and _unescaped(out, "[") == 0


def test_safe_markdown_autolinks_and_bare_urls_stay_visible_but_unlinked():
    text = "Mail <a.b@example.com>, see <https://evil.example/x?q=1>, https://evil.example/y and WWW.evil.example/z"
    out = theme.safe_markdown(text)
    assert out.replace(ZWSP, "") == ("Mail a.b@example.com, see https://evil.example/x?q=1, "
                                     "https://evil.example/y and WWW.evil.example/z")
    assert "<" not in out and not re.search(r"(?i)https?://|www\.", out)


def test_safe_markdown_keeps_formatting_escapes_and_is_fast():
    text = "### Risks\n- **High**: 2 pairs\n- cost $5, not \\$6\n- literal \\[x](https://e.example/a)\n<img src=x>"
    out = theme.safe_markdown(text)
    assert out == (f"### Risks\n- **High**: 2 pairs\n- cost \\$5, not \\$6\n"
                   f"- literal \\[x](https:{ZWSP}//e.example/a)\n")
    assert theme.safe_markdown(None) == ""
    start = time.perf_counter()
    for blob in ("[" * 100_000, "\\" * 100_000 + "[", "![a](" * 30_000, "[" + "[]" * 50_000, "<a" * 50_000):
        assert _unescaped(theme.safe_markdown(blob), "[") == 0
    assert time.perf_counter() - start < FAST


# ---------------------------------------------------------------------------
# Listen address and the LLM key name
# ---------------------------------------------------------------------------
def test_streamlit_listens_on_localhost_unless_overridden():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["server"]["address"] == "127.0.0.1"
    ps1 = (ROOT / "scripts" / "run_app.ps1").read_text(encoding="utf-8-sig")
    sh = (ROOT / "scripts" / "run_app.sh").read_text(encoding="utf-8-sig")
    assert "'--server.address', $Address" in ps1 and "[string]$Address = '127.0.0.1'" in ps1
    assert '--server.address "$ADDRESS"' in sh and 'ADDRESS="127.0.0.1"' in sh


def test_ui_tests_take_the_llm_key_name_from_config():
    key_name = LLMConfig().api_key_env
    if not key_name:
        pytest.skip("the configured LLM backend needs no API key")
    offenders = [p.name for p in sorted((ROOT / "tests").glob("test_app_*.py"))
                 if key_name in p.read_text(encoding="utf-8")]
    assert offenders == []
