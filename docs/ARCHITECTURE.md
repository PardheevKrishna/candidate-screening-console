# Architecture & Module Contracts

Candidate Screening & Integrity Console: a Streamlit app that fuses an assessment export (the Workday
assessment export `.xlsx`, or a HackerRank / other CSV or Excel file) with bulk resumes (`Resume.zip`
of multi-candidate PDF packets, or individual PDF/DOCX/TXT files and ZIPs of them), scores every
candidate with a weighted rubric (default 40/30/20/10), applies structured eligibility rules,
shortlists the top N with configurable tie-breaks, and runs an integrity engine that explains what
candidates who scored 100% have in common. An optional LLM (GitHub Copilot, reached through the local
VS Code extension in `vscode-copilot-proxy/`) writes explanatory text only: it never changes a score,
rank, cutoff or the shortlist.

Related documents: [README](../README.md) (overview and quick start), [USER_GUIDE.md](USER_GUIDE.md)
(every tab and field, for recruiters), [DEPLOYMENT.md](DEPLOYMENT.md) (installing and running the app
on a company machine), [HACKERRANK_DATA_REFERENCE.md](HACKERRANK_DATA_REFERENCE.md) (assessment
export reference: the Workday columns, the `Resume.zip` packet layout and the HackerRank fields),
[COPILOT_PROXY.md](COPILOT_PROXY.md) (GitHub Copilot proxy: setup, use and troubleshooting),
[vscode-copilot-proxy/README.md](../vscode-copilot-proxy/README.md) (the extension itself) and
[data/README.md](../data/README.md) (the data folder and the synthetic sample).

## Contents

1. [Module map](#module-map)
2. [Global rules (all modules)](#global-rules-all-modules)
3. [Data flow](#data-flow)
4. [screening/ingest.py](#screeningingestpy)
5. [screening/resume_parser.py + screening/skills.py](#screeningresume_parserpy--screeningskillspy)
6. [screening/injection.py](#screeninginjectionpy)
7. [screening/matching.py](#screeningmatchingpy)
8. [screening/duplicates.py](#screeningduplicatespy)
9. [screening/scoring.py](#screeningscoringpy)
10. [screening/risk.py](#screeningriskpy)
11. [Column contract: `PipelineResult.candidates`](#column-contract-pipelineresultcandidates)
12. [LLM layer (llm_backends.py, llm.py)](#llm-layer-llm_backendspy-llmpy)
13. [VS Code extension: vscode-copilot-proxy/](#vs-code-extension-vscode-copilot-proxy)
14. [screening/export.py](#screeningexportpy)
15. [Saved runs and fast reload (runs.py, snapshots.py)](#saved-runs-and-fast-reload-runspy-snapshotspy)
16. [screening/env.py](#screeningenvpy)
17. [UI (app.py + ui/)](#ui-apppy--ui)
18. [Assessment export fields (addendum)](#assessment-export-fields-addendum)
19. [Shortlist controls, comparison, run history and deployment](#shortlist-controls-comparison-run-history-and-deployment)
20. [Tests and the end-to-end check](#tests-and-the-end-to-end-check)

## Module map

```
app.py                      Streamlit entry (thin): loads the optional .env (screening/env.py) before the
                            ui modules are imported, then page config, theme, sidebar, tabs; a feature tab
                            whose module fails to import is hidden instead of breaking the app
ui/                         Streamlit-only code (imports streamlit)
  theme.py                  brand CSS, header banner, KPI cards, badges, getting-started panel
  state.py                  session_state keys, config get/set, input fingerprint, snapshot load/save,
                            evaluate memo, LLM token / status / client, run_screening
  sidebar.py                data source, recent datasets (fast reload), column-mapping editor and mapping
                            profiles, run button, data-quality summary, Copilot status badge
  charts.py                 plotly figures (pure functions -> go.Figure) and shared label helpers
  tab_shortlist.py          Tab 1 Candidate Shortlist & Leaderboard (+ ineligible candidates, LLM rationales /
                            borderline gap analysis, CSV and Excel downloads)
  tab_risk.py               Tab 2 Integrity & Risk Intelligence (+ adversarial resume text, LLM summaries)
  tab_deepdive.py           Tab 3 Individual Deep-Dive (sections A. Assessment / B. Resume / C. Academic /
                            D. Integrity, evidence pack, LLM explanation)
  tab_compare.py            Tab 4 Compare Candidates
  tab_campus.py             Tab 5 Campus Distribution
  tab_history.py            Tab 6 Run History (also shown before the first run)
  tab_config.py             Tab 7 Config & Rubric Tuning (also shown before the first run)
screening/                  engine (NO streamlit imports; unit-testable)
  schema.py                 canonical column names, header aliases, vocabulary, COLUMN_SOURCE groups,
                            eligibility rule keys
  config.py                 AppConfig dataclasses, TIE_BREAK_KEYS, PRESETS, LLM_PROVIDERS (one entry: "copilot")
  models.py                 ResumeDoc, ParsedResume, DataQualityReport, RiskResult, PipelineResult
  pipeline.py               PreparedData, prepare_data / evaluate / run_pipeline / load_sample_dataset
  ingest.py                 assessment export loading (Workday, HackerRank or any CSV/XLSX): sheet selection,
                            header auto-mapping, Workday comment parsing, normalisation
  resume_parser.py          upload / ZIP expansion, multi-candidate packet splitting, text extraction,
                            section / entity parsing
  injection.py              hidden-text and prompt-injection detection in resumes
  skills.py                 skill taxonomy, extraction, required-skill matching
  matching.py               resume <-> candidate linkage
  duplicates.py             duplicate applications (one person behind several candidate IDs)
  scoring.py                rubric scoring, college tiers, eligibility rules, tie-breaks, shortlist / status,
                            college cap, borderline band, score explanations
  risk.py                   integrity & anomaly engine
  analytics.py              campus distribution, shortlist diff (preview impact, run comparison)
  runs.py                   saved runs (RunStore) + the prepared-data writer / reader shared with snapshots
  snapshots.py              fast reload: prepared datasets cached on disk per input fingerprint
  llm.py                    LLMClient (retries, model fallback, JSON, disk cache) + the LLM text features
  llm_backends.py           transport to the local Copilot proxy: ProxyChatClient, connection-file discovery,
                            proxy_status, error classes, token masking
  env.py                    optional project .env loading
  export.py                 CSV / Excel / evidence-pack exports
vscode-copilot-proxy/       VS Code extension "Screening Copilot Proxy" (plain JavaScript, no build step)
  package.json              manifest: commands, settings, activation, engines (VS Code >= 1.95)
  extension.js              VS Code wiring: language-model API, settings, token, commands, status bar, log
  lib/proxy-core.js         HTTP server and request path (no `vscode` import; unit-tested with plain Node)
  test/                     node --test suites with fake language-model objects
generate_mock_data.py       synthetic Workday-style export + Resume.zip packets + ground truth (-> data/sample/,
                            generated locally; data/ is gitignored except data/README.md)
scripts/                    setup.ps1 / setup.sh, run_app.ps1 / run_app.sh, run_e2e.py (end-to-end check)
tests/                      pytest (pytest.ini sets pythonpath=. and testpaths=tests)
.env.example                optional settings (COPILOT_PROXY_TOKEN, SCREENING_RUNS_DIR, APP_BRAND_EYEBROW)
```

## Global rules (all modules)

* Python 3.11 to 3.14 (64-bit) in a virtual environment; the setup scripts look for these versions
  (a newer interpreter passed explicitly with `-PythonExe` / `--python` is used with a warning).
  `requirements.txt` uses compatible version ranges, not exact pins: streamlit `>=1.50,<2`,
  pandas `>=3.1.0,<3.2`, numpy `>=2.3,<3`, scikit-learn `>=1.7,<2`, scipy `>=1.15,<2`, plotly `>=6,<8`,
  networkx `>=3.4,<4`, rapidfuzz `>=3.10,<4`, pypdf `>=5.1,<7`, pdfplumber `>=0.11,<0.12`, python-docx
  `>=1.1,<2`, openpyxl `>=3.1,<4`, xlsxwriter `>=3.2,<4`, pyarrow `>=18,<28`, requests `>=2.31,<3` (HTTP
  client for the Copilot proxy), python-dotenv `>=1.0,<2` (optional .env loading), reportlab `>=4.2,<6`
  (synthetic data only) and pytest `>=8,<10`. Do not add dependencies without a strong reason.
* **pandas 3.x semantics**: Copy-on-Write is always on - never use chained assignment
  (`df[col][mask] = x`); use `df.loc[mask, col] = x`. Default string dtype is `str`
  (missing = NaN). Don't rely on object dtype for strings. Avoid deprecated APIs.
* `screening/` must not import streamlit. Use `from . import schema as S` and refer to
  columns as `S.CANDIDATE_ID`, never string literals.
* Never raise out of per-row/per-file work: record problems in DataQualityReport issues
  (`{"severity","stage","issue","candidate_id","file_name"}`) and keep going.
* No PII in logs/exceptions. Never log or print tokens or keys. Resume text, prompts and LLM replies
  are never logged; issue texts name candidate IDs, never names or e-mail addresses.
* **Rules decide, the LLM explains.** Eligibility, scores, ranks, cutoffs and statuses come only from
  the rules in `scoring.py`. LLM output is stored next to the results (session stores, exports, saved
  runs) and never feeds back into them.
* On-disk formats are JSON, Parquet and gzipped JSON only - never pickle - so opening a saved run or a
  cached dataset cannot execute code.
* Cells that hold lists are strings joined with `S.SEP_LIST` (", "), `S.SEP_NOTES` ("; ")
  or `S.SEP_REASONS` (" | ") as documented per column.
* Determinism: same inputs + config -> same outputs (sort before tie-breaks; seed RNGs).
* Performance budget at 3,000 candidates / 2,900 resumes: prepare_data < 90 s,
  evaluate < 5 s, risk analysis < 3 s for a focus group of ~300. For reference, `scripts/run_e2e.py` on
  the default synthetic sample (2,133 applications, 33 packets, 1,877 resumes, 1,360 perfect scorers)
  reports prepare_data at about 35-40 s (almost all of it resume parsing) and evaluate under 1 s.
* `progress_cb` callbacks are invoked **only from the calling thread** (never from worker
  threads) - Streamlit elements cannot be updated from other threads.

## Data flow

```
prepare_data(ta_file, resume_docs, cfg, *, mapping_override=None, progress_cb=None) -> PreparedData
  ta_file ──ingest.load_ta_feed──▶ ta_df       sheet selection, header mapping, Workday comment parsing,
                                                "Job Application" names, "#N/A" = missing, normalisation
  resume_docs ──resume_parser.expand_uploads──▶ docs
                                                ZIP members read one at a time; multi-candidate PDF packets
                                                are split per candidate BEFORE the per-resume size cap
  docs ──resume_parser.parse_resumes──▶ parsed {file_hash: ParsedResume}
                                                hidden PDF text blanked, invisible characters stripped,
                                                injection_findings recorded
  matching.match_resumes ──▶ matches
  matching.link_candidates ──▶ base, parsed_by_candidate
  duplicates.detect_duplicates(base, parsed_by_candidate) ──▶ base + duplicate_of / duplicate_ids /
                                                duplicate_evidence
  mode = "resumes_only" (no TA file) | "ta_only" (no resume parsed) | "ta+resumes"

evaluate(prepared, cfg, *, risk_result=None) -> PipelineResult
  scoring.score_candidates(base, parsed_by_candidate, cfg, mode=mode)
                                                tiers (only when enabled), "auto" missing-resume policy
  risk.run_risk_analysis(scored, parsed_by_candidate, cfg, parsed)      (skipped when risk_result is given)
                                                team-project rule, resume-text flags,
                                                duplicate applications never paired
  _merge_risk ──▶ scoring.apply_shortlist(df, cfg)
                                                eligibility rules -> INELIGIBILITY JSON (incl. exclusions
                                                and duplicates), tie-breaks, cutoff, holds, college cap
              ──▶ PipelineResult.candidates

LLM (optional, rationales only, never inside evaluate): llm.explain_candidates / summarize_clusters /
executive_summary -> session stores -> exports and saved runs.
```

```python
@dataclass
class PreparedData:                                 # output of the heavy stages; independent of rubric/risk settings
    ta_df: pd.DataFrame | None
    quality: DataQualityReport
    parsed: dict[str, ParsedResume]                 # file_hash -> ParsedResume
    matches: pd.DataFrame
    base: pd.DataFrame                              # one row per candidate (TA + linkage + duplicate columns)
    parsed_by_candidate: dict[str, ParsedResume]    # candidate_id -> ParsedResume
    mode: str = "ta+resumes"                        # "ta+resumes" | "ta_only" | "resumes_only"
    timings: dict[str, float]

def prepare_data(ta_file: tuple[str, bytes] | None, resume_docs: list[ResumeDoc], cfg: AppConfig, *,
                 mapping_override: dict[str, str] | None = None,
                 progress_cb: ProgressCB | None = None) -> PreparedData
def evaluate(prepared: PreparedData, cfg: AppConfig, *, risk_result: RiskResult | None = None) -> PipelineResult
    # deterministic for given prepared data + config (no LLM input); risk_result reuses an earlier
    # analysis when only non-risk settings changed
def run_pipeline(ta_file, resume_docs, cfg=None, *, mapping_override=None, progress_cb=None) -> PipelineResult
    # prepare_data + evaluate, no LLM (tests and scripts)
def load_sample_dataset(sample_dir=None) -> tuple[tuple[str, bytes] | None, list[ResumeDoc], str | None]
    # data/sample: test_scores_export.xlsx, Resume.zip as ONE upload that expand_uploads expands and
    # splits, job_description.txt
def sample_dataset_available(sample_dir=None) -> bool
```

* `ProgressCB(stage, done, total, detail)`. Stages: "Loading TA feed", "Reading resume files" (one
  step per uploaded file or supported ZIP member), "Parsing resumes" (one step per unique file),
  "Matching resumes", "Checking duplicate applications".
* `timings` keys: `ingest`, `parse` (includes upload expansion and packet splitting), `match`,
  `duplicates`, then `score`, `risk`, `shortlist` from evaluate. Prepared data loaded from a snapshot
  carries `{"snapshot_load": seconds}` instead of the preparation timings.
* A failure inside duplicate detection becomes a warning issue ("duplicates were not marked"); the run
  continues.
* `_merge_risk` adds `is_focus, risk_score, risk_level, risk_reasons, cluster_id, n_links,
  max_pair_score, individual_score, injection_flags` (defaults False / 0 / "Clear" / "" when the risk
  analysis has no row for a candidate).
* `PipelineResult`: `candidates, parsed_resumes, parsed_by_candidate, matches, risk, quality, config,
  llm_results, timings, mode`. `llm_results` is always `{}` for new runs (the LLM never changes
  scores); it is kept so runs saved by older versions still load.

## screening/ingest.py

```python
def read_table(file_name: str, content: bytes) -> pd.DataFrame
    # Every column as str (missing = NaN). Raises ValueError (message safe to show) only when the
    # file cannot be read as a table: empty, corrupt, or a legacy binary .xls ("re-save it as .xlsx
    # or CSV").
    # Workbooks (.xlsx/.xlsm/.xltx/.xltm; detected by the file signature, not the extension): the data
    # sheet is the sheet whose header row maps the most canonical fields; summary sheets (no row with
    # >= 3 non-empty cells, e.g. the Workday recruiter summary on Sheet1) are skipped; ties go to the
    # earlier sheet; with no header-like row anywhere the first non-empty sheet is read.
    # Text (.csv/.tsv/.txt, and web "Excel" downloads that are really text): UTF-16 BOM, utf-8-sig,
    # utf-8, cp1252, latin-1; delimiter sniffed from , ; \t |.
    # Header row: the first row with >= 3 non-empty cells and at least half as many as the widest
    # row; among the first five such rows one that reads like labels (>= 75% label-like cells) wins,
    # so title rows ("Generated | 2026-11-04 | v2") are skipped. Repeated header lines are dropped;
    # at most 500,000 rows are read.
def suggest_column_mapping(columns: list[str]) -> dict[str, str]
    # source header -> canonical name. normalize_header() then exact alias match
    # (schema.TA_FIELD_ALIASES), then answer-hash/per-question regexes, then rapidfuzz
    # (ratio >= 90) for leftovers. One-to-one: each canonical used at most once; exact beats
    # fuzzy; earlier column wins ties. Never map two sources to one canonical. Per-question
    # headers never fall through to whole-test fields.
def is_canonical_field(name: str) -> bool
    # a TA field, or q{n}_hash / q{n}_score / q{n}_similarity (targets of the mapping editor / profiles)
def normalize_ta_feed(raw: pd.DataFrame, mapping: dict[str, str], cfg: AppConfig
                      ) -> tuple[pd.DataFrame, DataQualityReport]
def load_ta_feed(file_name: str, content: bytes, cfg: AppConfig,
                 mapping_override: dict[str, str] | None = None
                 ) -> tuple[pd.DataFrame, DataQualityReport]
    # read_table -> (mapping_override or suggest_column_mapping) -> normalize_ta_feed.
    # report.column_mapping / unmapped_columns / missing_fields / rows_raw / rows_clean filled; the
    # first issue reads "Read N row(s); M column(s) mapped (auto-detected | user-supplied), K kept as
    # extra columns."
```

A user-edited mapping is validated first: blank targets are ignored, unknown targets are warned about
(the column is kept as an extra column), a canonical mapped from several columns keeps the earliest
one (warning), and mapped columns missing from the file are noted (info).

Normalisation rules (output has exactly one row per candidate_id):

* Missing values: empty cells and the tokens `#N/A`, `N/A`, `NA`, `n.a.`, `nan`, `null`, `none`,
  `nil`, `-`, `--`, `---`, "not available", "not applicable" (any case) are missing in every column.
  Workday writes `#N/A` (an Excel error value) for an application without an assessment result.
* Protected attributes (headers such as gender, sex, age, date of birth, religion, caste, category,
  marital status, nationality, disability, ethnicity, race, photo, father's / mother's name) are
  dropped outright, even when a mapping names them - never scored, displayed, exported or sent to an
  LLM - and an info issue lists them.
* `candidate_id`: stripped str, uppercased; spreadsheet artefacts such as "12345.0" become "12345".
  Missing column -> generated `ROW-00001...` (warning). Blank IDs -> generated + `generated_id` flag.
* `name`: collapse whitespace, title-case if all-lower/upper. If absent but first/last (and middle)
  name columns exist, join them. A Workday `job_application` value ("Asha Menon (asha menon)") supplies
  the name when no name column is usable and fills blank names (the text before " (").
* `email`: first address in the cell, lowercased; invalid -> NaN + `invalid_email`. `email_domain` derived.
* `phone`: digits only, keep last 10 (Indian numbers) - store as str; values Excel turned into
  scientific notation ("9.87E+09") are rejected; fewer than 10 digits -> NaN + `invalid_phone`.
* `hackerrank_score`, `max_score`: float. "85/100" cells give both. Missing max -> the same test's
  usual max -> inferred from score and percentage -> default 100 (info; `max_score_assumed` when a
  max column exists). Score > max -> clip to max + `score_exceeds_max`; negative -> `score_negative`.
  Missing score -> taken from the percentage column when present (`score_from_pct`), else NaN +
  `score_missing` (warning with the count).
* `hackerrank_pct` = score / max * 100 clipped to [0,100]. If a percentage column exists as well,
  score/max is kept and disagreements > 1 pt are noted as an info issue.
* `cutoff_score` (float, negative -> blank) and `cutoff_pct` (percent; a column whose values are all
  <= 1 without "%" is a fraction -> x100; outside 0-100 -> blank): the pass mark from the export,
  shown for reference (Deep-Dive section A). They do not set eligibility - the Config tab's
  "Min HackerRank %" does.
* `candidate_option` (assessment language track, e.g. "Django (Python)"): text; spellings unified.
* `cgpa`: 10-point scale. "8.5/10" -> 8.5, "3.6/4" -> 9.0, "78.5%" -> 7.85; a plain column whose
  maximum is <= 4.0 is a 4-point scale -> x2.5 (info); plain values 10 < v <= 100 -> v/10. Out of
  range or unparseable -> NaN + `cgpa_out_of_range`.
* `graduation_year`: Int64, the latest year mentioned ("Batch 2026", "2022-2026", "2025-26" -> 2026);
  outside 1980-2100 -> NaN + `invalid_graduation_year`.
* `test_start_time`, `submission_time`: mixed formats -> naive datetime (wall-clock as exported;
  a timezone suffix is dropped). ISO / year-first strings parse directly; day-first vs month-first is
  decided per column by the reading with fewer failures, then the tighter date range; Excel serial
  numbers and Unix epochs are accepted. Unparseable -> NaT + `invalid_test_start` /
  `invalid_submission_time`; submit before start -> `submission_before_start`.
* `completion_minutes`: numeric minutes; strings "HH:MM:SS", "MM:SS", "1h 12m", "72 min" parsed;
  if all numeric and median > 600 assume seconds -> /60 (info). Missing but start & submit
  present -> computed.
* `ip_address`: first IP if several are listed (the full list goes to `ip_addresses`); IPv4 with a
  port is accepted; `ip_subnet` = IPv4 /24 ("49.36.12.0/24") or IPv6 /64. Unrecognised -> `invalid_ip`.
* `tab_switches`, `copy_paste_events`, `secure_mode_exits`: Int64 (yes/no -> 1/0; negative or absurd
  -> NA). `out_of_window_seconds`: seconds; accepts 95, "95 s", "1m 35s", "00:01:35".
  `plagiarism_flag`: bool from yes/no/true/false/1/0/flagged/suspicious/"not suspicious"/blank;
  unrecognised values count as not flagged (warning with the count).
* `test_status`: Title Case ("Completed", "Invited", "Expired", "In Progress", "Did Not Attempt");
  API code 7 -> "Completed". `integrity_status`: Title Case. `test_mode`: "Remote" / "On-campus" when
  recognisable, else kept.
* `college_tier`: first digit found ("Tier 1" -> "1"; roman numerals I-IV accepted); NaN if none.
* `test_slot` derived = test_start_time floored to the hour, as str "YYYY-MM-DD HH:00".
* Per-question columns: answer hashes -> `q{n}_hash` (lower-case str); per-question scores ->
  `q{n}_score` (float); per-question code similarity -> `q{n}_similarity` (0-100).
* Free-text categories (college, city, degree, branch, source, test name, test mode, candidate
  option) are unified to the most frequent spelling when they differ only by case, whitespace or
  punctuation.
* Duplicated candidate_id: keep the row with the latest submission_time (else last row);
  count in `duplicates_dropped`; warning per dropped ID; `duplicate_id` flag on the kept row.
  (Different IDs of one person are a separate matter: see screening/duplicates.py.)
* Same email on different candidate_ids -> data flag `shared_email` on all of them (risk uses it).
* `plagiarism_match` resolved to `plagiarism_match_id` (candidate ID, e-mail or unique name in this
  file); unresolved values are counted in an info issue.
* `data_flags`: "; "-joined flags per row (e.g. `score_missing; score_exceeds_max`).
* Unmapped source columns are kept unchanged (prefixed `extra_` + normalised header) so
  nothing the TA team sends is lost.

### Workday assessment export

The export is read as delivered (details and examples: [HACKERRANK_DATA_REFERENCE.md, Part 1](HACKERRANK_DATA_REFERENCE.md#part-1---workday-assessment-export)):

* `Sheet1` (recruiter summary, two columns) is skipped; `Sheet2` holds the header row and one row per
  application. All 14 headers map automatically: Job Application -> `job_application`, Candidate ID ->
  `candidate_id`, Assessment Comment -> `assessment_comment`, Email -> `email`, CGPA -> `cgpa`, Branch ->
  `branch`, School -> `college`, Location -> `city`, Max Score -> `max_score`, Score ->
  `hackerrank_score`, Cutoff Score -> `cutoff_score`, Cutoff Score Percentage -> `cutoff_pct`,
  Percentage Score -> `hackerrank_pct`, Candidate Option -> `candidate_option`.
* Names come from Job Application (info issue "Candidate names taken from the 'Job Application'
  column."). The export has no phone, degree, graduation year, timing, IP or per-question columns;
  they are listed in `missing_fields` and the signals that need them are skipped.
* **Composite Assessment Comment** (`_parse_assessment_comments`): a comment column whose values carry
  labelled parts ("Hackerrank Completed – Qualified", "Integrity Issues: ...", "Integrity Summary: ...")
  is split into fields. A column counts as composite when at least 30% of its first 2,000 non-empty
  values have an "Integrity Issues:" / "Integrity Summary:" label or start with "HackerRank". HTML
  entities (Workday writes the first line break as the literal `&#xa;`; twice-escaped `&amp;#xa;` too)
  are decoded and line breaks unified before parsing.
  * Status line -> `test_status`: the word after the optional "HackerRank" prefix, cut at a dash
    ("Completed"); the qualification after the dash ("Qualified") stays in the comment text only.
  * "Integrity Issues: ..." -> `integrity_status`: "No Issues Detected", "No issues", "None", "Nil",
    "N/A", "Clear", "Clean", "OK", "-" -> "Clear"; a leading severity word (Low, Medium, Moderate,
    High, Critical, Review, Suspicious, Flagged) -> that word in Title Case; anything else is kept.
  * "Integrity Summary: ..." -> `proctoring_flags`; placeholders (N/A, None, Nil, Null, "-", "...",
    "No issues detected") -> blank.
  * The cleaned comment, one part per line, is stored in `assessment_comment`.
  * Derived fields are filled only when the file has no separate column mapped to them; an info issue
    names them ("Test Status, Integrity Status, Proctoring Flags read from the 'Assessment Comment'
    column."). A column whose own header is "Integrity Issues" (or "Integrity Summary") and that is
    mapped to `assessment_comment` is read as plain integrity-status (or proctoring-flag) values.
* `S.INTEGRITY_CONCERN_VALUES` = {review, suspicious, flagged, medium, high}: Workday Medium / High and
  HackerRank's Review / Suspicious / Flagged raise the risk flag; "Clear", "Low" and negated values
  ("Not suspicious") do not.

## screening/resume_parser.py + screening/skills.py

```python
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt"}
ARCHIVE_EXTENSIONS = {".zip"}
def expand_uploads(docs: list[ResumeDoc], cfg: ProcessingConfig | AppConfig,
                   progress_cb: Callable[[int, int, str], None] | None = None
                   ) -> tuple[list[ResumeDoc], list[dict]]
    # Expand .zip archives in memory (no extraction to disk), split multi-candidate PDF packets and
    # filter to parseable resumes; returns (docs, issues). progress_cb(done, total, detail) runs on
    # the calling thread once per uploaded file / supported archive member. Never raises.
def split_bundle(doc: ResumeDoc, cfg=None, issues: list[dict] | None = None,
                 max_docs: int | None = None) -> list[ResumeDoc] | None
    # None when doc is not a packet (or splitting is off); else one PacketResumeDoc per attachment
def load_resume_folder(path: str | Path) -> list[ResumeDoc]      # recursive, sorted by relative path
def extract_text(doc: ResumeDoc) -> tuple[str, list[str]]       # (text, warnings); never raises
def parse_resume_text(text: str, file_name: str = "", file_hash: str = "",
                      id_pattern: str | None = None) -> ParsedResume
def parse_resume(doc: ResumeDoc, id_pattern: str | None = None) -> ParsedResume   # never raises
def parse_resumes(docs: list[ResumeDoc], max_workers: int = 8,
                  progress_cb: Callable[[int, int, str], None] | None = None,
                  id_pattern: str | None = None) -> dict[str, ParsedResume]
    # keyed by file_hash; identical bytes parsed once (the first file name wins; the others are noted
    # in its warnings: "Identical file also uploaded as: ..."); ThreadPoolExecutor; progress_cb
    # (done, total, file_name) called from the calling thread (iterate as_completed in the caller).
```

Upload and ZIP rules (`ProcessingConfig` defaults; not shown in the UI, but a config JSON uploaded in
the Config tab can change them):

| Setting | Default | Meaning |
|---|---|---|
| `max_file_mb` | 10 | one resume file (also every file split out of a packet) |
| `max_bundle_mb` | 250 | one multi-candidate PDF packet |
| `split_bundles` | True | split packets into one resume per candidate attachment |
| `max_zip_members` | 10,000 | resumes taken from one archive (the rest are skipped with a warning) |
| `max_zip_total_mb` | 2,048 | uncompressed size of one archive |
| `max_workers` | 8 | parsing threads |
| `candidate_id_pattern` | `\b(?:C-\d{6,8}\|[A-Z]{2,6}[-_]?\d{3,8})\b` | candidate IDs in file names / resume text when the feed's own IDs are not found verbatim |

* ZIP archives are read member by member, so only one packet is held in memory at a time. Folders,
  `__MACOSX`, hidden and system files (`.*`, `~$*`, Thumbs.db, desktop.ini, .DS_Store) are skipped;
  nested ZIPs are not expanded (info); unsupported extensions are skipped (".doc / .rtf / .odt / .pages:
  save it as .docx or PDF"); encrypted members are skipped; only Stored / Deflate members are read
  (bzip2 / LZMA would inflate in one unbounded call); a member above its limit, a compression ratio
  above 200 (zip-bomb guard) or an archive above `max_zip_total_mb` stops that member / archive with an
  issue; reads are bounded even when the ZIP directory lies. File names are reduced to their basename.
* **Packets are split before the per-resume caps**: a PDF up to `max_bundle_mb` (any page count)
  goes to `split_bundle`; every split file must then fit `max_file_mb` (else skipped with an issue)
  and is read only when it has at most 50 pages (`PDF_MAX_PAGES`).

Multi-candidate packets (`split_bundle`; layout in
[HACKERRANK_DATA_REFERENCE.md, section 1.7](HACKERRANK_DATA_REFERENCE.md#17-resumezip-packet-layout)):

* A PDF is a packet when it has at least 2 pages (3 without bookmarks) and table-of-contents rows on
  its first 1-3 pages (`BUNDLE_TOC_PAGES`): lines "1. Asha Menon C-1204417" with a Workday-style ID
  (`C-` + 6-8 digits), or - after a "Table of Contents" / "Candidate Name Candidate ID" heading - any
  ID matching `candidate_id_pattern`. Without a heading at least 2 rows are needed; a name wrapped onto
  the next line is joined; repeated IDs are listed once.
* Plan: the PDF outline gives one start page per attachment (bookmarks inside the table of contents
  and "Contents" bookmarks are ignored). Bookmarks are assigned to table-of-contents rows in order by
  name (exact, or rapidfuzz token_sort_ratio >= 90) or candidate ID, with dynamic programming that
  allows several attachments per candidate. When fewer than half of the bookmark titles name a listed
  candidate, the listed names (or IDs) are looked up near the top of each page instead
  (`_plan_from_text`, at most 1,000 pages); when that fails too, bookmarks are assigned in order. A
  packet without bookmarks is always split by the names on the pages. Every fallback adds "- verify
  the split" to the issue. When no plan can be made the packet is kept as one file (warning
  "Multi-candidate packet listing N candidate(s) could not be split ...; kept as one file").
* Attachment k covers pages [start_k, start_k+1); the last one runs to the end of the packet.
* Output: one PDF per attachment, `"{candidate_id}_{Cleaned_Name}.pdf"` (name ASCII-folded,
  non-alphanumerics -> "_", at most 60 characters, "Candidate" when empty) and `"..._att2.pdf"`,
  `"..._att3.pdf"` for further attachments of the same candidate. Source `"bundle:<packet>"`,
  `bundle_source` `"<packet> p.<first>-<last>"` (1-based pages), the candidate ID in the PDF metadata
  (so identical resumes of two applications stay two files with their own hashes).
* Issue (info, or warning after a fallback): "Split multi-candidate packet into N file(s) for M
  candidate(s) (P pages)", plus "K listed candidate(s) have no attachment pages" when relevant.

Text extraction (`extract_text`; the hidden-text parts are in screening/injection.py):

* `.pdf`: each page's content stream is parsed once; `injection.scan_page` finds text a reader cannot
  see and `injection.neutralize` blanks it before pypdf extracts the page. pypdf decompression limits
  (30 MB per stream, `PDF_MAX_DECODED_MB`) apply to the reading thread only; files over 50 pages are not
  read; password-protected PDFs are an error. When pypdf yields < 100 characters, pdfplumber is tried
  (only after a pre-check that every stream is bounded) and the hidden runs the scan found are removed
  from its output. Link annotations are appended under "Hyperlinks". Image-only PDFs -> warning "No
  extractable text: the PDF looks image-only/scanned - OCR is required".
* `.docx`: zip-bomb guard first (at most 2,000 parts, 30 MB unpacked, ratio guard, Stored / Deflate
  only), then python-docx: headers / footers, body paragraphs and tables in document order, text boxes,
  hyperlinks. Runs Word never shows (the Hidden attribute, or text under 1 pt) are removed from the
  parsing text and reported when they look adversarial (instruction-like, 3+ known skills, or 15+ words).
* `.txt`: UTF-16 (with BOM), utf-8-sig, utf-8, cp1252, latin-1. File signatures beat extensions for PDF <-> DOCX
  mix-ups.
* Then `injection.scan_text` (instruction-like text, invisible characters) and
  `injection.strip_invisible` before normalisation and parsing.

Parsing requirements:

* Section detection: heading lines matched case-insensitively against synonyms, e.g.
  education ("education", "academic qualifications", "academics", "qualifications"),
  skills ("skills", "technical skills", "core competencies", "tech stack", "key skills"),
  projects ("projects", "academic projects", "key projects", "personal projects"),
  experience ("experience", "work experience", "internships", "internship experience",
  "professional experience", "employment"), certifications ("certifications",
  "certifications & courses", "licenses & certifications", "courses"), achievements
  ("achievements", "awards", "honors", "accomplishments", "extra-curricular"),
  summary ("summary", "profile", "objective", "career objective", "profile summary").
  Headings may be ALL CAPS, Title Case, end with ":", or be followed by text on the same line
  ("Skills: Python, Java"). Text before the first heading -> header/contact (summary if long).
* Bullets: lines starting with •, ●, ▪, ‣, -, *, –, or "o "; strip glyph; also treat
  sentence-like lines inside projects/experience as bullets.
* Entities: emails, phones (+91 / 10-digit), URLs, GitHub repo URLs normalised to
  `github.com/<user>/<repo>` lowercase (profile-only URLs -> `github.com/<user>`),
  LinkedIn, candidate_id_guess via id_pattern (search file name first, then text).
* name_guess: first non-empty line that looks like a person name (2-4 alphabetic tokens,
  no digits/@), else derived from file name.
* Projects: split the projects section into entries (title line followed by bullets);
  `tech` = skills found in the entry; `github` = repo URL in the entry.
* Experience entries: title/org line + date range; months from ranges like
  "Jun 2025 - Aug 2025", "06/2025 - 08/2025", "May 2024 – Present" (Present = 2026-11-01);
  `is_internship` if "intern" appears. `internship_count`, `total_experience_months`.
* Education: degree (B.Tech, B.E., M.Tech, MCA, B.Sc, M.Sc, BCA, MBA...), branch, college,
  CGPA ("CGPA: 8.7/10", "GPA 3.6/4" -> x2.5, "82%" -> 8.2), grad_year (latest 20xx in section).
* Certifications: lines of the certifications section (cleaned); if absent, lines containing
  "certified"/"certification".
* `ParsedResume.injection_findings`: list of `{"kind": "instruction" | "invisible_chars" |
  "hidden_text", "snippet", "detail"}`; `ParsedResume.bundle_source`: the packet and page range a
  split file came from (None otherwise). `parse_status` "ok" | "empty" (< 30 characters) | "error".
* skills.py: `SKILL_TAXONOMY` canonical -> regex synonyms with **word-boundary safety**
  ("Java" must not match "JavaScript"; handle "C++", "C#", ".NET", "Node.js", "Next.js",
  "scikit-learn", "Power BI"). Hierarchy via `SKILL_IMPLIES` (AWS/Azure/GCP -> Cloud;
  MySQL/PostgreSQL/Oracle/SQL Server/SQLite -> SQL; Spring Boot -> Java; Django/Flask/FastAPI/
  Pandas/NumPy -> Python; PyTorch/TensorFlow/Keras/scikit-learn -> Machine Learning; React/
  Angular/Node.js -> JavaScript). Skills are extracted from the parsing text, which never contains
  hidden text. Functions:
  ```python
  def extract_skills(text: str) -> list[str]                       # sorted canonical names
  def skill_evidence(sections: dict[str, str]) -> dict[str, list[str]]
  def canonicalize_skill(name: str) -> str                         # user input -> canonical (case-insensitive, synonyms)
  def match_required(required: list[str], found: list[str], text: str = "") -> tuple[list[str], list[str]]
      # (matched, missing) in the order of `required`. A required skill is matched if found
      # directly, implied by a found skill, or (for skills not in the taxonomy) appears in
      # `text` as a whole word (escaped regex, case-insensitive).
  ```

## screening/injection.py

Prompt-injection and hidden-text detection for resumes. Findings are review evidence, never verdicts;
nothing here raises for bad input and nothing is logged (snippets are candidate data).

```python
FINDING_KINDS = ("instruction", "invisible_chars", "hidden_text")
def scan_text(text: str) -> list[dict]                 # instruction-like text + invisible unicode
def scan_pdf_hidden_text(pdf_bytes: bytes, *, max_pages=None) -> list[dict]
def scan_page(page, ops, reader=None, page_no=1, *, budget=None, off_ocgs=None) -> PageScan
def neutralize(ops, indices) -> None                   # blank the hidden text-showing operators in place
def hidden_ocgs(reader) -> frozenset[int]              # optional-content layers that are switched off
def strip_invisible(text) -> str                       # zero-width, bidi-control and Unicode tag characters
def remove_snippets(text, snippets) -> str             # whitespace-tolerant removal
def strip_hidden(text, findings=None) -> str
def looks_like_instruction(text) -> bool
def summarize(findings, limit=6) -> str                # " | "-joined, most serious first (injection_flags)
def finding_summary(f, width=80) -> str
def reason_snippet(findings, width=60) -> str          # short snippet for the risk reason
FLAG_KEYS = ("prompt_injection", "hidden_text", "invisible_chars")   # risk flags, most serious first
FLAG_REASONS: dict[str, str]                           # flag key -> reason headline
def flag_kind(findings) -> str | None                  # the one risk flag for a resume
def flag_headline(injection_flags) -> str              # headline for an injection_flags summary ("" if none)
```

* **Hidden PDF text** (`kind` "hidden_text"): white or same-colour text on a white / same-colour
  background, invisible render modes (3 and 7), effective glyph height under 1 pt, fill or stroke
  opacity at or below 0.05, text outside the page box, and text in optional-content layers that are
  switched off. Precision guards: white text on a coloured shape or image is visible (template
  headers); invisible text over an image is the OCR text layer of a scanned resume; a page whose only
  text is invisible but which draws its content as images (>= 25% of the page) or many vector paths
  (>= 200) is a text layer, not hidden text; runs with fewer than 4 letters/digits are removed but not
  reported. Time guard: 400,000 content-stream operators per document.
* **Invisible characters** (`kind` "invisible_chars"): 5 or more zero-width characters, 3 or more
  bidi embeddings / isolates (overrides count from the first), or any Unicode tag characters.
* **Instruction-like text** (`kind` "instruction"): an instruction override ("ignore previous
  instructions"), text that addresses an AI system directly, asks for a top score or ranking,
  dictates the screening outcome, targets the system prompt, uses chat-template control tokens, or
  asks to keep the instruction secret. Ordinary resume sentences ("Designed system prompts for a
  chatbot") and sentences that describe security work ("detects prompts such as 'ignore previous
  instructions'") do not match.
* Limits: snippets are cut to 300 characters; at most 12 findings per document.
* Risk flag per resume (`flag_kind(findings)`; only the most serious kind counts, so one resume is
  never weighted twice): `FLAG_PROMPT_INJECTION` "prompt_injection" when any finding reads like an
  instruction to AI screeners (kind "instruction", or any finding whose detail mentions an
  instruction, such as hidden text that reads like one), else `FLAG_HIDDEN_TEXT` "hidden_text" when any finding
  is hidden PDF text, else `FLAG_INVISIBLE_CHARS` "invisible_chars"; None without findings.
  `FLAG_REASONS` holds the reason headlines: "Instruction aimed at AI screeners found in resume
  (prompt injection)", "Hidden text in resume (invisible to readers)" and "Unusual invisible
  characters in resume (possible obfuscation or copy-paste artefact)"; `flag_headline(injection_flags)`
  returns the headline for an `injection_flags` summary (Deep-Dive section D and Compare warnings).
* Where findings go: `ParsedResume.injection_findings` -> one risk individual flag per resume,
  `prompt_injection` (weight 50), `hidden_text` (35) or `invisible_chars` (15), with the reason
  "{FLAG_REASONS[flag]}: '{snippet}'", and the `injection_flags` column; hidden text never reaches sections, skills or scores; the LLM
  layer drops instruction-like lines before any prompt. The UI shows the snippets in Deep-Dive section
  "D. Integrity" and in the Risk tab's "Adversarial resume text" section; the Excel report and the
  evidence pack list them too.

## screening/matching.py

```python
def match_resumes(ta: pd.DataFrame | None, parsed: dict[str, ParsedResume], cfg: AppConfig) -> pd.DataFrame
    # one row per parsed resume; columns:
    # file_hash, file_name, candidate_id (str or NaN), match_method (S.MATCH_*),
    # match_confidence (0..1), note (str)
def link_candidates(ta: pd.DataFrame | None, parsed: dict[str, ParsedResume],
                    matches: pd.DataFrame, cfg: AppConfig
                    ) -> tuple[pd.DataFrame, dict[str, ParsedResume], list[dict]]
    # -> (base, parsed_by_candidate, issues)
```

Priority (first hit wins; record conflicts in `note` + issue): TA `resume_file` equals file
name (1.0) -> a TA candidate_id appears as a token in the file name (1.0) -> resume email ==
TA email (0.99) -> TA candidate_id token in text (0.97) -> phone last-10 match (0.95) -> fuzzy
name (rapidfuzz token_sort_ratio >= 92 on name_guess or file-name-derived name, unique: best
minus second-best >= 5) (score/100*0.9). An e-mail or phone shared by several TA candidates is
resolved by name at confidence 0.9. All-digit IDs shorter than 5 digits are never matched as tokens.
Use dict/set lookups, not O(n^2) scans (fuzzy only for the leftovers, via rapidfuzz.process.extractOne).
If several resumes map to one candidate keep the highest confidence, then the more resume-like file
(more of education / skills / projects / experience found), then more words, the primary attachment,
the newer version ("_v2", "(2)") and the file name; the others -> MATCH_DUPLICATE. Email in resume
pointing at a different candidate than the file-name ID -> note "identity conflict" + warning issue.
Notes never contain names, e-mails or phone numbers.

Files split from Workday packets are named `"{candidate_id}_{Cleaned_Name}.pdf"` and match by ID
(`id_in_filename`, 1.0); an `"..._att2.pdf"` file is a further attachment of the same candidate (the
primary resume is kept and the attachment is marked `duplicate`).

`base`: TA rows (all canonical + derived columns) plus `resume_file`, `resume_hash`,
`has_resume`, `match_method`, `match_confidence`. TA candidates without a resume ->
has_resume False, match_method "" + info issue (aggregate: one issue listing the count is
fine plus per-candidate rows). Resumes matching nobody -> warning issue (file_name).
**Resume-only mode** (ta is None): one candidate per resume, candidate_id = candidate_id_guess
or `RES-` + file_hash[:8].upper(), name/email/phone from resume, hackerrank_score NaN,
max_score NaN, hackerrank_pct NaN, match_method MATCH_RESUME_ONLY, data_flags "no_assessment".

## screening/duplicates.py

```python
def detect_duplicates(base: pd.DataFrame, parsed_by_candidate: Mapping[str, ParsedResume] | None = None
                      ) -> tuple[pd.DataFrame, list[dict]]
    # -> (a NEW frame = base + duplicate_of / duplicate_ids / duplicate_evidence, issues). Never raises.
def name_key(name, job_application=None) -> str      # 'MENON, Asha' -> 'asha menon'; Job Application fallback
def college_identity(college) -> str                 # 'XY-IICT' == 'xy iict' == 'XYIICT'; '' when not recorded
def email_domain(email) -> str
def is_webmail_domain(domain) -> bool / is_webmail(email) -> bool / is_institutional(email) -> bool
```

Workday exports one row per job *application*. A student who applies twice - typically once with a
personal webmail address and once with the university address - shows up as two candidates with
different IDs. Rows are the **same person** when they have different candidate IDs and:

* the same normalised name (case-folded, accents removed, tokens sorted; the name inside
  "Job Application" is used when there is no Name value; placeholders such as "N/A" never match), AND
* the same canonical college (case, whitespace and punctuation ignored, '&' read as 'and'; a college
  that is not recorded never matches), AND
* an identical resume file, OR resume texts with TF-IDF cosine >= 0.95 (`TEXT_SIMILARITY_MIN`), OR -
  when neither application has a resume - the same CGPA and the same branch.

Groups of two or more applications are supported (rows are bucketed by name + college and compared
with vectorised operations). Every group is marked:

* `duplicate_of` on each duplicate: candidate_id of the group's primary application;
* `duplicate_ids` on the primary: ", "-joined candidate_ids of its duplicates;
* `duplicate_evidence` on every member, e.g. "same name and college; identical resume file; emails
  differ (webmail vs institutional)" or "...; near-identical resume text (similarity 0.97)" or
  "...; no resumes; same CGPA and branch".

Primary = an application with an assessment score, then one with an institutional (non-webmail)
e-mail, then the lowest numeric candidate ID. Webmail = gmail.com, googlemail.com, outlook.com,
hotmail.com, live.com, icloud.com, rediffmail.com, protonmail.com, proton.me, aol.com, zoho.com,
mail.com, yahoo.* and mail.example.com (the personal domain of the synthetic sample). One info issue per
group: "Duplicate application: N applications from the same person - {primary} kept as the primary
application, {ids} marked as duplicate ({evidence})." A group of more than 10 applications
(`MAX_GROUP_SIZE`) points at placeholder data: it is reported as a warning for review and not marked.

Consumers: `scoring.apply_shortlist` makes duplicates Ineligible (rule "Duplicate Application") when
`shortlist.auto_disqualify_duplicates` is on (default) and appends " | merged duplicate
application(s) {ids}" to the primary's status reason; `risk` never pairs applications of one person
(they are not collusion); the Shortlist tab's "Ineligible candidates" section counts them; the
Deep-Dive header shows "Duplicate application" / "Merged duplicate applications" badges; exports
carry the three columns.

## screening/scoring.py

```python
def score_candidates(base: pd.DataFrame, parsed_by_candidate: Mapping[str, ParsedResume] | None,
                     cfg: AppConfig, *, mode: str = "ta+resumes") -> pd.DataFrame
def apply_shortlist(df: pd.DataFrame, cfg: AppConfig) -> pd.DataFrame
def resolve_missing_resume_policy(cfg: AppConfig, mode: str | None = None) -> str   # "zero" | "renormalize"
def resolve_tier(college, cfg: AppConfig, feed_tier=None) -> tuple[str | None, str]  # (tier, "feed"|"tier list"|"")
def tier_list_key(value) -> str                  # 'Q.Z.-IICT, Northfield' -> 'q z iict northfield'
def college_key(value) -> str                    # college as compared by the college cap ('' = not recorded)
def ineligibility_rules(value) -> list[dict]     # parse an INELIGIBILITY cell
def cutoff_band_ids(ranked: pd.DataFrame, cfg: AppConfig) -> list[str]
def explain_score(row: pd.Series | dict, cfg: AppConfig, *, mode: str | None = None) -> list[str]
def order_columns(df: pd.DataFrame) -> pd.DataFrame
```

Scoring is fully rule-based: an LLM may explain results but never changes scores, ranks or cutoffs.

* Weights: `cfg.weights.normalized()` (rescaled to sum to 100). Each component: sub-score 0..1, points
  = sub x weight. One-click presets (`config.PRESETS`, Config tab buttons): "With Resumes
  (40/30/20/10)" and "No Resumes (80/0/0/20)".
* HackerRank: sub = hackerrank_pct / 100 (NaN -> 0 + note "no assessment score").
* Skills: found = parsed.skills; matched/missing via skills.match_required(required skills, found,
  parsed.text) after canonicalising the configured skills. per_skill (default): sub = min(1, n_matched
  / skills_for_full_marks (3)); coverage: n_matched / len(required). Columns SKILLS_FOUND,
  MATCHED_SKILLS, MISSING_SKILLS, N_MATCHED_SKILLS.
* Experience (rules only): weighted (normalised w_internships 0.30, w_duration 0.15, w_projects 0.25,
  w_relevance 0.30) of internships min(1, count/2), duration min(1, months/6), projects (count/3,
  x0.7 + 0.3 x share of projects with quantified outcomes [numbers/%] or tech evidence), relevance
  (TF-IDF cosine - unigrams, English stop words, sublinear tf - of projects+experience+skills text vs
  job_description + required skills; scaled by the pool's 95th percentile cosine (floor 0.1), clipped
  0..1; stored in RELEVANCE). EXPERIENCE_SOURCE = "heuristic", or "no resume".
* Education: degree relevance (CS/IT/software/data/AI/computer -> 1.0; ECE/EEE/electrical/
  instrumentation/maths/statistics -> 0.7; other engineering -> 0.4; non-technical -> 0.2),
  CGPA ((cgpa - floor 6.0)/(ceiling 9.5 - floor 6.0) clipped; TA cgpa (0 < v <= 10) preferred, else
  resume -> CGPA_SOURCE "feed" | "resume" | ""; "resume" for resume-only runs), certifications
  min(1, relevant_count/2), tier (only when `education.tier_enabled`). Weights 0.35 / 0.35 / 0.20 /
  0.10 re-normalised over sub-components that have data (missing -> excluded, not zero).
* College tier (`tier_enabled`, **off by default**; the Config tab shows a fairness note: tiers can
  disadvantage strong candidates from less-known colleges). The feed's `college_tier` wins when it can
  be scored (`tier_scores` {"1": 1.0, "2": 0.7, "3": 0.4} by label or digit; an unlisted digit gets the
  lowest score); otherwise the college is looked up in the approved tier list
  (`education.tier_mapping`: normalised name / alias -> tier label, uploaded in the Config tab):
  exact name (case, accents and punctuation ignored, '&' = 'and', also with spaces removed), then the
  acronym rule ("QZ-IICT" matches a long name whose word initials give those letters in order,
  ignoring of/and/the; up to 2 trailing campus words), then rapidfuzz token_set_ratio >=
  `tier_match_threshold` (default 90; ambiguous matches with different tiers -> no tier; colleges
  made only of generic words are never fuzzy-matched). TIER_SOURCE "feed" | "tier list" | ""; a tier
  found in the list is written to COLLEGE_TIER. An unmatched college gets no tier sub-score - it is
  not penalised.
* Missing resume (`shortlist.missing_resume_policy`): "auto" (default) = "renormalize" when the run
  has no resumes at all (mode "ta_only"), else "zero"; "zero" -> skills/experience sub = 0 (note
  "no resume: skills & experience scored 0"); "renormalize" -> final = (HackerRank + education points)
  rescaled to 100 (note "no resume: final rescaled to 100 from {x} available pts"); a missing HackerRank
  score still counts 0. A linked resume without extractable text counts as missing ("resume has no
  extractable text").
* FINAL_SCORE rounded to 2 dp. Also adds INTERNSHIP_COUNT, EXPERIENCE_MONTHS, PROJECT_COUNT,
  CERTIFICATIONS, CERT_COUNT, RESUME_CGPA, RESUME_DEGREE, CGPA_SOURCE, TIER_SOURCE, SCORE_NOTES
  ("; "-joined: no assessment score, no resume ..., skills matching failed, CGPA from resume, no
  education data) and LLM_RATIONALE (always "": kept for column compatibility).

`apply_shortlist`:

* **Eligibility** -> INELIGIBILITY, a JSON list of `{"rule_violated", "candidate_value",
  "threshold_value", "explanation"}` (empty list = eligible). STATUS_REASON of an ineligible
  candidate = the explanations joined. Rules (`S.RULE_*`), in this order:

  | Rule | When | Explanation (example) |
  |---|---|---|
  | `Assessment` | no assessment score (skipped when nobody has one: resume-only data) | "No assessment score recorded on HackerRank." |
  | `Score Threshold` | `min_hackerrank_pct` > 0 and the percentage is below it | "HackerRank score 54.3% is below the required minimum of 60.0%." |
  | `Academic CGPA` | `min_cgpa` > 0 and the CGPA used for scoring (feed, else resume) is below it; a missing CGPA never fails | "CGPA 6.80 (from resume) is below the minimum threshold of 7.00." |
  | `Graduation Year` | `allowed_grad_years` set and a known year is not in it; a missing year never fails | "Graduation year 2025 is not within allowed batches (2026, 2027)." |
  | `Administrative Exclusion` | the candidate ID is in `exclude_candidates` (case-insensitive) | "Disqualified: administrative exclusion by recruiter." |
  | `Duplicate Application` | `duplicate_of` is set and `auto_disqualify_duplicates` is on | "Disqualified: Duplicate application of C-1204417 (merged into official profile {primary's e-mail})." |

* **Rank** (1..n, eligible only): final_score desc, then `shortlist.tie_break_order`, then
  candidate_id asc. Tie-break keys (`config.TIE_BREAK_KEYS`; default order hackerrank_pct, sub_skills,
  sub_experience):

  | Key | Label | Better |
  |---|---|---|
  | `hackerrank_pct` | HackerRank percentage | higher |
  | `coding_marks` | HackerRank coding-question marks (sum of q{n}_score over questions that also have a q{n}_similarity column; all q{n}_score when there is none) | higher |
  | `sub_skills` | Skills sub-score | higher |
  | `sub_experience` | Experience sub-score | higher |
  | `cgpa` | CGPA (feed, then resume) | higher |
  | `submission_time` | Earliest assessment submission | earlier |

  Values are rounded to 4 dp (float noise never breaks a tie); missing values sort last. Neighbours with
  the same final score get TIE_BREAK_NOTE, e.g. "tied at 91.20 pts; won tie-break on HackerRank
  percentage 100% vs 95%" / "lost tie-break on ..."; identical on every rule -> "tie unresolved
  (identical on every tie-break rule)". A tie across the shortlist cutoff also goes into both status
  reasons.
* **Cutoff**: top_n (default 300) / ceil(top_pct% of eligible) / final_score >= threshold. In the band:
  risk_level in review_risk_levels (default High, Medium) -> STATUS_REVIEW ("Integrity review: {level}
  risk - {top 3 reasons}"), else STATUS_SHORTLISTED ("Rank 12 of 2,125 (top 300)"). If
  exclude_high_risk: High-risk in band -> STATUS_HOLD ("Integrity hold: ...; slot backfilled") and the
  band is extended so the shortlist still has N clean+review candidates (top_n / top_pct only).
  Others: STATUS_NOT ("Rank 512 of 2,125 (cutoff: top 300)"; threshold: "Rank r of m; score 64.20 below
  threshold 70"); ineligible: STATUS_INELIGIBLE. Works if risk columns are missing (treat as Clear).
* College cap: see "Shortlist controls" below.
* Returns a new frame sorted by rank (ineligible last, by final score then ID), index reset, columns
  in the final order (`order_columns`).
* `cutoff_band_ids` (borderline band): the first `cfg.llm.borderline_band` (default 50) eligible
  "Not Shortlisted" candidates ranked just below the last shortlisted rank, best rank first (threshold
  method: the best-ranked candidates below the threshold). Read-only: it selects candidates for gap
  explanations. The Shortlist tab uses `llm.borderline_ids`, which takes the eligible "Not Shortlisted"
  candidates of the finished table in rank order (with a college cap, capped candidates ranked above
  the cutoff come first).

## screening/risk.py

```python
def run_risk_analysis(candidates: pd.DataFrame, parsed_by_candidate: dict[str, ParsedResume],
                      cfg: AppConfig, parsed_all: dict[str, ParsedResume] | None = None) -> RiskResult
```

Focus group = hackerrank_pct >= cfg.risk.focus_threshold_pct (default 100 -> perfect scorers).
All signals degrade gracefully when columns are missing (skip + RiskResult.warnings), and a failure
inside one signal never aborts the analysis.

1. **Commonality (lift)** for every attribute in `S.COMMONALITY_ATTRIBUTES` present: per value,
   focus_count/focus_total vs population_count/population_total, lift, one-sided Fisher exact
   p (hypergeometric tail), BH q-value (scipy.stats.false_discovery_control); significant =
   focus_count >= lift_min_count and lift >= lift_min and q <= lift_alpha. Columns: attribute, value,
   focus_count, focus_total, focus_share, population_count, population_total, population_share, lift,
   p_value, q_value, significant, attribute_key. Sorted significant first, then lift desc.
2. **Pair signals among the focus group** (vectorised / sorted-window, no Python O(n^2) over
   3,000 - focus group only, optional focus x all for text):
   * shared answer hashes per `q{n}_hash`, ignoring blank / empty-string hashes and "common" hashes
     held by more than max(common_hash_max_share x focus size, min(10, focus size / 2)) members (MCQ
     answer keys) - weight per hash, cap 2x;
   * shared GitHub repo (exact normalised repo URL, not profile-only; repos on many resumes are
     boilerplate). **Team project rule**: a pair sharing 1..`team_max_shared_repos` (2) repos with
     resume similarity below `team_max_text_similarity` (0.40) and at most `team_max_shared_bullets`
     (1) shared rare bullet is a legitimate team project: signal `team_github_repo` ("Shared team
     project GitHub repo") weighted `team_repo_weight` (15) instead of 35, reason "Shared team project
     GitHub repo {repo} with {ids} - pattern consistent with legitimate team collaboration (verify
     candidate's individual role)". The UI labels such pairs "Team project (likely legitimate)" and
     hides them from the risk network by default;
   * resume text similarity: TF-IDF (word 1-2 grams, sublinear tf) on projects+experience
     sections (fallback full text under 25 words) -> cosine >= high / >= medium thresholds;
   * shared rare bullets: normalised bullet with >= min_bullet_words words, held by
     <= max(max_bullet_population_share x all resumes, 5) resumes (boilerplate filter), count + examples;
   * same IP (only if that IP is used by <= shared_ip_max_group candidates overall, OR the two
     candidates are from different colleges and not both On-campus; an IP shared by more than 50
     perfect scorers is a venue / VPN / proxy server and never counts); same /24 subnet (weak);
   * sync_start / sync_submission within sync_window_sec; duration_match within
     duration_tolerance_min - **timing weights x0.25 when both candidates are On-campus at the
     same college or the session is crowded** (scheduled sessions are synchronised by design);
     timing alone links candidates only inside a synchronised group of >= 3;
   * sequential_same_ip: same IP and one session starts 0..proxy_gap_max_min after the other
     ends (proxy test-taker);
   * same phone / same email across different candidate_ids (values on more than 10 candidates and
     dummy numbers are placeholders and ignored); these identity signals and HackerRank's plagiarism
     match also link a perfect scorer to a candidate outside the focus group;
   * **duplicate applications** (`duplicate_of` / `duplicate_ids`) are never paired:
     eligibility handles them (`params["duplicate_application_pairs_ignored"]`).
   pair_score = min(100, sum(weights)). Keep pairs with pair_score > 0 that have at least one
   non-timing signal or score >= edge_threshold.
   Pairs columns: cand_a, cand_b (cand_a < cand_b), pair_score, signals (" | " human-readable),
   signal_keys (", "), shared_hashes (", " e.g. "q2, q3"), resume_similarity (float/NaN),
   shared_bullets (int), shared_bullet_examples (" | ", max 3), shared_github (", "), same_ip,
   same_subnet, same_college, same_phone, same_email (bool), start_gap_sec, submit_gap_sec,
   duration_gap_min, sequential_gap_min (float/NaN).
3. **Individual flags** (all candidates): speed_anomaly (focus group members with completion
   < speed_anomaly_ratio x focus median; needs >= 5 completion times), plagiarism_flag true,
   high_code_similarity, integrity_status (Workday Medium / High: "Assessment integrity issues:
   Medium"; HackerRank Review / Suspicious / Flagged: "HackerRank AI-proctoring status: ..."),
   proctoring_flags (non-empty notes), out_of_window_time, secure_mode_exits, tab_switches, copy_paste
   and one resume-text flag per resume, the most serious kind only (`injection.flag_kind`):
   **prompt_injection** ("Prompt injection": "Instruction aimed at AI screeners found in resume (prompt
   injection): '{snippet}'"), **hidden_text** ("Hidden text in resume": "Hidden text in resume
   (invisible to readers): '{snippet}'") or **invisible_chars** ("Invisible characters in resume":
   "Unusual invisible characters in resume (possible obfuscation or copy-paste artefact):
   '{snippet}'"). Columns: candidate_id, flag_key, flag, detail, weight.
4. **Clusters**: graph of pairs with pair_score >= edge_threshold (networkx connected
   components, size >= 2), ids "C01".. ordered by max pair score desc then size. Columns:
   cluster_id, size, members (", "), member_names (", "), risk_level, max_pair_score,
   mean_pair_score, density, top_signals (", " labels by frequency), shared_traits (" | "
   e.g. "College: X (8/8)", "IP 49.36.12.7 (6/6)", "Submissions span 3.5 min",
   "Identical answers: q2, q3"), colleges (", "), n_colleges, start_span_min,
   submit_span_min, summary (one deterministic sentence).
5. **Candidate risk** (all candidates): risk_score = min(100, max_pair_score + individual_score);
   level High >= high_threshold, Medium >= medium_threshold, Low > 0, else Clear. Columns:
   candidate_id, is_focus, risk_score, risk_level, risk_reasons (" | ", strongest first, each
   reason names the evidence e.g. "Identical answers to q2, q3 with CAND-00123"), cluster_id
   ("" if none), n_links, max_pair_score, individual_score, injection_flags (" | "-joined summaries
   of hidden / adversarial resume text, most serious first).
6. `params` = thresholds used (plus guard constants and counts); `warnings` = skipped signals.

Default weights (`RiskConfig`): pair - shared_answer_hash 35 (per answer, cap 2x), shared_github_repo
35, team project repo 15, resume_text_high 30, resume_text_medium 15, shared_bullets 8 (per bullet, cap
3x), same_ip 20, same_subnet 5, sync_submission 15, sync_start 10, duration_match 5, same_phone 40,
same_email 40, sequential_same_ip 25, plagiarism_match 40; individual - prompt_injection 50,
hidden_text 35, invisible_chars 15, plagiarism_flag 30, speed_anomaly 25, high_code_similarity 20,
integrity_status 20, proctoring_flags 15, tab_switches 10, out_of_window_time 10, copy_paste 10,
secure_mode_exits 10. Thresholds: edge 30, medium 30, high 60. So a prompt-injection or hidden-text
resume alone is Medium, invisible characters alone are Low (zero-width characters can come from
copy-paste), and a Workday row with "Integrity Issues: Medium" plus an Integrity Summary is Medium
(20 + 15).

Risk flags are **review evidence, not verdicts**: wording must say "pattern consistent with ..."
not "cheated".

## Column contract: `PipelineResult.candidates`

Final columns, in this order where present (`scoring.order_columns`):

rank, status, status_reason, candidate_id, name, job_application, email, phone, college,
college_tier, tier_source, degree, branch, graduation_year, cgpa, cgpa_source, city, final_score,
pts_hackerrank, pts_skills, pts_experience, pts_education, sub_hackerrank, sub_skills,
sub_experience, sub_education, hackerrank_score, max_score, hackerrank_pct, cutoff_score,
cutoff_pct, candidate_option, assessment_comment, risk_level, risk_score, risk_reasons, cluster_id,
is_focus, n_links, max_pair_score, individual_score, injection_flags, duplicate_of, duplicate_ids,
duplicate_evidence, matched_skills, missing_skills, n_matched_skills, skills_found,
internship_count, experience_months, project_count, certifications, cert_count, resume_cgpa,
resume_degree, relevance, experience_source, llm_rationale, score_notes, tie_break_note,
test_start_time, submission_time, completion_minutes, ip_address, ip_subnet, test_mode,
tab_switches, copy_paste_events, plagiarism_flag, source, email_domain, test_slot, q{n}_hash...,
q{n}_score..., resume_file, resume_hash, has_resume, match_method, match_confidence, eligible,
ineligibility, college_capped, data_flags, then any other feed columns (test_name, test_status,
integrity_status, proctoring_flags, out_of_window_seconds, secure_mode_exits, max_code_similarity,
q{n}_similarity, plagiarism_match, plagiarism_match_id, ip_addresses, report_url, ...), then extra_*.

Columns added in this release:

| Column | Type | Written by | Meaning |
|---|---|---|---|
| `job_application` | str | ingest | Workday "Job Application" value ("First Last (first last)"); the name source when there is no name column |
| `assessment_comment` | str | ingest | cleaned assessment comment, one part per line (Workday composite comment, or a plain comment column) |
| `candidate_option` | str | ingest | assessment language track, e.g. "Spring Boot (Java)" |
| `cutoff_score` | float | ingest | pass mark in points from the export (reference only) |
| `cutoff_pct` | float | ingest | pass mark in percent from the export (reference only) |
| `ineligibility` | str (JSON list) | apply_shortlist | `[{"rule_violated", "candidate_value", "threshold_value", "explanation"}]`; "[]" when eligible |
| `tie_break_note` | str | apply_shortlist | how a tie on final score was decided; "" when the score is not tied |
| `cgpa_source` | str | score_candidates | "feed", "resume" or "" - where the CGPA used for scoring came from |
| `tier_source` | str | score_candidates | "feed", "tier list" or "" - where the tier used for scoring came from ("" also when tier scoring is off) |
| `duplicate_of` | str | duplicates | on a duplicate application: the primary application's candidate_id |
| `duplicate_ids` | str (", ") | duplicates | on the primary application: its duplicates' candidate_ids |
| `duplicate_evidence` | str | duplicates | why the applications were judged to be one person |
| `injection_flags` | str (" \| ") | risk | summaries of hidden / adversarial resume text |

Source groups (`S.COLUMN_SOURCE`; used for leaderboard and CSV header prefixes, the Excel
super-header row and the Compare tab):

| Group (`S.SRC_*`) | Prefix | Columns |
|---|---|---|
| Assessment | `HackerRank:` | hackerrank_score, max_score, hackerrank_pct, pts_hackerrank, sub_hackerrank, test_name, test_status, test_start_time, submission_time, completion_minutes, cutoff_score, cutoff_pct, candidate_option, integrity_status, proctoring_flags, plagiarism_flag, max_code_similarity, tab_switches, copy_paste_events, out_of_window_seconds, secure_mode_exits, ip_address, ip_subnet, ip_addresses, test_mode, assessment_comment, plagiarism_match, plagiarism_match_id, report_url, test_slot (+ q{n}_hash / q{n}_score / q{n}_similarity in exports) |
| Resume | `Resume:` | skills_found, matched_skills, missing_skills, n_matched_skills, pts_skills, sub_skills, pts_experience, sub_experience, internship_count, experience_months, project_count, certifications, cert_count, relevance, resume_degree, resume_cgpa, has_resume, resume_file, resume_hash, match_method, match_confidence, experience_source |
| Academic | `Academic:` | college, college_tier, tier_source, degree, branch, graduation_year, cgpa, cgpa_source, pts_education, sub_education |
| Integrity | `Integrity:` | risk_level, risk_score, risk_reasons, cluster_id, is_focus, n_links, max_pair_score, individual_score, injection_flags, duplicate_of, duplicate_ids, duplicate_evidence |

Exports add three groups of their own: "Candidate & Outcome" (rank, status, status_reason,
candidate_id, name, job_application, email, phone, city, source, email_domain, final_score, eligible,
ineligibility, tie_break_note, college_capped, score_notes - no prefix), "AI Notes (LLM)" and "Other"
(data_flags, extra_*). Example labels: "HackerRank: Score", "HackerRank: Comment", "Resume: Skills
Pts", "Academic: CGPA", "Academic: Tier Source", "Integrity: Risk Level", "Integrity: Hidden Text
Flags", "Integrity: Duplicate Of".

## LLM layer (llm_backends.py, llm.py)

There is exactly one LLM path: GitHub Copilot chat models served by the "Screening Copilot Proxy"
VS Code extension on the same machine. Provider-specific values live only in `config.LLM_PROVIDERS`
(one entry, `"copilot"`: label "GitHub Copilot (VS Code proxy)", base_url `http://127.0.0.1:5757/v1`,
model "" = the proxy picks, key_env `COPILOT_PROXY_TOKEN`, no fallback models) and in
`screening/llm_backends.py`.

The LLM is used only for rationales: shortlist rationales, borderline gap analyses, the Deep-Dive
"explain this candidate", risk-group (cluster) summaries and the batch executive summary (see the
table under screening/llm.py below). It never changes scores, ranks, cutoffs or the shortlist, and
it does not answer free-form questions.

`LLMConfig` defaults: enabled True; provider "copilot"; api_key_env "COPILOT_PROXY_TOKEN"; base_url
"http://127.0.0.1:5757/v1"; model "" (proxy default); fallback_models []; temperature 0.2; max_tokens
2048; timeout_sec 120; max_retries 3; max_concurrency 4; max_input_chars 6000; cache_dir ".cache/llm";
borderline_band 50. The connection fields (provider, api_key_env, base_url, fallback_models,
cache_dir) are never taken from an uploaded config profile (`config.LLM_CONNECTION_FIELDS`), so a
profile cannot redirect the token or candidate data.

### screening/llm_backends.py (transport)

```python
class ProxyChatClient:          # requests.Session with trust_env=False (never via HTTP(S)_PROXY / .netrc),
                                # no redirects; timeouts (connect 5 s, read cfg.timeout_sec)
    chat.completions.create(model="", messages=[...], temperature=None, max_tokens=None)
        # -> resp.choices[0].message.content (attribute access over the JSON reply)
    models.list(timeout=None)   # -> resp.data[*].id
def discovery_path() -> Path                     # ~/.screening-copilot-proxy.json
def discover_proxy(path=None) -> dict | None
    # {"base_url", "token", "pid", "started_at", "version"}; base_url kept only when it is a loopback
    # URL (else None), so a tampered file can never send candidate data elsewhere; None when the file
    # is missing, over 64 KB, not JSON, or the token is not 16-512 token characters
def is_loopback_url(url) -> bool                 # http(s) to localhost / 127.0.0.0/8 / ::1
def resolve_connection(cfg, api_key=None) -> tuple[str, str, str]   # (base_url, token, "configured"|"discovered"|"")
def build_chat_client(cfg, api_key) -> ProxyChatClient             # ValueError when no token exists
def proxy_status(cfg, token=None, *, timeout=3.0) -> dict
    # one GET /v1/models -> {"reachable", "models", "error", "base_url", "token_source"};
    # never raises, never contains the token
def classify_error(exc) -> str                   # FATAL | MODEL_UNAVAILABLE | TRANSIENT | OTHER
def describe_error(exc, cfg, *, model=None, detail="") -> str       # friendly, token-free message
class ProxyError(Exception)                      # HTTP error answered by the proxy: status_code, error_type,
                                                 # code, body, response.headers (Retry-After)
class ProxyNotRunningError(ConnectionError)      # connection refused: VS Code or the extension is not running
KEY_PATTERNS                                     # 32+ hex characters, "sk-..." keys, GitHub tokens: masked
```

* Connection: a configured token (environment variable, Streamlit secret or explicit) wins; when the
  connection file holds the same token, the file's URL is used (a changed port is followed), otherwise
  `cfg.base_url`. Without a configured token the connection file supplies both token and URL. The
  file's URL is only ever used together with the file's own token.
* Failures: connection refused -> `ProxyNotRunningError` ("Could not reach the Copilot proxy at {url}.
  Start VS Code with the Screening Copilot proxy extension (status bar: "Copilot proxy: on"), or run
  "Screening Proxy: Start" from the VS Code Command Palette."); dropped connection -> ConnectionError;
  timeout -> TimeoutError; HTTP errors -> `ProxyError` with the proxy's `{"error": {"message", "type",
  "code"}}`.

| Class | When | What `LLMClient` does |
|---|---|---|
| `FATAL` | proxy not running; type/code `no_models`, `consent_required`, `unauthorized`, `invalid_token`, `unknown_endpoint`, `forbidden_host`, `browser_request`; HTTP 401 / 403 | no retries; a batch skips its remaining items ("Skipped: ...") |
| `MODEL_UNAVAILABLE` | `model_not_found`; HTTP 404 / 410 | moves to the next fallback model (none by default, so the error is reported) |
| `TRANSIENT` | timeouts, dropped connections, HTTP 408 / 409 / 425 / 429, 5xx | backs off and retries up to `max_retries` (Retry-After / Retry-After-Ms honoured, capped at 60 s; otherwise 1.5 s x 2^attempt, capped at 30 s, with jitter) |
| `OTHER` | anything else (bad request, ...) | this item fails; the batch continues |

### screening/llm.py (features)

```python
PROMPT_VERSION = "2026-11-09.1"                  # part of the cache key; bumped whenever prompts change
def resolve_token(explicit=None, env_name=None, *, discover=True) -> tuple[str | None, str]
    # explicit (e.g. st.secrets) > environment variable env_name (default COPILOT_PROXY_TOKEN)
    # > the connection file of the running proxy; source "explicit" | "environment" | "discovered" | ""
def resolve_api_key(explicit=None, env_name=None, *, discover=True) -> str | None
def describe_key(api_key) -> str                 # "configured (…c3d4)": never more than the last 4 characters
def redact_pii(text, names=None) -> str          # emails (also "name [at] host [dot] com"), URLs and bare
                                                 # domains, IPv4/IPv6, phone numbers, the given names
                                                 # -> [REDACTED_EMAIL|URL|IP|PHONE|NAME]
class LLMError(Exception)                        # user-presentable; .fatal stops a batch
class LLMClient:
    def __init__(self, cfg: LLMConfig, api_key: str | None, client: Any | None = None)  # client injectable for tests
    def test_connection(self) -> tuple[bool, str]          # one tiny uncached request ("Reply with the single word OK.")
    def chat_json(self, system, user, *, max_tokens=None, required_keys=()) -> dict
    def chat_json_meta(self, system, user, *, max_tokens=None, required_keys=()) -> tuple[dict, str]  # source "cache"|"llm"
def create_client(cfg: LLMConfig, api_key=None) -> LLMClient | None    # None when disabled or no token
def borderline_ids(candidates, cfg, band=None) -> list[str]
def explain_candidates(candidate_ids, candidates, parsed_by_candidate, risk_result, cfg, client,
                       progress_cb=None, *, focus="qualified") -> dict[str, dict]
    # -> {id: {"ok", "focus", "summary", "strengths", "gaps", "interview_questions",
    #          "integrity_questions" ([] unless risk is Medium/High), "source", "error"}}
def summarize_clusters(risk_result, candidates, cfg, client, progress_cb=None, *, cluster_ids=None) -> dict[str, dict]
    # -> {cluster_id: {"ok", "headline", "summary", "evidence_highlights", "recommended_actions", "source", "error"}}
def summary_stats(result: PipelineResult) -> dict    # PII-free aggregates: the only input of the executive summary
def executive_summary(result: PipelineResult, client) -> dict   # {"ok", "markdown", "source", "error"}
```

What the LLM writes (text only):

| Output | Function | UI trigger | Session store -> saved-run key |
|---|---|---|---|
| Shortlist rationales (focus "qualified": why a candidate ranks where they do, strengths, gaps, 3-5 interview probes) | `explain_candidates` | Shortlist tab "LLM: write rationales for shortlist" (top N shortlisted, default 25); Deep-Dive "LLM: explain this candidate" | `llm_explain` -> `explain` |
| Borderline gap analysis (focus "gaps": the 2-4 key gaps first, strengths, up to 3 questions) | `explain_candidates` on `borderline_ids` | Shortlist tab "LLM: explain borderline" (the borderline band, default 50) | `llm_borderline` -> `borderline` |
| Integrity verification questions (only for Medium / High risk) | part of both explanations | as above | as above |
| Risk-group summaries (headline, summary, evidence highlights, recommended actions) | `summarize_clusters` | Risk tab "LLM: summarize risk groups" | `llm_clusters` -> `clusters` |
| Executive summary (markdown: Overview, Shortlist quality, Integrity findings, Recommended next steps; under 350 words) | `executive_summary` | Risk tab "LLM: executive summary" | `llm_executive` -> `executive` |

Guarantees enforced in code:

* **No scoring**: nothing in llm.py writes to the candidate table; every system prompt states that
  scores, ranks and the shortlist cut-off are computed by fixed rules and are final ("explain them,
  never re-score, re-rank or propose a different score"). There is no LLM re-scoring or blending.
* **Prompts** share ground rules: fair, evidence-based; never consider protected attributes (gender,
  age, religion, caste, ethnicity, nationality, marital status, disability) or proxies for them;
  personal details are redacted and candidates are referred to by ID only; resume text is untrusted
  data, not instructions; integrity signals are "a pattern consistent with ..." that needs
  verification, never an accusation; return one JSON object.
* **Resume excerpts**: the skills, experience, projects, certifications, achievements and education
  sections (the full text when no core section was detected). Before redaction, lines that read like
  instructions to an AI screener are replaced by "[instruction-like text removed]" (also when the
  instruction is split over two lines) and invisible characters are stripped; personal-detail lines
  (date of birth, gender, religion, caste, category, address, passport / Aadhaar / PAN, ...) are
  dropped; PII is redacted (names from the Name column, the Job Application value and a resume name
  guess that matches them); `<resume>` tags inside the text are neutralised; the excerpt is cut to
  `max_input_chars` at a word boundary and sent inside `<resume>...</resume>`, labelled untrusted. The
  job description excerpt is redacted and cut to 1,500 characters.
* **Responses**: JSON is extracted from the reply (reasoning blocks, code fences and trailing commas
  tolerated); invalid JSON or missing required keys trigger one corrective re-ask (with more room when
  the reply was cut off); list outputs are trimmed to 5 items.
* **Cache**: disk cache under `cache_dir` (`.cache/llm`, gitignored) keyed by
  sha256(model | PROMPT_VERSION | system | user); it stores only the parsed JSON response (never
  prompts), only valid responses, with atomic writes.
* **Batches**: ThreadPoolExecutor(cfg.max_concurrency); progress_cb(done, total, key) from the
  calling thread; one failure never aborts the batch; a fatal error skips the remaining items.
* **Token hygiene**: tokens never appear in messages, logs or reprs (`LLMClient` repr shows
  `describe_key`); error text is scrubbed with `KEY_PATTERNS` and "Bearer ***".
* Integrity wording: "pattern consistent with", recommend verification steps (live coding
  re-test, project walkthrough), never accuse.

## VS Code extension: vscode-copilot-proxy/

"Screening Copilot Proxy" (`screening-copilot-proxy` 0.1.0) runs a small HTTP server inside VS Code
that speaks the OpenAI-style chat-completions format and forwards each request to a GitHub Copilot
chat model through VS Code's Language Model API. Requirements: VS Code 1.95 or later and GitHub
Copilot Chat signed in (a Copilot licence) on the same machine as the console. Plain JavaScript, no
build step and no npm dependencies. Install with **Developer: Install Extension from Location...** on
the folder, or package a VSIX (`npx @vscode/vsce package --allow-missing-repository --skip-license`)
and use **Extensions: Install from VSIX...**. User documentation: [COPILOT_PROXY.md](COPILOT_PROXY.md)
(setup, use in the app, troubleshooting) and [vscode-copilot-proxy/README.md](../vscode-copilot-proxy/README.md).

```
Screening console (Streamlit)  --HTTP + bearer token-->  extension (VS Code)  -->  GitHub Copilot
   llm_backends.ProxyChatClient       127.0.0.1:5757           vscode.lm.selectChatModels / sendRequest
```

Split of responsibilities:

* `lib/proxy-core.js` - everything on the request path, with no `vscode` import (the language-model
  API, the chat-message factory and a cancellation factory are injected): `CopilotProxy` (Node `http`
  server), routing, authentication, model choice, message mapping, error mapping, limits, the
  connection file and a health probe. Unit-tested with plain Node and fake language-model objects.
* `extension.js` - VS Code wiring: reads the settings, keeps the token, registers the commands,
  shows the status bar item ("Copilot proxy: on" / "off"; click = Show Status) and writes the output
  channel "Screening Copilot Proxy".
* `package.json` - activation `onStartupFinished`, `extensionKind` ["ui", "workspace"] (prefers the
  local machine), commands and settings.

Endpoints (every request needs `Authorization: Bearer <token>`; the `/v1`-less aliases `/models` and
`/chat/completions` and `/v1/health` also work):

| Endpoint | Response |
|---|---|
| `GET /health` | `{"ok": true, "models": <count>, "service": "screening-copilot-proxy", "version": "0.1.0"}` |
| `GET /v1/models` | `{"object": "list", "data": [{"id", "family", "name", "vendor": "copilot", "max_input_tokens", ...}]}` |
| `POST /v1/chat/completions` | non-streaming; body `{"model", "messages", "temperature", "max_tokens"}` -> `{"id", "object": "chat.completion", "created", "model", "choices": [{"index": 0, "message": {"role": "assistant", "content"}, "finish_reason": "stop"}], "usage"}` |

* Model choice: `model` matched to a Copilot model id or family (exact, then case-insensitive), else
  the `screeningProxy.modelFamily` setting, else the first available model; the reply's `model` names
  the model that answered. System messages are prepended to the first user message under
  "Instructions:" (Copilot's chat API has no system role). `temperature` is clamped to 0-2 and retried
  once without it when a model only accepts its default; `max_tokens` is accepted but the model's own
  output limit applies; `stream: true` -> 400; token usage is reported as zeros.
* Errors use `{"error": {"message", "type", "code"}}`: 400 `invalid_request_error` (malformed body,
  `stream_not_supported`, `context_length_exceeded`), 401 `invalid_token`, 403 `consent_required` /
  `forbidden_host` / `browser_request`, 404 `model_not_found` / `unknown_endpoint`, 405
  `method_not_allowed`, 413 `body_too_large`, 429 `blocked` (Copilot rate limit or quota) / `queue_full`
  (`Retry-After: 2`), 500 `internal_error`, 502 `copilot_error`, 503 `no_models`, 504 `timeout`. The
  Python client maps them as in the table above.

Security and limits:

* Binds `127.0.0.1` by default. When bound to a loopback address it rejects requests whose `Host`
  header is not a loopback name (403 `forbidden_host`; blocks DNS rebinding). Any other host setting
  shows a warning that the proxy (and the Copilot access) becomes reachable from the network.
* Bearer token: 32 random bytes as 64 hex characters, created on first start and kept in the
  extension's global state on this machine (not synced); compared in constant time (both sides
  hashed with SHA-256, then `timingSafeEqual`); read per request, so **Rotate Token** applies at once.
* Requests carrying an `Origin` header (web pages) are refused (403 `browser_request`); no CORS
  headers are ever sent. Responses carry `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.
* Bodies over 2 MB -> 413. At most `maxConcurrency` Copilot requests run at once (default 4, 1-32)
  with a queue of 16; a full queue answers 429. Each request has a time limit including queue time
  (`requestTimeoutSec`, default 120, 5-1800) after which the Copilot request is cancelled (504); a
  client that disconnects cancels its Copilot request.
* Consent: the first Copilot request makes VS Code ask whether the extension may use the Copilot
  language models (justification "Candidate screening console"). Until a request succeeds the proxy
  runs one request at a time, so only one dialog appears. A refused or dismissed dialog -> 403
  `consent_required` and a notification (at most once a minute) with an **Allow Access** button that
  runs **Test Copilot Access** (one tiny request, "Reply with the single word OK.", 90 s limit).
* Logging: one line per request (method, path, status, duration, model, error code); never prompts,
  replies or the token.
* Settings (`screeningProxy.port` 5757, `host` 127.0.0.1, `autoStart` true, `modelFamily` "",
  `maxConcurrency` 4, `requestTimeoutSec` 120) are machine-scoped: a workspace (a cloned repository)
  cannot change them. Changing a setting restarts a running proxy.
* Connection file `~/.screening-copilot-proxy.json` (`base_url`, `token`, `pid`, `started_at`,
  `version`): written atomically with mode 600 where the OS supports it while the proxy runs, updated on
  Rotate Token, and removed on Stop / when VS Code closes - only while it still describes this instance.
  The console reads it (`llm_backends.discover_proxy`), so normally no token is configured.
  `COPILOT_PROXY_TOKEN` (environment, `.env` or `.streamlit/secrets.toml`) pins a token copied with
  **Screening Proxy: Copy Token** instead.
* Several VS Code windows: the first window serves the port; another window that finds the port busy
  probes `/health` with the same token, shows "Copilot proxy: on" (served by another window) and checks
  every 30 s, taking over when the serving window stops. A different proxy (another token) or another
  program on the port -> an error message naming the port setting.
* Commands: **Screening Proxy: Start**, **Stop**, **Show Status** (URL, request counts, concurrency,
  available models and the default one, connection file; buttons Start/Stop, **Test Copilot Access**,
  Copy Token, Open Log), **Copy Token**, **Rotate Token** (confirmation; the old token stops working
  immediately).
* Tests: 54 Node tests (`cd vscode-copilot-proxy && node --test "test/*.test.js"`, or `npm test`) in
  auth, chat, connection-file, errors, extension, limits and models-messages suites.

## screening/export.py

```python
SHEET_NAMES = ["Summary", "Shortlist", "All Candidates", "Ineligible Candidates", "Integrity Evidence",
               "Risk Clusters", "Risk Pairs", "Commonality", "Data Quality", "Config"]
def to_csv_bytes(df: pd.DataFrame, *, labelled: bool = False) -> bytes   # utf-8-sig for Excel; labelled =
                                                                          # source-prefixed headers
def labelled_columns(df: pd.DataFrame, *, group: bool = False) -> pd.DataFrame
def column_source(col) -> str / column_group(col) -> str
def column_label(col, *, prefixed: bool = False) -> str / column_labels(columns, *, prefixed=True) -> dict
def ineligible_rows(candidates: pd.DataFrame) -> pd.DataFrame
    # one row per violated rule: candidate_id, name, rule, candidate_value, threshold_value, explanation
def ineligibility_counts(candidates: pd.DataFrame) -> list[tuple[str, int]]
def build_excel_report(result: PipelineResult, llm_notes: Mapping | None = None) -> bytes
def build_evidence_pack(result: PipelineResult, candidate_id: str) -> str   # markdown for recruiters
def sanitize_cell(value) / sanitize_frame(df)                               # formula-injection guard
```

* Formula injection: a text cell that begins with = + - @ (or a tab / carriage return) is prefixed
  with an apostrophe in every export; plain signed numbers ("-5", "+919876543210") are left as is.
* Excel: "Shortlist" (Shortlisted + Integrity Review, by rank) and "All Candidates" group their columns
  by data source under a coloured super-header row (row 1; labels in row 2); "Ineligible Candidates"
  lists one row per violated rule and the Summary sheet counts them by rule; "Integrity Evidence"
  lists flagged candidates with reasons, linked candidates, duplicate links and hidden-text flags. The
  Summary carries the disclaimer that integrity flags are evidence for human review, not findings of
  misconduct.
* CSV downloads (Shortlist tab) use the prefixed single-row headers ("HackerRank: Score").
* `llm_notes` (built by `ui/tab_shortlist.export_llm_notes` from the session; every key optional;
  notes with "ok": False are skipped):
  * "executive_summary": str or {"ok", "markdown"} -> "AI executive summary (LLM-generated - verify
    before acting)" block on Summary;
  * "clusters": {cluster_id: {"headline", "summary", "recommended_actions"}} -> AI columns on Risk Clusters;
  * "candidates": {id: {"summary", "strengths", "gaps"}} -> "AI Notes (LLM)" column on Shortlist,
    All Candidates and Integrity Evidence;
  * "borderline": {id: {...same shape...}} -> the same column, prefixed "Near the cutoff:".
* Evidence pack: profile, score breakdown, duplicate applications, risk reasons, hidden resume text,
  linked candidates with the shared evidence and neutral verification steps; e-mail and phone are
  never included.

## Saved runs and fast reload (runs.py, snapshots.py)

Both stores write JSON, Parquet and gzipped JSON only (never pickle), write into a temporary sibling
folder and move it into place with one rename, validate their ids against a strict pattern and stay
inside their base folder. Both hold candidate personal data.

**Saved runs** (`runs.RunStore(base_dir = env SCREENING_RUNS_DIR or <project>/runs)`): save /
list_runs / read_meta / load / rename / delete / compare_runs. One folder per run, run_id
`YYYYMMDD-HHMMSS-<6 hex>`:

```
meta.json              label, created_at, dataset label, mode, counts, app version, config, timings,
                       manifest of the stored tables
candidates.parquet     PipelineResult.candidates (every column, incl. ineligibility, tie-break notes,
                       CGPA / tier sources, duplicate links, hidden-text flags)
base.parquet / ta.parquet (absent for resume-only runs) / matches.parquet
risk_<name>.parquet    candidate_risk, pairs, clusters, commonality, individual
risk_meta.json         RiskResult focus_ids, params, warnings
quality.json           DataQualityReport
parsed.json.gz         every ParsedResume keyed by file_hash (incl. injection findings and the packet a
                       resume was split from) + the candidate -> resume links
llm.json               {"format": 1, "state": {"executive": {...} | None, "explain": {...},
                       "borderline": {...}, "clusters": {...}}} - every LLM output of the session
```

`runs.normalize_llm_state` always returns those four keys ("explain", "borderline" and "clusters" as
dicts, "executive" as a dict or None); the legacy key "rescore" from older versions is dropped on save
and load. Loading restores PreparedData + PipelineResult + config + the four LLM stores, so a run can
be re-tuned without the original files. Text that UTF-8 cannot encode (lone surrogates from PDF
extraction) is repaired on the way out.

**Fast reload** (`snapshots.py`): `prepare_data` output is cached per input fingerprint so a page
refresh, a new session or a colleague opening the same files reloads it in about a second.

```python
def save_snapshot(prepared, fingerprint, label="", cfg=None) -> dict | None   # None when disabled / ttl 0 / failed
def load_snapshot(fingerprint, cfg=None) -> PreparedData | None   # None when disabled, absent, expired, stale, damaged
def list_snapshots(cfg=None) -> pd.DataFrame     # fingerprint, label, created_at, candidates, resumes, size_mb, expires_at
def delete_snapshot(fingerprint, cfg=None) -> bool
def purge_expired(cfg=None, *, now=None) -> int  # runs on every list and load
def cache_root(cfg=None) -> Path                 # SnapshotConfig.cache_dir (relative to the project root)
def engine_signature() -> str
```

```
<cache_dir>/<fingerprint>/        default data/cache (gitignored with the rest of data/)
    manifest.json     kind, format, fingerprint, label, created_at, app version, engine signature,
                      mode, counts, original timings and the stored tables
    base.parquet / matches.parquet / ta.parquet / quality.json / parsed.json.gz   (runs.write_prepared)
```

* `SnapshotConfig`: enabled True, ttl_days 7 (the Config tab offers 1-30 days, or switching caching
  off), cache_dir "data/cache" (never taken from an uploaded profile).
* Only prepared data is cached - no scores, ranks or LLM output: evaluate recomputes those from the
  current configuration.
* Fingerprints must be 16-128 lowercase hex characters. A snapshot written by different preparation
  code is a cache miss and is deleted: the engine signature hashes the app version and the source of
  every `screening/` module except analytics, export, llm, llm_backends, risk and scoring (a
  parser fix is never hidden behind a stale cache). Expired, damaged and interrupted snapshots are
  purged; transient read errors (file in use) leave the snapshot in place.

## screening/env.py

```python
def load_env(path=None) -> list[str]        # names of the variables newly set (never their values)
def parse_env_text(text: str) -> dict[str, str]
```

`app.py` calls `load_env()` before the ui modules are imported (ui/theme.py reads `APP_BRAND_EYEBROW`
at import time). It reads `<project>/.env` (see `.env.example`; gitignored) and sets only variables
that are not already set - real environment variables always win. python-dotenv is used when
installed, otherwise a small built-in reader (`KEY=VALUE` lines, `export` prefixes, quotes, `#`
comments; no `${VAR}` expansion). A missing file is not an error; a file over 256 KB is ignored; values
are never logged. Variables the app reads: `COPILOT_PROXY_TOKEN` (or the name in `llm.api_key_env`),
`SCREENING_RUNS_DIR`, `APP_BRAND_EYEBROW`.

## UI (app.py + ui/)

Brand palette: red `#D71E28` / `#BA0C2F`, gold `#FFCD00` / `#F1B521`, slate `#1A1A1A` /
`#333333`, background `#F9F9F9`. Header banner (red gradient + gold rule; eyebrow text from env
`APP_BRAND_EYEBROW` - also from `.env` - else the built-in `BRAND_EYEBROW` default in theme.py), KPI
cards (Total Processed, Shortlisted, Risk Flags (High / Medium), Average Score, Perfect Scorers (100%)).
Tabs before the first run: Getting started (with **Load synthetic sample & run**); Config & Rubric
Tuning; Run History. After a run, 7 tabs in `state.TAB_LABELS` order: Candidate Shortlist &
Leaderboard; Integrity & Risk Intelligence; Individual Deep-Dive; Compare Candidates; Campus
Distribution; Run History; Config & Rubric Tuning. The three feature tabs are imported
lazily (`app.ENABLED_FEATURE_TABS`); one that fails to import is hidden instead of breaking the app.
Only the open tab renders (`st.tabs(..., on_change="rerun")`, `tab.open`).

Sidebar (`ui/sidebar.py`):

* "Data source": **Synthetic sample dataset** (caption: synthetic Workday export, 2,133 applications +
  Resume.zip with 33 PDF packets) or **Upload TA feed + resumes** - uploaders "TA export: Workday or
  HackerRank (CSV or Excel)" (csv, tsv, txt, xlsx, xlsm, xls) and "Resumes: Resume.zip, PDF, DOCX or
  TXT" (pdf, docx, txt, zip; several files). **Run screening**.
* "Review column mapping" expander (uploads only): a data editor with columns **Source column**,
  **Sample values** (up to 3 distinct values; HTML entities decoded, placeholders such as "#N/A"
  skipped), **Maps to** and **Action** (Include / Ignore); caption "Sheet used: {sheet} - detected
  automatically ..." for workbooks with several sheets; **Load mapping profile** /
  **Download mapping (JSON)** (`column_mapping.json`, `{"version": 1, "mapping": {header: field |
  "ignore"}}`, at most 64 KB; profile targets must be canonical fields, including q{n}_hash /
  q{n}_score / q{n}_similarity). Each field can be mapped once (the earlier column wins). The override
  is sent only when it differs from the auto-detected mapping.
* "Recent datasets (fast reload)" expander (only when caching is on; the cache folder is read only while
  the expander is open): up to 8 cached datasets with label, cached time, candidates, resumes, size and
  expiry; **Load** / delete buttons.
* **Clear results**, "Last run" summary with data-quality counts, "Data quality details" and "Timings".
* Copilot status badge "GitHub Copilot (VS Code proxy)": green "Connected · N models: ..." (with the
  token source: environment, secrets or connection file - never the token), orange "Not connected" /
  "Connected, but no models", gray when LLM features are off; **Check again** forces a new check;
  caption: the LLM only writes text.

UI state flow (`ui/state.py`):

```
sidebar.render() -> RunRequest(label, ta_file, resume_docs, mapping_override, use_sample, snapshot_fp)
state.run_screening(req)
  snapshot_fp given -> prepared = the session copy (same fingerprint) or snapshots.load_snapshot(fp)
                       (gone -> "This dataset is no longer in the cache ..." warning)
  else              -> use_sample: pipeline.load_sample_dataset() (+ the sample job description while the
                       config still holds the default one)
                       fp = inputs_fingerprint(ta_file, docs, mapping_override, cfg)
                       prepared = the session copy (same fp) or snapshots.load_snapshot(fp)
                                  or pipeline.prepare_data(...) then snapshots.save_snapshot(prepared, fp, label)
  fp changed        -> clear the LLM stores, the executive summary, the Deep-Dive selection and the memos
  result = _evaluate(prepared, fp, cfg)        toast "Screened N candidates [from the cache] in X s (...)"
state.current_result()
  config changed since the stored result -> _evaluate(prepared, fp, cfg) ("Re-scoring with the updated
  configuration..."); on failure the previous result stays on screen with an error
```

* `inputs_fingerprint` = sha256 over the TA file name + the sha256 of its bytes, the sorted (resume file
  name, sha256) pairs, the sorted mapping override and `asdict(cfg.processing)` - 64 hex characters,
  also the snapshot folder name.
* `eval_key` = sha256(prepared fingerprint | `cfg.fingerprint()`), where `AppConfig.fingerprint()` is the
  whole configuration as sorted JSON.
* `_evaluate` memo: an LRU of up to 4 PipelineResults per session (`_evaluate_memo`). Risk memo
  (`_risk_memo`): the last RiskResult keyed by sha256(prepared fingerprint | risk settings), passed
  to `pipeline.evaluate(..., risk_result=...)` while the risk settings are unchanged, so rubric and
  shortlist changes re-score in well under a second.
* Session keys: `app_config`, `app_config_version` (bumped on every config change), `prepared_data`,
  `prepared_fingerprint`, `pipeline_result`, `pipeline_result_key`, `_evaluate_memo`, `_risk_memo`,
  `llm_explain`, `llm_borderline`, `llm_clusters`, `llm_executive`, `_llm_client`, `_llm_status`,
  `dataset_label`, `main_tabs`, `deepdive_candidate`, `compare_candidate_ids`,
  `loaded_run_id`.
* LLM in the UI: `llm_token(cfg)` -> (token, source) from the environment variable `cfg.llm.api_key_env`,
  then `st.secrets` under the same name (a missing or malformed secrets file is ignored), then the
  connection file; `llm_ready` (no network call); `llm_status` (one `proxy_status` call, cached 30 s per
  session; `app.py` renders it last into its sidebar slot so the page never waits for it);
  `get_llm_client` (reused while the LLM settings and the token are unchanged; with a connection-file
  token the client also takes the URL from the file); `run_llm_task` (st.status with a progress bar;
  failures are shown with secret-looking text redacted).
* Opening a saved run (Run History) makes it the current screening with fingerprint `run:<run_id>`,
  restores its configuration and the four LLM stores, and resets the memos; saving stores the
  session's current LLM outputs with the run.
* The column-mapping preview uses `st.cache_data` (the table read keyed by file name + content hash,
  4 entries, 1 hour).

## Assessment export fields (addendum)

**Workday assessment export** (the primary input; full reference with examples in
[HACKERRANK_DATA_REFERENCE.md, Part 1](HACKERRANK_DATA_REFERENCE.md#part-1---workday-assessment-export)):
Job Application, Candidate ID, Assessment Comment, Email, CGPA, Branch, School, Location, Max Score,
Score, Cutoff Score, Cutoff Score Percentage, Percentage Score, Candidate Option. Canonical names in
schema.py: `JOB_APPLICATION, ASSESSMENT_COMMENT, CANDIDATE_OPTION, CUTOFF_SCORE, CUTOFF_PCT` (plus the
general fields). The composite comment fills `TEST_STATUS`, `INTEGRITY_STATUS` and `PROCTORING_FLAGS`.

**HackerRank for Work** (still accepted; header mapping in
[HACKERRANK_DATA_REFERENCE.md, Part 2](HACKERRANK_DATA_REFERENCE.md#part-2---hackerrank-for-work-exports)).
Research (HackerRank Work API v3 `TestCandidate` + CSV export) shows employers receive:
`full_name, email, score, max_score, percentage_score, attempt_starttime, attempt_endtime,
status (7 = completed), ats_state, questions (per-question scores), ip_address,
plagiarism_status (bool), plagiarism (detail), max_code_similarity, out_of_window_events,
out_of_window_duration (s), editor_paste_count, secure_mode_exit_count,
secure_mode_exit_duration, proctor_images, candidate_details (pre-test form: college,
degree, CGPA, phone, ...), report_url, pdf_url, performance_summary (AI), invited_by,
invite dates`, plus an AI-proctoring integrity status on newer plans.
Canonical names live in schema.py (`TEST_NAME, TEST_STATUS, HR_PCT, OUT_OF_WINDOW_SEC,
SECURE_MODE_EXITS, MAX_CODE_SIMILARITY, PLAGIARISM_MATCH, INTEGRITY_STATUS,
PROCTORING_FLAGS, REPORT_URL, q{n}_score, q{n}_similarity, q{n}_hash`; aliases in
TA_FIELD_ALIASES; per-question header regexes `*_HEADER_RE` allow trailing text, e.g.
"Q3 Score (Coding - Graphs)" -> q3_score).

Ingest normalisation for these:
* `test_status`: Title Case ("Completed", "Invited", "Expired", "In Progress",
  "Did Not Attempt"); API code 7 -> "Completed".
* `hackerrank_pct`: use the mapped percentage column when the score is missing; otherwise
  score/max. If both exist, keep score/max but note disagreements > 1 pt as an info issue.
* `out_of_window_seconds`: seconds; accepts 95, "95 s", "1m 35s", "00:01:35".
* `secure_mode_exits`: Int64. `max_code_similarity`, `q{n}_similarity`: float 0..100
  ("92%" -> 92; if a column's max <= 1 treat as fraction -> x100; outside 0-100 -> blank).
* `plagiarism_match`: other candidate's ID or email as given; derive `plagiarism_match_id`
  = resolved candidate_id (via ID, email or unique name lookup) or NaN.
* `plagiarism_flag`: True for suspicious/flagged/yes/true/1; False for "not suspicious",
  clear, no, false, 0, blank.
* `integrity_status`: Title Case str; `proctoring_flags`: str; `test_name`, `report_url`: str.

Risk usage (in addition to the signals above):
* Individual flags: `high_code_similarity` (max_code_similarity or max q{n}_similarity >=
  cfg.risk.code_similarity_threshold), `integrity_status` with a word in
  `S.INTEGRITY_CONCERN_VALUES` {review, suspicious, flagged, medium, high}, `proctoring_flags`
  non-empty, `out_of_window_time` (>= out_of_window_sec_threshold), `secure_mode_exits`
  (>= secure_mode_exit_threshold), `tab_switches` (out_of_window_events), `copy_paste`
  (editor_paste_count), `plagiarism_flag`, `speed_anomaly`, and one of `prompt_injection`,
  `hidden_text`, `invisible_chars` per resume. Weights in cfg.risk.individual_weights (12 keys, one
  Config box each).
* Pair signal `plagiarism_match`: A.plagiarism_match_id == B.candidate_id (either
  direction) -> weight cfg.risk.pair_weights["plagiarism_match"]; included when at least one
  side is in the focus group (the matched "source" may have scored < 100%).

## Shortlist controls, comparison, run history and deployment

Wiring: `ui/state.py` has `TAB_COMPARE`, `TAB_CAMPUS`, `TAB_HISTORY`,
`K_COMPARE_IDS`, `K_LOADED_RUN`; `app.py` renders `ui/tab_compare.py`,
`ui/tab_campus.py`, `ui/tab_history.py` (each `render(result: PipelineResult | None)`;
`tab_history.render(None)` and `tab_config.render(None)` also run before the first screening).
`ShortlistConfig` (defaults): method "top_n", top_n 300, top_pct 10, threshold 70, min_hackerrank_pct 0,
min_cgpa 0, allowed_grad_years [], exclude_high_risk False, review_risk_levels [High, Medium],
missing_resume_policy "auto", college_cap_pct 0 (= off), exclude_candidates [],
auto_disqualify_duplicates True, tie_break_order [hackerrank_pct, sub_skills, sub_experience].

### screening/analytics.py
```python
IN_SHORTLIST = (S.STATUS_SHORTLISTED, S.STATUS_REVIEW)
def campus_distribution(candidates: pd.DataFrame, by: str = S.COLLEGE, cfg: AppConfig | None = None) -> pd.DataFrame
    # one row per group value (NaN -> "(unknown)"): group, applicants, eligible, perfect_scorers,
    # shortlisted (IN_SHORTLIST), in_review, capped_out (COLLEGE_CAPPED), shortlist_rate (shortlisted/eligible),
    # share_of_shortlist, share_of_applicants, representation (share_of_shortlist/share_of_applicants),
    # avg_final_score, avg_hackerrank_pct, flagged (risk High+Medium), over_cap (bool: by==college and
    # cap>0 and share_of_shortlist*100 > cap). Sorted shortlisted desc, applicants desc, group.
def shortlist_diff(before: pd.DataFrame, after: pd.DataFrame) -> dict
    # {"entering", "leaving", "rank_moves", "status_changes": DataFrames, "summary": dict(shortlisted_before,
    #  shortlisted_after, entering, leaving, cutoff_before, cutoff_after, review_before, review_after)}
```
### College cap (scoring.apply_shortlist)
cap_n = max(1, floor(college_cap_pct/100 x shortlist slots)). Walk eligible candidates in rank order;
a candidate whose college already holds cap_n shortlist places (Shortlisted + Integrity Review; holds
don't count) is not shortlisted: STATUS_NOT, COLLEGE_CAPPED True, reason "College cap reached: {college}
already has {n} shortlisted (cap {pct}%)"; the next-best candidates fill the slots (top_n / top_pct).
Threshold method: capped candidates are excluded, no backfill below the threshold. Unknown college is
never capped. cap 0 -> no cap at all (COLLEGE_CAPPED False everywhere).

### Config & Rubric Tuning (ui/tab_config.py)
One form ("Apply & re-score"; "Preview impact" for the what-if) holds every setting: rubric weights and
the presets "With Resumes (40/30/20/10)" / "No Resumes (80/0/0/20)"; required skills and the job
description; experience and education sub-weights; "Score college tier" (off by default, with the
fairness note) and its fuzzy match threshold (70-100); shortlist method, Min HackerRank %, Min CGPA,
allowed graduation years, "Max share of shortlist per college (%)", "Tie-break order (after the final
score)" (caption: candidate ID is always the final tie-break), missing-resume policy ("Auto
(recommended)", "Score Skills & Experience as 0", "Rescale HackerRank + Education to 100"); "Duplicates &
exclusions" ("Auto-disqualify duplicate applications ..." checkbox and "Exclude / disqualify candidate
IDs", comma / space / newline separated, matched ignoring case); integrity policy, review levels, risk
thresholds, team-project settings and signal weights; the LLM section "LLM (GitHub Copilot via VS Code
proxy)" (status line, "LLM features enabled", Model (the proxy's models plus "(proxy default)"; a
model id can also be typed), Max concurrency 1-32, Temperature 0-1, Max tokens 128-8192, Timeout
5-600 s, Max input chars 1,000-50,000, Borderline band 0-500) and "Fast reload" ("Cache parsed datasets for fast reload", "Keep
cached datasets for (days)" 1-30). Below the form: "College tier list" (upload CSV / XLSX up to 2 MB and
5,000 rows with columns college, tier and optional aliases separated by ';'; preview of the dataset's
colleges with tier and source; "Use this tier list", "Download tier list (CSV)", "Clear tier list"),
config download / upload ("Download current config (JSON)", "Upload a config JSON", "Reset to defaults"),
"Clear cached datasets" and "Test connection" (one tiny LLM request).

### What-if preview (ui/tab_config.py)
The form has a second submit button "Preview impact": evaluate(prepared, candidate_cfg) and show
analytics.shortlist_diff(current, preview) (summary metrics, entering, leaving, status changes, rank moves,
candidates moving into or out of ineligibility by rule) with "Apply these changes" / "Discard preview".
Disabled before the first run.

### Compare (ui/tab_compare.py) - 2-4 candidates side by side, values grouped by data source with prefixed labels; "Add to compare" button in ui/tab_deepdive.py.
### Run history (screening/runs.py + ui/tab_history.py)
"Save current run" (label, **Save run**; a caption lists the LLM outputs that will be kept), the saved-runs
table (with an LLM column summarising what each run's llm.json holds), open / rename / delete, and a
comparison of two runs (shortlist diff and configuration changes). Storage: see "Saved runs and fast
reload" above; the retention note reminds users that saved runs contain candidate personal data.
### Deployment (docs/DEPLOYMENT.md + scripts/setup.ps1|.sh, scripts/run_app.ps1|.sh) for a company VM.
Listens on 127.0.0.1 by default (`[server] address` in .streamlit/config.toml and the run scripts'
default); `-Address 0.0.0.0` / `--address 0.0.0.0` (or `STREAMLIT_SERVER_ADDRESS` without the scripts)
exposes it on the intranet. Uploads up to 1,024 MB per file (`maxUploadSize`). The LLM features need
VS Code with the Screening Copilot Proxy extension and a signed-in GitHub Copilot account **on the
machine that runs the app** (the proxy only listens on 127.0.0.1); without it every LLM button explains
what is missing and everything else works (setup: [COPILOT_PROXY.md](COPILOT_PROXY.md)). The synthetic
sample is generated on each machine (data/ is gitignored). Full procedure: [DEPLOYMENT.md](DEPLOYMENT.md).

## Tests and the end-to-end check

* Python: 1,114 pytest tests (`pytest` from the project root; pytest.ini sets `pythonpath = .`,
  `testpaths = tests`, `-q`), passing on Python 3.11 and 3.14. Engine modules are tested without
  Streamlit; the UI is exercised with Streamlit's app-testing harness and fixtures built from the
  column contract in this document (`tests/ui_fixtures.py`).
* Extension: 54 Node tests (`cd vscode-copilot-proxy && node --test "test/*.test.js"` or `npm test`).
* `scripts/run_e2e.py` loads `data/sample` through `pipeline.load_sample_dataset`, runs prepare_data +
  evaluate (rules only, no LLM), prints mode, counts, risk levels, clusters, the shortlist outcome and
  stage timings, and - when `<sample>/_ground_truth/` exists - evaluates 13 checks: duplicate recall,
  duplicate precision, institutional application kept, namesakes kept apart, duplicates marked
  Ineligible, team projects not flagged, resume mills flagged, code-similarity rows flagged, hidden /
  adversarial resume text flagged (each planted resume at its expected level), hidden keywords not
  counted as skills, boilerplate-bullet resumes not flagged,
  missing-resume count, unscored row not shortlisted. Options: `--sample DIR`, `--excel out.xlsx`,
  `--strict` (exit 1 when a check fails). Output contains candidate IDs and counts only.
* On the default sample (`python generate_mock_data.py`, seed 42) all 13 checks pass: 7 of 7 planted
  duplicate pairs found with no false pair (the institutional application kept each time, 4 namesake
  pairs kept apart), 13 team-project pairs with no member above Low, 11 of 11 resume-mill candidates
  High in 2 clusters, both code-similarity rows Medium, 5 of 5 hidden / adversarial-text resumes at the
  expected level (the invisible-characters-only resume Low, the other four Medium) with no hidden
  keyword counted as a skill, 0 of 145 boilerplate-bullet resumes flagged, 256 applications
  (12.0%) without a resume, and the unscored "#N/A" row Ineligible. Shortlist: 299 Shortlisted, 1
  Shortlisted (Integrity Review), 8 Ineligible (Duplicate Application 7, Assessment 1).
