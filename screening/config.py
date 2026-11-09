"""Typed configuration for the whole engine (rubric, risk, LLM, processing).

AppConfig is plain dataclasses so it can be hashed for caching, edited in the
Config tab, and round-tripped to JSON (download/upload a tuning profile).
"""
from __future__ import annotations

import json
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from typing import Any

DEFAULT_REQUIRED_SKILLS = ["Python", "Java", "SQL", "Cloud", "PyTorch"]

DEFAULT_JOB_DESCRIPTION = """\
Software Engineering Program - Campus Hire (Technology)

We are hiring graduating engineers to build secure, scalable software for
digital banking, payments and risk platforms.

What you will do
- Design, build and test backend services and REST APIs in Python or Java
  (Spring Boot, Flask/FastAPI) backed by SQL databases.
- Build data pipelines and analytics on cloud platforms (AWS, Azure or GCP),
  using containers (Docker, Kubernetes) and CI/CD.
- Apply machine learning (PyTorch, scikit-learn) to problems such as fraud
  detection, credit risk and customer analytics.
- Write clean, well-tested, secure code; work in agile teams with Git.

What we look for
- Strong data structures, algorithms and problem-solving skills.
- Hands-on projects or internships showing real implementation depth,
  measurable outcomes and modern engineering practices.
- Solid fundamentals: OOP, databases/SQL, operating systems, networking.
"""


@dataclass
class RubricWeights:
    """Top-level rubric weights: points out of 100 (default 40/30/20/10)."""

    hackerrank: float = 40.0
    skills: float = 30.0
    experience: float = 20.0
    education: float = 10.0

    def total(self) -> float:
        return self.hackerrank + self.skills + self.experience + self.education

    def normalized(self) -> "RubricWeights":
        """Scale so the four weights sum to 100 (no-op when they already do)."""
        t = self.total()
        if t <= 0:
            return RubricWeights()
        k = 100.0 / t
        return RubricWeights(self.hackerrank * k, self.skills * k, self.experience * k, self.education * k)


@dataclass
class SkillsConfig:
    required_skills: list[str] = field(default_factory=lambda: list(DEFAULT_REQUIRED_SKILLS))
    # "per_skill": fixed points per required skill found (default 10 pts each, capped at 30)
    #              => full marks once `skills_for_full_marks` skills are matched.
    # "coverage":  proportional - matched / len(required_skills).
    mode: str = "per_skill"
    skills_for_full_marks: int = 3


@dataclass
class ExperienceConfig:
    job_description: str = DEFAULT_JOB_DESCRIPTION
    # Heuristic sub-weights (normalised internally; sum need not be exactly 1)
    w_internships: float = 0.30
    w_duration: float = 0.15
    w_projects: float = 0.25
    w_relevance: float = 0.30
    internships_for_full: int = 2
    months_for_full: float = 6.0
    projects_for_full: int = 3


@dataclass
class EducationConfig:
    # Sub-weights (normalised over the sub-components that have data)
    w_degree: float = 0.35
    w_cgpa: float = 0.35
    w_certifications: float = 0.20
    w_tier: float = 0.10  # used only when tier_enabled
    cgpa_floor: float = 6.0     # <= floor -> 0
    cgpa_ceiling: float = 9.5   # >= ceiling -> 1
    certs_for_full: int = 2
    tier_scores: dict[str, float] = field(default_factory=lambda: {"1": 1.0, "2": 0.7, "3": 0.4})
    # College tier scoring is OFF unless the TA team turns it on (ideally with an approved
    # tier list): tiers can act as a proxy for socioeconomic background. When off, the tier
    # weight is ignored and the remaining education sub-weights are re-normalised.
    tier_enabled: bool = False
    # Approved tier list uploaded in the Config tab: normalised college name/alias -> tier label.
    tier_mapping: dict[str, str] = field(default_factory=dict)
    tier_match_threshold: int = 90   # rapidfuzz score needed to match a college to the list


@dataclass
class ShortlistConfig:
    method: str = "top_n"          # "top_n" | "top_pct" | "threshold"
    top_n: int = 300
    top_pct: float = 10.0
    threshold: float = 70.0
    # Eligibility knock-outs (0 / empty = disabled)
    min_hackerrank_pct: float = 0.0
    min_cgpa: float = 0.0
    allowed_grad_years: list[int] = field(default_factory=list)
    # Integrity policy. Default: keep flagged candidates in the
    # shortlist with a red flag + evidence pack for recruiter review.
    # True => High-risk candidates move to "Integrity Hold" and the slot is backfilled.
    exclude_high_risk: bool = False
    review_risk_levels: list[str] = field(default_factory=lambda: ["High", "Medium"])
    # Candidates without a resume: "zero" scores resume components as 0; "renormalize"
    # rescales the remaining components to 100; "auto" = renormalize when no resumes were
    # loaded at all (TA-feed-only run), otherwise zero (no unfair boost for a missing CV).
    missing_resume_policy: str = "auto"
    # Max share of the shortlist any single college may take (percent of shortlist size;
    # 0 = no cap). Candidates beyond the cap are not shortlisted and the next-best fill the slots.
    college_cap_pct: float = 0.0
    # Candidate IDs a recruiter disqualifies by hand (administrative exclusion).
    exclude_candidates: list[str] = field(default_factory=list)
    # Detected duplicate applications (same person, different email/ID) are made Ineligible,
    # keeping one application (institutional email preferred, else the earliest ID).
    auto_disqualify_duplicates: bool = True
    # Tie-break order after final score (candidate ID is always the last resort).
    # Keys: see TIE_BREAK_KEYS.
    tie_break_order: list[str] = field(default_factory=lambda: ["hackerrank_pct", "sub_skills", "sub_experience"])


# Tie-break keys -> (label, higher_is_better)
TIE_BREAK_KEYS: dict[str, tuple[str, bool]] = {
    "hackerrank_pct": ("HackerRank percentage", True),
    "coding_marks": ("HackerRank coding-question marks", True),
    "sub_skills": ("Skills sub-score", True),
    "sub_experience": ("Experience sub-score", True),
    "cgpa": ("CGPA (feed, then resume)", True),
    "submission_time": ("Earliest assessment submission", False),
}

# One-click rubric presets shown in the Config tab.
PRESETS: dict[str, dict[str, Any]] = {
    "with_resumes": {"label": "With Resumes (40/30/20/10)",
                     "weights": {"hackerrank": 40.0, "skills": 30.0, "experience": 20.0, "education": 10.0}},
    "no_resumes": {"label": "No Resumes (80/0/0/20)",
                   "weights": {"hackerrank": 80.0, "skills": 0.0, "experience": 0.0, "education": 20.0}},
}


@dataclass
class RiskConfig:
    focus_threshold_pct: float = 100.0      # focus group = hackerrank_pct >= this
    compare_against_all: bool = False       # also compare focus resumes against the whole pool
    text_similarity_high: float = 0.80      # TF-IDF cosine on project/experience text
    text_similarity_medium: float = 0.60
    min_bullet_words: int = 8               # shared bullet must have >= N words
    max_bullet_population_share: float = 0.02  # bullets in > 2% of resumes are boilerplate
    sync_window_sec: float = 120.0          # start / submit within N seconds
    duration_tolerance_min: float = 1.0     # completion times within N minutes
    proxy_gap_max_min: float = 20.0         # same IP: next session starts <= N min after previous ended
    common_hash_max_share: float = 0.10     # answer hash shared by > 10% of focus group = expected (MCQ etc.)
    shared_ip_max_group: int = 5            # IP used by more candidates than this = lab/NAT -> down-weighted
    speed_anomaly_ratio: float = 0.40       # completion < 40% of focus-group median -> anomaly
    tab_switch_threshold: int = 5           # HackerRank out_of_window_events
    copy_paste_threshold: int = 3           # HackerRank editor_paste_count
    out_of_window_sec_threshold: float = 120.0   # out_of_window_duration
    secure_mode_exit_threshold: int = 1
    code_similarity_threshold: float = 80.0      # HackerRank max code similarity %
    edge_threshold: float = 30.0            # pair score needed to link two candidates
    high_threshold: float = 60.0
    medium_threshold: float = 30.0
    lift_min_count: int = 3
    lift_min: float = 2.0
    lift_alpha: float = 0.05
    # Legitimate team projects: teammates list the same GitHub repo on otherwise different
    # resumes. A pair with <= team_max_shared_repos shared repos, resume similarity below
    # team_max_text_similarity and <= team_max_shared_bullets shared rare bullets counts as a
    # team project; its repo signal uses team_repo_weight instead of the full weight.
    team_max_shared_repos: int = 2
    team_max_text_similarity: float = 0.40
    team_max_shared_bullets: int = 1
    team_repo_weight: float = 15.0
    pair_weights: dict[str, float] = field(default_factory=lambda: {
        "shared_answer_hash": 35.0,   # per shared non-trivial answer, capped at 2x
        "shared_github_repo": 35.0,
        "resume_text_high": 30.0,
        "resume_text_medium": 15.0,
        "shared_bullets": 8.0,        # per shared rare bullet, capped at 3x
        "same_ip": 20.0,
        "same_subnet": 5.0,
        "sync_submission": 15.0,
        "sync_start": 10.0,
        "duration_match": 5.0,
        "same_phone": 40.0,
        "same_email": 40.0,
        "sequential_same_ip": 25.0,   # proxy test-taker pattern
        "plagiarism_match": 40.0,     # HackerRank names the other candidate as most-similar
    })
    individual_weights: dict[str, float] = field(default_factory=lambda: {
        "speed_anomaly": 25.0,
        "plagiarism_flag": 30.0,      # HackerRank plagiarism_status = suspicious
        "high_code_similarity": 20.0, # max_code_similarity >= code_similarity_threshold
        "integrity_status": 20.0,     # AI-proctoring verdict Review/Suspicious
        "proctoring_flags": 15.0,     # webcam / AI proctor notes present
        "tab_switches": 10.0,
        "out_of_window_time": 10.0,
        "copy_paste": 10.0,
        "secure_mode_exits": 10.0,
        "prompt_injection": 50.0,     # instruction aimed at AI screeners in the resume (visible or hidden)
        "hidden_text": 35.0,          # resume text a reader cannot see (e.g. white keyword stuffing)
        "invisible_chars": 15.0,      # only zero-width / bidi characters (obfuscation or copy-paste)
    })


# LLM access goes through the local VS Code extension in vscode-copilot-proxy/, which serves
# GitHub Copilot models over an OpenAI-style endpoint on this machine only. Provider-specific
# values live ONLY here and in screening/llm_backends.py.
LLM_PROVIDERS: dict[str, dict[str, Any]] = {
    "copilot": {
        "label": "GitHub Copilot (VS Code proxy)",
        "base_url": "http://127.0.0.1:5757/v1",
        "model": "",                 # "" = the proxy picks the best available Copilot chat model
        "key_env": "COPILOT_PROXY_TOKEN",
        "fallback_models": [],
    },
}


# Never accepted from uploaded profiles (see AppConfig.from_json).
LLM_CONNECTION_FIELDS = frozenset({"provider", "api_key_env", "base_url", "fallback_models", "cache_dir"})


@dataclass
class LLMConfig:
    """LLM settings. The LLM only writes rationales (candidate explanations, interview and
    integrity questions, risk-group and batch summaries); it never changes scores, ranks or cutoffs."""

    enabled: bool = True                     # still requires the proxy to be running
    provider: str = "copilot"                # key of LLM_PROVIDERS
    api_key_env: str = "COPILOT_PROXY_TOKEN" # env var / st.secrets key holding the proxy token
    base_url: str = "http://127.0.0.1:5757/v1"
    model: str = ""                          # "" = proxy default; or a Copilot model id/family
    fallback_models: list[str] = field(default_factory=list)
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout_sec: float = 120.0
    max_retries: int = 3
    max_concurrency: int = 4
    max_input_chars: int = 6000
    cache_dir: str = ".cache/llm"
    # "Explain borderline candidates": the first N ranks just below the shortlist cutoff.
    borderline_band: int = 50

    def apply_provider(self, provider: str) -> "LLMConfig":
        """Switch base_url / model / fallbacks / key name to a preset (in place)."""
        preset = LLM_PROVIDERS.get(provider)
        if preset is None:
            raise ValueError(f"Unknown LLM provider '{provider}'")
        self.provider = provider
        self.api_key_env = preset["key_env"]
        self.base_url = preset["base_url"]
        self.model = preset["model"]
        self.fallback_models = list(preset["fallback_models"])
        return self


@dataclass
class ProcessingConfig:
    max_workers: int = 8
    max_file_mb: float = 10.0          # single resume file
    max_bundle_mb: float = 250.0       # multi-candidate PDF packet (e.g. Workday "..._set 1.pdf")
    split_bundles: bool = True         # split multi-candidate packets into one resume per candidate
    max_zip_members: int = 10000
    max_zip_total_mb: float = 2048.0
    # Pattern used to spot candidate IDs in resume file names / text when the TA
    # feed's own IDs are not found verbatim (Workday "C-1234567" and similar).
    candidate_id_pattern: str = r"\b(?:C-\d{6,8}|[A-Z]{2,6}[-_]?\d{3,8})\b"


@dataclass
class SnapshotConfig:
    """Fast reload: parsed datasets are cached on disk so a browser refresh does not re-parse."""

    enabled: bool = True
    ttl_days: int = 7                  # cached datasets older than this are purged
    cache_dir: str = "data/cache"      # under data/ -> gitignored (contains candidate data)


@dataclass
class AppConfig:
    weights: RubricWeights = field(default_factory=RubricWeights)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    experience: ExperienceConfig = field(default_factory=ExperienceConfig)
    education: EducationConfig = field(default_factory=EducationConfig)
    shortlist: ShortlistConfig = field(default_factory=ShortlistConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    snapshots: SnapshotConfig = field(default_factory=SnapshotConfig)

    # ---- serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def fingerprint(self) -> str:
        """Stable string for cache keys."""
        return json.dumps(self.to_dict(), sort_keys=True, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AppConfig":
        return _from_dict(cls, data or {})

    @classmethod
    def from_json(cls, text: str) -> "AppConfig":
        """Load a tuning profile (e.g. an uploaded config file).

        Profiles are untrusted input: LLM *connection* settings (proxy address, token variable,
        fallback list, cache folder) and the snapshot cache folder are always taken from the
        code defaults, so a profile can never redirect the proxy token or candidate data.
        """
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("llm"), dict):
            data["llm"] = {k: v for k, v in data["llm"].items() if k not in LLM_CONNECTION_FIELDS}
        if isinstance(data, dict) and isinstance(data.get("snapshots"), dict):
            data["snapshots"] = {k: v for k, v in data["snapshots"].items() if k != "cache_dir"}
        return cls.from_dict(data)

    # ---- validation ----------------------------------------------------
    def validate(self) -> list[str]:
        """Human-readable problems; empty list means OK."""
        problems: list[str] = []
        w = self.weights
        if min(w.hackerrank, w.skills, w.experience, w.education) < 0:
            problems.append("Rubric weights must be non-negative.")
        if abs(w.total() - 100.0) > 0.01:
            problems.append(f"Rubric weights sum to {w.total():.1f}; they will be rescaled to 100.")
        if not self.skills.required_skills:
            problems.append("No required skills configured; the skills component will score 0.")
        if self.skills.mode not in {"per_skill", "coverage"}:
            problems.append(f"Unknown skills mode '{self.skills.mode}'.")
        if self.shortlist.method not in {"top_n", "top_pct", "threshold"}:
            problems.append(f"Unknown shortlist method '{self.shortlist.method}'.")
        if self.shortlist.missing_resume_policy not in {"auto", "zero", "renormalize"}:
            problems.append(f"Unknown missing-resume policy '{self.shortlist.missing_resume_policy}'.")
        unknown = [k for k in self.shortlist.tie_break_order if k not in TIE_BREAK_KEYS]
        if unknown:
            problems.append(f"Unknown tie-break rule(s): {', '.join(unknown)}.")
        if self.risk.medium_threshold > self.risk.high_threshold:
            problems.append("Risk medium threshold is above the high threshold.")
        return problems


def _from_dict(cls, data: dict[str, Any]):
    """Rebuild nested dataclasses, ignoring unknown keys (forward compatible)."""
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        default = f.default_factory() if f.default_factory is not MISSING else f.default  # type: ignore[misc]
        if is_dataclass(default) and isinstance(value, dict):
            kwargs[f.name] = _from_dict(type(default), value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)
