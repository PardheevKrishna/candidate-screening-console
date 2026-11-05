# Architecture & Module Contracts

Candidate Screening & Integrity Console: a Streamlit app that fuses a TA/HackerRank
export (CSV/XLSX) with bulk resumes (PDF/DOCX/TXT/ZIP), scores every candidate with
a weighted rubric (default 40/30/20/10), shortlists the top N, and runs an integrity engine that
explains what candidates who scored 100% have in common.

```
app.py                      Streamlit entry (thin): page config, theme, sidebar, tabs; a feature tab
                            whose module fails to import is hidden instead of breaking the app
ui/                         Streamlit-only code (imports streamlit)
  theme.py                  brand CSS, header banner, KPI cards, badges
  state.py                  session_state keys, config get/set, cached pipeline calls, LLM readiness
  sidebar.py                data sources, column mapping, run button, LLM status
  charts.py                 plotly figure builders (pure functions -> go.Figure)
  tab_shortlist.py          Tab 1 Candidate Shortlist & Leaderboard (+ Excel download with LLM notes)
  tab_risk.py               Tab 2 Integrity & Risk Intelligence
  tab_deepdive.py           Tab 3 Individual Deep-Dive
  tab_compare.py            Tab 4 Compare Candidates
  tab_campus.py             Tab 5 Campus Distribution
  tab_ask.py                Tab 6 Ask the Data
  tab_history.py            Tab 7 Run History (also shown before the first run)
  tab_config.py             Tab 8 Config & Rubric Tuning
screening/                  engine (NO streamlit imports; unit-testable)
  schema.py                 canonical column names, aliases, vocab
  config.py                 AppConfig dataclasses, LLM_PROVIDERS preset
  models.py                 ResumeDoc, ParsedResume, DataQualityReport, RiskResult, PipelineResult
  pipeline.py               prepare_data / evaluate / run_llm_rescore / run_pipeline / load_sample_dataset
  ingest.py                 TA feed loading, header auto-mapping, normalisation
  resume_parser.py          text extraction + section/entity parsing
  skills.py                 skill taxonomy, extraction, required-skill matching
  matching.py               resume <-> candidate linkage
  scoring.py                rubric scoring, shortlist/status, cutoff band, college cap
  risk.py                   integrity & anomaly engine
  analytics.py              campus distribution, shortlist diff (preview impact, run comparison)
  ask.py                    Ask the Data: query-plan prompt, validation, local execution
  runs.py                   saved runs (RunStore)
  llm.py                    LLMClient (retries, model fallback, JSON, disk cache) + LLM features
  llm_backends.py           provider adapter: chat client (NVIDIA endpoint through the openai SDK
                            by default), error classification, key masking, TachyonShim
  export.py                 CSV / Excel / evidence-pack exports
generate_mock_data.py       synthetic TA/HackerRank export + resumes + ground truth (-> data/sample/,
                            generated locally; data/ is gitignored except data/README.md)
tests/                      pytest (pytest.ini sets pythonpath=.)
```

## Global rules (all modules)

* Python 3.11 virtual environment (streamlit 1.66, pandas 3.1, numpy 2.4, scikit-learn 1.9, scipy 1.17, plotly 7.1,
  networkx 3.6, pypdf 6, pdfplumber 0.11, python-docx 1.2, openai 3.46, rapidfuzz 3.14,
  reportlab 5.1, xlsxwriter 3.3, openpyxl 3.1). Do not add dependencies without a strong reason.
* **pandas 3.x semantics**: Copy-on-Write is always on - never use chained assignment
  (`df[col][mask] = x`); use `df.loc[mask, col] = x`. Default string dtype is `str`
  (missing = NaN). Don't rely on object dtype for strings. Avoid deprecated APIs.
* `screening/` must not import streamlit. Use `from . import schema as S` and refer to
  columns as `S.CANDIDATE_ID`, never string literals.
* Never raise out of per-row/per-file work: record problems in DataQualityReport issues
  (`{"severity","stage","issue","candidate_id","file_name"}`) and keep going.
* No PII in logs/exceptions. Never log or print API keys.
* Cells that hold lists are strings joined with `S.SEP_LIST` (", "), `S.SEP_NOTES` ("; ")
  or `S.SEP_REASONS` (" | ") as documented per column.
* Determinism: same inputs + config -> same outputs (sort before tie-breaks; seed RNGs).
* Performance budget at 3,000 candidates / 2,900 resumes: prepare_data < 90 s,
  evaluate < 5 s, risk analysis < 3 s for a focus group of ~300.
* `progress_cb` callbacks are invoked **only from the calling thread** (never from worker
  threads) - Streamlit elements cannot be updated from other threads.

## Data flow

```
ta_file ──ingest.load_ta_feed──▶ ta_df (canonical cols + derived) ─┐
resumes ──resume_parser.expand_uploads/parse_resumes──▶ parsed ────┤
                         matching.match_resumes ──▶ matches ───────┤
                         matching.link_candidates ──▶ base, parsed_by_candidate
evaluate(cfg): scoring.score_candidates ─▶ risk.run_risk_analysis ─▶ merge ─▶ scoring.apply_shortlist
               (+ LLM blend for ids in scoring.cutoff_band_ids, then re-score + re-shortlist)
```

## screening/ingest.py

```python
def read_table(file_name: str, content: bytes) -> pd.DataFrame
    # .csv/.tsv/.txt: try utf-8-sig, utf-8, cp1252, latin-1; sniff delimiter (, ; \t |).
    # .xlsx/.xlsm/.xls: first sheet with data (openpyxl). Header row auto-detected: first row
    # with >= 3 non-empty cells (exports sometimes have title rows). All columns read as str.
def suggest_column_mapping(columns: list[str]) -> dict[str, str]
    # source header -> canonical name. normalize_header() then exact alias match
    # (schema.TA_FIELD_ALIASES), then answer-hash/per-question regexes, then rapidfuzz
    # (ratio >= 90) for leftovers. One-to-one: each canonical used at most once; exact beats
    # fuzzy; earlier column wins ties. Never map two sources to one canonical.
def normalize_ta_feed(raw: pd.DataFrame, mapping: dict[str, str], cfg: AppConfig
                      ) -> tuple[pd.DataFrame, DataQualityReport]
def load_ta_feed(file_name: str, content: bytes, cfg: AppConfig,
                 mapping_override: dict[str, str] | None = None
                 ) -> tuple[pd.DataFrame, DataQualityReport]
    # read_table -> (mapping_override or suggest_column_mapping) -> normalize_ta_feed.
    # report.column_mapping / unmapped_columns / missing_fields / rows_raw / rows_clean filled.
```

Normalisation rules (output has exactly one row per candidate_id):

* `candidate_id`: stripped str, uppercased. Missing column -> generated `ROW-00001...`
  (warning). Blank IDs -> generated + warning.
* `name`: collapse whitespace, title-case if all-lower/upper. If absent but first/last name
  columns exist (`firstname`,`lastname`), join them.
* `email`: lowercase/strip; invalid -> NaN + warning. `email_domain` derived.
* `phone`: digits only, keep last 10 (Indian numbers) - store as str.
* `hackerrank_score`, `max_score`: float. Missing max -> default 100 (info). Score > max ->
  clip to max + warning flag `score_exceeds_max`. Missing score -> NaN + flag `score_missing`.
  If a percentage column is present (mapped to `hackerrank_pct`) and score is missing, use it.
* `hackerrank_pct` = score / max * 100 clipped to [0,100].
* `cgpa`: float; if 10 < v <= 100 treat as percentage -> v/10; if column max <= 4.0 treat as
  4-point scale -> v*2.5. Out of range -> NaN + warning.
* `graduation_year`: int (nullable Int64).
* `test_start_time`, `submission_time`: `pd.to_datetime(errors="coerce", format="mixed")`
  (naive local time). Unparseable -> NaT + warning.
* `completion_minutes`: numeric minutes; strings "HH:MM:SS", "MM:SS", "1h 12m", "72 min" parsed;
  if all numeric and median > 600 assume seconds -> /60 (info). Missing but start & submit
  present -> computed.
* `ip_address`: first IP if multiple listed (keep the full list in `ip_addresses` if a
  multi-IP column exists); `ip_subnet` = IPv4 /24 ("49.36.12.0/24").
* `tab_switches`, `copy_paste_events`: Int64. `plagiarism_flag`: bool from
  yes/no/true/false/1/0/flagged/suspicious/"not suspicious"/blank.
* `test_mode`: normalise to "Remote" / "On-campus" when recognisable, else keep text.
* `college_tier`: keep as str of the first digit found ("Tier 1" -> "1"); NaN if none.
* `test_slot` derived = test_start_time floored to the hour, as str "YYYY-MM-DD HH:00".
* Per-question columns: answer hashes -> `q{n}_hash` (str); per-question scores ->
  `q{n}_score` (float) when present.
* Duplicated candidate_id: keep the row with the latest submission_time (else last row);
  count in `duplicates_dropped`; warning per dropped ID.
* Same email on different candidate_ids -> data flag `shared_email` on all of them (risk uses it).
* `data_flags`: "; "-joined flags per row (e.g. `score_missing; score_exceeds_max`).
* Unmapped source columns are kept unchanged (prefixed `extra_` + normalised header) so
  nothing the TA team sends is lost.

## screening/resume_parser.py + screening/skills.py

```python
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt"}
def expand_uploads(docs: list[ResumeDoc], cfg: ProcessingConfig) -> tuple[list[ResumeDoc], list[dict]]
    # Expand .zip archives in memory (no extraction to disk): skip directories, __MACOSX,
    # hidden files, unsupported extensions; enforce max_zip_members, max_zip_total_mb,
    # max_file_mb, zip-bomb ratio guard (uncompressed/compressed > 200 -> skip member);
    # strip paths (basename only). Unsupported top-level files -> issue. Returns issues.
def load_resume_folder(path: str | Path) -> list[ResumeDoc]      # non-recursive + recursive? -> recursive, sorted
def extract_text(doc: ResumeDoc) -> tuple[str, list[str]]       # (text, warnings); never raises
    # .pdf: pypdf first; if < 100 chars, fall back to pdfplumber. .docx: python-docx paragraphs
    # + tables (+ header/footer). .txt: utf-8-sig/utf-8/cp1252/latin-1.
def parse_resume_text(text: str, file_name: str = "", file_hash: str = "",
                      id_pattern: str | None = None) -> ParsedResume
def parse_resume(doc: ResumeDoc, id_pattern: str | None = None) -> ParsedResume   # never raises
def parse_resumes(docs: list[ResumeDoc], max_workers: int = 8,
                  progress_cb: Callable[[int, int, str], None] | None = None,
                  id_pattern: str | None = None) -> dict[str, ParsedResume]
    # keyed by file_hash; identical bytes parsed once; ThreadPoolExecutor; progress_cb(done,total,file_name)
    # called from the calling thread (iterate as_completed in the caller).
```

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
* skills.py: `SKILL_TAXONOMY` canonical -> regex synonyms with **word-boundary safety**
  ("Java" must not match "JavaScript"; handle "C++", "C#", ".NET", "Node.js", "Next.js",
  "scikit-learn", "Power BI"). Hierarchy via `SKILL_IMPLIES` (AWS/Azure/GCP -> Cloud;
  MySQL/PostgreSQL/Oracle/SQL Server/SQLite -> SQL; Spring Boot -> Java; Django/Flask/FastAPI/
  Pandas/NumPy -> Python; PyTorch/TensorFlow/Keras/scikit-learn -> Machine Learning; React/
  Angular/Node.js -> JavaScript). Functions:
  ```python
  def extract_skills(text: str) -> list[str]                       # sorted canonical names
  def skill_evidence(sections: dict[str, str]) -> dict[str, list[str]]
  def canonicalize_skill(name: str) -> str                         # user input -> canonical (case-insensitive, synonyms)
  def match_required(required: list[str], found: list[str], text: str = "") -> tuple[list[str], list[str]]
      # (matched, missing) in the order of `required`. A required skill is matched if found
      # directly, implied by a found skill, or (for skills not in the taxonomy) appears in
      # `text` as a whole word (escaped regex, case-insensitive).
  ```

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
minus second-best >= 5) (score/100*0.9). Use dict/set lookups, not O(n^2) scans
(fuzzy only for the leftovers, via rapidfuzz.process.extractOne). If several resumes map to
one candidate keep the highest confidence (tie: more words), others -> MATCH_DUPLICATE.
Email in resume pointing at a different candidate than the file-name ID -> note
"identity conflict" + warning issue.

`base`: TA rows (all canonical + derived columns) plus `resume_file`, `resume_hash`,
`has_resume`, `match_method`, `match_confidence`. TA candidates without a resume ->
has_resume False, match_method "" + info issue (aggregate: one issue listing the count is
fine plus per-candidate rows). Resumes matching nobody -> warning issue (file_name).
**Resume-only mode** (ta is None): one candidate per resume, candidate_id = candidate_id_guess
or `RES-` + file_hash[:8].upper(), name/email/phone from resume, hackerrank_score NaN,
max_score NaN, hackerrank_pct NaN, match_method MATCH_RESUME_ONLY, data_flags "no_assessment".

## screening/scoring.py

```python
def score_candidates(base: pd.DataFrame, parsed_by_candidate: dict[str, ParsedResume],
                     cfg: AppConfig, llm_experience: dict[str, float] | None = None) -> pd.DataFrame
def apply_shortlist(df: pd.DataFrame, cfg: AppConfig) -> pd.DataFrame
def cutoff_band_ids(ranked: pd.DataFrame, cfg: AppConfig) -> list[str]
def explain_score(row: pd.Series | dict, cfg: AppConfig) -> list[str]   # human-readable breakdown lines
```

* Weights: `cfg.weights.normalized()`. Each component: sub-score 0..1, points = sub x weight.
* HackerRank: sub = hackerrank_pct / 100 (NaN -> 0 + note "no assessment score").
* Skills: found = parsed.skills; matched/missing via skills.match_required(cfg.skills.required_skills,
  found, parsed.text). per_skill: sub = min(1, n_matched / skills_for_full_marks);
  coverage: n_matched / len(required). Columns SKILLS_FOUND, MATCHED_SKILLS, MISSING_SKILLS,
  N_MATCHED_SKILLS.
* Experience (heuristic): weighted (normalise w_*) of internships min(1, count/internships_for_full),
  duration min(1, months/months_for_full), projects (count/projects_for_full, x0.7 + 0.3 x share
  of projects with quantified outcomes [numbers/%] or tech evidence), relevance (TF-IDF cosine of
  projects+experience+skills text vs job_description + required skills; scale by the pool's 95th
  percentile cosine (floor 0.1), clip 0..1; store in RELEVANCE). If llm_experience has the id:
  sub = (1 - blend_weight) x heuristic + blend_weight x llm, EXPERIENCE_SOURCE = EXP_HYBRID, else
  EXP_HEURISTIC; no resume -> EXP_NO_RESUME.
* Education: degree relevance (CS/IT/software/data/AI/computer -> 1.0; ECE/EEE/electrical/
  instrumentation/maths/statistics -> 0.7; other engineering -> 0.4; non-technical -> 0.2),
  CGPA ((cgpa - floor)/(ceiling - floor) clipped; TA cgpa preferred, else resume), certifications
  min(1, relevant_count/certs_for_full), tier (cfg.education.tier_scores by tier digit). Weights
  re-normalised over sub-components that have data (missing -> excluded, not zero).
* No resume: policy "zero" -> skills/experience sub = 0 (education uses TA data only);
  "renormalize" -> final = points from available components rescaled to 100. Note in SCORE_NOTES.
* FINAL_SCORE rounded to 2 dp. Also add INTERNSHIP_COUNT, EXPERIENCE_MONTHS, PROJECT_COUNT,
  CERTIFICATIONS, CERT_COUNT, RESUME_CGPA, RESUME_DEGREE, LLM_RATIONALE ("" unless llm).
* apply_shortlist: ELIGIBLE = has a HackerRank score (unless resumes_only mode, where all
  candidates have NaN scores - then eligible), hackerrank_pct >= min_hackerrank_pct, cgpa >= min_cgpa
  (NaN cgpa passes), grad year in allowed list (if set). RANK: eligible sorted by final_score desc,
  then hackerrank_pct desc, sub_skills desc, sub_experience desc, candidate_id asc (dense ints 1..n).
  Cutoff: top_n / ceil(top_pct% of eligible) / final_score >= threshold. In the band:
  risk_level in review_risk_levels -> STATUS_REVIEW (reason lists risk reasons), else
  STATUS_SHORTLISTED. If exclude_high_risk: High-risk in band -> STATUS_HOLD and the band is
  extended so the shortlist still has N clean+review candidates. Others: STATUS_NOT;
  ineligible: STATUS_INELIGIBLE with STATUS_REASON. Works if risk columns are missing
  (treat as Clear).
* cutoff_band_ids: eligible candidates whose RANK is within +/- cfg.llm.cutoff_band of the cutoff
  rank, with has_resume True (threshold method: by score distance -> nearest 2 x band).

## screening/risk.py

```python
def run_risk_analysis(candidates: pd.DataFrame, parsed_by_candidate: dict[str, ParsedResume],
                      cfg: AppConfig, parsed_all: dict[str, ParsedResume] | None = None) -> RiskResult
```

Focus group = hackerrank_pct >= cfg.risk.focus_threshold_pct (default 100 -> perfect scorers).
All signals degrade gracefully when columns are missing (skip + RiskResult.warnings).

1. **Commonality (lift)** for every attribute in `S.COMMONALITY_ATTRIBUTES` present: per value,
   focus_count/focus_total vs population_count/population_total, lift, one-sided Fisher exact
   p (scipy.stats.fisher_exact alternative="greater"), BH q-value
   (scipy.stats.false_discovery_control); significant = focus_count >= lift_min_count and
   lift >= lift_min and q <= lift_alpha. Columns: attribute, value, focus_count, focus_total,
   focus_share, population_count, population_total, population_share, lift, p_value, q_value,
   significant. Sorted significant first, then lift desc.
2. **Pair signals among the focus group** (vectorised / sorted-window, no Python O(n^2) over
   3,000 - focus group only, optional focus x all for text):
   * shared answer hashes per `q{n}_hash`, ignoring blank and "common" hashes held by >
     common_hash_max_share of the focus group (MCQ answer keys) - weight per hash, cap 2x;
   * shared GitHub repo (exact normalised repo URL, not profile-only);
   * resume text similarity: TF-IDF (word 1-2 grams, sublinear tf) on projects+experience
     sections (fallback full text) -> cosine >= high / >= medium thresholds;
   * shared rare bullets: normalised bullet with >= min_bullet_words words, held by
     <= max_bullet_population_share of all resumes (boilerplate filter), count + examples;
   * same IP (only if that IP is used by <= shared_ip_max_group candidates overall, OR the two
     candidates are from different colleges); same /24 subnet (weak);
   * sync_start / sync_submission within sync_window_sec; duration_match within
     duration_tolerance_min - **timing weights x0.25 when both candidates are On-campus at the
     same college** (scheduled sessions are synchronised by design);
   * sequential_same_ip: same IP and one session starts 0..proxy_gap_max_min after the other
     ends (proxy test-taker);
   * same phone / same email across different candidate_ids.
   pair_score = min(100, sum(weights)). Keep pairs with pair_score > 0 that have at least one
   non-timing signal or score >= edge_threshold.
   Pairs columns: cand_a, cand_b (cand_a < cand_b), pair_score, signals (" | " human-readable),
   signal_keys (", "), shared_hashes (", " e.g. "q2, q3"), resume_similarity (float/NaN),
   shared_bullets (int), shared_bullet_examples (" | ", max 3), shared_github (", "), same_ip,
   same_subnet, same_college, same_phone, same_email (bool), start_gap_sec, submit_gap_sec,
   duration_gap_min, sequential_gap_min (float/NaN).
3. **Individual flags** (all candidates): speed_anomaly (focus group members with completion
   < speed_anomaly_ratio x focus median), plagiarism_flag true, tab_switches >= threshold,
   copy_paste_events >= threshold. Columns: candidate_id, flag_key, flag, detail, weight.
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
   ("" if none), n_links, max_pair_score, individual_score.
6. `params` = thresholds used; `warnings` = skipped signals.

Risk flags are **review evidence, not verdicts**: wording must say "pattern consistent with ..."
not "cheated".

## screening/llm.py (OpenAI-compatible chat client; NVIDIA endpoint by default)

```python
def resolve_api_key(explicit: str | None = None, env_name: str | None = None) -> str | None
    # explicit > env var env_name (default LLMConfig().api_key_env, i.e. NVIDIA_API_KEY)
def redact_pii(text: str, names: list[str] | None = None) -> str  # emails, phones, URLs, names -> [REDACTED_*]
class LLMError(Exception)
class LLMClient:
    def __init__(self, cfg: LLMConfig, api_key: str | None, client: Any | None = None)  # client injectable for tests
    def test_connection(self) -> tuple[bool, str]
    def chat_json(self, system: str, user: str, *, max_tokens: int | None = None,
                  required_keys: tuple[str, ...] = ()) -> dict
        # retries w/ exponential backoff + jitter on 429/5xx/timeouts/connection errors and on
        # unparseable JSON (re-ask once with "Return ONLY valid JSON"); moves to the next
        # cfg.fallback_models entry on 404/410; disk cache keyed by
        # sha256(model|PROMPT_VERSION|system|user) under cfg.cache_dir; temperature from cfg.
def create_client(cfg: LLMConfig, api_key: str | None = None) -> LLMClient | None
def rescore_candidates(candidate_ids, candidates: pd.DataFrame, parsed_by_candidate, cfg: AppConfig,
                       client: LLMClient | None, progress_cb=None) -> dict[str, dict]
    # -> {id: {"ok": bool, "score": 0..1, "rationale": str, "highlights": [str], "source": "llm"|"cache", "error": str}}
def explain_candidates(candidate_ids, candidates, parsed_by_candidate, risk_result, cfg, client,
                       progress_cb=None) -> dict[str, dict]
    # -> {id: {"ok", "summary", "strengths": [..], "gaps": [..], "interview_questions": [..],
    #          "integrity_questions": [..] (only when risk Medium/High), "error"}}
def summarize_clusters(risk_result, candidates, cfg, client, progress_cb=None) -> dict[str, dict]
    # -> {cluster_id: {"ok", "headline", "summary", "evidence_highlights": [..], "recommended_actions": [..]}}
def executive_summary(result: PipelineResult, client) -> dict   # {"ok", "markdown", "error"}
```

* The chat client comes from `llm_backends.build_chat_client(cfg, api_key)`: for the default provider
  `openai.OpenAI(base_url=cfg.base_url, api_key=..., timeout=cfg.timeout_sec, max_retries=0)`, with
  `cfg.request_extras` added to every request; for provider "tachyon", `TachyonShim`. llm.py only
  calls `client.chat.completions.create(model=..., messages=..., temperature=..., max_tokens=...)`;
  `llm_backends.classify_error` / `describe_error` turn client exceptions into retry, model
  fallback or fatal, with key-free messages.
* Concurrency: ThreadPoolExecutor(cfg.max_concurrency); progress_cb(done, total, detail) from the
  calling thread; one failure never aborts the batch.
* Every prompt: redacted resume text (truncate to max_input_chars), no names/emails/phones,
  instruction to ignore protected attributes (gender, age, religion, caste, ethnicity, marital
  status, disability) and to return strict JSON. Experience score on 0-20 -> /20.
* Integrity wording: "pattern consistent with", recommend verification steps (live coding
  re-test, project walkthrough), never accuse.

## screening/export.py

```python
def to_csv_bytes(df: pd.DataFrame) -> bytes                      # utf-8-sig for Excel
def build_excel_report(result: PipelineResult, llm_notes: dict | None = None) -> bytes
    # sheets: Summary, Shortlist, All Candidates, Integrity Evidence (flagged candidates with
    # reasons + linked candidates), Risk Clusters, Risk Pairs, Commonality, Data Quality, Config
    # llm_notes (built by ui/tab_shortlist.export_llm_notes from the session; every key optional):
    #   "candidates": {id: {"summary", "strengths", "gaps"}} -> "AI Notes (LLM)" column on
    #                 Shortlist and Integrity Evidence
    #   "clusters": {cluster_id: {"headline", "summary", "recommended_actions"}} -> "AI Headline
    #               (LLM)", "AI Summary (LLM)", "AI Recommended Actions (LLM)" on Risk Clusters
    #   "executive_summary": {"ok", "markdown"} -> "AI executive summary (LLM-generated - verify
    #                        before acting)" block on Summary
def build_evidence_pack(result: PipelineResult, candidate_id: str) -> str   # markdown for recruiters
```

## Final `PipelineResult.candidates` columns (in this order where present)

rank, status, status_reason, candidate_id, name, email, phone, college, college_tier, degree,
branch, graduation_year, cgpa, city, final_score, pts_hackerrank, pts_skills, pts_experience,
pts_education, sub_hackerrank, sub_skills, sub_experience, sub_education, hackerrank_score,
max_score, hackerrank_pct, risk_level, risk_score, risk_reasons, cluster_id, is_focus, n_links,
max_pair_score, individual_score, matched_skills, missing_skills, n_matched_skills,
skills_found, internship_count, experience_months, project_count, certifications, cert_count,
resume_cgpa, resume_degree, relevance, experience_source, llm_rationale, score_notes,
test_start_time, submission_time, completion_minutes, ip_address, ip_subnet, test_mode,
tab_switches, copy_paste_events, plagiarism_flag, source, email_domain, test_slot, q{n}_hash...,
q{n}_score..., resume_file, resume_hash, has_resume, match_method, match_confidence, eligible,
college_capped, data_flags, extra_*.

## UI (app.py + ui/)

Brand palette: red `#D71E28` / `#BA0C2F`, gold `#FFCD00` / `#F1B521`, slate `#1A1A1A` /
`#333333`, background `#F9F9F9`. Header banner (red gradient + gold rule; eyebrow text from env
`APP_BRAND_EYEBROW`, else the built-in `BRAND_EYEBROW` default in theme.py), KPI cards
(Total Processed, Shortlisted, Risk Flags (High/Medium), Average Score, Perfect Scorers).
Tabs before the first run: Getting started; Config & Rubric Tuning; Run History. After a run, 8 tabs
in `state.TAB_LABELS` order: Candidate Shortlist & Leaderboard; Integrity & Risk Intelligence;
Individual Deep-Dive; Compare Candidates; Campus Distribution; Ask the Data; Run History;
Config & Rubric Tuning. The four feature tabs are imported lazily (`app.ENABLED_FEATURE_TABS`); one
that fails to import is hidden instead of breaking the app. Heavy work is cached per session in
`st.session_state` (`prepare_data` once per input fingerprint; `evaluate` memoised on inputs, config
and LLM results); the column-mapping preview uses `st.cache_data`. LLM actions are explicit buttons
with progress and graceful "no API key" state; API key resolved as env `cfg.llm.api_key_env`
(default `NVIDIA_API_KEY`), then `st.secrets` under the same name (guard missing secrets file),
shown only as "configured" (never the value).

## HackerRank for Work fields (addendum)

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
* `hackerrank_pct`: use the mapped percentage column when present; otherwise score/max.
  If both exist, keep score/max but note disagreements > 1 pt as an info issue.
* `out_of_window_seconds`: seconds; accepts 95, "95 s", "1m 35s", "00:01:35".
* `secure_mode_exits`: Int64. `max_code_similarity`, `q{n}_similarity`: float 0..100
  ("92%" -> 92; if a column's max <= 1 treat as fraction -> x100).
* `plagiarism_match`: other candidate's ID or email as given; derive `plagiarism_match_id`
  = resolved candidate_id (via ID or email lookup) or NaN.
* `plagiarism_flag`: True for suspicious/flagged/yes/true/1; False for "not suspicious",
  clear, no, false, 0, blank.
* `integrity_status`: Title Case str; `proctoring_flags`: str; `test_name`, `report_url`: str.

Risk usage (in addition to the signals above):
* Individual flags: `high_code_similarity` (max_code_similarity or max q{n}_similarity >=
  cfg.risk.code_similarity_threshold), `integrity_status` in {review, suspicious, flagged},
  `proctoring_flags` non-empty, `out_of_window_time` (>= out_of_window_sec_threshold),
  `secure_mode_exits` (>= secure_mode_exit_threshold), `tab_switches` (out_of_window_events),
  `copy_paste` (editor_paste_count), `plagiarism_flag`, `speed_anomaly`. Weights in
  cfg.risk.individual_weights.
* Pair signal `plagiarism_match`: A.plagiarism_match_id == B.candidate_id (either
  direction) -> weight cfg.risk.pair_weights["plagiarism_match"]; included when at least one
  side is in the focus group (the matched "source" may have scored < 100%).

## Shortlist controls, comparison, ask-the-data, run history and deployment

Wiring: `ui/state.py` has `TAB_COMPARE`, `TAB_CAMPUS`, `TAB_ASK`, `TAB_HISTORY`,
`K_COMPARE_IDS`, `K_ASK_HISTORY`, `K_LOADED_RUN`; `app.py` renders `ui/tab_compare.py`,
`ui/tab_campus.py`, `ui/tab_ask.py`, `ui/tab_history.py` (each `render(result: PipelineResult | None)`;
`tab_history.render(None)` also runs before the first screening). `ShortlistConfig.college_cap_pct`
(default 0 = off) and `S.COLLEGE_CAPPED` exist.

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
don't count) is not shortlisted: STATUS_NOT, COLLEGE_CAPPED True, reason "College cap reached: <college>
already has <n> shortlisted (cap <pct>%)"; the next-best candidates fill the slots (top_n / top_pct).
Threshold method: capped candidates are excluded, no backfill below the threshold. Unknown college is
never capped. cap 0 -> no cap at all (COLLEGE_CAPPED False everywhere).

### What-if preview (ui/tab_config.py)
The form has a second submit button "Preview impact": evaluate(prepared, candidate_cfg) and show
analytics.shortlist_diff(current, preview) (summary metrics, entering, leaving, status changes, rank moves)
with "Apply these changes" / "Discard preview". Disabled before the first run.

### Compare (ui/tab_compare.py) - 2-4 candidates side by side; "Add to compare" button in ui/tab_deepdive.py.
### Ask the Data (screening/ask.py + ui/tab_ask.py)
LLM converts a question into a JSON query plan (filters / group_by / metrics / columns / sort / limit /
chart); the plan is validated against a whitelist and executed locally with pandas (no eval/exec, no
DataFrame.query strings). The LLM sees only column names, types, descriptions and value examples of
low-cardinality NON-PII columns - never names, emails, phones, IPs, resume text or row data.
### Run history (screening/runs.py + ui/tab_history.py)
RunStore(base_dir = env SCREENING_RUNS_DIR or <project>/runs): save / list_runs / load / delete / rename /
compare_runs. JSON + Parquet + JSON.gz only (never pickle); run_id validated (no path traversal); atomic
writes. Loading restores PreparedData + PipelineResult + config + LLM stores so the run can be re-tuned.
### Deployment (docs/DEPLOYMENT.md + scripts/setup.ps1|.sh, scripts/run_app.ps1|.sh) for a company VM.
Listens on 127.0.0.1 by default (`[server] address` in .streamlit/config.toml and the run scripts'
default); `-Address 0.0.0.0` / `--address 0.0.0.0` (or `STREAMLIT_SERVER_ADDRESS` without the scripts)
exposes it on the intranet. The synthetic sample is generated on each machine (data/ is gitignored).
