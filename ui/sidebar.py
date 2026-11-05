"""Sidebar: data source, optional column-mapping review, run button, LLM and data-quality status."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pandas as pd
import streamlit as st

from screening import ingest
from screening import schema as S
from screening.models import PipelineResult, ResumeDoc
from ui import state

SOURCE_SAMPLE = "Synthetic sample dataset"
SOURCE_UPLOAD = "Upload TA feed + resumes"
IGNORE = "(ignore)"
TA_TYPES = ["csv", "tsv", "txt", "xlsx", "xlsm", "xls"]
RESUME_TYPES = ["pdf", "docx", "txt", "zip"]
_Q_COL_RE = re.compile(r"^q(\d+)_(hash|score|similarity)$")


def render() -> state.RunRequest | None:
    """Data-source controls. Returns a RunRequest when the user clicks Run."""
    with st.sidebar:
        st.markdown("#### Data source")
        source = st.radio("Data source", [SOURCE_SAMPLE, SOURCE_UPLOAD], key="sb_source",
                          label_visibility="collapsed")
        request = _sample_controls() if source == SOURCE_SAMPLE else _upload_controls()
        if st.session_state.get(state.K_RESULT) is not None:
            st.button("Clear results", on_click=state.clear_results, icon=":material/delete_sweep:",
                      width="stretch", key="sb_clear")
    return request


def render_status(result: PipelineResult | None) -> None:
    """LLM availability plus a summary of the last run (data quality, timings)."""
    with st.sidebar:
        st.divider()
        _llm_status()
        if result is not None:
            _run_summary(result)


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
def _sample_controls() -> state.RunRequest | None:
    available = state.sample_available()
    if available:
        st.caption("Synthetic TA/HackerRank export + resumes with planted integrity patterns (no real PII).")
    else:
        st.warning('Sample data not found. Run `python generate_mock_data.py` in the project root, then '
                   "click Run again.", icon=":material/info:")
    if st.button("Run screening", type="primary", icon=":material/play_arrow:", width="stretch",
                 disabled=not available, key="sb_run_sample"):
        return state.RunRequest(label="Synthetic sample", use_sample=True)
    return None


def _upload_controls() -> state.RunRequest | None:
    ta = st.file_uploader("TA / HackerRank export (CSV or Excel)", type=TA_TYPES, key="sb_ta_file")
    resumes = st.file_uploader("Resumes (PDF, DOCX, TXT or a ZIP)", type=RESUME_TYPES,
                               accept_multiple_files=True, key="sb_resumes")
    mapping = _mapping_review(ta) if ta is not None else None
    if ta is None and not resumes:
        st.caption("Upload a TA feed, resumes, or both (resume-only screening is supported).")
    if not st.button("Run screening", type="primary", icon=":material/play_arrow:", width="stretch",
                     disabled=ta is None and not resumes, key="sb_run_upload"):
        return None
    ta_file = (Path(ta.name).name, ta.getvalue()) if ta is not None else None
    docs = [ResumeDoc(file_name=Path(f.name).name, content=f.getvalue(), source="upload") for f in resumes or []]
    label = ta_file[0] if ta_file else f"{len(docs):,} resume file(s)"
    return state.RunRequest(label=f"Upload: {label}", ta_file=ta_file, resume_docs=docs, mapping_override=mapping)


@st.cache_data(show_spinner="Reading the TA feed...", max_entries=4, ttl=3600)
def _read_table_cached(file_name: str, digest: str, _content: bytes) -> pd.DataFrame:
    """ingest.read_table keyed by file name + content hash (bytes not re-hashed)."""
    return ingest.read_table(file_name, _content)


def _field_label(value: str) -> str:
    if value == IGNORE:
        return IGNORE
    if m := _Q_COL_RE.match(value):
        kind = {"hash": "answer hash", "score": "score", "similarity": "code similarity %"}[m.group(2)]
        return f"Q{m.group(1)} {kind} ({value})"
    return f"{S.TA_FIELD_LABELS.get(value, value)} ({value})"


def _example(series: pd.Series) -> str:
    values = series.dropna().astype(str).str.strip()
    values = values[values != ""]
    return values.iloc[0][:40] if len(values) else ""


def _mapping_review(ta) -> dict[str, str] | None:  # noqa: ANN001 - streamlit UploadedFile
    """Editable header -> canonical mapping. Returns an override only when the user changed it."""
    with st.expander("Review column mapping", icon=":material/table_edit:"):
        read_table = state.engine_fn(ingest, "read_table")
        suggest = state.engine_fn(ingest, "suggest_column_mapping")
        if read_table is None or suggest is None:
            st.info("Mapping preview is unavailable in this build; columns will be auto-mapped.")
            return None
        content = ta.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        try:
            raw = _read_table_cached(Path(ta.name).name, digest, content)
            sources = [str(c) for c in raw.columns]
            suggested = {str(k): str(v) for k, v in suggest(sources).items() if v}
        except Exception as exc:  # noqa: BLE001 - malformed upload: explain, fall back to auto-mapping
            st.warning(f"Could not preview this file: {state.safe_error(exc)}")
            return None
        q_cols = sorted({v for v in suggested.values() if _Q_COL_RE.match(v)},
                        key=lambda v: (int(_Q_COL_RE.match(v).group(1)), v))  # type: ignore[union-attr]
        options = [IGNORE, *S.TA_FIELD_LABELS, *q_cols]
        rows = pd.DataFrame({
            "source": sources,
            "example": [_example(raw[c]) for c in raw.columns],
            "maps_to": [suggested.get(c, IGNORE) if suggested.get(c, IGNORE) in options else IGNORE for c in sources],
        })
        edited = st.data_editor(
            rows, key=f"sb_mapping_{digest[:16]}", hide_index=True, width="stretch", num_rows="fixed",
            disabled=["source", "example"],
            column_config={
                "source": st.column_config.TextColumn("Source column"),
                "example": st.column_config.TextColumn("Example"),
                "maps_to": st.column_config.SelectboxColumn("Maps to", options=options, required=True,
                                                            format_func=_field_label),
            },
        )
        mapping: dict[str, str] = {}
        duplicates: list[str] = []
        for src, dst in zip(edited["source"], edited["maps_to"]):
            if not isinstance(dst, str) or dst == IGNORE:
                continue
            if dst in mapping.values():
                duplicates.append(f"{src} -> {dst}")
                continue
            mapping[str(src)] = dst
        if duplicates:
            st.warning("Each field can be mapped once; ignored: " + "; ".join(duplicates))
        missing = [S.TA_FIELD_LABELS[f] for f in S.REQUIRED_FIELDS if f not in mapping.values()]
        if missing:
            st.warning(f"Not mapped: {', '.join(missing)} - candidates will be scored without it.")
        st.caption(f"{len(mapping)} of {len(sources)} columns mapped. Unmapped columns are kept as extra_* fields.")
        return mapping if mapping != suggested else None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def _llm_status() -> None:
    cfg = state.get_config()
    source = state.llm_key_source()
    if source:
        st.badge("LLM: configured", icon=":material/smart_toy:", color="green")
        note = "" if cfg.llm.enabled else " · switched off in Config"
        auth = "No API key needed" if source == state.NO_KEY_SOURCE else f"Key from {source}"
        st.caption(f"{auth} · model `{cfg.llm.model or 'default'}`{note}")
    else:
        st.badge("LLM: not configured", icon=":material/smart_toy:", color="gray")
        st.caption(f"Set {cfg.llm.api_key_env} (environment or .streamlit/secrets.toml) to enable LLM "
                   "re-scoring, rationales and summaries.")


def _run_summary(result: PipelineResult) -> None:
    q = result.quality
    counts = q.counts()
    st.markdown("#### Last run")
    st.caption(f"{state.MODE_LABELS.get(result.mode, result.mode)} · {q.rows_raw:,} rows in feed → "
               f"{q.rows_clean:,} candidates · {q.duplicates_dropped:,} duplicate(s) dropped")
    st.markdown(f"🔴 {counts.get('error', 0):,} errors · 🟠 {counts.get('warning', 0):,} warnings · "
                f"🔵 {counts.get('info', 0):,} info")
    with st.expander("Data quality details", icon=":material/fact_check:"):
        issues = q.to_frame()
        if issues.empty:
            st.caption("No issues recorded.")
        else:
            order = {"error": 0, "warning": 1, "info": 2}
            issues = issues.sort_values("severity", key=lambda s: s.map(order).fillna(3), kind="stable")
            st.dataframe(issues, hide_index=True, height=260, width="stretch")
        if q.missing_fields:
            st.caption("Optional fields absent: " + ", ".join(S.TA_FIELD_LABELS.get(f, f) for f in q.missing_fields))
        if q.unmapped_columns:
            st.caption("Kept as extra_* columns: " + ", ".join(map(str, q.unmapped_columns)))
    if result.timings:
        with st.expander("Timings", icon=":material/timer:"):
            st.dataframe(pd.DataFrame({"stage": list(result.timings),
                                       "seconds": [round(float(v), 2) for v in result.timings.values()]}),
                         hide_index=True, width="stretch")
