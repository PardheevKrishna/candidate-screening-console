# Candidate Screening & Integrity Console

A Streamlit app for campus hiring that:

1. **Fuses** the TA / HackerRank assessment export (CSV or Excel) with bulk resumes
   (PDF, DOCX, TXT or a ZIP of them).
2. **Scores** every candidate with a weighted rubric (default: HackerRank 40 ·
   Skills 30 · Experience & projects 20 · Education & certifications 10) and produces a
   sorted shortlist (default: top 300).
3. **Flags integrity-risk patterns** among candidates who scored 100%: shared or identical
   solutions, proxy test-takers, synchronized sessions, leaked-paper speed runs, plagiarised
   resumes. For each pattern it shows **what the perfect scorers have in common** and the
   evidence for recruiter review.
4. Uses an **LLM** (NVIDIA NIM today; switchable to the internal TachyonClient, see
   [docs/SWITCH_TO_TACHYON.md](docs/SWITCH_TO_TACHYON.md)) for rationales, interview and
   integrity questions, risk-group summaries, an executive summary, and a 50/50 re-score of
   candidates near the cutoff.

> Risk flags are **evidence for human review, not verdicts.** By default,
> flagged candidates stay in the shortlist with a red "Integrity Review" tag and an evidence
> pack, and the recruiter decides.

---

## Quick start (Windows / PowerShell)

```powershell
# 1. Create a virtual environment. Keep it outside cloud-synced folders (OneDrive etc.); syncing a venv breaks pip.
py -3.11 -m venv $env:USERPROFILE\.venvs\hiring-screening
& $env:USERPROFILE\.venvs\hiring-screening\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Generate the synthetic dataset (3,000 candidates + ~2,900 resumes, ~2-4 min)
python generate_mock_data.py            # --quick for 300 candidates

# 3. LLM key (optional - everything except LLM features works offline)
copy .streamlit\secrets.toml.example .streamlit\secrets.toml   # then paste your key
#    or: $env:NVIDIA_API_KEY = "nvapi-..."

# 4. Run
streamlit run app.py
```

macOS/Linux: `python3.11 -m venv ~/.venvs/hiring-screening && source ~/.venvs/hiring-screening/bin/activate`, then the same steps.

Tests: `python -m pytest` (~690 tests, ~40 s)  ·  End-to-end + ground-truth check: `python scripts/run_e2e.py`

**Run again later** (environment already created):

```powershell
& $env:USERPROFILE\.venvs\hiring-screening\Scripts\python.exe -m streamlit run app.py
```

Then click **Load synthetic sample & run** (about 30–40 s for 3,000 candidates and 2,892 resumes).

### Validation on the synthetic dataset

`python scripts/run_e2e.py` (3,000 candidates, 2,892 resumes, seed 42):

| Check | Result |
|---|---|
| Processing time | parse + match about 21 s, score + risk + shortlist 0.7 s |
| Shortlist | 300: 267 Shortlisted and 33 Shortlisted (Integrity Review); 32 ineligible (expired or no score) |
| Planted integrity patterns (6 types, 42 candidates) | **100% flagged**, each recovered as its own cluster |
| Decoys (lab college with 15 legit 100% scorers, boilerplate resumes, MCQ answer key) | **0% High** (the 6% at Medium are caused by HackerRank's own per-candidate flags) |
| Other perfect scorers flagged High | 0% |
| Resume linkage | 1,437 by email, 1,422 by ID in the file name, 3 by phone or name. All 25 walk-in resumes are correctly left unmatched |

---

## Using the app

**Sidebar → Data source**
* *Synthetic sample dataset*: loads `data/sample/` (created by `generate_mock_data.py`).
* *Upload*: the TA feed (CSV/XLSX) plus resumes (multi-select PDF/DOCX/TXT, or one ZIP).
  Headers are auto-mapped (for example "Email ID", "Percentage Score", "Out of Window Events",
  "Q3 Score (Coding - DP)"). Open **Review column mapping** to fix any mapping before you run.
* Resumes are linked to candidates by: the file name listed in the TA feed, then the candidate
  ID in the file name, then email, then the ID in the text, then phone, then a fuzzy name match.
  Unmatched and duplicate files show up under data quality.

**Tabs**

| Tab | What you get |
|---|---|
| Candidate Shortlist & Leaderboard | Ranked table with filters (status, risk, college, skills, search), score histogram with the cutoff line, CSV/Excel export, LLM re-score near the cutoff, LLM rationales for the shortlist |
| Integrity & Risk Intelligence | What 100% scorers have in common (lift + significance), risk network graph, cluster cards with shared traits and evidence, timeline of synchronized sessions, individual anomalies, LLM risk-group summaries and executive summary |
| Individual Deep-Dive | Profile, radar chart of the four sub-scores, points breakdown, parsed resume (projects, internships, education, certifications), risk evidence and linked candidates, LLM explanation with interview and integrity questions, downloadable evidence pack |
| Compare Candidates | 2–4 candidates side by side: scores, skills ✓/✗ matrix, experience, education, risk evidence and an overlaid radar. Use "Add to compare" on Deep-Dive to queue candidates |
| Campus Distribution | Applicants, perfect scorers, shortlisted, shortlist rate and shortlist share vs applicant share by college, tier, city, degree or branch. Shows which colleges are over the college cap |
| Ask the Data | Plain-English questions ("average final score by college tier"). The LLM turns each question into a structured query that is validated and run locally. It sees only column descriptions, never candidate data |
| Run History | Save a run (settings + results + LLM outputs), reopen it later without re-parsing, rename or delete it, and compare two runs (who entered or left the shortlist, which settings changed). Also available before the first screening |
| Config & Rubric Tuning | Weights, required skills, job description, experience and education sub-weights, shortlist rule, **college cap**, eligibility filters, integrity policy and thresholds, LLM model, config import/export, and **Preview impact** (who enters or leaves the shortlist before you apply). Available before the first run |

---

## Scoring model

| Component | Default weight | How it is computed |
|---|---|---|
| HackerRank | 40 | `score / max_score × 40` (works for mixed test versions, e.g. /100 and /150) |
| Skills match | 30 | Required skills (default Python, Java, SQL, Cloud, PyTorch) matched by a skills taxonomy with synonyms and implications (AWS ⇒ Cloud, PostgreSQL ⇒ SQL). Default rule: 10 pts per skill, capped at 30. Proportional coverage is also available. |
| Experience & projects | 20 | Internships, months of experience, project depth, and relevance to the job description (TF-IDF). For candidates near the cutoff, the LLM re-scores this component and the result is blended 50/50 with the rule-based score. |
| Education & certifications | 10 | Degree relevance, CGPA, relevant certifications, institution tier (small sub-weight; set it to 0 to ignore tier) |

**Statuses:** `Shortlisted`, `Shortlisted (Integrity Review)` (in the top N with a High or Medium
flag), `Integrity Hold` (only when "hold & backfill" is enabled), `Not Shortlisted`, and `Ineligible`
(no assessment score, or knocked out by the eligibility filters). Ties break deterministically on
HackerRank %, then skills, then experience, then ID.

---

## Integrity & risk engine

The **focus group** is candidates with HackerRank % ≥ 100 (adjustable in Config).

| Signal | Source | Notes |
|---|---|---|
| Identical answers | `Q{n} Code/Answer Hash` | Hashes shared by >10% of perfect scorers (for example the MCQ answer key) are ignored |
| HackerRank plagiarism | `Plagiarism Status`, `Max Code Similarity %`, `Q{n} Code Similarity %`, `Most Similar Candidate` | A named "most similar candidate" becomes a direct link |
| Proctoring | `Out of Window Events/Duration`, `Editor Paste Count`, `Secure Mode Exit Count`, `Integrity Status`, `Proctoring Flags` | Individual flags |
| Network | `IP Address`, /24 subnet | A lab or NAT IP shared by many candidates is down-weighted. Back-to-back sessions on one IP point to a proxy test-taker |
| Timing | `Attempt Start/End Time`, `Time Taken` | Synchronized starts and submissions, matching durations, implausibly fast perfect scores. Timing is down-weighted for on-campus sessions at the same college, which are scheduled together |
| Resume | parsed resumes | TF-IDF similarity of project and experience text, rare shared bullet lines (boilerplate filtered out), shared GitHub repos |
| Identity | phone / email | One phone number or email used across several candidate IDs |

**Outputs:** a *commonality* table (attribute values over-represented among 100% scorers,
with Fisher exact test and BH correction), risk *pairs*, *clusters* (connected groups with
shared traits), and a per-candidate risk score and level (High ≥ 60, Medium ≥ 30) with
plain-English reasons that name the evidence and the linked candidates.

---

## Synthetic dataset

`generate_mock_data.py` writes `data/sample/`:

* `ta_feed.csv`: a HackerRank for Work-style export (attempt times, score, max score,
  percentage, per-question scores, code similarity, plagiarism status, out-of-window events
  and duration, editor paste count, secure-mode exits, IP, AI-proctoring integrity status)
  plus the candidate-details columns a campus drive collects (college, degree, branch, CGPA,
  year of passing, city) and TA fields (tier, test mode, source).
* `resumes/` + `resumes.zip`: PDF, DOCX and TXT resumes in three layouts with skills,
  projects, internships, education and certifications, using realistic file-name variety.
* `job_description.txt`, `README.md`.
* `_ground_truth/`: planted integrity patterns, decoys, and injected data-quality issues, used by
  `scripts/run_e2e.py` and the tests. The app never reads it.

All people, colleges and companies are fictional. Emails use reserved `example.*` domains.

---

## LLM integration

| | |
|---|---|
| Default provider | NVIDIA NIM (`https://integrate.api.nvidia.com/v1`), model `nvidia/nemotron-3-ultra-550b-a55b`, with its hidden "thinking" switched off (`request_extras`). Measured 2026-11-05: re-score about 2 s per candidate, explanation about 20 s, risk-group summary about 7 s |
| Fallbacks | `nvidia/nemotron-3.5-bolt-e4b`, `meta/llama-3.3-70b-instruct`, `google/gemma-4-31b-it`. `meta/llama-3.3-70b-instruct` (the earlier default) now returns **HTTP 410 (retired)** on NVIDIA's API. On 404/410 the client moves to the next model automatically |
| What the LLM does | (1) rationale for each shortlisted candidate; (2) any candidate on click; (3) plain-English summary of each risk group with verification steps; (4) executive summary of the run; (5) interview questions that probe each candidate's gaps; (6) integrity questions for Medium/High-risk candidates; (7) re-scores Experience & Projects only for the ±50 ranks around the cutoff, blended 50/50 with the rule-based score |
| Switch to the internal TachyonClient | Follow [docs/SWITCH_TO_TACHYON.md](docs/SWITCH_TO_TACHYON.md). It is a file-by-file checklist, and when you finish there are no NVIDIA references left |
| Key lookup | Environment variable first, then `st.secrets`. The UI only ever shows "configured" |
| Privacy | Emails, phone numbers, URLs and candidate names are redacted before any text leaves the machine. Prompts tell the model to ignore protected attributes |
| Cost/time control | Only explicit button actions call the LLM. Re-scoring is limited to the ±50 ranks around the cutoff. Responses are cached on disk (`.cache/llm/`, gitignored) |

---

## Security, privacy & fairness

* **Secrets:** `.streamlit/secrets.toml` and `.env*` are gitignored. Never commit keys, and rotate
  any key that may have been exposed.
* **Candidate PII:** everything under `data/` is gitignored, except the README. Process real TA data
  only on approved company infrastructure, and only with LLM providers your data policy allows.
* **Exports** neutralise spreadsheet formula injection (cells starting with `= + - @`).
* **Telemetry:** Streamlit usage stats are disabled in `.streamlit/config.toml`.
* **Fairness:** the rubric uses only job-relevant evidence. Protected attributes are never used. You can
  set institution tier to 0. Integrity flags require human review before any decision.

---

## Deploying on a company VM

For a shared installation on an internal Windows Server or Linux VM, follow
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). It covers installs through a package mirror or fully
offline, proxy and certificate settings, running as a service (NSSM, Task Scheduler, systemd), a
reverse proxy with SSO and TLS, the health check, retention, backups and upgrades. The short version:

```powershell
# Windows: virtual environment + dependencies + tests, then start (reachable from the intranet)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -VenvPath C:\apps\venvs\hiring-screening
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_app.ps1 -VenvPath C:\apps\venvs\hiring-screening -Address 0.0.0.0 -PublicHost <vm-host-name>
```

```bash
# Linux
bash scripts/setup.sh --venv /opt/venvs/hiring-screening
bash scripts/run_app.sh --venv /opt/venvs/hiring-screening --address 0.0.0.0 --public-host <vm-host-name>
```

The app has no login of its own: keep it on the intranet or VPN, and put it behind the company
reverse proxy (SSO and TLS) when several recruiters share it.

---

## Project layout

```
app.py                 Streamlit entry point (tabs, header, KPIs)
ui/                    Streamlit components: theme, sidebar, state, charts and one module per tab
                       (shortlist, risk, deep-dive, compare, campus, ask, history, config)
screening/             Engine (no Streamlit imports): schema, config, models, ingest, resume_parser,
                       skills, matching, scoring, risk, analytics, ask, runs, llm, llm_backends,
                       export, pipeline
generate_mock_data.py  Synthetic HackerRank/TA export + resumes + ground truth
scripts/               run_e2e.py (headless check), setup.ps1|sh and run_app.ps1|sh (VM setup/run)
tests/                 pytest suite
docs/                  ARCHITECTURE, USER_GUIDE, DEPLOYMENT, SWITCH_TO_TACHYON, HACKERRANK_DATA_REFERENCE
```
