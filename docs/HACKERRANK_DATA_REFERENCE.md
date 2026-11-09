<!-- Reference: Part 1 describes the Workday assessment export and the Resume.zip packets as the
     console reads them; Part 2 describes what HackerRank for Work exposes to employers after an
     online assessment (compiled from public documentation; see Sources at the end).
     Every example value is fictional. -->

# Assessment export reference (Workday and HackerRank)

This reference describes, column by column, the assessment data the Candidate Screening & Integrity Console reads. It is written for the TA team that downloads the files and for the IT person who supports the app.

- **Part 1 - Workday assessment export.** The primary input: the Workday assessment export (`.xlsx`) and the `Resume.zip` of multi-candidate PDF packets that goes with it. What every column and every part of a packet contains, and what the console does with it.
- **Part 2 - HackerRank for Work exports.** HackerRank CSV/XLSX files are still accepted. How the console maps their headers, followed by a research reference of what HackerRank exposes to employers: field catalog, an example export layout, integrity signals, caveats and sources.

Every example value in this document is fictional: names, candidate IDs, colleges, and e-mail addresses on reserved `.example` domains.

Related documents: [USER_GUIDE.md](USER_GUIDE.md) (the screens and fields), [ARCHITECTURE.md](ARCHITECTURE.md#screeningingestpy) (the ingest and packet-splitting contracts), [DEPLOYMENT.md](DEPLOYMENT.md) (installation) and [data/README.md](../data/README.md) (the synthetic sample, which uses the Part 1 format).

## Contents

- [Part 1 - Workday assessment export](#part-1---workday-assessment-export)
  - [1.1 What TA uploads](#11-what-ta-uploads)
  - [1.2 Workbook layout](#12-workbook-layout)
  - [1.3 The 14 columns](#13-the-14-columns)
  - [1.4 The Assessment Comment](#14-the-assessment-comment)
  - [1.5 Missing values and "#N/A"](#15-missing-values-and-na)
  - [1.6 Repeat applications and fields the export lacks](#16-repeat-applications-and-fields-the-export-lacks)
  - [1.7 Resume.zip packet layout](#17-resumezip-packet-layout)
  - [1.8 How the console splits a packet](#18-how-the-console-splits-a-packet)
  - [1.9 Checking a new export](#19-checking-a-new-export)
- [Part 2 - HackerRank for Work exports](#part-2---hackerrank-for-work-exports)
  - [2.1 How the console reads a HackerRank export](#21-how-the-console-reads-a-hackerrank-export)
  - [2.2 Sources, confidence and where-exposed codes](#22-sources-confidence-and-where-exposed-codes)
  - [2.3 Field catalog](#23-field-catalog)
  - [2.4 Example HackerRank export layout](#24-example-hackerrank-export-layout)
  - [2.5 Integrity signals](#25-integrity-signals)
  - [2.6 Unverified / caveats](#26-unverified--caveats)
- [Sources](#sources)
- [Questions to settle before onboarding a new export](#questions-to-settle-before-onboarding-a-new-export)

## Part 1 - Workday assessment export

### 1.1 What TA uploads

| File | What it is | Where it goes in the app |
|---|---|---|
| Assessment export (`.xlsx`; the synthetic sample calls it `test_scores_export.xlsx`) | Workday report of one requisition's applications with their HackerRank results | Sidebar > **Upload TA feed + resumes** > "TA export: Workday or HackerRank (CSV or Excel)" |
| `Resume.zip` | A `Resume/` folder of multi-candidate PDF packets downloaded from Workday | Same panel > "Resumes: Resume.zip, PDF, DOCX or TXT" |

Upload both files as delivered: do not rename columns, re-save the workbook as CSV or unzip the archive. Either file also works alone. An export without resumes is scored on HackerRank + Education (the default "auto" missing-resume policy rescales those to 100 points). Resumes without an export are screened in resume-only mode. The synthetic sample (`python generate_mock_data.py`, see [data/README.md](../data/README.md)) has exactly this shape.

### 1.2 Workbook layout

The workbook has two sheets.

- **`Sheet1` - recruiter summary.** Two columns. Example (fictional counts):

  | A | B |
  |---|---|
  | Review Candidates: R-100200 2027 Technology Program Intern | |
  | Active Candidates | 14673 of 32326 |
  | Applied Prefilters | |
  | Numeric Return Attribute | Recruiting Stage (Workday Owner) |
  | 10696 | Review |
  | 0 | Screen |
  | 3977 | Assessment |
  | ... | one row per further stage: Interview, Reference Check, Offer, Employment Agreement, Background Check, Ready for Hire |
  | Total | 14673 |

  The console skips this sheet: no row has three or more filled cells, so it cannot hold the data.
- **`Sheet2` - one row per application.** A header row with exactly the 14 columns of [1.3](#13-the-14-columns), then one row per application (2,133 in the sample). The Assessment Comment cells are multi-line.

How the console picks the sheet:

- The first 50 rows of every sheet are inspected. The sheet whose header row maps the most known fields is read; ties go to the earlier sheet. When no sheet has a header-like row, the first non-empty sheet is read.
- In the sidebar, **Review column mapping** confirms the choice with the caption "Sheet used: **Sheet2** - detected automatically as the best header match among the workbook's 2 sheets."
- Title rows above the header are skipped. The header is the first row with at least 3 filled cells and at least half as many cells as the widest row; among the first few such rows, one that reads like column labels wins.
- Legacy binary `.xls` workbooks are refused with "Legacy binary .xls files are not supported - re-save the file as .xlsx or CSV."

### 1.3 The 14 columns

All 14 headers map automatically. The **Maps to** column of **Review column mapping** shows the result. Examples are fictional.

| # | Column | Type | Example | Console field | How the console uses it |
|---|---|---|---|---|---|
| 1 | Job Application | text: "First Last (first last)" | `Asha Menon (asha menon)` | `job_application`, gives `name` | The export's only name field: there is no Name or Phone column. The name is the text before " (", here `Asha Menon`; names typed all lower-case or all capitals are Title-cased. Used for display, for the duplicate-application check, and to redact the name before anything is sent to the LLM. |
| 2 | Candidate ID | text: `C-` + 7 digits | `C-1204417` | `candidate_id` | The candidate's key, upper-cased. A blank ID gets a generated `ROW-00001`-style ID and the data flag `generated_id`. Resumes split from packets are linked by this ID in their file names. A repeat applicant has a second, later ID ([1.6](#16-repeat-applications-and-fields-the-export-lacks)). If one ID appears on two rows, the last row is kept (data flag `duplicate_id`). |
| 3 | Assessment Comment | multi-line text | see [1.4](#14-the-assessment-comment) | `assessment_comment`, gives `test_status`, `integrity_status`, `proctoring_flags` | Split into the test status, the integrity status and the integrity summary. The cleaned comment is shown in Deep-Dive section **A. Assessment (HackerRank)** and exported as "HackerRank: Comment". |
| 4 | Email | text | `asha.menon24@mail.example.com` (webmail) or `24cs0117@northfield.example` (institutional) | `email` | The first address in the cell, lower-cased; an invalid value is left blank (data flag `invalid_email`). The domain tells personal webmail from an institutional address: the duplicate-application check keeps the institutional application. The same address under two IDs adds the data flag `shared_email` and the "Same email" risk signal. |
| 5 | CGPA | number, 10-point scale | `9.12` | `cgpa` | Education sub-score (6.0 or less scores 0, 9.5 or more full marks), the **Min CGPA** eligibility rule and the "CGPA (feed, then resume)" tie-break. Also accepts "8.5/10", "3.6/4" (converted x2.5) and percentages ("78.5%" becomes 7.85). Out-of-range values are left blank (data flag `cgpa_out_of_range`). The export's CGPA wins over the resume's ("CGPA Source" = feed). |
| 6 | Branch | text | `Computer Science & Engineering` | `branch` | Degree relevance in the education score (computer science, IT, data or AI score full marks; electronics, electrical, maths or statistics 0.7; other engineering 0.4). Also used by the no-resume duplicate check, the commonality analysis and Campus Distribution. |
| 7 | School | text | `Northfield Institute of Technology`, or an acronym such as `QZ-IICT` | `college` | Spellings that differ only by case, spaces or punctuation are unified to the most common one. Used by the college cap, the college tier (only when tier scoring is switched on; acronyms are matched against the tier list), Campus Distribution, the commonality analysis and the duplicate check (same college). |
| 8 | Location | text | `Rivertown` | `city` | The college city or the candidate's hometown. Display, commonality analysis and Campus Distribution. |
| 9 | Max Score | number | `175` | `max_score` | Denominator of the percentage. |
| 10 | Score | number, decimals allowed | `168.5` | `hackerrank_score` | Percentage = Score / Max Score x 100, clipped to 0-100. It drives the HackerRank part of the rubric (40 of 100 points by default) and the integrity focus group (100% scorers). `#N/A` means no score ([1.5](#15-missing-values-and-na)). |
| 11 | Cutoff Score | number | `96` | `cutoff_score` | The campaign's pass mark in points. Shown for reference in Deep-Dive section A: the "Cutoff score" card, and the Score card's difference such as "+72.5 vs cutoff". It does **not** set eligibility; use **Min HackerRank %** in Config & Rubric Tuning for that. |
| 12 | Cutoff Score Percentage | number | `55` | `cutoff_pct` | The pass mark in percent, shown next to the cutoff score ("96 (55%)"). A column of fractions (0.55) is read as percent. |
| 13 | Percentage Score | number | `96.29` | `hackerrank_pct` | Used only when Score is blank: the score becomes percentage x max / 100 (data flag `score_from_pct`). When both are present, Score / Max Score wins and differences above 1 point are listed as an info item in **Data quality details**. |
| 14 | Candidate Option | text | `Django (Python)` | `candidate_option` | The assessment track the candidate chose (the sample uses Spring Boot (Java), Django (Python) and Node.JS (JavaScript)). Shown in Deep-Dive section A and on the Excel "Shortlist" sheet. |

### 1.4 The Assessment Comment

Each cell holds up to three parts. Workday stores the first line break as the literal text `&#xa;` and the second as a real line break. A clean application:

```
Hackerrank Completed – Qualified&#xa;Integrity Issues: No Issues Detected
Integrity Summary: N/A
```

An application with an integrity finding:

```
Hackerrank Completed – Qualified&#xa;Integrity Issues: Medium
Integrity Summary: Detected Code Similarity With Other Submissions
```

The console decodes the entity (a twice-escaped `&amp;#xa;` too), unifies the line breaks and splits the cell at the labels "Integrity Issues:" and "Integrity Summary:".

| Part | Example | Console field | Rule | Effect |
|---|---|---|---|---|
| Status line | `Hackerrank Completed – Qualified` | `test_status` = `Completed` | The word after the optional "HackerRank" prefix, cut at the dash (en dash, em dash or a spaced hyphen; "Completed-Qualified" works too). Known words are normalised: Completed (also complete, submitted, done, finished, attempted), Invited, In Progress, Expired, Did Not Attempt. The qualification after the dash ("Qualified") is not a field of its own; it stays in the comment text. | Deep-Dive section A "Test status"; "HackerRank: Test Status" in exports. |
| Integrity Issues | `No Issues Detected`, `Medium`, `High` | `integrity_status` | "No Issues Detected", "No issues", "None", "Nil", "N/A", "Clear", "Clean", "OK" and "-" become **Clear**. A value that starts with a severity word (Low, Medium, Moderate, High, Critical, Review, Suspicious, Flagged) becomes that word, for example "Medium". Anything else is kept as written. | **Medium** and **High** count as integrity concerns: the risk engine adds the individual flag "Proctoring integrity status" (weight 20, detail "Assessment integrity issues: Medium"), Deep-Dive section A shows a warning and section **D. Integrity** repeats the status. Clear and Low raise nothing. |
| Integrity Summary | `N/A`, `Detected Code Similarity With Other Submissions` | `proctoring_flags` | Placeholders (N/A, None, Nil, Null, "-", "...", "No issues detected") become blank; other text is kept. | A non-empty summary adds the individual flag "Proctoring flags" (weight 15, detail "Proctoring notes: ..."). Deep-Dive section A "Proctoring flags". |

With the default thresholds, a row with "Integrity Issues: Medium" and a summary collects 35 risk points (20 + 15). That is **Medium** risk, so the candidate, if shortlisted, becomes **Shortlisted (Integrity Review)**.

More rules:

- The cleaned comment, one part per line, is kept in `assessment_comment`.
- The three parts fill their fields only when the file has no separate column mapped to them. **Data quality details** then shows the info item "Test Status, Integrity Status, Proctoring Flags read from the 'Assessment Comment' column."
- A comment column is split only when at least 30% of its first 2,000 filled cells carry one of the two labels or start with "HackerRank". Otherwise it is kept as plain text.
- A separate column headed "Integrity Issues" (or "Integrity Summary") that is mapped to **Assessment Comment** is read as plain integrity-status (or proctoring-flag) values.

### 1.5 Missing values and "#N/A"

- Workday writes `#N/A` (an Excel error value, like a failed lookup) for an application without an assessment result. In every column the console treats `#N/A`, `N/A`, `NA`, `n.a.`, `nan`, `null`, `none`, `nil`, `-`, `--`, `---`, "not available", "not applicable" (any case) and empty cells as missing.
- An unscored application (in the sample: one row with `#N/A` in Score, Percentage Score, Cutoff Score Percentage and Candidate Option, and Max Score 100):
  - has no score: data flag `score_missing`, and the warning "1 candidate(s) have no assessment score (data flag 'score_missing')." in **Data quality details**;
  - fails the eligibility rule **Assessment** ("No assessment score recorded on HackerRank.") and is **Ineligible**: red banner in the Deep-Dive, listed under "Ineligible candidates" on the Shortlist tab and on the Excel "Ineligible Candidates" sheet;
  - keeps its other fields blank.
- `#N/A` in any other column only leaves that field blank. A blank CGPA never fails the Min CGPA rule.

### 1.6 Repeat applications and fields the export lacks

- **One person, two applications.** Workday exports one row per application. A student who applied twice, typically once with a personal webmail address and once with the university address, appears under two candidate IDs with the same name, the same school and the same resume. The console marks such rows: same name, same college, and an identical or near-identical resume (or, when neither has a resume, the same CGPA and branch). One application is kept: the one with an assessment score, then the one with the institutional e-mail, then the lowest ID. While **Auto-disqualify duplicate applications** is on (the default), the others become **Ineligible** with the rule **Duplicate Application**, for example "Disqualified: Duplicate application of C-1204417 (merged into official profile ...)". Namesakes (the same name at another college, or with a different resume) are never merged, and duplicate applications are never treated as collusion pairs.
- **Fields the Workday export does not have**: phone, degree, graduation year, test start and submission times, completion time, IP address, test mode, tab switches, paste counts, per-question scores and answer hashes. **Data quality details** lists them under "Optional fields absent". The integrity checks that need them (timing, shared IP, speed, per-question similarity, same phone) are skipped and named in the Risk tab's "Signals skipped (missing data)" expander. Without a graduation year, the "Allowed graduation years" rule excludes nobody.

### 1.7 Resume.zip packet layout

`Resume.zip` contains one folder, `Resume/`, with one or more PDF **packets**. A packet bundles the attachments of many candidates of one requisition.

- **Packet file name**: `<requisition> - <job title>_set <N>.pdf`, for example `R-100200 - 2027 Technology Program Intern_set 1.pdf`. The sample has 33 packets (`_set 1` to `_set 33`) of 5 to 100 candidates each; the last set is a small remainder batch. The console does not depend on the file name.
- **Table of contents** on the first 1-3 pages:

  ```
  Table of Contents
  R-100200 - 2027 Technology Program Intern (1 of 33)

  Candidate Name                  Candidate ID      Attachments
  1. Asha Menon                   C-1204417
                                                    Asha_Menon_Resume.pdf
  2. Rohan Iyer                   C-1204452
                                                    Rohan_Iyer_CV.pdf
                                                    Cover_Letter.pdf
  3. ...
  ```

  One row per candidate: number, name, and the candidate ID in its own column, with the candidate's attachment file names on the lines below. Only candidates with at least one attachment are listed; in the sample, about 12% of the applications have no resume and appear in no packet.
- **Attachments** follow in table-of-contents order. First the resume: in the sample about 83% are one page, 16% two pages and 1% three to six pages (a resume followed by certificate pages). About 5% of candidates have a second attachment: a cover letter, a grade card or transcript, or a certificate. Attachment file names vary, for example `Asha_Menon_Resume.pdf`, `AshaMenon_CV.pdf`, `Resume_Asha_Menon.pdf`, `Resume.pdf`, `CV.pdf`, `Cover_Letter.pdf`, `Grade_Card.pdf`, `Internship_Certificate.pdf`.
- **Bookmarks**: the PDF outline has one bookmark per attachment, pointing at the attachment's first page. Bookmarks that carry the candidate's name ("Asha Menon" or "Asha Menon (asha menon)") are matched directly. The synthetic sample titles them `<Name> - <attachment file name>` ("Asha Menon - Asha_Menon_Resume.pdf").
- **Page ranges**: the first bookmark's page is where the table of contents ends. A candidate's pages run from their first bookmark to the next candidate's first bookmark; the last candidate's run to the end of the packet. Example: a one-page table of contents, then Asha Menon on pages 2-3 (a two-page resume) and Rohan Iyer on pages 4-5 (a one-page CV and a one-page cover letter).

### 1.8 How the console splits a packet

1. **Reading the archive.** The ZIP is read member by member, so only one packet is in memory at a time; the progress bar shows "Reading resume files - n/33". A packet may be up to **250 MB** with any number of pages. Folders, `__MACOSX` and hidden files are ignored, nested ZIPs are not opened, and encrypted or unusually compressed members are skipped with a warning.
2. **Recognising a packet.** A PDF is treated as a packet when it has at least 2 pages (3 without bookmarks) and table-of-contents rows on its first 1-3 pages: lines such as "1. Asha Menon C-1204417" with a Workday ID (`C-` + 6 to 8 digits), or, below a "Table of Contents" or "Candidate Name Candidate ID" heading, any ID that matches the candidate ID pattern. Without such a heading, at least two rows are needed. A name wrapped onto the next line is joined. Any other PDF is an ordinary resume.
3. **Finding each attachment.** The bookmarks give every attachment's first page. They are matched to the listed candidates in order, by name (exact or a close fuzzy match) or by candidate ID, and a candidate may have several attachments. When fewer than half of the bookmark titles name a listed candidate (as in the synthetic sample), the console looks for each listed name or ID near the top of the pages instead; if that fails as well, it assigns the bookmarks to the candidates in order. A packet without bookmarks is always split by the names on the pages.
4. **Writing the split files.** One PDF per attachment:

   | Attachment | Split file name |
   |---|---|
   | first | `<Candidate ID>_<First_Last>.pdf`, e.g. `C-1204417_Asha_Menon.pdf` |
   | second, third, ... | `C-1204417_Asha_Menon_att2.pdf`, `C-1204417_Asha_Menon_att3.pdf` |

   The name part is the listed name with accents removed and every run of other characters replaced by `_` (at most 60 characters). Each split file remembers its packet and 1-based page range, for example "R-100200 - 2027 Technology Program Intern_set 1.pdf p.2-3", which Deep-Dive section **B. Resume (self-reported)** shows as "From packet ..., pages 2-3". The candidate ID is also written into the file's PDF metadata, so two applications with the same resume stay two separate files. When the names had to be located on the pages, a candidate's file covers all of their pages: the resume and any second attachment.
5. **Caps after the split.** Every split file then passes the normal per-resume limits: at most **10 MB** (a larger file is skipped with a warning) and at most **50 pages** (a longer file is not read as a resume). One archive yields at most 10,000 resumes and 2,048 MB uncompressed.
6. **Linking.** A split file links to its application through the candidate ID in its file name: match method "ID in file name", confidence 100%. An `_att2` file of a candidate whose resume is already linked is set aside as a further attachment (match method "duplicate") and is not scored.
7. **Messages** in **Data quality details**:
   - info: "Split multi-candidate packet into 64 file(s) for 64 candidate(s) (86 pages)", plus "K listed candidate(s) have no attachment pages" when a listed candidate has none;
   - warning, when a fallback was used, ending in "verify the split": "... bookmark titles do not name the listed candidates, so the names were located on the pages - verify the split", "... bookmark titles do not name the listed candidates; attachments were assigned in order - verify the split" or "... the packet has no bookmarks, so the names were located on the pages - verify the split". The synthetic sample produces the first of these for every packet, because its bookmark titles also contain the attachment file name; the split itself is correct, and all 1,877 resumes link by ID;
   - warning: "Multi-candidate packet listing N candidate(s) could not be split (no usable bookmarks and the listed names were not found on the pages); kept as one file".

The packet size limit (`max_bundle_mb`), the per-resume limit (`max_file_mb`), the archive limits and the switch `split_bundles` live in the `processing` section of the configuration. They are not on screen, but a configuration JSON loaded with **Upload a config JSON** in Config & Rubric Tuning can change them.

### 1.9 Checking a new export

1. Before running, open **Review column mapping**: 14 of 14 columns mapped, "Sheet used: Sheet2". If Workday renamed a column, pick the field in **Maps to**, then **Download mapping (JSON)** and load that profile with **Load mapping profile** next time.
2. After the run, open **Data quality details** in the sidebar. Expect: "Read N row(s); 14 column(s) mapped (auto-detected), 0 kept as extra columns."; "Test Status, Integrity Status, Proctoring Flags read from the 'Assessment Comment' column."; "Candidate names taken from the 'Job Application' column."; one "Split multi-candidate packet ..." item per packet; a warning for every unscored row.
3. Compare the KPI card **Total Processed** ("N with a linked resume") with the number of candidates listed in the packets' tables of contents.
4. Open a few candidates in the Deep-Dive. Section B shows the packet and page range a resume came from: check one against the packet itself.
5. Look at the Shortlist tab's "Ineligible candidates" section: duplicate applications and unscored rows should be the only surprises.

## Part 2 - HackerRank for Work exports

### 2.1 How the console reads a HackerRank export

A HackerRank CSV or Excel file is read the same way as any other assessment export: headers are mapped to the console's fields by exact aliases, per-question patterns and close fuzzy matches; unmapped columns are kept as `extra_*` columns (in the candidate table and the exports, never scored). The table shows how the full example header row of [2.4](#24-example-hackerrank-export-layout) is mapped automatically ("Maps to" labels as in **Review column mapping**).

| Export header | Maps to | Notes |
|---|---|---|
| `ID` | Candidate ID | |
| `Name` | Name | `Full Name` stays an extra column. |
| `Email` | Email | `Personal Email Address` stays an extra column while `Email` is present. |
| `Contact Number` | Phone | |
| `Univ/College Name`, `Degree`, `Stream/Branch`, `Graduation Year`, `CGPA`, `City` | College, Degree, Branch, Graduation Year, CGPA, City | |
| `IP Address` | IP Address | |
| `Score`, `Max Score`, `Percentage Score` | HackerRank Score, Max Score, HackerRank % | |
| `Cutoff Score` | Cutoff Score | Reference only, as in Part 1. |
| `Status` | Test Status | |
| `Test Name` | Test Name | |
| `Attempt Start time`, `Attempt Endtime` | Test Start Time, Submission Time | |
| `Test Duration` | Completion (min) | The test's configured length, not the time taken: see the note below. |
| `Attempt Time Taken` | (not mapped) | Not mapped while `Test Duration` holds Completion (min). |
| `Detailed Report Url` | Report URL | |
| `Attempt Plagiarism` | Plagiarism Flag | |
| `Number Of Window Exits` | Tab Switches | |
| `Out Of Window Duration (seconds)` | Out-of-Window (sec) | |
| `Copy Paste Frequency` | Copy-Paste Events | |
| `Fullscreen Exit Count` | Secure-Mode Exits | |
| `Proctor Flag` | Integrity Status | |
| `Proctor Flag Reasons` | Proctoring Flags | |
| `Feedback Comments` | Assessment Comment | The candidate's feedback, kept as text (it carries no integrity labels). |
| `Q{n} - Plagiarism Percent` | Q{n} code similarity % | Feeds the "High code similarity" flag. |
| `Gender` | (dropped) | Protected attribute: never read, scored, shown, exported or sent to the LLM. |
| all other columns (`Tag`, `Full Name`, `Student Roll Number`, `Personal Email Address`, `Work Experience`, company, invite, test-owner and team fields, `Attempt ID`, `Attempt Count`, `Last Attempt Date And Time`, `Network Disconnected Duration`, `Product Rating`, `Fullscreen Exit Duration`, `Proctor Activity *`, `Attempt Activity *`, `Integrity Summary`, the four split-score columns, the other `Q{n} - ...` columns, `Integrity Issues`, `Code Quality`) | extra columns | Kept as `extra_<header>`; not scored. |

Adjustments worth making for a full HackerRank export, in **Review column mapping** (each field takes one column; when two columns claim a field, the earlier one wins):

- **Completion time.** Set `Test Duration` to "— not mapped —" (or Action **Ignore**) and map `Attempt Time Taken` to "Completion (min)". An all-numeric completion column whose median is above 600 is read as seconds and converted to minutes.
- **HackerRank's overall integrity verdict.** To use `Integrity Issues` (High / Medium / No issues detected) instead of `Proctor Flag`, set `Proctor Flag` to "— not mapped —" and map `Integrity Issues` to "Integrity Status". High and Medium then count as integrity concerns, as in Part 1. Likewise `Integrity Summary` can replace `Proctor Flag Reasons` as "Proctoring Flags".
- **Per-question scores.** Headers such as `Q11 - Question Score` are not recognised automatically: the per-question patterns expect the score word right after the question number ("Q3 Score", "Question 3 Marks", "Q3 Score (Coding - Graphs)"). Map them with a mapping profile, for example:

  ```json
  {"version": 1, "mapping": {"Test Duration": "ignore", "Attempt Time Taken": "completion_minutes",
                             "Q11 - Question Score": "q11_score", "Q12 - Question Score": "q12_score",
                             "Q13 - Question Score": "q13_score"}}
  ```

  Load it with **Load mapping profile** (at most 64 KB; headers that are not in the file are listed as skipped, and the other columns keep the auto-detected mapping). Per-question scores feed the "HackerRank coding-question marks" tie-break: questions that also have a code-similarity column count as coding questions.

Headers the console recognises on its own, besides the ones above: "Q3 Score" / "Q3 Marks" / "Q3 Points" (per-question score), "Q2 Code Similarity" / "Q2 Similarity" / "Q2 Plagiarism %" (per-question code similarity), "Q1 Answer Hash" / "Q1 Code Hash" / "Q1 Signature" (per-question answer hash, used by the "Identical answers" pair signal; HackerRank's bulk export has no such column), "Max Code Similarity", "Plagiarism Match" / "Most Similar Candidate", "Integrity Status", "Proctoring Flags", "Test Mode", "Tab Switches" and "College Tier".

### 2.2 Sources, confidence and where-exposed codes

Each underlying field appears once. Each row uses the most exact label available, which is the export header where one is known, and carries the strongest confidence its sources support. All source URLs are kept as keys that resolve in **Sources**. Speculative items that no second source backs up are listed only under **Unverified / caveats**.


**Where-exposed codes used below:**
- **XL**: the Excel export (Tests › Candidates › Export › Excel). Each user picks its columns in Report Settings. The Report Settings section is shown in brackets: Cand, Co, Test, Att, Team, Res.
- **XL+**: Excel columns that appear only in the Proctor Mode export walkthrough, not in Report Settings.
- **CR-A / CR-Q / CR-T / CR-QS / CR-I**: Insights › Custom Reports objects. These are Candidate Test Attempts and Invites, Candidate Question Attempt, Tests, Questions, and Interviews.
- **TDD**: the Insights "Download Test Data" CSV.
- **API / API+ / API-T**: API v3 TestCandidate default field / field returned only when requested via `additional_fields` / field on the Test object.
- **WH**: the webhook payload sent to `test_result_url` (fields under `data.*`).
- **SR / DR**: the online Summary Report / Detailed Report.
- **CL**: the Candidates list in the UI.
- **PDF**: the per-candidate PDF report.
- **ATS**: fields written back to applicant tracking systems.

### 2.3 Field catalog

| Label | Category | Where exposed (aliases) | Confidence | Sources |
|---|---|---|---|---|
| `Email` | identity | XL [Cand]; CR-A "Candidate Email"; API `email` (required at invite, filterable); WH `data.email`; PDF "Email:"; SR; login form "Email address / Login" | verified | RS, CR, API, WH, SUM, PDF19, HRL |
| `Name` | identity | XL [Cand]; CR-A "Candidate Name"; CL "Candidate"; API `full_name` (settable at invite). Name on the candidate record; can differ from the onboarding `Full Name` the candidate types (likely) | verified | RS, CR, CL, API, INV |
| `ID` | identity | XL [Cand]; API `id`, which is the `{candidate_id}` in report and PDF URLs (string in schema, numeric in practice, e.g. 82851884); WH `data.id` (WH also carries a different integer `data.test_candidate_id`, meaning undocumented); CR-Q "Candidate ID (Question Attempt)". XL `ID` = API `id` is likely | verified | RS, API, WH, CR |
| `Attempt ID` | identity | XL [Att]; CR-A and CR-Q "Attempt ID (Question Attempt)" (join key); API org-wide search `attempts[].attempt_id` (doc example reuses `id`); `attempt_events[].attempt_id`; a top-level `attempt_id` appears only in a practitioner client (likely) | verified | RS, CR, API, CLI |
| `Invite ID` | identity | XL [Test] | verified | RS |
| `uuid` | identity | API `GET /x/api/v3/candidates/search` only: a person-level ID across tests, with `name`, `email`, `created_at`, `updated_at`, `attempts[]` | verified | API, PY |
| `IP Address` | identity | XL [Cand]; CR-A; API+ `ip_address`; WH `data.ip_address` (sent by default). One value per attempt; no user-agent, device or geolocation field exists. CR-I "Candidate IP Address" covers live interviews only | verified | RS, CR, API, WH |
| `candidate_details` | candidate_info | API `[{field_name, title, value}]` (strings); WH items add `type`, `options`, `db_field`, `required`; PDF header "Title: value"; SR Candidate Details (all fields since Jan 2026); CR-A one column per predefined field; XL only via [Res] `Candidate Detail Answer` (header unverified) | verified | API, WH, ONB, PDF19, RN2601, CR, RS |
| `Full Name` | candidate_info | onboarding `full_name`; CR-A "Candidate Full Name"; PDF "Full Name:" | verified | ONB, API, CR, PDF19, HRL |
| `Student Roll Number` | candidate_info | `roll_number`; CR-A same; PDF "Student Roll Number:" | verified | ONB, API, CR, PDF19, RN2601 |
| `Univ/College Name` | candidate_info | `univ` (free text); CR-A "University/College Name" | verified | ONB, API, CR |
| `Stream/Branch` | candidate_info | `branch`; CR-A "Stream / Branch"; PDF "Stream/Branch:" | verified | ONB, API, CR, PDF19 |
| `Degree` / `Post Graduation Degree` / `Major` | candidate_info | `degree` / `pgdegree` / `major`; CR-A "Degree" / "Postgraduate Degree" / "Major" | verified | ONB, API, CR |
| `Graduation Year` | candidate_info | `year_of_graduation` (string; API example title "Year of graduation"); CR-A same | verified | ONB, API, CR |
| `CGPA` / `GPA` | candidate_info | `cgpa` / `gpa`, self-reported free text; CR-A same; PDF "CGPA:" | verified | ONB, API, CR, PDF19 |
| `Contact Number` | candidate_info | `phone_number` (no validation); CR-A same; PDF "Contact Number:" | verified | ONB, API, CR, PDF19, HRL |
| `Personal Email Address` | candidate_info | `email_address` (not the login email); CR-A "Personal Email"; PDF same | verified | ONB, API, CR, PDF19 |
| `Gender` | candidate_info | `gender`, Single Select (options undocumented); CR-A; PDF "Gender:". The private demographic block on the login page (`gender_private`) is never shared with employers | verified | ONB, API, CR, PDF19, HRL |
| `Work Experience` | candidate_info | `work_experience`, Single Select (webhook sample options `< 1`, `1` … `> 5`); CR-A; PDF "Work Experience:" | verified | ONB, API, WH, CR, PDF19, W-SUM22 |
| `City` / `City (During Graduation)` / `Country` | candidate_info | `city` / `city_graduation` / Country (API key unconfirmed); CR-A "Candidate City" / "Graduation City" / "Country"; SR shows city (new report) and country (old report) | verified | ONB, API, CR, SUM, HRL |
| `Job Role` / `Resume` / `Contact Recruiter` / `linkedin` | candidate_info | `role` / `resume` (.pdf .doc .docx, ≤5 MB) / `contact_recruiter` / `linkedin` (in the API list only); CR-A "Job Role" / "Resume" / "Recruiter Contact" | verified | ONB, API, CR, HRL |
| Custom field (named by recruiter) | candidate_info | Text Field, Radio Button, Checkbox, Dropdown, File (API `input`, `select`, `radio`, `checkbox`, `file`); CR-A "Custom Question Answers"; checkbox picks comma-joined | verified | ONB, API, CTS, CR, HRL |
| `Feedback Comments` | candidate_info | XL [Att]; CR-A "Candidate Feedback Comments"; API `feedback`; WH `data.feedback`; SR; CL filter; Lever "Candidate feedback" | verified | RS, CR, API, WH, SUM, W-SUM22, CL, LV |
| `Product Rating` | candidate_info | XL [Att]; CR-A; new SR "candidate ratings"; scale undocumented | verified | RS, CR, SUM |
| `Test Severity Level (Candidate Rated)` | candidate_info | CR-A; defined only as "Overall severity score for the test" | verified | CR |
| `invite_metadata` | candidate_info | API free-form JSON set at invite (e.g. ATS IDs); WH `data.metadata` (probably the same value echoed back) | verified | API, WH, ZAP |
| `Score` | score | XL [Att]; CR-A "Test Score"; API `score` (float); WH `data.score`; SR; PDF "375/375"; CL; ATS. Changes with reviewer edits and negative marking on multiple-choice questions | verified | RS, CR, API, WH, SUM, PDF19, CL, SCO, GH, LV, WD, EF |
| `Percentage Score` | score | XL [Att]; CR-A "Score (%)"; API `percentage_score` (0–100); WH; CL "100%"; Workday puts it in the "Comment" field | verified | RS, CR, API, WH, CL, WD, PDF19 |
| `Max Score` | score | XL [Att] (Report Settings wording is garbled; this is the test maximum); CR-A "Test Max Score"; CR-T "Test Maximum Score"; WH `data.max_score`; not on the API TestCandidate object (use Test `max_score` / `candidate_max_score`) | verified | RS, CR, WH, API |
| `Cutoff Score` | score | XL [Att]; CR-T "Test Cut-Off Score"; API-T `cutoff_score` (+`cutoff_score_percentage`); at or above it → Qualified; changing it later does not re-grade | verified | RS, CR, API, SCO, CUT, STAT |
| `Section Score` | score | XL [Res]; API+ `scores_sections_split` (appears only as a parameter example: likely); CL filter | verified | RS, API, CL, SKT |
| `Skill Score` | score | XL [Res]; API `scores_skills_split`; SR "Skill Scores"; PDF "Skill distribution"; CL filter | verified | RS, API, SUM, DL |
| `Tag Score` | score | XL [Res]; API `scores_tags_split` (numbers); WH same key as "scored/max" strings; PDF "Tags Score:"; difficulty labels count as tags | verified | RS, API, WH, SUM, PDF19, TAGQ |
| `Question Type Score` | score | XL [Res]; WH `data.scores_questions_split` (`{code: 0, database: 0}`) | verified | RS, WH |
| `Code Quality` | score | CL column and filter; new SR; DR; new PDF; API+ `questions.code_quality` (single-candidate endpoint only) / `code_quality_grade`. Values: A, B, C, Not Generated (score below 50%), Not Available. AI Add-on only. No documented bulk-export column | verified | CQ, CL, SUM, API, RN2601, W-KEY |
| `Benchmark` | score | New SR only: percentile against last calendar year's takers of the same questions (needs ≥500 attempts; not available for AI-assisted tests) | verified | BEN, SUM, RN2601 |
| `Optimality` | score | New SR column; DR; coding questions only; AI Add-on; scale undocumented; no API field | verified | OPT, SUM |
| `AI Fluency` | score | New SR; DR; API+ `attempt_metrics.ai_fluency` (grade A/B/C/NA, summary, rubrics) and `questions.ai_fluency`; only for tests that use the built-in AI assistant | verified | AIU, API, AIT |
| Performance Summary | score | New SR; new PDF; API+ `performance_summary` (AI-written text) | verified | SUM, DL, API, RN2601 |
| `Attempt Count (Lifetime)`, `Total Questions Asked`, `Percentage of Questions Answered`, `At Least 1 Question Answered`, `All Questions Answered` | score | TDD (added Oct 2024) | verified | RN2410, INS |
| Test and Question Insights aggregates | score | Tests › Insights: score distribution, Median Score, Median Attempt Time, Median Time to Full Score; per-question median, 75th and 90th percentiles; each chart downloads as CSV | verified | TINS, QINS |
| Lever 1–4 assessment score | score | Lever converts score ÷ max into a 1–4 bucket | likely | LEV |
| `Attempt Start time` | timing | XL [Att]; CR-A "Attempt Started At"; API `attempt_starttime` (`2016-05-24T06:27:25+0000`, filterable by range); WH `data.starttime` (`…:38.000Z`); ATS "Started At" / "Start time" | verified | RS, CR, API, WH, GH, EF, LV, MOSS |
| `Attempt Endtime` | timing | XL [Att]; CR-A "Attempt Ended At"; API `attempt_endtime`; WH `data.endtime`; ATS "Completed At" / "Finished At" / "End time" | verified | RS, CR, API, WH, GH, EF, LV |
| `Attempt Time Taken` | timing | XL [Att] (unit undocumented); CR-A "Attempt Duration (s)"; PDF "Time Taken: 180 min 25 sec/ 300 min"; WH `data.total_time_taken` ("10 seconds"); ATS "Total Time Taken"; the API has no such field (end − start) | verified | RS, CR, PDF19, SUM, WH, GH, LV |
| `Test Duration` | timing | XL [Test]; CR-T "Test Duration (min)"; API-T `duration` (min); PDF denominator | verified | RS, CR, API, SUM, PDF19 |
| Taken On | timing | PDF header "Taken On:" (includes timezone); new SR "test date" | verified | PDF19, SUM |
| `Last Attempt Date And Time` | timing | XL [Att] | verified | RS |
| `Invited At` | timing | XL [Test]; CR-A "Invite Sent At"; API `invited_on`; WH `invite_details.timestamp`; CL "Invited on" (shows "-" for public-link candidates) | verified | RS, CR, API, WH, ISS |
| `Invite Valid From` / `Invite Valid To` | timing | XL [Test]; CR-A; API `invite_valid_from` / `invite_valid_to` (null = always valid); CL "Invitation expiry date" | verified | RS, CR, API, FILT, PBP |
| Test login window | timing | API-T `starttime` / `endtime` ("Test expiration time"); candidates already logged in keep their full duration | verified | GEN, API, W-ACC |
| `added_time` / `unclaimed_added_time` / `accommodations.additional_time_percent` | timing | API (minutes; string now, integer before Jun 2026); WH `data.added_time_buffer`; UI "Add Time" / "Time Accommodation" | verified | API, API26, TIME, WH, PY |
| `Candidate Record Created At` / Completed on | timing | CR-A / CL column and filter | verified | CR, CL, FILT |
| `attempt_events` | timing | API+ `{id, attempt_id, event, data, inserttime}`. Event codes: 1 login, 2 question list, 3 viewed question, 4 submitted, 5 ran code, 6 ping, 7 left tab, 8 returned, 9 pasted from outside, 10 rendered code. WH `data.timeline[]` (epoch timestamps, readable messages) | verified | API, API26, KIT, WH |
| Attempt Activity / Timeline | timing | SR tab: time per question, events, integrity events, plus Proctor, Image Analysis and Photo ID sub-sections; the legacy Timeline tab showed pasted text | verified | SUM, GLOS, PRT, PBP, W-DS |
| `Question Score` | per_question | XL [Res]; CR-Q "Question Score" and "Question Max Score"; API `questions.{qid}.score` / `max_score`; WH `score` / `points`; PDF "75/ 75"; DR (editable) | verified | RS, CR, API, WH, PDF19, DET |
| `Question Answered` | per_question | XL [Res]; CR-Q "Is Question Answered"; API and WH `answered` | verified | RS, CR, API, WH |
| `Time Taken Per Question` | per_question | XL [Res] (unit undocumented); CR-Q "Time per Question (s)"; API+ `questions.time_taken`; WH; PDF "28 min 50 sec" | verified | RS, CR, API, WH, PDF19, SUM, W-SUM22 |
| `Solution Status` | per_question | CR-Q (values undocumented); SR symbols: correct, incorrect, partially correct, not attempted, requires manual evaluation; PDF "Correct Answer"; WH `status` "unattempted" | verified | CR, SUM, PDF19, WH |
| `Question ID (Attempt)` / `Section Name` / question metadata | per_question | CR-Q; API `questions` keyed by question ID (`name`, `type`: code, mcq, multiple_mcq, approx, whiteboard, file_upload, textAns, plus database/fullstack/coderepo_task); CR-QS "Question Type", "Question Difficulty", "Question Source", "Recommended Time (min)"; WH tags, level, skills | verified | CR, API, WH, PDF19, KIT |
| `Code Language` | per_question | XL [Res]; CR-Q; PDF "Language used:"; DR; API `answer.language` (likely) | verified | RS, CR, PDF19, DET, CLI |
| `Mcq Answer` / `Subjective Answer` | per_question | XL [Res] (format undocumented) | verified | RS |
| `Candidate Detail Answer` | per_question | XL [Res]: "Detailed Answers given by the candidate in their attempt". Could mean code and answers or onboarding answers (ambiguous) | verified | RS |
| `Submitted Response (Question)` | per_question | CR-Q; API `questions.{qid}.answer` (the prose says `answers`; `{code, language}` shape likely); PDF "CANDIDATE ANSWER"; DR | verified | CR, API, PDF19, W-DET19, CLI |
| `Lines Of Code` / `Compile And Test` | per_question | XL [Res]; CR-Q "Lines of Code" / "Compile and Test Count" | verified | RS, CR, W-RP |
| `Test Case Is Passed` | per_question | XL [Res]; CR-Q "Test Case(s) Pass Results" (encoding undocumented), "Total Test Cases", "Passed Test Cases"; API+ `questions.submission_result`; DR/PDF status "Success", "Terminated due to timeout", "Runtime Error" | verified | RS, CR, API, PDF19, ERR, W-KEY |
| `Test Case Time` / `Test Case Memory` | per_question | XL [Res] (Report Settings definitions are wrong); CR-Q "Test Case Execution Time (s)" / "Test Case Memory (KB)"; DR/PDF "TIME TAKEN" / "MEMORY USED" | verified | RS, CR, PDF19 |
| Test-case table | per_question | DR; PDF (TESTCASE, DIFFICULTY, TYPE, SCORE); WH `testcase_metadata`. Sample cases score 0 points now (2 points in a 2019 PDF) | verified | PDF19, DET, WH, CODQ |
| `questions.solves` / `questions.submission_result` | per_question | API+ (requesting `questions.submissions` returns `solves`); WH `submissions`. Item fields (`id, answer, score, is_valid, created_at, metadata`) come from a practitioner client (likely) | verified | API, WH, CLI, BL1 |
| `questions.time_to_start_typing` / `questions.total_typing_time` | per_question | API+ only | verified | API |
| Code Playback | per_question | DR only (keystroke replay for coding questions); not exported | verified | DET, AIP, ERR, RN2601, BL1 |
| AI transcript and project artifacts | per_question | DR; API+ `ai_transcript_url`, `initial_repo_url`, `solution_zip_url`, `scoring_output_url`, `code_diff_url`, `ide_signals_url` | verified | AIT, PROJ, API, RN2607, W-DET19 |
| Evaluation Status, report messages, Output Diff | per_question | DR; fixed messages such as "No code was submitted. The last compiled version is displayed to the user"; status values undocumented | verified | DET, ERR, MOSS |
| `Integrity Issues` | integrity | New SR; CL filter with red triangle; new PDF; API `integrity_status` "High" / "Medium" / "No Issues Detected" / null (filterable); ATS None / Medium / High; no documented XL column | verified | IRP, SUM, CL, API, EF, SRC, RN2601, RN2604 |
| `Integrity Summary` | integrity | XL+ (free text); SR evidence bullets plus "View Session Replay"; new PDF; API `integrity_summary`; ATS | verified | ISS, IRP, SUM, API, EF, SRC, RN2607 |
| Integrity override | integrity | SR edit icon plus a required reason → status becomes "No issues detected"; the summary records who overrode it and why | verified | SUM, RN2601, MOSS |
| Suspicious-activity filters | integrity | CL filters: Code similarity, Copy paste, Tab switch, Multiple faces detected, No face detected, Suspicious activity, Secure mode violation; old SR "Suspicious Activity Detected" tile | verified | CL, SUM, MOSS, DET, W-SUM22 |
| `Attempt Plagiarism` | integrity | XL [Att] Yes/No (matches inside and outside the test); CR-A "Is Attempt Plagiarised"; API `plagiarism_status` (deprecated); WH `data.plagiarism`; ATS "Plagiarism Status" / "Plagiarism check" / "Plagiarism" | verified | RS, PBP, MOSS, CR, API, WH, GH, LV, EF |
| `Question Plagiarism` | integrity | XL [Res]; CR-Q "Is Plagiarism Detected (Question)"; API `questions.{qid}.plagiarism`; PDF marks flagged scores | verified | RS, CR, API, MOSS, PBP |
| `Plagiarism Percent` | integrity | XL [Res]; API `plagiarism.similarity`, `plagiarism_details.max_code_similarity`, deprecated `max_code_similarity`; DR "match percentage" | verified | RS, MOSS, PBP, API, RN2607 |
| `Max Plagiarism Probability (Question)` | integrity | CR-Q; scale undocumented | verified | CR, HRB |
| `Plagiarism Link` | integrity | XL [Res] (described as "Link to the webpage from where the candidate plagiarised the answer"); CR-A "Plagiarism Report URL" | verified | RS, CR |
| Plagiarism matches (who matched whom) | integrity | DR Suspicious Activity tab. The legacy table had MATCH, DATE, CANDIDATE, TEST and View Diff, marked matches in the same test "(same test)", and showed other companies' candidates as "Protected". New SR "Detected code similarity with other submissions". API `questions.{qid}.plagiarism {solve_id, test_candidate_id, email, similarity}`. Not in XL or CR | verified | API, API19, W-PLAG, W-AIP23, IRP, SUM, MOSS |
| `questions.plagiarism_details` | integrity | API+ `suspicion_score`, `suspicion_category`, `max_code_similarity`, `suspicion_reason`, reference-taking intervals (key likely); WH `data.plagiarism_details` | verified | API, API26, AIP |
| `plagiarism` / `plagiarism_status` / `max_code_similarity` | integrity | API, deprecated in favour of `integrity_status` / `integrity_summary` | verified | API, API19, API23 |
| `Attempt Activity Suspicion Severity` / `Attempt Activity Suspicion Score` / `Attempt Activity Reasons` | integrity | XL+ (High/Medium; score 0–10 with 8–10 = high; reasons "Large copy-paste events", "Suspicious code resets"; in 2023: "Coding Patterns", "Code Similarity"); CR-A "Plagiarism Suspicion Level / Score / Reasons (Attempt)"; API `suspicion_*` | verified | ISS, CR, W-AIP23, RN2601, API |
| AI plagiarism flag (report view) | integrity | SR: High/Medium; reasons Code Similarity and Coding Patterns (Suspicious Code Resetting, External Copy-Paste); "View in Codeplayer"; coding questions only | verified | AIP, W-AIP23, HRB |
| `Suspicion Category` | integrity | CR-A; API `suspicion_category`; values undocumented | verified | CR, API |
| `Copy Paste Frequency` | integrity | XL [Res]; CR-A "Paste Count"; API `editor_paste_count` (needs `track_editor_paste`); CL filter "Copy paste" | verified | RS, CR, API, PRT, PBP, BPI, RN2504, RN2510 |
| Pasted content | integrity | SR Timeline / Attempt Activity; DR paste marker; API+ `attempt_events` type 9 (`pasted_text`, character and line counts, truncation flag) | verified | PRT, PBP, API |
| `Is Question Leaked` | integrity | CR-QS; Tests-tab export "Total Leaked Questions"; API-T `disable_leakage_protection` | verified | CR, LEAK, RN2410, EXPT, API |
| `Number Of Window Exits` | proctoring | XL [Att] (header confirmed in the export screenshot); CR-A "Out-of-Window Events"; API `out_of_window_events` (needs `candidate_tab_switch`); `attempt_events` 7/8; SR "Candidate switched tabs" with time ranges; CL "Tab switch" | verified | RS, ISS, CR, API, PRT, PBP, IRP, RN2510, BPI |
| `Out Of Window Duration (seconds)` | proctoring | XL [Att]: Report Settings calls it "Out Of Window Duration", but the exported header adds "(seconds)" and values look like 62.0; CR-A "Out-of-Window Duration (s)"; API `out_of_window_duration` | verified | RS, ISS, CR, API, PBP, BPI |
| `Fullscreen Exit Count` / `Fullscreen Exit Duration` | proctoring | XL+ (integers, e.g. 3 / 927); CR-A same, plus "(s)" on the duration; API `secure_mode_exit_count` / `_duration` (needs `enable_secure_assessment_mode`); SR "Candidate exited full-screen mode"; CL "Secure mode violation" | verified | ISS, CR, API, IRP, SM |
| `Network Disconnected Duration` | proctoring | XL [Att]; CR-A "Disconnected Time (s)" | verified | RS, CR, IRP |
| `Proctor Flag` / `Proctor Flag Reasons` | proctoring | XL [Att]; values undocumented | verified | RS |
| `Proctor Activity Reasons` / `Proctor Activity Suspicion Severity` / `Proctor Activity Suspicion Score` | proctoring | XL+ in this order (e.g. "Almost no face was detected at all" / High / 10; otherwise N/A); CR-A "Suspicious Activity Reasons / Level / Score (Proctor)"; severity ranks Secondary face, then Multiple faces, then No face | verified | ISS, CR, W-IA23, BPI |
| Detected in webcam images (Image Analysis) | proctoring | SR: Multiple faces, Secondary faces, No face, Static face, phones/tablets (2026); up to 5 images per category; CL filters | verified | IMP, IRP, CL, BPI, RN2601, RN2607 |
| `proctor_images` / Photo ID | proctoring | API image URLs (needs `enable_proctoring`); SR Proctor and Photo ID sections. Photo ID is never checked automatically and cannot be combined with Image Proctoring. Images are kept for 30, 45 or 90 days | verified | API, IMP, PM, PID, CTS, HRI, W-PRT19 |
| Webcam gap messages | proctoring | SR / Session Replay: "Candidate was offline", "Video feed stopped sending frames", "Images could not be uploaded due to network issues", each with a duration | verified | IRP, RN2610 |
| Detected in screenshots | proctoring | Proctor Mode screenshots every 15 s (every 5 s near violations); SR and Session Replay; tool names also appear in the XL+ `Integrity Summary` | verified | PM, IRP, ISS, RN2607 |
| Conversation and gaze detection | proctoring | SR Integrity Summary: "Detected conversation in code editor" (AI Add-on, 2026); gaze detection is always reported as Medium (Jul 2026, limited availability) | verified | IRP, RN2601, RN2607 |
| Session Replay | proctoring | New SR only (Proctor Mode and Desktop App Mode); not exported | verified | IRP, PM, DAM, RN2507, RN2604, RN2610 |
| `Monitors Detected Count (Max)` / `Webcam Count` | proctoring | CR-A only; violations such as an extra monitor, screen mirroring or blocked apps appear in the SR Integrity Summary | verified | CR, SM, PM, DAM, RN2510 |
| `Status` | meta | XL [Att] (values undocumented; the UI shows To Evaluate / Passed / Failed for completed attempts); CR-A "Candidate Status" | verified | RS, DL, CL, SUM, CR |
| `status` / `ats_state` | meta | API `status`: −1 invited, 0 in progress, 1 completed with report pending, 7 report ready. API `ats_state` 0–22: 1 Evaluation Required, 2 Qualified, 3 Failed … 21 Hired, 22 Rejected. Also in WH; CR-A "ATS Status"; Workday/Ashby use "HackerRank Completed - Qualified" etc. | verified | API, STAT, WH, CR, WD, ASH |
| `Attempt Count` / `Recency Status (Invite / Attempt)` | meta | XL [Att] / CR-A; a re-invite archives the earlier attempt (API `force_reattempt`) | verified | RS, CR, RETK, API |
| `Detailed Report Url` | meta | XL [Att]; CR-A "Detailed Report URL"; API `report_url`, `authenticated_report_url`, `pdf_url` (returns 503 until the PDF is ready); WH (`pdf_url` uses an `/x/api/v2/` path); pattern `https://www.hackerrank.com/x/tests/{test_id}/candidates/{id}/report` | verified | RS, CR, API, WH, DL, RN2601 |
| `Invite Method` | meta | XL [Test]; CR-A (e.g. "invited via email, public link"); CL "Invited by" filter: Me / Public URL / Others; WH `data.invited` | verified | RS, CR, CL, WH |
| `Invited By Email` / `Invited By Name` | meta | XL [Test]; CR-A "Inviter Email" / "Inviter Name"; API `user`; WH `invite_details.invited_by`; PDF "Invited by:"; CL shows "Public URL" for public-link candidates | verified | RS, CR, API, WH, PDF19, ISS |
| `Is Invite Valid` | meta | CR-A (1/0); API `invite_valid`, `invite_email_done`, `invite_link` | verified | CR, API |
| `Tag` | meta | XL [Cand]; CR-A "Candidate Tags"; API `tags` | verified | RS, CR, API, INV |
| `Test ID` / `Test Name` / `Role` / `Test Tags` / `Test Owner Email` / `Test Owner Name` | meta | XL [Test]; CR-T (also "Target Role", "Required Experience"); API `test`; WH `data.tid`; PDF "Test Name:" | verified | RS, CR, API, WH, PDF19, SUM |
| `Company ID` / `Company Name`; `Department` / `Region` / `Team` / `Owner` | meta | XL [Co] / [Team]; CR-A "Team Department" / "Team Region" / "Team" / "Team Owner" | verified | RS, CR |
| `Is GDPR Deleted` | meta | CR-A | verified | CR, RN2410 |
| Comments (Recruiter/Team Comments) | meta | SR; DR; PDF ("No Comments." when empty); API+ `comments` | verified | PDF19, DET, API |
| Test integrity settings | meta | CR-T "Is Copy-Paste Tracked", "Is Tab Proctoring Enabled", "Is Photo Capture Enabled", "Is Image Proctoring Enabled", "Is Watermark Enabled", "Is Multi-Monitor Detection Enabled", "Is AI Plagiarism Detection Enabled"; API-T `track_editor_paste`, `candidate_tab_switch`, `enable_proctoring`, `enable_advanced_proctoring`, `enable_secure_assessment_mode`, `enable_ml_plagiarism_analysis`, `enable_photo_identification` | verified | CR, API, CTS, WM, PRT |
| Other Test API flags | meta | `enable_ai_proctor`, `enable_hackerrank_app`, `enable_live_face_detection`, `enable_candidate_screenshare`, `ai_proctor_frozen_flag` (names only) | likely | API |
| Test structure and scoring settings | meta | CR-T "Are Questions Shuffled", "Are Sections Shuffled", "Are Question Titles Hidden", "Test Section Count", "Questions per Section", "Current Test Version"; API-T `shuffle_questions`, `mcq_correct_score` / `mcq_incorrect_score` (example +4/−1), `experience` ("Intern", "New Graduate") | verified | CR, API, SEC, QSET, SCO, MCQ, CODQ, CERT |
| Webhook delivery | meta | API `test_result_url` (report is POSTed once processed; auth required) and `accept_result_updates` | verified | API, API23, ZAP |
| PDF report and ATS writebacks | meta | One PDF per candidate; the new version adds integrity and performance summaries; admins can block PDF downloads (Jan 2026). ATS integrations (Greenhouse, Lever, Workday, Ashby, Eightfold, SmartRecruiters, Unified.to) write 4–10 fields | verified | DL, PDF19, RN2601, GH, LV, WD, ASH, EF, SRC, UNI |
| Anonymized reports | meta | The Diversity & Inclusion setting can hide personal data in reports and downloads | likely | W-DI, HRU |

### 2.4 Example HackerRank export layout

The bundled generator (`generate_mock_data.py`) writes only the Workday format of [Part 1](#part-1---workday-assessment-export). This section is a reference for what a full HackerRank Candidates-tab export can contain, and a recipe for building HackerRank test files by hand. The console maps such a file as shown in [2.1](#21-how-the-console-reads-a-hackerrank-export).

**Assumed campus OA (illustrative values):**
- Test "Campus OA 2026 - SDE (Set A)", 90 minutes, taken through a public link.
- Two slots, each with a 15-minute login window.
- Section "Aptitude & CS Fundamentals": Q1–Q10, multiple-choice, +4 correct / −1 wrong, 40 points.
- Section "Coding": Q11 easy (50), Q12 medium (75), Q13 hard (100). Sample test cases are worth 0 points.
- Max Score 265; cutoff 60%, which is 159.
- About 1,200 completers from 6 colleges: 4 colleges sit in campus labs, 2 take it remotely. 10–20% score 100%.
- Two integrity profiles:
  - **baseline**: default tracking of tab switches, pastes and code similarity. XL+ columns are `N/A`.
  - **proctored**: Proctor Mode, AI plagiarism detection and Image Analysis switched on.

**Ordering and missing values:**
- Sections follow the Report Settings order (Candidate, Company, Test, Attempt Settings, Team, Attempt Result).
- Within a section, columns follow the order the doc lists them.
- Onboarding answers come right after the Candidate section.
- Proctor Mode columns follow the adjacency seen in the screenshots: Reasons, then Severity, then Score.
- The real column order is unverified, so any parser should match columns by header name.
- Missing integrity values are written `N/A`, as in the screenshot. Optional candidate details are left blank.

| # | Column header | Type | Example (100% scorer) | Generation notes |
|---|---|---|---|---|
| | **A · Candidate** | | | |
| 1 | `Email` | string | `aarav.mehta21@college-a.example` | Unique per completed attempt. Use the reserved `.example` domain. |
| 2 | `ID` | int (8 digits) | `90215533` | Increases with login order, with random gaps. Reused in the report URL. |
| 3 | `IP Address` | IPv4/IPv6 | `203.0.113.47` | Use documentation ranges only (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32). Lab colleges: 1–3 shared NAT IPs for 20–80 candidates. Remote: ~95% unique. Plant colluders who share an IP across colleges. |
| 4 | `Name` | string | `Aarav Mehta` | Record name. About 3% differ from `Full Name` (initials, case). |
| 5 | `Tag` | string | `campus-2026,college-a` | College or slot tags. Separator unverified. About 30% blank. |
| | **B · Onboarding details** (header = field title; placement unverified) | | | |
| 6 | `Full Name` | string | `Aarav Mehta` | As typed by the candidate (stray spaces, all caps). |
| 7 | `Student Roll Number` | string | `21BCE1045` | Pattern per college. Plant 1–2 roll numbers reused under different emails (proxy test-takers). |
| 8 | `Univ/College Name` | string | `College A Institute of Technology` | 3–6 spellings per college. This is the grouping key. |
| 9 | `Degree` | string | `B.Tech` | B.Tech 70%, B.E. 20%, MCA/M.Tech 10% (assumed). |
| 10 | `Stream/Branch` | string | `CSE` | Free-text variants of CSE, IT, ECE, EEE, ME. |
| 11 | `Graduation Year` | string | `2027` | About 95% 2027 for an autumn-2026 drive. |
| 12 | `CGPA` | string | `8.42` | Normal(7.8, 0.8), clipped to 5.0–9.9. About 5% messy (`8.42/10`, `84%`). |
| 13 | `Contact Number` | string | `+91 50000 41234` | Free format. Use a leading 5 (not used for Indian mobiles) so no real number is produced. Plant one number shared by two logins. |
| 14 | `Personal Email Address` | string | `aarav.m.dev@mail.example` | Different from the login email. Plant one address reused across two logins. |
| 15 | `Gender` | string | `Male` | Optional. Never use as an integrity feature. |
| 16 | `City` | string | `Vellore` | College city for lab sittings, home city for remote ones. |
| 17 | `Work Experience` | string | `< 1` | Options from the webhook sample; `< 1` about 97%. |
| | **C · Company** | | | |
| 18 | `Company ID` | int | `413522` | Constant. |
| 19 | `Company Name` | string | `Example Corp` | Constant. |
| | **D · Test** | | | |
| 20 | `Invite ID` | int | `55102938` | May be empty for public-link logins (unverified). |
| 21 | `Invite Method` | string | `public link` | Vocabulary unverified. |
| 22 | `Invite Valid From` | datetime | `N/A` | Email invites only: the slot opening time. |
| 23 | `Invite Valid To` | datetime | `N/A` | Slot opening + 15–30 minutes. |
| 24 | `Invited At` | datetime | `N/A` | The UI shows "-" for public-link candidates. |
| 25 | `Invited By Email` | string | `N/A` | Filled for email invites only. |
| 26 | `Invited By Name` | string | `N/A` | Filled for email invites only. |
| 27 | `Role` | string | `Software Engineer - New Grad` | Constant. |
| 28 | `Test Duration` | int (min) | `90` | Constant. |
| 29 | `Test ID` | int (7 digits) | `1734512` | One per test version. |
| 30 | `Test Name` | string | `Campus OA 2026 - SDE (Set A)` | |
| 31 | `Test Owner Email` | string | `ta.campus@corp.example` | Constant. |
| 32 | `Test Owner Name` | string | `Campus TA` | Constant. |
| 33 | `Test Tags` | string | `campus,2026,sde` | Constant. |
| | **E · Attempt Settings** | | | |
| 34 | `Attempt Count` | int | `1` | 2 for about 1% (re-invites; the row shows the latest attempt). |
| 35 | `Attempt Endtime` | datetime | `2026-09-12T05:52:40+0000` | Start + time taken; at most start + duration + added time. |
| 36 | `Attempt ID` | int | `90215533` | Assume it equals `ID` (unverified). |
| 37 | `Attempt Plagiarism` | Yes/No | `No` | Yes if any question is flagged. About 15–20% overall; more for leaked questions. |
| 38 | `Attempt Start time` | datetime | `2026-09-12T04:33:05+0000` | API-style ISO timestamp; the Excel format and timezone are unverified. About 70% start within 5 minutes of the slot opening. |
| 39 | `Attempt Time Taken` | int (seconds; unit unverified) | `4775` | 100% scorers: median about 75 minutes. Must be ≥ the sum of per-question times. |
| 40 | `Cutoff Score` | number | `159` | Constant. |
| 41 | `Detailed Report Url` | url | `https://www.hackerrank.com/x/tests/1734512/candidates/90215533/report` | Built from Test ID and ID. |
| 42 | `Feedback Comments` | text | (blank) | About 85% blank. |
| 43 | `Last Attempt Date And Time` | datetime | `2026-09-12T04:33:05+0000` | Start of the latest attempt (assumed). |
| 44 | `Max Score` | number | `265` | Constant. |
| 45 | `Network Disconnected Duration` | number (s) | `0` | 0 for about 88%. Add bursts shared by a whole lab at the same time; these are innocent. |
| 46 | `Number Of Window Exits` | int | `0` | 0 for 65%, 1 for 20%, 2–5 for 13%, more for 2%. |
| 47 | `Out Of Window Duration (seconds)` | float, 1 decimal | `0.0` | Per exit lognormal with median about 8 s. Flagged attempts total about 65 s at the median. |
| 48 | `Percentage Score` | float | `100` | Score ÷ Max × 100, 2 decimals. |
| 49 | `Proctor Flag` | string | `N/A` | Format unverified. |
| 50 | `Proctor Flag Reasons` | string | `N/A` | Face-anomaly wording (unverified). |
| 51 | `Product Rating` | int | `4` | Assume 1–5, skewed high; about 30% blank. |
| 52 | `Score` | float | `265` | Sum of question scores. |
| 53 | `Status` | string | `Passed` | Passed at or above the cutoff, otherwise Failed (strings unverified). |
| | **F · Proctor Mode / AI plagiarism** (`N/A` unless the feature ran) | | | |
| 54 | `Fullscreen Exit Count` | int | `0` | Secure, Proctor or Desktop App mode only. 2 or more exits → High. |
| 55 | `Fullscreen Exit Duration` | int (s) | `0` | A single exit longer than 10 s is flagged. |
| 56 | `Proctor Activity Reasons` | string | `N/A` | `Almost no face was detected at all`, `Secondary face`, `Multiple faces`. Invigilated labs produce many innocent multiple-face flags. |
| 57 | `Proctor Activity Suspicion Severity` | High/Medium/N/A | `N/A` | |
| 58 | `Proctor Activity Suspicion Score` | 10, 5 or N/A | `N/A` | |
| 59 | `Attempt Activity Reasons` | string | `N/A` | `Large copy-paste events`, `Suspicious code resets`. Column order copied from the Proctor Activity group. |
| 60 | `Attempt Activity Suspicion Severity` | High/Medium/N/A | `N/A` | Needs AI plagiarism detection on. |
| 61 | `Attempt Activity Suspicion Score` | 0–10 or N/A | `N/A` | High = 8–10. |
| 62 | `Integrity Summary` | text | `N/A` | Reuse the screenshot sentences: "Candidate exited full-screen mode and switched tabs in violation of Proctor Mode rules.", "Candidate wasn't visible in the webcam for a total of 27 min". Add tool names (ChatGPT, InterviewCoder, AnyDesk). |
| | **G · Team** | | | |
| 63–66 | `Department`, `Region`, `Team`, `Owner` | string | `Engineering`, `APAC`, `Campus Hiring`, `Campus TA` | Constants. |
| | **H · Attempt Result, attempt level** | | | |
| 67 | `Copy Paste Frequency` | int | `0` | Attempt total; a real file might split it per question. Baseline mode: 0 for about 70%. Secure and Proctor modes block pasting, so about 0. |
| 68 | `Question Type Score` | string | `MCQ: 40/40; Coding: 225/225` | Cell format unverified; the webhook uses "scored/max". |
| 69 | `Section Score` | string | `Aptitude & CS Fundamentals: 40/40; Coding: 225/225` | Same format. |
| 70 | `Skill Score` | string | `Problem Solving (Intermediate): 225/225; CS Fundamentals: 40/40` | Same format. |
| 71 | `Tag Score` | string | `Arrays: 50/50; Graphs: 100/100; Hard: 100/100` | Difficulty labels count as tags. |
| | **I · Per question, multiple-choice (repeat for n = 1…10, grouped by question)** | | | |
| 72–111 | `Q{n} - Question Score` | number | `4` | +4 / 0 skipped / −1 wrong. Draw correctness from an item-response model. |
| | `Q{n} - Question Answered` | Yes/No | `Yes` | About 3% skipped outside the 100% group (could be TRUE/FALSE). |
| | `Q{n} - Time Taken Per Question` | int (s) | `52` | Lognormal, median 45 s. |
| | `Q{n} - Mcq Answer` | string | `C` | Letter(s), e.g. `A,C` for multi-select (format unverified). Skew wrong-answer popularity so partial scorers share wrong answers. |
| | **J · Per question, coding (repeat for n = 11…13)** | | | |
| 112–141 | `Q{n} - Question Score` | number | `100` | Sum of passed hidden-test-case points: Q11 10×5, Q12 15×5, Q13 10×10. |
| | `Q{n} - Question Answered` | Yes/No | `Yes` | |
| | `Q{n} - Time Taken Per Question` | int (s) | `2140` | Lognormal medians 12, 22 and 35 minutes. |
| | `Q{n} - Code Language` | string | `cpp` | C++ 50%, Java 25%, Python 20%, C 5% (assumed). Slug vs display name unverified. |
| | `Q{n} - Lines Of Code` | int | `54` | Normal distribution per language and question. Members of a collusion ring land within ±2 lines of each other. |
| | `Q{n} - Compile And Test` | int | `7` | Poisson with mean 3 / 5 / 8 by difficulty; 0–1 for pasted code. |
| | `Q{n} - Test Case Is Passed` | string | `1;1;1;1;1;1;1;1;1;1` | Pass vector (encoding unverified). |
| | `Q{n} - Question Plagiarism` | Yes/No | `No` | Yes at ≥75% similarity (10+ lines of code) or ≥90% (fewer than 10 lines). |
| | `Q{n} - Plagiarism Percent` | 0–100 | `41` | Honest answers 15–70; Q11 is easy, so honest answers reach 60–90. Colluders 85–99. Add a switch that blanks values below the threshold, since it is unknown whether unflagged answers carry a value. |
| | `Q{n} - Plagiarism Link` | url | (blank) | Only filled when flagged. |
| | **K · Optional columns TA might append** (not in HackerRank's file) | | | |
| 142 | `Integrity Issues` | High / Medium / No issues detected | `No issues detected` | Copied from the Candidates-list filter or the ATS (where it reads `None`). |
| 143 | `Code Quality` | A/B/C/Not Generated | `A` | AI Add-on only. |

Full header row:

```csv
Email,ID,IP Address,Name,Tag,Full Name,Student Roll Number,Univ/College Name,Degree,Stream/Branch,Graduation Year,CGPA,Contact Number,Personal Email Address,Gender,City,Work Experience,Company ID,Company Name,Invite ID,Invite Method,Invite Valid From,Invite Valid To,Invited At,Invited By Email,Invited By Name,Role,Test Duration,Test ID,Test Name,Test Owner Email,Test Owner Name,Test Tags,Attempt Count,Attempt Endtime,Attempt ID,Attempt Plagiarism,Attempt Start time,Attempt Time Taken,Cutoff Score,Detailed Report Url,Feedback Comments,Last Attempt Date And Time,Max Score,Network Disconnected Duration,Number Of Window Exits,Out Of Window Duration (seconds),Percentage Score,Proctor Flag,Proctor Flag Reasons,Product Rating,Score,Status,Fullscreen Exit Count,Fullscreen Exit Duration,Proctor Activity Reasons,Proctor Activity Suspicion Severity,Proctor Activity Suspicion Score,Attempt Activity Reasons,Attempt Activity Suspicion Severity,Attempt Activity Suspicion Score,Integrity Summary,Department,Region,Team,Owner,Copy Paste Frequency,Question Type Score,Section Score,Skill Score,Tag Score,Q1 - Question Score,Q1 - Question Answered,Q1 - Time Taken Per Question,Q1 - Mcq Answer,Q2 - Question Score,Q2 - Question Answered,Q2 - Time Taken Per Question,Q2 - Mcq Answer,Q3 - Question Score,Q3 - Question Answered,Q3 - Time Taken Per Question,Q3 - Mcq Answer,Q4 - Question Score,Q4 - Question Answered,Q4 - Time Taken Per Question,Q4 - Mcq Answer,Q5 - Question Score,Q5 - Question Answered,Q5 - Time Taken Per Question,Q5 - Mcq Answer,Q6 - Question Score,Q6 - Question Answered,Q6 - Time Taken Per Question,Q6 - Mcq Answer,Q7 - Question Score,Q7 - Question Answered,Q7 - Time Taken Per Question,Q7 - Mcq Answer,Q8 - Question Score,Q8 - Question Answered,Q8 - Time Taken Per Question,Q8 - Mcq Answer,Q9 - Question Score,Q9 - Question Answered,Q9 - Time Taken Per Question,Q9 - Mcq Answer,Q10 - Question Score,Q10 - Question Answered,Q10 - Time Taken Per Question,Q10 - Mcq Answer,Q11 - Question Score,Q11 - Question Answered,Q11 - Time Taken Per Question,Q11 - Code Language,Q11 - Lines Of Code,Q11 - Compile And Test,Q11 - Test Case Is Passed,Q11 - Question Plagiarism,Q11 - Plagiarism Percent,Q11 - Plagiarism Link,Q12 - Question Score,Q12 - Question Answered,Q12 - Time Taken Per Question,Q12 - Code Language,Q12 - Lines Of Code,Q12 - Compile And Test,Q12 - Test Case Is Passed,Q12 - Question Plagiarism,Q12 - Plagiarism Percent,Q12 - Plagiarism Link,Q13 - Question Score,Q13 - Question Answered,Q13 - Time Taken Per Question,Q13 - Code Language,Q13 - Lines Of Code,Q13 - Compile And Test,Q13 - Test Case Is Passed,Q13 - Question Plagiarism,Q13 - Plagiarism Percent,Q13 - Plagiarism Link,Integrity Issues,Code Quality
```

**Scenarios to plant.** Record the true labels in a separate file, never in the CSV.
1. **Innocent lab cluster.** 30–80 candidates on one NAT IP, overlapping attempt windows, independent code. This should not be flagged.
2. **Collusion ring of 3–5 people.** All score 100%. Q12 and Q13 show 85–99% similarity and are flagged. They use the same language, and their line counts are within ±2. Followers finish each question 3–10 minutes after the ring's source candidate, with fewer compile runs, and leave the window more (30–120 s).
3. **Leaked Q11.** 25–40% of second-slot candidates match one canonical solution at 90% or more, with short solve times and 0–1 compile runs.
4. **One candidate with outside help.** Attempt Activity reasons are set, paste counts are high in baseline mode, and tool names appear in the proctored Integrity Summary.
5. **Proxy test-taker.** One roll number, phone or personal email appears under two logins, plus a `Secondary face` flag.
6. **Honest convergence on Q11.** Many honest answers reach 80–90% similarity. This is the false-positive trap.

**Companion files (optional):**
- `question_attempts.csv`, one row per candidate per question, using the Candidate Question Attempt (CR-Q) labels:
  - Keys: `Attempt ID (Question Attempt)`, `Candidate ID (Question Attempt)`, `Question ID (Attempt)`, `Section Name`.
  - Timing and effort: `Time per Question (s)`, `Lines of Code`, `Compile and Test Count`, `Code Language`.
  - Scores and test cases: `Question Score`, `Question Max Score`, `Is Question Answered`, `Solution Status`, `Test Case(s) Pass Results`, `Total Test Cases`, `Passed Test Cases`, `Test Case Memory (KB)`, `Test Case Execution Time (s)`.
  - Code and plagiarism: `Submitted Response (Question)`, `Is Plagiarism Detected (Question)`, `Max Plagiarism Probability (Question)`.
- If TA has API access, add `attempt_events.jsonl` (event codes 1–10, including `pasted_text`) and `plagiarism_edges.csv` (`test_candidate_id`, `email`, `similarity`, `solve_id`).

#### Signals not in the bulk export

The Workday assessment export carries HackerRank's overall Integrity Issues and Integrity Summary inside its Assessment Comment ([1.4](#14-the-assessment-comment)), so for Workday exports the first two rows below are already covered.

| Signal | Where it lives | Would TA plausibly add it? |
|---|---|---|
| Integrity Issues (overall) | Candidates list, new Summary Report, PDF, API, ATS | **High.** It is a one-click filter, and HackerRank's own walkthrough exports only High and Medium rows. |
| Integrity Summary (non-Proctor tests) | Summary Report, PDF, API | Medium. It is already exported in Proctor Mode tests. |
| Code Quality | Candidates list, Summary Report, PDF, API | Medium. It is a visible tie-breaker for 100% scorers, but AI Add-on only. |
| Who matched whom (partner email, %, "(same test)") | Detailed Report, Summary Report, API | Low through the UI (one candidate at a time). Medium if TA has API access. This is the most useful collusion field, so ask for it for flagged 100% scorers. |
| Event timelines (tab-switch time ranges, pasted text, submit times) | Summary Report, Session Replay, API `attempt_events` | Low through the UI; Medium through the API. |
| Typing metrics and submission history | API only | Low without API access. |
| Code Playback, Session Replay, webcam images, screenshots, Photo ID | Report only (images are available as API URLs) | Very low because of volume and personal data. Expect at most a reviewer-verdict column. |
| Benchmark, Optimality, Performance Summary, AI Fluency | Summary Report / PDF | Low. Benchmark is the same for everyone at 100%. AI Fluency applies only to AI-assisted tests. |
| Override reasons, reviewer comments | Summary Report, Detailed Report, PDF, API | Medium, as a notes column. |
| Fields only in Custom Reports: Monitors Detected Count (Max), Webcam Count, Suspicion Category, Max Plagiarism Probability, Is Question Leaked, Submitted Response | Custom Reports | Medium, if TA has Insights or admin access. |
| Taken On (with timezone) | PDF / Summary Report | Low, but useful for settling which timezone the timestamps are in. |

### 2.5 Integrity signals

Among 100% scorers, every outcome field is saturated. Score equals Max Score, every test case passes, and every `Mcq Answer` equals the key. So multiple-choice patterns and test-case pass vectors carry no signal inside this group; they only help with partial scorers (shared wrong answers, identical failure patterns). The evidence has to come from how and when the answers were produced, and from HackerRank's own flags.

| Signal family | Fields (where) | How to use it on 100% scorers | Thresholds and pitfalls |
|---|---|---|---|
| HackerRank similarity flags | `Attempt Plagiarism`; per question `Question Plagiarism`, `Plagiarism Percent` (XL); CR-Q plagiarism fields | Compare the per-question flag rate inside the 100% group with the rest. A question flagged for most candidates points to a leaked or too-easy question, so discount it. Otherwise, send flagged 100% scorers to review. | Flag at ≥75% similarity (10+ lines of code) or ≥90% (fewer lines). Bands: High ≥90, Medium 80–90. Per-difficulty guidance: 90 / 80–90 / 75 for easy / medium / hard. Thresholds became language-aware in Jul 2026. Excel gives only the highest % per question, not who the match was. |
| Who matched whom | Detailed Report match list ("(same test)"); API `questions.{qid}.plagiarism` | Build a graph with candidates as nodes and same-test matches above threshold as edges. Groups linked on 2 or more questions are likely rings. A star around one candidate suggests a shared source. Matches outside the test suggest a leak. This is the strongest single collusion signal. | Not in Excel or Custom Reports. Matches with other companies' candidates show as "Protected". |
| Your own code similarity | `Submitted Response (Question)`; API `answer`; PDF | Normalize the code (strip comments and whitespace, standardize identifiers), then compare per question using token or syntax-tree similarity. Plant a decoy containing the known online solution, as HackerRank suggests. Same language plus line counts within ±2 sharpen clusters. | Short, easy problems converge naturally. Compare within the same language. Needs privacy sign-off. |
| Time co-occurrence | `Attempt Start time`, `Attempt Endtime`, `Time Taken Per Question`; API `attempt_events` 3/4/5 | Look for overlapping attempt windows (HackerRank's code-similarity checker itself uses them). Look for leader-follower patterns: B finishes each question a steady few minutes after A, with near-identical code. Look for correlated per-question time profiles. Compare slots: a higher 100% rate and faster solves in the later slot point to a leak between slots. | In a single slot everyone overlaps, so rely on per-question timing. Question shuffling changes the order questions are seen. |
| Effort anomalies | `Time Taken Per Question`, `Compile And Test`, `Lines Of Code`; `Recommended Time (min)`; API typing times and submissions | Full marks on the hard question far below the recommended time. Zero or one compile runs, or a single submission. Typing time much shorter than the code length implies. A long idle period followed by a burst. A late switch of language with a working solution. | Strong students are also fast. These point to outside help more than collusion. |
| Pastes and leaving the window | `Copy Paste Frequency`, `Number Of Window Exits`, `Out Of Window Duration (seconds)`, `Fullscreen Exit Count/Duration`, `Network Disconnected Duration`; API events 7/8/9 | HackerRank's rule: 2 or more exits, or a single exit longer than 10 s. Identical `pasted_text` across candidates means a shared source. Tab-switches at the same minutes inside a cluster. Disconnects that line up with webcam "offline" gaps. | Flagged attempts have a median of 1 exit and 65 s away. A 0 can mean tracking was off. Pasting is blocked in Secure, Proctor and Desktop App modes. Lab Wi-Fi drops cluster innocently. |
| AI plagiarism model | `Attempt Activity Suspicion Severity / Score / Reasons`; CR-A; API `plagiarism_details` | Review queue: High with a score of 8–10. The reasons "Large copy-paste events" and "Suspicious code resets" indicate outside help. Combine with the match graph before calling it collusion. | 85% precision, weak on short solutions, coding questions only. HackerRank advises against automatic rejection. The "reference taking" reason was removed in Jan 2026, yet the export screenshot still shows "referencing the answer from external source". |
| Proctoring | `Proctor Activity *`, `Integrity Summary`, `Proctor Flag`; `Monitors Detected Count (Max)`, `Webcam Count` | A secondary face suggests impersonation. Tool names (ChatGPT, InterviewCoder, AnyDesk, TeamViewer, Discord, WhatsApp) suggest outside help or live collusion. "Conversation in code editor" suggests chatting with a helper. Watch for more than one monitor or webcam. | Needs Image Analysis or Proctor Mode (AI Add-on). Ignore multiple faces in invigilated halls. Photo ID is never checked automatically. |
| Network and identity | `IP Address`; onboarding roll number, college, city, personal email, phone | A shared IP matters when it links 100% scorers from different colleges, cities or homes; inside one lab it is expected. The same roll number, phone or personal email under two logins suggests a proxy. Use college and slot to set the expected-overlap baseline. | One IP per attempt and no device or browser data. Mobile carriers' shared addresses and IPv6 changes add noise. |
| Leak context | `Is Question Leaked`, `Question Source`; "Total Leaked Questions"; shuffling and hidden-title settings; login window | Discount evidence on leaked library questions. Compare the 100% rate by slot and by question. | Long login windows saw "upwards of 50%" of candidates flagged. Recommended windows: 15–20 minutes for up to 10,000 candidates, 30 minutes above that. |

**Workflow**
1. Read the test settings first (paste tracking, tab tracking, AI plagiarism, integrity mode). Mark columns as "not collected" rather than "clean".
2. Select candidates with `Percentage Score` = 100, optionally widening to ≥95.
3. Compute per-question flag and match rates. Set aside leaked questions and questions where honest answers naturally converge.
4. Build match edges, from HackerRank's list or the API, or otherwise from your own similarity on submitted code. Cluster them.
5. For each cluster, overlay timing, IPs outside the lab baseline, synchronized window exits, identical pastes, effort anomalies and proctoring flags.
6. Call it "likely collusion" only when at least 2 independent families agree, for example similarity plus timing, or similarity plus network. A single family means "review".
7. Review through `Detailed Report Url` (code playback, match diffs, Session Replay) and record reasons for any override. Do not reject automatically.

For clean 100% scorers, the tie-breakers are `Attempt Time Taken` and `Code Quality`.

### 2.6 Unverified / caveats

**Unverified (speculative, with no second source):**
- How the Candidates-tab Excel lays out per-question data. It could use question titles or `Q1`, could group by question or by field, and may or may not give test cases their own columns. The `Q{n} - <label>` pattern above is a working convention.
- Whether any bulk export has an `Integrity Issues` column, or `Code Quality` / `Benchmark` / `AI Fluency` columns.
- Whether onboarding answers appear as Excel columns, and under which header. What `Candidate Detail Answer` actually contains.
- Column order, number of sheets, and file type. The UI says Excel, Report Settings says "Microsoft Excel CSV", and the Jan 2026 notes say CSV.
- Value vocabularies: `Status`, `Invite Method`, `Proctor Flag` and its reasons, `Mcq Answer`, `Question Answered`, `Solution Status`, `Suspicion Category`, `Test Case(s) Pass Results`, `Gender` options, Evaluation Status, `Optimality` scale.
- Units and formats in the Excel: `Attempt Time Taken`, `Time Taken Per Question`, `Network Disconnected Duration`, timestamps and timezone, and the four split-score cells.
- Scales of API `similarity`, per-question `suspicion_score` and `Max Plagiarism Probability (Question)`.
- What these mean or point to: the `Plagiarism Link` target, `Test Severity Level (Candidate Rated)`, webhook `data.rating` and `data.test_candidate_id`, the Country API key, the LinkedIn export label, how Resume is represented, whether exports reflect overrides, and whether `Plagiarism Percent` is filled for unflagged answers.
- Campus custom fields such as "10th %", "12th %" and "Active Backlogs" (illustrative only).

**Likely (one practitioner source, or inferred):**
- A top-level `attempt_id`, and `questions` returned as `{qid: score}` on list endpoints.
- `answer {code, language}` and the submission item fields.
- `scores_sections_split` and the reference-taking interval key.
- Excel `ID` = API `id` = `Attempt ID`.
- Lever's 1–4 buckets; the Diversity & Inclusion anonymization setting reaching exports.
- `Name` is the record name while `Full Name` is what the candidate typed.
- The undocumented Test flags.
- Typical campus formats: 2–3 coding questions, sometimes plus multiple-choice, 60–90 minutes, cutoffs of 50–70% (TARO, BL2, W-GS21).

**Conflicts resolved:**
- `Max Score`: Report Settings describes it as the candidate's maximum; it is the test maximum.
- `Test Case Time` / `Test Case Memory`: Report Settings definitions are wrong; they are execution time in seconds and memory in KB.
- The export header is "Out Of Window Duration (seconds)", not the Report Settings label.
- Proctor score: 10 / 5 now. The 2023 bands were 8–10 / 5–7 / <5.
- Webcam capture interval: 5 s vs 15 s (docs differ; once a minute in 2019).
- No-issues status is written "No issues detected" in the UI, "No Issues Detected" in the API, and "None" in ATS writebacks. The unofficial client's demo values (`clean`, `review`, `Low`) are invented.
- Sample test cases score 0 points now; a 2019 PDF showed 2.
- `added_time` is a string vs an integer; `id` is a string vs an integer; the schema says `answer` while the prose says `answers`.
- AI plagiarism detection: 85% precision (KB) vs 93% accuracy (blog).

**Read zeros carefully:**
- Paste and tab tracking have been on by default since Oct 2025.
- AI plagiarism, Image Proctoring/Analysis, Photo ID and Secure Mode are off unless someone enabled them.
- Proctor Mode, Desktop App, Code Quality, Optimality, AI Fluency and the 2026 detections need the AI Add-on. Proctor Mode only works on tests created after Jul 2025.
- Report Settings are per user, so two TA exports can differ. Admins can restrict the attempt export (Apr 2025) and PDF downloads (Jan 2026).
- Only completed candidates are exported. A file built with HackerRank's walkthrough contains only High and Medium rows, so it is a biased sample.
- Re-invites archive earlier attempts. GDPR-deleted or anonymized rows can lose personal data.
- Changing the cutoff does not re-grade, and Greenhouse does not re-sync edited scores.
- Webcam images expire after 30, 45 or 90 days.
- The legacy Detailed Report was retired on 28 Jan 2026.
- Campus confounders: lab NAT IPs, invigilated multiple faces and Wi-Fi drops look suspicious without being so.

**Calibration numbers (soft, and no URL was captured for any of them):**
- A New York City bias audit of the plagiarism system (5,844 candidates) found 14–24% flagged.
- HackerRank marketing says "25% of technical assessments show signs of plagiarism".
- An Atlassian case with 35,000 campus applicants saw false flags drop from 10% to 4%.

**Data handling:** IPs, phone numbers, webcam URLs and code are personal data. PDF19 is a real person's report; only its labels were used. Synthetic data should use reserved domains and IP ranges.

**Research limits:**
- No real Candidates-tab export was found, only the four column-group screenshots in KB 9728791902.
- Some sources could not be reviewed in full.
- The Internet Archive (Wayback Machine) could not be retrieved, including a 2019 KB screenshot ("Excel_Report.jpg") of a real export, so it was not reviewed.
- Lever's help page renders only with JavaScript.

## Sources

The KB articles below on support.hackerrank.com have mirrors under the same IDs at https://hackerrank-knowledge-base.help.usepylon.com/articles/

- RS: https://support.hackerrank.com/articles/5213013268-report-settings
- CR: https://support.hackerrank.com/articles/7355624862-create-custom-reports
- CRS: https://support.hackerrank.com/articles/8981360888-schedule-a-custom-report
- SUM: https://support.hackerrank.com/articles/8111267829-viewing-a-candidate%27s-test-summary-report
- DET: https://support.hackerrank.com/articles/8263794320-viewing-a-candidate%27s-detailed-test-report
- DL: https://support.hackerrank.com/articles/1786677446-downloading-pdf-and-excel-test-reports
- CL: https://support.hackerrank.com/articles/9135272973-candidates-list-in-hackerrank-tests
- FILT: https://hackerrank-knowledge-base.help.usepylon.com/articles/1829746072-filtering-and-sorting-candidate-test-reports
- ONB: https://support.hackerrank.com/articles/5956879724-configure-onboarding-settings-for-tests
- IRP: https://support.hackerrank.com/articles/9607697321-review-integrity-issues-in-proctor-mode
- ISS: https://support.hackerrank.com/articles/9728791902-interpreting-integrity-signals-at-scale-in-proctor-mode-tests
- MOSS: https://support.hackerrank.com/articles/2106056073-plagiarism-detection-using-moss-%28measure-of-software-similarity%29
- PBP: https://support.hackerrank.com/articles/4291690360-plagiarism-best-practices-guide ; https://support.hackerrank.com/hc/en-us/articles/5325206196883-Plagiarism-Best-Practices-Guide
- AIP: https://support.hackerrank.com/articles/7287334157-ai-plagiarism-detection ; https://support.hackerrank.com/articles/8000786908-ai-plagiarism-detection
- PRT: https://support.hackerrank.com/articles/1079706165-proctoring-hackerrank-tests
- BPI: https://support.hackerrank.com/articles/1878974014-best-practices-to-maintain-test-integrity ; https://support.hackerrank.com/hc/en-us/articles/5324947127187-Best-Practices-to-Maintain-Test-Integrity
- IMP: https://support.hackerrank.com/articles/7825915809-impersonation-detection
- PID: https://support.hackerrank.com/articles/3499319445-photo-identification
- PM: https://support.hackerrank.com/articles/5663779659-proctor-mode
- DAM: https://support.hackerrank.com/articles/5973590014-hackerrank-desktop-app-mode
- SM: https://support.hackerrank.com/articles/2545360734-secure-mode
- CTS: https://support.hackerrank.com/articles/6271327319-centralized-test-settings
- LEAK: https://support.hackerrank.com/articles/8820947031-managing-leaked-questions
- WM: https://support.hackerrank.com/articles/2519766322-watermarking-the-tests
- CQ: https://support.hackerrank.com/articles/9625818007-code-quality-evaluation
- OPT: https://support.hackerrank.com/articles/4755292353-optimality
- BEN: https://support.hackerrank.com/articles/6075450367-candidate-benchmarking
- AIU: https://support.hackerrank.com/articles/1773201418-ai-usage-summary
- AIT: https://support.hackerrank.com/articles/1152916770-ai-assisted-tests
- ERR: https://support.hackerrank.com/articles/1972468979-understanding-errors-in-post-assessment-reports-for-coding-questions
- PROJ: https://support.hackerrank.com/articles/3527033062
- GLOS: https://support.hackerrank.com/articles/3572240492-hackerrank-glossary
- SCO: https://support.hackerrank.com/articles/8294988738-modifying-score-settings-for-tests
- CUT: https://support.hackerrank.com/articles/6321836373-what-happens-to-candidate-statuses-if-the-cut-off-score-is-altered%3F
- STAT: https://support.hackerrank.com/articles/3094540490-candidate-status-in-api-results
- TAGQ: https://support.hackerrank.com/articles/9412060538-associating-tags-to-questions
- EXPT: https://support.hackerrank.com/articles/7081156839-exporting-tests-data
- INS: https://hackerrank-knowledge-base.help.usepylon.com/articles/4945003466-account-insights
- TINS: https://hackerrank-knowledge-base.help.usepylon.com/articles/7964147006
- QINS: https://hackerrank-knowledge-base.help.usepylon.com/articles/3255724462
- INV: https://support.hackerrank.com/articles/6027855406-inviting-candidates-to-a-test
- BULK: https://support.hackerrank.com/articles/7127221409-sending-bulk-email-invites-to-a-test
- RETK: https://support.hackerrank.com/articles/4428221834-how-to-allow-candidates-to-retake-a-test%3F
- GEN: https://support.hackerrank.com/articles/9672590042-modifying-general-settings-for-tests
- TINV: https://support.hackerrank.com/articles/9544354489-configure-test-invites-settings-for-tests
- TIME: https://support.hackerrank.com/articles/4811403281-adding-extra-time-for-candidates
- MCQ: https://support.hackerrank.com/articles/2513748038-multiple-choice-questions
- QSET: https://support.hackerrank.com/articles/5424328799-modifying-question-settings-for-tests
- SEC: https://support.hackerrank.com/articles/5546852679-section-based-testing ; https://support.hackerrank.com/articles/2528926257-modify-sections-settings-for-tests
- CODQ: https://support.hackerrank.com/articles/2474669643-coding-question ; https://support.hackerrank.com/articles/3245197419-test-cases-in-coding-questions ; https://support.hackerrank.com/hc/en-us/articles/115006676188-Scoring-a-Coding-Question
- CERT: https://support.hackerrank.com/articles/8606775858-scoring-certified-assessments
- CAMP: https://support.hackerrank.com/articles/7220470055-support-for-campus-events ; https://support.hackerrank.com/articles/8754663182-what-is-the-maximum-number-of-candidates-i-can-assess-using-coding-assessments%3F
- SCR: https://support.hackerrank.com/articles/3584214233-best-practices-for-screening
- ZAP: https://hackerrank-knowledge-base.help.usepylon.com/articles/7100809601
- RN2410: https://support.hackerrank.com/articles/1189675016-october-2024-release-notes
- RN2504: https://support.hackerrank.com/articles/5686123513-april-2025-release-notes
- RN2507: https://support.hackerrank.com/articles/3121307537-july-2025-release-notes
- RN2510: https://support.hackerrank.com/articles/8474307750-october-2025-release-notes
- RN2601: https://support.hackerrank.com/articles/2321596225-january-2026-release-notes
- RN2604: https://support.hackerrank.com/articles/4368819843-april-2026-release-notes
- RN2607: https://support.hackerrank.com/articles/8142080826-july-2026-release-notes
- RN2610: https://support.hackerrank.com/articles/9093711123-october-2026-release-notes
- EF: https://support.hackerrank.com/articles/2155086316
- LV: https://support.hackerrank.com/articles/6548604920-lever-v2-hackerrank-integration-test-user-guide
- GH: https://support.hackerrank.com/articles/2499611070-greenhouse---hackerrank-integration-tests-user-guide- ; https://hackerrank-knowledge-base.help.usepylon.com/articles/3678461564
- SRC: https://support.hackerrank.com/articles/2880884436-smartrecruiters---hackerrank-integration-user-guide
- WD: https://hackerrank-knowledge-base.help.usepylon.com/articles/1999755553-workday---hackerrank-tests-user-guide
- ASH: https://hackerrank-knowledge-base.help.usepylon.com/articles/5999209869
- API: https://www.hackerrank.com/apidoc ; https://www.hackerrank.com/work/apidocs ; https://hrcdn.net/hackerrank/assets/x_apidocs_libraries-563a6558680742137f69577cf1400421924efac31824c0095f781ea27412aecf.js
- API19 / API23 / API26: https://web.archive.org/web/20190829135127/https://www.hackerrank.com/apidoc ; https://web.archive.org/web/20230407130425/https://www.hackerrank.com/apidoc ; https://web.archive.org/web/20260616192421/https://www.hackerrank.com/apidoc
- WH (official webhook sample payload): https://drive.google.com/file/d/1jIZzpQhe_NnzJcy7YoW7Jg1BTz18p_q_/view
- W-SUM22: https://web.archive.org/web/20220518065003/https://support.hackerrank.com/hc/en-us/articles/4408913281427-Viewing-a-Candidate-s-Test-Summary-Report
- W-DET19: https://web.archive.org/web/20190723160219/https://support.hackerrank.com/hc/en-us/articles/360012556773-Viewing-a-Candidate-s-detailed-Test-report
- W-RP: https://web.archive.org/web/20190823031102/https://support.hackerrank.com/hc/en-us/articles/115007545447-Report-preferences ; https://web.archive.org/web/20230128010622/https://support.hackerrank.com/hc/en-us/articles/115007545447-Report-Preferences
- W-KEY: https://web.archive.org/web/20200921054916/https://support.hackerrank.com/hc/en-us/articles/360042800834-Key-Components-in-Candidate-Report ; http://web.archive.org/web/20220628125833/https://support.hackerrank.com/hc/en-us/articles/360042800834-Key-Components-in-Candidate-Report
- W-DS: https://web.archive.org/web/20240224115035/https://support.hackerrank.com/hc/en-us/articles/360034682313-Data-Science-Test-Reports
- W-PLAG: https://web.archive.org/web/20220518064924/https://support.hackerrank.com/hc/en-us/articles/360033764253-How-to-Check-for-Plagiarism-in-HackerRank-Tests- ; http://web.archive.org/web/20211027103805/https://support.hackerrank.com/hc/en-us/articles/360033764253-How-to-check-for-Plagiarism-in-HackerRank-Tests
- W-AIP23: https://web.archive.org/web/20230925000638/https://support.hackerrank.com/hc/en-us/articles/16147827935379-How-to-Check-Suspicious-Activity-Using-the-AI-Powered-Plagiarism-Detection-Solution
- W-IA23: https://web.archive.org/web/20230924225710/https://support.hackerrank.com/hc/en-us/articles/17971376196627-Detect-Suspicious-Activity-Using-Image-Analysis
- W-PRT19: https://web.archive.org/web/20190823020633/https://support.hackerrank.com/hc/en-us/articles/360011479133-Proctoring-HackerRank-Tests
- W-ACC: https://web.archive.org/web/20231205175303/https://support.hackerrank.com/hc/en-us/articles/115006328228-Modifying-Test-Access-Settings
- W-GS21: https://web.archive.org/web/20210225021837/https://support.hackerrank.com/hc/en-us/articles/1500002299462-GS-Engineering-Campus-Hiring-Program-2021
- W-DI: https://web.archive.org/web/20231205193628/https://support.hackerrank.com/hc/en-us/articles/360036104154-Diversity-and-Inclusion-Settings-Page
- HRL: https://www.hackerrank.com/test-v2/sample/login ; https://www.hackerrank.com/tests/sample
- HRU: https://www.hackerrank.com/solutions/university-recruiting/
- HRI: https://www.hackerrank.com/about-us/image-detection-notice
- HRB: https://www.hackerrank.com/blog/hackerrank-launches-ai-powered-plagiarism-detection/ ; https://www.hackerrank.com/blog/how-plagiarism-detection-works-at-hackerrank/ ; https://www.hackerrank.com/blog/putting-integrity-to-the-test-in-fighting-invisible-threats/
- PDF19 (real 2019 report; labels only): https://pdfcoffee.com/efficient-janiator-pdf-free.html
- KIT: https://github.com/adamtheturtle/HackerRankKit/blob/main/Sources/HackerRankKit/TestCandidate.swift ; https://raw.githubusercontent.com/adamtheturtle/HackerRankKit/main/Sources/HackerRankKit/TestCandidate.swift ; https://github.com/adamtheturtle/HackerRankKit/blob/main/Tests/HackerRankKitTests/LiveContractTests.swift
- PY: https://raw.githubusercontent.com/adamtheturtle/hackerrank/HEAD/tests/models/test_candidates.py
- CLI: https://raw.githubusercontent.com/jholm117/hackerrank-cli/HEAD/internal/api/types.go ; https://raw.githubusercontent.com/jholm117/hackerrank-cli/HEAD/cmd/candidates.go ; https://raw.githubusercontent.com/jholm117/hackerrank-cli/HEAD/docs/superpowers/specs/2026-04-08-hackerrank-cli-design.md
- BL1: https://www.teamblind.com/post/hackerrank-makes-it-easy-to-tell-when-you-cheat-g4vwgd1v
- BL2: https://www.teamblind.com/post/how-thoroughly-does-capital-one-grade-hackerrank-online-assessments-for-software-engineers-bupz5slj
- TARO: https://www.jointaro.com/interviews/companies/zscaler/work-experiences/associate-software-engineer-mohali-punjab-october-31-2024-4-497ea7fb/ ; https://www.jointaro.com/interviews/companies/oracle/work-experiences/associate-applications-developer-mumbai-august-23-2023-4-2f4956e3/
- LEV: https://help.lever.co/hc/en-us/articles/20087328094365
- UNI: https://docs.unified.to/integrations/hackerrankwork.md
- SKT: https://docs.scalekit.com/agentkit/connectors/hackerrankwork/

## Questions to settle before onboarding a new export

Use these with the TA team when a new requisition, a new assessment or a new export layout comes in.

1. Which file will TA send: the Workday assessment export with `Resume.zip` (Part 1, the console's primary input), the HackerRank Candidates-tab Excel, a Custom Report, API/webhook JSON, another ATS export, or PDFs? Can they share the header row plus two redacted rows? That alone settles header text, column order, per-question layout, units and timezone.
2. Will the file cover all completers, or only some of them: only qualified candidates (every Workday row reads "Hackerrank Completed – Qualified"), or only rows with Integrity Issues = High or Medium (which is what HackerRank's own walkthrough exports)?
3. Does the company have the AI Add-on? Which mode was used (default, Secure, Proctor, Desktop App)? Were AI plagiarism detection, Image Proctoring/Analysis, Photo ID, question shuffling, hidden titles or watermarking on?
4. What was the test design: how many multiple-choice vs coding questions, the points, negative marking, sections or random question pools, library vs custom questions, duration and cutoff? Was it one slot or several, and how long was the login window?
5. Which onboarding fields were collected, with their exact titles? For example roll number, CGPA, college, branch, graduation year, and any custom fields such as 10th or 12th marks.
6. Was the test taken in campus labs (shared IPs, invigilated halls) or remotely? Through a public link or individual invites? Were re-attempts allowed?
7. Can TA provide the plagiarism match list (partner email and similarity %) for flagged 100% scorers, the submitted code, or API access (event logs, typing times)? Are we cleared to process code, IPs and phone numbers?
8. Roughly how many candidates and 100% scorers are there per test? Should HackerRank test files also include the older plagiarism fields (`plagiarism_status`, `max_code_similarity`) and the companion files (per-question long format and event log)?
9. For Workday packets: how are the bookmarks titled (candidate name, name plus file name, or something else), and are all of a candidate's attachments in one packet? Check the "verify the split" warnings in **Data quality details** after the first run ([1.8](#18-how-the-console-splits-a-packet)).
