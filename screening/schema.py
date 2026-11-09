"""Canonical column names, header aliases and shared vocabulary.

Every module in the engine and the UI refers to columns through these
constants, so a TA feed with arbitrary headers ("Email ID", "Time Taken
(mins)", ...) is mapped once at ingest time and everything downstream works
on one stable schema.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canonical TA-feed columns (output of ingest.normalize_ta_feed)
# ---------------------------------------------------------------------------
CANDIDATE_ID = "candidate_id"
NAME = "name"
EMAIL = "email"
PHONE = "phone"
COLLEGE = "college"
COLLEGE_TIER = "college_tier"
DEGREE = "degree"
BRANCH = "branch"
GRAD_YEAR = "graduation_year"
CGPA = "cgpa"
CITY = "city"
HR_SCORE = "hackerrank_score"
MAX_SCORE = "max_score"
TEST_START = "test_start_time"
SUBMISSION_TIME = "submission_time"
COMPLETION_MIN = "completion_minutes"
IP_ADDRESS = "ip_address"
TEST_MODE = "test_mode"
TAB_SWITCHES = "tab_switches"
COPY_PASTE = "copy_paste_events"
PLAGIARISM_FLAG = "plagiarism_flag"
SOURCE = "source"
RESUME_FILE = "resume_file"
# HackerRank for Work fields (API v3 TestCandidate / CSV export)
TEST_NAME = "test_name"
TEST_STATUS = "test_status"                  # Completed / Invited / Expired / In progress ...
OUT_OF_WINDOW_SEC = "out_of_window_seconds"  # out_of_window_duration (seconds)
SECURE_MODE_EXITS = "secure_mode_exits"      # secure_mode_exit_count
MAX_CODE_SIMILARITY = "max_code_similarity"  # 0..100 % (HackerRank plagiarism engine)
PLAGIARISM_MATCH = "plagiarism_match"        # id/email of the most similar other candidate
INTEGRITY_STATUS = "integrity_status"        # HackerRank AI-proctoring verdict (e.g. Clear/Review/Suspicious)
PROCTORING_FLAGS = "proctoring_flags"        # webcam / AI proctor notes ("Multiple faces detected; ...")
REPORT_URL = "report_url"
PLAGIARISM_MATCH_ID = "plagiarism_match_id"  # derived: plagiarism_match resolved to a candidate_id
IP_ADDRESSES = "ip_addresses"                # derived: full IP list when the export lists several
# Workday assessment export fields
JOB_APPLICATION = "job_application"          # "First Last (first last)" - name fallback when no Name column
ASSESSMENT_COMMENT = "assessment_comment"    # composite: status + "Integrity Issues:" + "Integrity Summary:"
CANDIDATE_OPTION = "candidate_option"        # language track, e.g. "Django (Python)"
CUTOFF_SCORE = "cutoff_score"                # pass mark in points (campaign constant)
CUTOFF_PCT = "cutoff_pct"                    # pass mark in percent

# Integrity statuses that count as a concern (lower-case). Workday severities Medium/High
# and HackerRank AI-proctoring Review/Suspicious all qualify; "Clear" does not.
INTEGRITY_CONCERN_VALUES = frozenset({"review", "suspicious", "flagged", "medium", "high"})

# Per-question columns are dynamic: q1_hash, q1_score, q1_similarity, ...
ANSWER_HASH_TEMPLATE = "q{n}_hash"
ANSWER_HASH_RE = re.compile(r"^q(\d+)_hash$")
QUESTION_SCORE_TEMPLATE = "q{n}_score"
QUESTION_SCORE_RE = re.compile(r"^q(\d+)_score$")
QUESTION_SIMILARITY_TEMPLATE = "q{n}_similarity"
QUESTION_SIMILARITY_RE = re.compile(r"^q(\d+)_similarity$")
# Header (already normalised: lowercase alphanumerics) -> question number.
# Trailing text after the keyword is allowed: "Q1 Score (MCQ)" -> "q1scoremcq".
ANSWER_HASH_HEADER_RE = re.compile(
    r"^(?:q|ques|question)(\d+)(?:answer|code|submission|solution)?(?:hash|signature|digest|fingerprint)\w*$"
)
QUESTION_SCORE_HEADER_RE = re.compile(r"^(?:q|ques|question)(\d+)(?:score|marks|points)\w*$")
QUESTION_SIMILARITY_HEADER_RE = re.compile(
    r"^(?:q|ques|question)(\d+)(?:code)?(?:similarity|similar|plagiarism)\w*$"
)

# ---------------------------------------------------------------------------
# Derived at ingest
# ---------------------------------------------------------------------------
HR_PCT = "hackerrank_pct"          # 0..100, score / max_score * 100 (clipped)
IP_SUBNET = "ip_subnet"            # IPv4 /24, e.g. "49.36.12.0/24"
EMAIL_DOMAIN = "email_domain"      # part after "@"
DATA_FLAGS = "data_flags"          # "; "-joined data-quality flags for the row

# ---------------------------------------------------------------------------
# Resume linkage (added by matching.link_candidates)
# ---------------------------------------------------------------------------
RESUME_HASH = "resume_hash"        # sha256 of the matched resume file (or "")
HAS_RESUME = "has_resume"          # bool
MATCH_METHOD = "match_method"      # see MATCH_* below
MATCH_CONFIDENCE = "match_confidence"  # 0..1

MATCH_TA_FILE = "ta_resume_file"   # TA feed's resume_file column named the file
MATCH_ID_FILENAME = "id_in_filename"
MATCH_EMAIL = "email"
MATCH_ID_TEXT = "id_in_text"
MATCH_PHONE = "phone"
MATCH_NAME = "name_fuzzy"
MATCH_RESUME_ONLY = "resume_only"  # no TA feed: candidate created from resume
MATCH_NONE = "unmatched"
MATCH_DUPLICATE = "duplicate"      # another resume already linked to the candidate

# ---------------------------------------------------------------------------
# Resume-derived features (added by scoring.score_candidates)
# ---------------------------------------------------------------------------
SKILLS_FOUND = "skills_found"          # ", "-joined canonical skills
MATCHED_SKILLS = "matched_skills"      # ", "-joined required skills satisfied
MISSING_SKILLS = "missing_skills"      # ", "-joined required skills not found
N_MATCHED_SKILLS = "n_matched_skills"  # int
INTERNSHIP_COUNT = "internship_count"  # int
EXPERIENCE_MONTHS = "experience_months"  # float
PROJECT_COUNT = "project_count"        # int
CERTIFICATIONS = "certifications"      # "; "-joined
CERT_COUNT = "cert_count"              # int
RESUME_CGPA = "resume_cgpa"            # float (10-point scale) parsed from resume
RESUME_DEGREE = "resume_degree"        # str parsed from resume

# ---------------------------------------------------------------------------
# Scores (added by scoring.score_candidates)
# sub_* are normalised 0..1; pts_* are rubric points (sub * weight)
# ---------------------------------------------------------------------------
SUB_HR = "sub_hackerrank"
SUB_SKILLS = "sub_skills"
SUB_EXPERIENCE = "sub_experience"
SUB_EDUCATION = "sub_education"
PTS_HR = "pts_hackerrank"
PTS_SKILLS = "pts_skills"
PTS_EXPERIENCE = "pts_experience"
PTS_EDUCATION = "pts_education"
FINAL_SCORE = "final_score"            # 0..100
RELEVANCE = "relevance"                # 0..1 job-description relevance
EXPERIENCE_SOURCE = "experience_source"  # see EXP_* below
LLM_RATIONALE = "llm_rationale"        # str ("" when not used)
SCORE_NOTES = "score_notes"            # "; "-joined notes (e.g. "no resume")

EXP_HEURISTIC = "heuristic"
EXP_NO_RESUME = "no resume"

# ---------------------------------------------------------------------------
# Shortlisting (added by scoring.apply_shortlist)
# ---------------------------------------------------------------------------
ELIGIBLE = "eligible"          # bool
RANK = "rank"                  # 1..N among eligible candidates (NaN if ineligible)
STATUS = "status"
STATUS_REASON = "status_reason"
COLLEGE_CAPPED = "college_capped"  # bool: would have been shortlisted but the college cap was reached
INELIGIBILITY = "ineligibility"    # JSON list of {"rule_violated","candidate_value","threshold_value","explanation"}
TIE_BREAK_NOTE = "tie_break_note"  # "tied at 91.20 pts; won tie-break on HackerRank percentage 100% vs 95%"
CGPA_SOURCE = "cgpa_source"        # "feed" | "resume" | ""
TIER_SOURCE = "tier_source"        # "feed" | "tier list" | "" (tier used for scoring)
DUPLICATE_OF = "duplicate_of"      # primary candidate_id when this row is a duplicate application
DUPLICATE_IDS = "duplicate_ids"    # on the primary: ", "-joined duplicate candidate_ids
DUPLICATE_EVIDENCE = "duplicate_evidence"  # why the rows were judged the same person
INJECTION_FLAGS = "injection_flags"  # " | "-joined summaries of hidden / adversarial resume text

# Ineligibility rule keys (INELIGIBILITY "rule_violated")
RULE_NO_SCORE = "Assessment"
RULE_MIN_SCORE = "Score Threshold"
RULE_MIN_CGPA = "Academic CGPA"
RULE_GRAD_YEAR = "Graduation Year"
RULE_EXCLUDED = "Administrative Exclusion"
RULE_DUPLICATE = "Duplicate Application"

STATUS_SHORTLISTED = "Shortlisted"
STATUS_REVIEW = "Shortlisted (Integrity Review)"
STATUS_HOLD = "Integrity Hold"
STATUS_NOT = "Not Shortlisted"
STATUS_INELIGIBLE = "Ineligible"
STATUS_ORDER = [STATUS_SHORTLISTED, STATUS_REVIEW, STATUS_HOLD, STATUS_NOT, STATUS_INELIGIBLE]

# ---------------------------------------------------------------------------
# Risk (merged from RiskResult.candidate_risk by pipeline)
# ---------------------------------------------------------------------------
IS_FOCUS = "is_focus"            # bool: in the perfect-scorer focus group
RISK_SCORE = "risk_score"        # 0..100
RISK_LEVEL = "risk_level"        # see RISK_* below
RISK_REASONS = "risk_reasons"    # " | "-joined human-readable reasons
CLUSTER_ID = "cluster_id"        # "C01"... or "" when not in a cluster
N_LINKS = "n_links"              # number of risk edges touching the candidate
MAX_PAIR_SCORE = "max_pair_score"
INDIVIDUAL_SCORE = "individual_score"

RISK_HIGH = "High"
RISK_MEDIUM = "Medium"
RISK_LOW = "Low"
RISK_CLEAR = "Clear"
RISK_ORDER = [RISK_HIGH, RISK_MEDIUM, RISK_LOW, RISK_CLEAR]

# Separators used for list-like values stored in DataFrame cells
SEP_LIST = ", "      # skills, ids, question ids, repos
SEP_NOTES = "; "     # data flags, certifications, notes
SEP_REASONS = " | "  # risk reasons, signal descriptions, bullet examples

# ---------------------------------------------------------------------------
# Resume sections (ParsedResume.sections keys)
# ---------------------------------------------------------------------------
SECTION_KEYS = [
    "summary",
    "education",
    "skills",
    "projects",
    "experience",
    "certifications",
    "achievements",
    "other",
]

# ---------------------------------------------------------------------------
# Header aliases for auto-mapping. Keys are canonical names; values are
# *normalised* header spellings (lowercase, alphanumerics only) — use
# normalize_header() before lookup.
# ---------------------------------------------------------------------------
TA_FIELD_ALIASES: dict[str, list[str]] = {
    CANDIDATE_ID: [
        "candidateid", "candid", "cid", "applicantid", "applicationid", "applicationno",
        "hackerrankid", "hrid", "studentid", "registrationid", "registrationno", "regno",
        "rollno", "rollnumber", "uid", "id", "candidatecode", "candidatenumber",
    ],
    NAME: [
        "name", "candidatename", "fullname", "applicantname", "studentname", "candidate",
        "nameofcandidate",
    ],
    EMAIL: [
        "personalemailaddress", "email", "emailid", "emailaddress", "mail", "mailid", "candidateemail", "personalemail",
        "emailidofcandidate",
    ],
    PHONE: [
        "phone", "phoneno", "phonenumber", "mobile", "mobileno", "mobilenumber", "contact",
        "contactno", "contactnumber", "cell", "cellphone", "whatsapp",
    ],
    COLLEGE: [
        "univcollegename", "universitycollegename", "univcollege", "collegeuniversity", "universitycollege", "college", "collegename", "university", "universityname", "institute", "institution",
        "institutename", "institutionname", "school", "campus", "campusname",
    ],
    COLLEGE_TIER: ["tier", "collegetier", "institutetier", "campustier", "collegecategory"],
    DEGREE: [
        "degree", "qualification", "highestqualification", "course", "program", "programme",
        "degreename",
    ],
    BRANCH: [
        "streambranch", "branchstream", "branch", "major", "specialization", "specialisation", "discipline", "stream",
        "department", "dept",
    ],
    GRAD_YEAR: [
        "graduationyear", "gradyear", "yearofpassing", "passingyear", "passoutyear", "yop",
        "batch", "yearofgraduation", "graduatingyear", "expectedgraduation",
    ],
    CGPA: ["cgpa", "gpa", "cpi", "sgpa", "aggregate", "aggregatepercentage", "academicpercentage",
           "cgpaoutof10", "cgpa10", "marks"],
    HR_PCT: ["percentagescore", "percentage", "scorepercentage", "percentscore", "pctscore",
             "hackerrankpercentage", "percentagescored"],
    CITY: ["city", "location", "currentlocation", "currentcity", "hometown", "basedin"],
    HR_SCORE: [
        "hackerrankscore", "hrscore", "score", "testscore", "totalscore", "assessmentscore",
        "marksobtained", "codingscore", "hackerrank", "obtainedscore", "finalscore",
        "onlinetestscore",
    ],
    MAX_SCORE: [
        "maxscore", "maximumscore", "totalmarks", "outof", "maxmarks", "fullmarks",
        "maxpossiblescore", "testmaxscore", "possiblescore",
    ],
    TEST_START: [
        "teststarttime", "starttime", "startedat", "teststart", "attemptstarttime",
        "attemptstartedat", "teststartedat", "testbegin", "starttimestamp",
    ],
    SUBMISSION_TIME: [
        "submissiontime", "submittedat", "endtime", "testendtime", "submittedon", "completedat",
        "testsubmittedat", "finishtime", "attemptendtime", "submissiontimestamp", "submitted",
    ],
    COMPLETION_MIN: [
        "attempttimetaken", "attempttimetakenseconds", "attempttimetakenmins", "completiontime", "timetaken", "duration", "timespent", "completionminutes",
        "testduration", "timetakenmins", "timetakenminutes", "timetakenmin", "durationmins",
        "durationminutes", "timetakeninminutes",
    ],
    IP_ADDRESS: ["ip", "ipaddress", "clientip", "sourceip", "loginip", "ipaddr"],
    TEST_MODE: ["testmode", "mode", "assessmentmode", "testlocation", "venue", "proctoringmode"],
    TAB_SWITCHES: [
        "numberofwindowexits", "windowexits", "windowexitcount", "outofwindowevents", "outofwindowcount", "outofwindow", "tabswitches", "tabswitchcount",
        "windowswitches", "tabchanges", "focuslost", "tabswitch", "browserswitches",
    ],
    COPY_PASTE: [
        "copypastefrequency", "editorpastecount", "editorpaste", "copypasteevents", "copypaste", "pasteevents",
        "copypastecount", "pastecount", "copypastedetected",
    ],
    PLAGIARISM_FLAG: [
        "attemptplagiarism", "plagiarismstatus", "plagiarismflag", "plagiarism", "plagiarized", "plagiarismdetected",
        "suspicious", "flagged",
    ],
    SOURCE: ["source", "sourcingchannel", "channel", "referral", "referralsource", "applicationsource"],
    RESUME_FILE: ["resumefile", "resume", "resumefilename", "cvfile", "cv", "resumename", "cvfilename"],
    TEST_NAME: ["testname", "test", "assessmentname", "assessment", "testtitle"],
    TEST_STATUS: ["status", "teststatus", "attemptstatus", "candidatestatus", "assessmentstatus"],
    OUT_OF_WINDOW_SEC: [
        "outofwindowduration", "outofwindowdurations", "outofwindowdurationsec",
        "outofwindowdurationseconds", "outofwindowtime", "outofwindowseconds", "timeoutofwindow",
    ],
    SECURE_MODE_EXITS: ["fullscreenexitcount", "securemodeexitcount", "securemodeexits", "securemodeexit", "fullscreenexits"],
    MAX_CODE_SIMILARITY: [
        "maxcodesimilarity", "maxcodesimilaritypct", "maxcodesimilaritypercent", "codesimilarity",
        "similaritypercentage", "maxsimilarity", "plagiarismsimilarity",
    ],
    PLAGIARISM_MATCH: [
        "plagiarismmatch", "mostsimilarcandidate", "similarcandidate", "matchedcandidate",
        "plagiarismmatchedcandidate", "similartocandidate", "plagiarismmatchedwith",
    ],
    INTEGRITY_STATUS: ["proctorflag", "integritystatus", "integrity", "proctoringstatus", "aiproctoringstatus", "integrityverdict"],
    PROCTORING_FLAGS: [
        "proctorflagreasons", "proctoringflags", "proctoringnotes", "webcamflags", "imageproctoringflags",
        "proctoringviolations", "aiproctoringflags", "suspiciousactivity",
    ],
    REPORT_URL: ["detailedreporturl", "reporturl", "reportlink", "candidatereport", "pdfurl", "report"],
    JOB_APPLICATION: ["jobapplication", "application", "jobapplicationname", "applicant"],
    ASSESSMENT_COMMENT: ["assessmentcomment", "assessmentcomments", "comment", "comments", "feedbackcomments",
                         "integrityissues", "integritysummary"],
    CANDIDATE_OPTION: ["candidateoption", "languagetrack", "track", "techstack", "codinglanguage", "language"],
    CUTOFF_SCORE: ["cutoffscore", "cutoff", "passmark", "passingscore", "qualifyingscore"],
    CUTOFF_PCT: ["cutoffscorepercentage", "cutoffpercentage", "cutoffpct", "passpercentage"],
}

# Human-friendly labels for the UI (canonical -> label)
TA_FIELD_LABELS: dict[str, str] = {
    CANDIDATE_ID: "Candidate ID",
    NAME: "Name",
    EMAIL: "Email",
    PHONE: "Phone",
    COLLEGE: "College",
    COLLEGE_TIER: "College Tier",
    DEGREE: "Degree",
    BRANCH: "Branch",
    GRAD_YEAR: "Graduation Year",
    CGPA: "CGPA",
    CITY: "City",
    HR_SCORE: "HackerRank Score",
    MAX_SCORE: "Max Score",
    TEST_START: "Test Start Time",
    SUBMISSION_TIME: "Submission Time",
    COMPLETION_MIN: "Completion (min)",
    IP_ADDRESS: "IP Address",
    TEST_MODE: "Test Mode",
    TAB_SWITCHES: "Tab Switches",
    COPY_PASTE: "Copy-Paste Events",
    PLAGIARISM_FLAG: "Plagiarism Flag",
    SOURCE: "Source",
    RESUME_FILE: "Resume File",
    HR_PCT: "HackerRank %",
    TEST_NAME: "Test Name",
    TEST_STATUS: "Test Status",
    OUT_OF_WINDOW_SEC: "Out-of-Window (sec)",
    SECURE_MODE_EXITS: "Secure-Mode Exits",
    MAX_CODE_SIMILARITY: "Max Code Similarity %",
    PLAGIARISM_MATCH: "Most Similar Candidate",
    INTEGRITY_STATUS: "Integrity Status",
    PROCTORING_FLAGS: "Proctoring Flags",
    REPORT_URL: "Report URL",
    JOB_APPLICATION: "Job Application",
    ASSESSMENT_COMMENT: "Assessment Comment",
    CANDIDATE_OPTION: "Candidate Option",
    CUTOFF_SCORE: "Cutoff Score",
    CUTOFF_PCT: "Cutoff Score %",
}

# Where each value comes from (shown as grouped/prefixed columns in the UI and exports).
SRC_ASSESSMENT = "Assessment"   # HackerRank / assessment export (verified test telemetry)
SRC_RESUME = "Resume"           # parsed from the candidate's CV (self-reported)
SRC_ACADEMIC = "Academic"       # academic profile (feed, reconciled with the resume)
SRC_INTEGRITY = "Integrity"     # integrity engine output
COLUMN_SOURCE: dict[str, str] = {
    **{c: SRC_ASSESSMENT for c in (
        HR_SCORE, MAX_SCORE, HR_PCT, PTS_HR, SUB_HR, TEST_NAME, TEST_STATUS, TEST_START, SUBMISSION_TIME,
        COMPLETION_MIN, CUTOFF_SCORE, CUTOFF_PCT, CANDIDATE_OPTION, INTEGRITY_STATUS, PROCTORING_FLAGS,
        PLAGIARISM_FLAG, MAX_CODE_SIMILARITY, TAB_SWITCHES, COPY_PASTE, OUT_OF_WINDOW_SEC,
        SECURE_MODE_EXITS, IP_ADDRESS, TEST_MODE, ASSESSMENT_COMMENT, IP_SUBNET, IP_ADDRESSES,
        PLAGIARISM_MATCH, PLAGIARISM_MATCH_ID, REPORT_URL)},
    **{c: SRC_RESUME for c in (
        SKILLS_FOUND, MATCHED_SKILLS, MISSING_SKILLS, N_MATCHED_SKILLS, PTS_SKILLS, SUB_SKILLS,
        PTS_EXPERIENCE, SUB_EXPERIENCE, INTERNSHIP_COUNT, EXPERIENCE_MONTHS, PROJECT_COUNT,
        CERTIFICATIONS, CERT_COUNT, RELEVANCE, RESUME_DEGREE, RESUME_CGPA, HAS_RESUME, RESUME_FILE,
        RESUME_HASH, MATCH_METHOD, MATCH_CONFIDENCE, EXPERIENCE_SOURCE)},
    **{c: SRC_ACADEMIC for c in (
        COLLEGE, COLLEGE_TIER, TIER_SOURCE, DEGREE, BRANCH, GRAD_YEAR, CGPA, CGPA_SOURCE, PTS_EDUCATION,
        SUB_EDUCATION)},
    **{c: SRC_INTEGRITY for c in (
        RISK_LEVEL, RISK_SCORE, RISK_REASONS, CLUSTER_ID, IS_FOCUS, N_LINKS, MAX_PAIR_SCORE,
        INDIVIDUAL_SCORE, INJECTION_FLAGS, DUPLICATE_OF, DUPLICATE_IDS, DUPLICATE_EVIDENCE)},
}

# Fields that must be mappable for scoring to work. Identity: at least one of
# IDENTITY_FIELDS must exist (candidate_id is generated if absent).
REQUIRED_FIELDS = [HR_SCORE]

# Protected / sensitive attributes. HackerRank's candidate-details form can collect
# some of these (e.g. Gender). Ingest DROPS such columns (they are never scored,
# displayed, exported or sent to an LLM) and records an info issue. Normalised headers.
PROTECTED_ATTRIBUTE_HEADERS = {
    "gender", "sex", "age", "dateofbirth", "dob", "birthdate", "religion", "caste",
    "category", "castecategory", "maritalstatus", "nationality", "disability",
    "pwd", "ethnicity", "race", "photo", "photograph", "fathersname", "mothersname",
}
IDENTITY_FIELDS = [CANDIDATE_ID, EMAIL, NAME]

# Attributes used by the commonality (lift) analysis: column -> label.
COMMONALITY_ATTRIBUTES: dict[str, str] = {
    COLLEGE: "College",
    CITY: "City",
    DEGREE: "Degree",
    BRANCH: "Branch",
    GRAD_YEAR: "Graduation Year",
    COLLEGE_TIER: "College Tier",
    TEST_MODE: "Test Mode",
    SOURCE: "Source",
    IP_ADDRESS: "IP Address",
    IP_SUBNET: "IP Subnet (/24)",
    EMAIL_DOMAIN: "Email Domain",
    TEST_NAME: "Test Name",
    "test_slot": "Test Start Slot (hour)",
}

TEST_SLOT = "test_slot"  # derived at ingest: test_start_time floored to the hour
COLUMN_SOURCE[TEST_SLOT] = SRC_ASSESSMENT


def normalize_header(header: object) -> str:
    """Lowercase and strip everything except a-z0-9: 'Time Taken (mins)' -> 'timetakenmins'."""
    return re.sub(r"[^a-z0-9]", "", str(header).strip().lower())


def _question_columns(columns, pattern: re.Pattern) -> list[str]:
    found = [(int(m.group(1)), c) for c in columns if (m := pattern.match(str(c)))]
    return [c for _, c in sorted(found)]


def answer_hash_columns(columns) -> list[str]:
    """Canonical answer-hash columns (q1_hash, q2_hash, ...) sorted by question number."""
    return _question_columns(columns, ANSWER_HASH_RE)


def question_score_columns(columns) -> list[str]:
    """Canonical per-question score columns (q1_score, ...) sorted by question number."""
    return _question_columns(columns, QUESTION_SCORE_RE)


def question_similarity_columns(columns) -> list[str]:
    """Canonical per-question code-similarity columns (q2_similarity, ...) sorted."""
    return _question_columns(columns, QUESTION_SIMILARITY_RE)
