# data/

Everything in this folder except this README is gitignored: real exports and resumes contain
candidate PII and must never be committed.

* `sample/` - synthetic dataset in the shape of a Workday recruiting export. It is not in the
  repository: generate it locally from the project root with `python generate_mock_data.py`
  (2,133 fictional applications; `--quick` writes 300). It contains:
  * `test_scores_export.xlsx` - `Sheet1` recruiter summary, `Sheet2` one row per application with
    the 14 export columns (Job Application, Candidate ID, Assessment Comment, Email, CGPA, Branch,
    School, Location, Max Score, Score, Cutoff Score, Cutoff Score Percentage, Percentage Score,
    Candidate Option);
  * `Resume.zip` - `Resume/` folder of multi-candidate PDF packets
    (`R-100200 - 2027 Technology Program Intern_set N.pdf`) with a table of contents and one
    bookmark per attachment; about 12% of applications have no resume;
  * `job_description.txt`, a `README.md` describing the files, and `_ground_truth/` labels for
    the planted evaluation cases (duplicate applications, team projects, resume mills,
    code-similarity flags, prompt-injection resumes, namesakes) in `labels.csv`, `clusters.json`,
    `injected_issues.json` and `resume_index.csv`. The app never reads `_ground_truth/`;
    `python scripts/run_e2e.py` uses it to score a run (`--strict` exits 1 when a check fails).

  Fictional people, colleges and companies; e-mail addresses only on `mail.example.com` and
  `<college>.example`. The generator refuses to write into a non-empty folder it did not create.
  The app's **Load synthetic sample & run** button reads it from here. The file format is
  described column by column in
  [docs/HACKERRANK_DATA_REFERENCE.md, Part 1](../docs/HACKERRANK_DATA_REFERENCE.md#part-1---workday-assessment-export).
* `cache/` - parsed-dataset snapshots for fast reloads (created by the app; contains candidate
  data, so it stays here). Snapshots are deleted after 7 days by default (Config & Rubric Tuning >
  Fast reload).
