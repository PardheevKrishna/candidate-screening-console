# data/

Everything in this folder except this README is gitignored: real TA feeds and resumes contain
candidate PII and must never be committed.

* `sample/` - synthetic dataset. It is not in the repository: generate it locally from the project
  root with `python generate_mock_data.py` (3,000 fictional candidates, about 2,900 resumes in
  `resumes/`, `resumes.zip` unless `--no-zip`, a job description and ground-truth labels;
  `--quick` writes 300 candidates). Fictional people, colleges and companies; reserved example.*
  email domains. The app's **Load synthetic sample & run** button reads it from here.
