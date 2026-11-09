# Candidate Screening & Integrity Console

A Streamlit app for campus hiring that:

1. **Fuses** the assessment export with bulk resumes: the Workday assessment export (`.xlsx`) with
   its `Resume.zip` of multi-candidate PDF packets (split per candidate automatically), or a
   HackerRank-style CSV/XLSX with individual PDF, DOCX or TXT resumes (or a ZIP of them).
2. **Scores** every application with a weighted rubric (default: HackerRank 40 · Skills 30 ·
   Experience & projects 20 · Education & certifications 10; preset for drives without resumes:
   80 / 0 / 0 / 20), applies eligibility rules that record a reason for every knock-out, sets
   duplicate applications aside, breaks ties in a configurable order and produces a ranked
   shortlist (default: top 300).
3. **Flags integrity-risk patterns** among candidates who scored 100%: identical answers, proxy
   test-takers, synchronized sessions, leaked-paper speed runs, resume mills and plagiarised resumes,
   plus hidden or adversarial resume text aimed at AI screeners (prompt injection). For each pattern
   it shows **what the perfect scorers have in common** and the evidence for recruiter review.
   Teammates who list the same GitHub repo on otherwise different resumes are labelled
   "Team project (likely legitimate)" instead of being flagged.
4. **Keeps the sources apart**: verified assessment results (HackerRank), self-reported resume
   content, the academic profile and integrity findings are labelled separately on screen and in
   every export.
5. Optionally uses **GitHub Copilot**, through a small VS Code extension on the same computer
   ([docs/COPILOT_PROXY.md](docs/COPILOT_PROXY.md)), for rationales only: shortlist rationales,
   borderline gap analyses, the Deep-Dive "explain this candidate", risk-group (cluster) summaries
   and the batch executive summary.

> Risk flags are **evidence for human review, not verdicts.** By default, flagged candidates stay
> in the shortlist with a red "Integrity Review" tag and an evidence pack, and the recruiter decides.
> The LLM **only writes text**: it never changes scores, ranks, cutoffs or the shortlist.

---

## Quick start

Requires 64-bit **Python 3.11, 3.12, 3.13 or 3.14**. Create the virtual environment outside
cloud-synced folders (OneDrive and similar): syncing a virtual environment breaks pip.

**Windows (PowerShell)**

```powershell
# 1. Virtual environment and dependencies (py -3.11, -3.12, -3.13 or -3.14)
py -3.11 -m venv $env:USERPROFILE\.venvs\hiring-screening
& $env:USERPROFILE\.venvs\hiring-screening\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Synthetic Workday sample in data\sample\ (2,133 applications, 33 resume packets, about 5 s).
#    It is not in the repository; it is created locally.
python generate_mock_data.py            # --quick for 300 applications

# 3. Run (listens on 127.0.0.1 only; open http://127.0.0.1:8501)
streamlit run app.py
```

**Linux / macOS**

```bash
python3.11 -m venv ~/.venvs/hiring-screening      # or python3.12, python3.13, python3.14
source ~/.venvs/hiring-screening/bin/activate
pip install -r requirements.txt
python generate_mock_data.py
streamlit run app.py
```

Instead of step 1 you can run the setup script, which finds a Python 3.11-3.14 interpreter, creates
the virtual environment (default `~/.venvs/hiring-screening`), installs `requirements.txt` and runs
the tests: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1` (Windows) or
`bash scripts/setup.sh` (Linux). `requirements.txt` lists compatible version ranges, not exact pins.

Then click **Load synthetic sample & run** (or, in the sidebar, **Synthetic sample dataset** >
**Run screening**). The first run parses the 1,877 resumes in about 40 s. The parsed dataset is
cached, so **Recent datasets (fast reload)** in the sidebar loads it again in about a second.

**Run again later** (environment already created):

```powershell
& $env:USERPROFILE\.venvs\hiring-screening\Scripts\python.exe -m streamlit run app.py
```

**Tests:** `python -m pytest` (1,114 tests, about 1.5 minutes; passing on Python 3.11 and 3.14) ·
VS Code extension: `cd vscode-copilot-proxy` then `node --test "test/*.test.js"` (54 tests) ·
end-to-end + ground-truth check: `python scripts/run_e2e.py --strict` (below).

**Who can reach it.** The app listens on `127.0.0.1` by default (`address` in the `[server]` section of
`.streamlit/config.toml`), so only this machine can open it. To make it reachable from the intranet, start
it with the run script and `-Address 0.0.0.0` (Windows) or `--address 0.0.0.0` (Linux), as shown in
[Deploying on a company VM](#deploying-on-a-company-vm), or set the environment variable
`STREAMLIT_SERVER_ADDRESS=0.0.0.0` before `streamlit run app.py`. The app has no login of its own, so
restrict the port with the firewall first ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), section 8).

### Optional: GitHub Copilot for the LLM features (3 steps)

Everything except the LLM-written text works without this.

1. On the computer that runs the app, open **VS Code 1.95 or later**, signed in to **GitHub
   Copilot** (the account needs a Copilot licence).
2. Command Palette (`Ctrl+Shift+P`) > **Developer: Install Extension from Location...** > select the
   `vscode-copilot-proxy` folder of this project. The status bar shows **Copilot proxy: on**.
3. Run **Screening Proxy: Show Status** > **Test Copilot Access** and choose **Allow** when VS Code
   asks whether *Screening Copilot Proxy* may use the GitHub Copilot models.

There is no key or token to configure: the app finds the proxy through a connection file the
extension writes, and the sidebar badge **GitHub Copilot (VS Code proxy)** turns green. Settings,
security and troubleshooting: [docs/COPILOT_PROXY.md](docs/COPILOT_PROXY.md).

### Validation on the synthetic dataset

`python scripts/run_e2e.py --strict` on `data/sample` (2,133 applications, 1,877 resumes in
33 packets, seed 42; rules only, no LLM). It scores the run against the planted ground truth and
exits with an error if a check fails:

| Check | Result |
|---|---|
| Processing time | reading, packet splitting, parsing, matching and the duplicate check about 37 s (parsing 36.7 s); scoring + risk + shortlist 0.7 s |
| Resume linkage | 1,877 of 1,877 split resumes linked by the candidate ID in the file name; the 256 applications without a resume (12.0%) are exactly the planted ones |
| Shortlist | 300: 299 Shortlisted and 1 Shortlisted (Integrity Review); 1,825 Not Shortlisted; 8 Ineligible (7 Duplicate Application, 1 Assessment: the row without a score) |
| Duplicate applications (7 planted pairs) | **7 of 7 found, no false pairs**; the institutional-e-mail application kept in 7 of 7; 0 of 4 namesake pairs merged |
| Resume mills (2 groups, 11 candidates) | **11 of 11 High**, each group recovered as its own cluster (C01, C02) |
| Team projects (13 pairs) | **0 flagged High or Medium**, no pair linked into a cluster (all Low) |
| Code-similarity rows ("Integrity Issues: Medium") | 2 of 2 Medium |
| Prompt-injection resumes (hidden text, invisible characters, instructions) | **5 of 5 flagged** (Medium); hidden keywords counted as skills: 0 |
| Boilerplate-bullet resumes (decoy, 145 candidates) | 0 flagged High or Medium |
| Overall | **13 of 13 ground-truth checks pass**. Risk levels: 11 High, 7 Medium, 38 Low, 2,077 Clear; 1,360 perfect scorers; 44 risk pairs in 2 clusters |

---

## Using the app

**Sidebar**

* **Data source**
  * *Synthetic sample dataset*: **Run screening** loads `data/sample/` (created locally by
    `python generate_mock_data.py`; the button is unavailable until the sample exists).
  * *Upload TA feed + resumes*: **TA export: Workday or HackerRank (CSV or Excel)** and
    **Resumes: Resume.zip, PDF, DOCX or TXT** (several files at once). A TA export alone, resumes
    alone, or both can be screened.
* **Review column mapping** (after a TA export is uploaded): one row per column of the file, with
  **Source column**, **Sample values** (up to 3), **Maps to** (the field it fills) and **Action**
  (*Include* or *Ignore*). Workday headers ("Job Application", "Percentage Score", "Cutoff Score
  Percentage") and HackerRank headers ("Email ID", "Out of Window Events", "Q3 Score (Coding - DP)")
  are mapped automatically, and for a workbook the caption names the sheet that was used.
  **Download mapping (JSON)** saves the mapping as a profile; **Load mapping profile** applies it to
  the next export.
* **Recent datasets (fast reload)**: every parsed dataset is cached on the server. **Load** screens it
  again in about a second without re-parsing; the delete icon next to it removes it. Cached datasets
  are deleted after 7 days (adjustable from 1 to 30 days in Config).
* **Clear results**, the LLM status badge **GitHub Copilot (VS Code proxy)** with **Check again**,
  and **Last run**: rows in the feed, candidates, duplicate rows dropped, error / warning / info
  counts, **Data quality details** and **Timings**.

**Workday export.** Sheet1 (the recruiter summary) is skipped and the data sheet is found
automatically. Names come from the Job Application column ("First Last (first last)"). The composite
Assessment Comment ("Hackerrank Completed - Qualified", "Integrity Issues: ...", "Integrity Summary:
...") becomes the test status, the assessment integrity status (Medium and High count as concerns)
and the proctoring flags. `#N/A` counts as missing. Each PDF packet in `Resume.zip` (a table of
contents plus one bookmark per attachment, up to 250 MB) is split into one file per candidate,
`<candidate id>_<Name>.pdf`, before the per-resume limits apply (10 MB and 50 pages per resume).
Every column is described in [docs/HACKERRANK_DATA_REFERENCE.md](docs/HACKERRANK_DATA_REFERENCE.md)
(Part 1).

Resumes are linked to candidates by: the file name listed in the TA feed, then the candidate ID in
the file name, then e-mail, then the ID in the resume text, then phone, then a fuzzy name match.
Unmatched files, extra attachments and duplicates show up under data quality.

**Tabs**

| Tab | What you get |
|---|---|
| Candidate Shortlist & Leaderboard | Ranked table with filters (status, risk level, college, min final score, required skills, search by name, ID or e-mail). Headers name the source ("HackerRank: Score", "Resume: Skills Pts", "Academic: CGPA", "Integrity: Risk Level"); the tie-break column explains every tie decided by the tie-break order. **Ineligible candidates** lists each failed eligibility rule with the candidate's value, the threshold and an explanation. Score histogram with the cutoff line, CSV and full Excel export, **LLM: write rationales for shortlist**, **LLM: explain borderline** |
| Integrity & Risk Intelligence | Metrics (perfect scorers, High and Medium risk, linked clusters, significant traits, hidden-text flags), what 100% scorers have in common (lift + significance), risk network (team projects hidden unless switched on), cluster cards with shared traits and evidence, submission timeline, **Adversarial resume text** (snippets of hidden or instruction-like text), individual anomalies, all risk pairs with their pair type, CSV downloads, **LLM: summarize risk groups**, **LLM: executive summary** |
| Individual Deep-Dive | Red banner listing failed eligibility rules; badges (hidden text, duplicate application, no resume, perfect score, cluster); links between duplicate applications; radar of the four sub-scores, rubric points and the tie-break note; evidence grouped as **A. Assessment (HackerRank)**, **B. Resume (self-reported)**, **C. Academic** and **D. Integrity** (risk reasons, hidden-text snippets, linked candidates); **LLM: explain this candidate** with interview and integrity questions; **Evidence pack (.md)**; **Add to compare** |
| Compare Candidates | 2–4 candidates side by side: metrics grouped by source with the best value highlighted, required-skills matrix, overlaid radar, resume highlights and integrity notes, CSV export. Use "Add to compare" on Deep-Dive to queue candidates |
| Campus Distribution | Applicants, perfect scorers, shortlisted, shortlist rate and shortlist share vs applicant share by college, tier, city, degree or branch. Shows which colleges are over the college cap |
| Run History | Save a run (settings, results and every LLM output), reopen it later without re-parsing, rename or delete it, and compare two runs (who entered or left the shortlist, which settings changed). Also available before the first screening |
| Config & Rubric Tuning | Rubric presets and weights, missing-resume policy, required skills, job description, experience and education sub-weights, college tier scoring (off by default) with the **College tier list** upload and preview, shortlist rule, eligibility filters, **college cap**, **tie-break order**, duplicate handling, **Exclude / disqualify candidate IDs**, integrity policy, thresholds and the team-project rule, LLM settings with **Test connection**, the fast-reload cache, config import/export, and **Preview impact** (who enters or leaves the shortlist before you apply). Available before the first run |

Every tab and field is described in [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

---

## Scoring model

| Component | Default weight | How it is computed |
|---|---|---|
| HackerRank | 40 | `score / max_score × 40` (works for mixed test versions, e.g. /100 and /175) |
| Skills match | 30 | Required skills (default Python, Java, SQL, Cloud, PyTorch) matched by a skills taxonomy with synonyms and implications (AWS ⇒ Cloud, PostgreSQL ⇒ SQL). Default rule: 10 pts per skill, full marks at 3 skills. Proportional coverage is also available. Hidden resume text never counts. |
| Experience & projects | 20 | Internships, months of experience, project depth and relevance to the job description (TF-IDF). Rules only. |
| Education & certifications | 10 | Degree relevance, CGPA (from the TA feed, else from the resume) and relevant certifications; college tier only when tier scoring is switched on |

* **Presets:** **With Resumes (40/30/20/10)** and **No Resumes (80/0/0/20)** fill in the four
  weights; **Apply & re-score** uses them. Weights are always rescaled to 100.
* **Candidates without a resume:** *Auto (recommended)* rescales HackerRank + Education to 100 when
  the dataset has no resumes at all, and otherwise scores a missing resume as 0 for Skills and
  Experience (no unfair boost for a missing CV). *Score Skills & Experience as 0* and *Rescale
  HackerRank + Education to 100* force one behaviour.
* **College tier** is **off by default**: tiers can disadvantage strong candidates from less-known
  colleges. When switched on, the tier given in the TA feed is used, else the approved tier list
  uploaded in Config (CSV or Excel with the columns `college`, `tier` and optional `aliases`
  separated by `;`; at most 2 MB and 5,000 rows). Names are matched ignoring case, accents and
  punctuation, acronyms and close spellings included (fuzzy match threshold, default 90). A preview
  shows how the dataset's colleges resolve; unmatched colleges get no tier points and are not
  penalised.

**Eligibility.** A candidate who fails any rule is **Ineligible**: not ranked and not shortlisted.
Every failed rule is recorded with the candidate's value, the threshold and an explanation, and is
shown in the Shortlist's **Ineligible candidates** section, as a red banner on Deep-Dive, on the
Excel sheet "Ineligible Candidates" (one row per rule) and as counts on the Excel Summary.

| Rule | When it applies |
|---|---|
| Assessment | No assessment score recorded (for example `#N/A` in the Workday export). Skipped for resume-only runs |
| Score Threshold | HackerRank percentage below **Min HackerRank %** (0 = off) |
| Academic CGPA | CGPA (TA feed, else resume) below **Min CGPA** (0 = off). A missing CGPA never knocks anyone out |
| Graduation Year | Graduation year not in **Allowed graduation years** (empty = any year). A missing year never knocks anyone out |
| Administrative Exclusion | Candidate ID listed in **Exclude / disqualify candidate IDs** (comma, space or newline separated, case ignored) |
| Duplicate Application | Another application of the same person was kept, and **Auto-disqualify duplicate applications** is on (default) |

**Duplicate applications.** Two or more applications with different candidate IDs belong to one
person when the normalised name and the college match and the resumes are identical or nearly so
(text similarity of at least 0.95; without resumes: the same CGPA and branch). One application is
kept: the one with an assessment score, then the one with an institutional (non-webmail) e-mail,
then the lowest candidate ID. The others become Ineligible ("Disqualified: Duplicate application of
C-…"), and the kept application's status reason names the merged IDs. Duplicates are never treated
as collusion pairs, and a group of more than 10 applications is reported for review instead of
being marked.

**Tie-breaks.** Candidates with the same final score are ordered by **Tie-break order (after the
final score)**: by default HackerRank percentage, then Skills sub-score, then Experience sub-score.
HackerRank coding-question marks, CGPA (feed, then resume) and earliest assessment submission are
also available; the candidate ID is always the final tie-break. The outcome is written next to the
candidate, for example "tied at 91.20 pts; won tie-break on HackerRank percentage 100% vs 95%".

**Statuses:** `Shortlisted`, `Shortlisted (Integrity Review)` (on the shortlist with a risk level
chosen under "Risk levels that mark a shortlisted candidate for integrity review", default High and
Medium), `Integrity Hold` (only when "Hold & backfill" is selected), `Not Shortlisted` and
`Ineligible`. The shortlist rule is Top N (default 300), Top % of eligible or a score threshold;
the optional **college cap** limits any one college's share of the shortlist.

---

## Integrity & risk engine

The **focus group** is candidates with HackerRank % ≥ 100 (adjustable in Config).

| Signal | Source | Notes |
|---|---|---|
| Identical answers | `Q{n} Code/Answer Hash` | Hashes shared by >10% of perfect scorers (for example the MCQ answer key) are ignored |
| Plagiarism and code similarity | HackerRank `Plagiarism Status`, `Max Code Similarity %`, `Q{n} Code Similarity %`, `Most Similar Candidate`; Workday Assessment Comment ("Integrity Issues: Medium / High", "Integrity Summary: Detected Code Similarity ...") | A named "most similar candidate" becomes a direct link; Medium and High from the Workday comment count as assessment integrity concerns |
| Proctoring | `Out of Window Events/Duration`, `Editor Paste Count`, `Secure Mode Exit Count`, `Integrity Status`, `Proctoring Flags` | Individual flags |
| Network | `IP Address`, /24 subnet | A lab or NAT IP shared by many candidates is down-weighted. Back-to-back sessions on one IP point to a proxy test-taker |
| Timing | `Attempt Start/End Time`, `Time Taken` | Synchronized starts and submissions, matching durations, implausibly fast perfect scores. Timing is down-weighted for on-campus sessions at the same college, which are scheduled together |
| Resume | parsed resumes | TF-IDF similarity of project and experience text, rare shared bullet lines (boilerplate filtered out), shared GitHub repos |
| Team projects | parsed resumes | Two candidates who list the same 1–2 GitHub repos on otherwise different resumes (text similarity below 0.40, at most 1 shared rare bullet), as teammates do, get a small repo weight (15 instead of 35). A pair linked by nothing else has the pair type "Team project (likely legitimate)", is hidden from the risk network by default and is not counted as a collusion pattern; the reason asks the recruiter to verify each candidate's own role |
| Hidden text / prompt injection | resume files | White, tiny, transparent or off-page PDF text, zero-width, bidirectional and tag characters, and instruction-like text aimed at AI screeners ("ignore previous instructions ..."). Individual flag weight 50; badge "Adversarial text detected in resume (Prompt Injection / Hidden Text)". Hidden text never counts towards skills and is never sent to the LLM |
| Identity | phone / email | One phone number or email used across several candidate IDs (duplicate applications of one person are handled by eligibility instead) |

**Outputs:** a *commonality* table (attribute values over-represented among 100% scorers, with
Fisher exact test and BH correction), risk *pairs*, *clusters* (connected groups with shared
traits), and a per-candidate risk score and level (High ≥ 60, Medium ≥ 30, Low above 0, otherwise
Clear) with plain-English reasons that name the evidence and the linked candidates. Hidden-text
findings appear with their snippets on Deep-Dive (section D) and in the Risk tab's **Adversarial
resume text** section. Thresholds and every signal weight are adjustable in Config.

---

## Synthetic dataset

The sample is not part of the repository (everything under `data/` except `data/README.md` is
gitignored). `python generate_mock_data.py` writes it to `data/sample/` in the shape of a Workday
recruiting export (about 5 s; the same options always produce byte-identical files):

* `test_scores_export.xlsx`: `Sheet1` recruiter summary, `Sheet2` one row per application with the
  14 export columns: Job Application, Candidate ID, Assessment Comment, Email, CGPA, Branch, School,
  Location, Max Score, Score, Cutoff Score, Cutoff Score Percentage, Percentage Score, Candidate
  Option.
* `Resume.zip`: a `Resume/` folder of multi-candidate PDF packets, each with a table of contents and
  one bookmark per attachment (33 packets, 1,877 candidates, 6 MB for the default size).
* `job_description.txt`, `README.md`.
* `_ground_truth/` (`labels.csv`, `clusters.json`, `injected_issues.json`, `resume_index.csv`):
  the planted cases and data-quality details used by `scripts/run_e2e.py` and the tests. The app
  never reads it.

Planted cases: duplicate applications (webmail and institutional e-mail, two candidate IDs, same
resume), legitimate team projects, resume mills, code-similarity rows, prompt-injection resumes,
namesakes, boilerplate resume bullets, about 12% of applications without a resume and one row
without an assessment score (`#N/A`).

| Option | Default | Meaning |
|---|---|---|
| `--n` | 2133 | Number of applications (150-12,000) |
| `--quick` | off | 300 applications (5 packets, under 1 MB, about 3 s) |
| `--seed` | 42 | Random seed |
| `--packets` | scales with `--n` (33 for 2,133) | Number of resume packets (5-100 candidates each) |
| `--out` | `data/sample` | Output folder. The generator only replaces files it wrote itself and refuses a non-empty folder it did not create |
| `--workers` | 0 (auto) | Processes used to render the PDFs (1 = no process pool) |

All people, colleges and companies are fictional. E-mail addresses use only the reserved names
`mail.example.com` and `<college>.example`.

---

## LLM integration

| | |
|---|---|
| Provider | **GitHub Copilot** chat models, reached through the **Screening Copilot Proxy** VS Code extension (`vscode-copilot-proxy/`) on the same computer. It serves a token-protected, localhost-only OpenAI-style API at `http://127.0.0.1:5757/v1` (`GET /health`, `GET /v1/models`, `POST /v1/chat/completions`). Requires VS Code 1.95+ with GitHub Copilot signed in. Setup: [docs/COPILOT_PROXY.md](docs/COPILOT_PROXY.md) |
| Connection | Automatic: while the proxy runs, the extension writes `~/.screening-copilot-proxy.json` (address + token) and the app reads it. Optionally pin the token with `COPILOT_PROXY_TOKEN` (environment variable, `.env` or `.streamlit/secrets.toml`; value from the VS Code command **Screening Proxy: Copy Token**), for example when the app runs under another user account. The UI only shows where the token came from |
| What the LLM writes | Rationales only: (1) rationale for each shortlisted candidate with interview questions; (2) gap analysis for the eligible candidates just below the cutoff (borderline band, default 50); (3) explanation of any candidate on Deep-Dive, with integrity verification questions for Medium/High risk; (4) plain-English summary of each risk group with verification steps; (5) executive summary of the run from aggregate statistics |
| What it never does | Change scores, ranks, cutoffs, statuses or the shortlist, or answer free-form questions. Nothing it writes is fed back into scoring, and the prompts state that scores are final |
| Privacy | Emails, phone numbers, links, IP addresses and candidate names are redacted before any text leaves the app; personal-detail lines (date of birth, gender, religion, address, ID numbers) are dropped. Resume text is treated as untrusted: invisible characters and instruction-like lines are removed and hidden PDF text is never extracted. Candidates are referred to by ID; resume excerpts are cut to 6,000 characters (adjustable) |
| Cost/time control | Only explicit button actions call the LLM. Batches run 4 requests at a time (adjustable) and retry temporary errors. Responses are cached on disk (`.cache/llm/`, gitignored) |
| In the Excel report | The **Full Excel report** includes the session's LLM output: rationales and explanations as "AI Notes (LLM)" on the Shortlist, All Candidates and Integrity Evidence sheets (borderline ones prefixed "Near the cutoff:"), risk-group summaries on Risk Clusters and the executive summary on Summary |
| In Run History | **Save run** keeps every LLM output of the session (executive summary, rationales, borderline analyses, cluster summaries); opening the run restores them |

---

## Security, privacy & fairness

* **Secrets:** `.streamlit/secrets.toml`, `.env` (and `.env.*` except `.env.example`), certificate and
  key files and package-manager credential files (`.npmrc`, `pip.conf` / `pip.ini`, `NuGet.Config`,
  `.gradle/`) are gitignored. Never commit tokens; if the proxy token may have been exposed, run
  **Screening Proxy: Rotate Token** in VS Code.
* **Candidate PII:** everything under `data/` is gitignored except the README, including
  `data/cache/`, where parsed datasets are cached for fast reload (deleted after 7 days by default;
  **Clear cached datasets** in Config deletes them at once). Saved runs (`runs/` or
  `SCREENING_RUNS_DIR`) also hold personal data. Process real TA data only on approved company
  infrastructure.
* **LLM proxy:** listens on `127.0.0.1` only, requires a bearer token on every request (compared in
  constant time), refuses non-loopback `Host` headers (DNS rebinding) and browser requests (`Origin`
  header), cannot be re-pointed by a workspace (machine-scoped settings) and never logs prompts or
  answers. The app connects only to loopback addresses and never routes those calls through an HTTP
  proxy. Text sent to Copilot is redacted first (see above) and is processed under the
  organisation's GitHub Copilot terms; use the LLM features on real data only where the data policy
  allows it, otherwise switch them off in Config.
* **Uploads:** resume files are size-, page- and ZIP-bomb-limited, hidden PDF text is removed before
  parsing, and uploaded config profiles cannot change the LLM connection or the cache folder.
* **Exports** neutralise spreadsheet formula injection (cells starting with `= + - @`).
* **Network:** the app listens on `127.0.0.1` (this machine only) unless you start it with another
  address, and has no login of its own (see "Who can reach it" under Quick start).
* **Telemetry:** Streamlit usage stats are disabled in `.streamlit/config.toml`.
* **Fairness:** the rubric uses only job-relevant evidence. Protected attributes are never used.
  College tier scoring is off by default. Integrity flags require human review before any decision,
  and the LLM never decides anything.

---

## Deploying on a company VM

For a shared installation on an internal Windows Server or Linux VM, follow
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). It covers installs through a package mirror or fully
offline, proxy and certificate settings, running as a service (NSSM, Task Scheduler, systemd), a
reverse proxy with SSO and TLS, the health check, retention of saved runs and caches, backups and
upgrades. The short version:

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
reverse proxy (SSO and TLS) when several recruiters share it. The LLM features additionally need
VS Code with GitHub Copilot running on the same VM ([docs/COPILOT_PROXY.md](docs/COPILOT_PROXY.md)).

---

## Project layout

```
app.py                 Streamlit entry point (loads .env, sidebar, header, KPIs, tabs)
ui/                    Streamlit components: theme, sidebar, state, charts and one module per tab
                       (shortlist, risk, deep-dive, compare, campus, history, config)
screening/             Engine (no Streamlit imports): schema, config, env, models, ingest, resume_parser,
                       skills, matching, duplicates, injection, scoring, risk, analytics, runs,
                       snapshots, llm, llm_backends, export, pipeline
vscode-copilot-proxy/  VS Code extension "Screening Copilot Proxy" (plain JavaScript, no build step;
                       Node tests in test/)
generate_mock_data.py  Synthetic Workday export + Resume.zip packets + ground truth (writes data/sample/)
data/                  Only README.md is in the repository; data/sample/ and data/cache/ are gitignored
scripts/               run_e2e.py (headless check), setup.ps1|sh and run_app.ps1|sh (VM setup/run)
tests/                 pytest suite
docs/                  Guides (below)
.env.example           Optional settings: COPILOT_PROXY_TOKEN, SCREENING_RUNS_DIR, APP_BRAND_EYEBROW
.streamlit/            config.toml (listen address, upload limit, no telemetry), secrets.toml.example
```

A few engine modules worth knowing: `duplicates.py` finds duplicate applications, `injection.py`
detects hidden text and prompt injection in resumes, `snapshots.py` keeps the fast-reload cache in
`data/cache/`, `env.py` loads the optional `.env` file, and `llm_backends.py` is the only module that
knows how to reach the Copilot proxy (`llm.py` holds the prompts, redaction and caching).

**Documentation**

| Guide | For | Contents |
|---|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Recruiters (TA team) | Every tab, field, status and export, step by step |
| [docs/COPILOT_PROXY.md](docs/COPILOT_PROXY.md) | Recruiters and IT | Installing and using the GitHub Copilot proxy extension, security model, troubleshooting |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | IT | Installing and running the app on a company VM |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Developers | Module contracts, data flow and output columns |
| [docs/HACKERRANK_DATA_REFERENCE.md](docs/HACKERRANK_DATA_REFERENCE.md) | TA, IT and developers | Assessment export reference: the Workday export and `Resume.zip` packets column by column (Part 1), HackerRank exports and their integrity signals (Part 2) |
| [vscode-copilot-proxy/README.md](vscode-copilot-proxy/README.md) | Developers and IT | The extension's HTTP API, commands, settings and tests |
