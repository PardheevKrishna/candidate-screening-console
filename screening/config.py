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
    w_tier: float = 0.10  # institution tier; set 0 to ignore (fairness: can proxy socioeconomic status)
    cgpa_floor: float = 6.0     # <= floor -> 0
    cgpa_ceiling: float = 9.5   # >= ceiling -> 1
    certs_for_full: int = 2
    tier_scores: dict[str, float] = field(default_factory=lambda: {"1": 1.0, "2": 0.7, "3": 0.4})


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
    # Candidates in the TA feed without a resume: "zero" scores resume components as 0;
    # "renormalize" rescales the remaining components to 100.
    missing_resume_policy: str = "zero"
    # Max share of the shortlist any single college may take (percent of shortlist size;
    # 0 = no cap). Candidates beyond the cap are not shortlisted and the next-best fill the slots.
    college_cap_pct: float = 0.0


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
    })


# Active LLM provider presets. Provider-specific values live ONLY here and in
# screening/llm_backends.py - see docs/SWITCH_TO_TACHYON.md for the migration.
LLM_PROVIDERS: dict[str, dict[str, Any]] = {
    "nvidia": {
        "label": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "nvidia/nemotron-3-ultra-550b-a55b",
        "key_env": "NVIDIA_API_KEY",
        "fallback_models": ["nvidia/nemotron-3.5-bolt-e4b", "meta/llama-3.3-70b-instruct", "google/gemma-4-31b-it"],
        # Nemotron models "think" before answering; hidden reasoning can eat the whole token
        # budget (empty reply). Disabling it measured 0 reasoning tokens and ~35% lower latency.
        "request_extras": {"chat_template_kwargs": {"thinking": False}},
    },
}


# Never accepted from uploaded profiles (see AppConfig.from_json).
LLM_CONNECTION_FIELDS = frozenset({
    "provider", "api_key_env", "backend_factory", "base_url", "fallback_models", "request_extras",
    "cache_dir",
})


@dataclass
class LLMConfig:
    enabled: bool = True                     # still requires an API key at runtime
    provider: str = "nvidia"                 # key of LLM_PROVIDERS
    api_key_env: str = "NVIDIA_API_KEY"      # env var / st.secrets key holding the API key
    backend_factory: str = ""                # "module:Class" for provider "tachyon" (internal wrapper)
    base_url: str = "https://integrate.api.nvidia.com/v1"
    # meta/llama-3.3-70b-instruct now returns HTTP 410 (retired) on the NVIDIA API;
    # measured 2026-11-05: nemotron-3-ultra ~5 s/call with valid JSON.
    model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    # Tried in order when the primary model is retired/unavailable (404/410) or keeps timing out.
    fallback_models: list[str] = field(default_factory=lambda: [
        "nvidia/nemotron-3.5-bolt-e4b",
        "meta/llama-3.3-70b-instruct",
        "google/gemma-4-31b-it",
    ])
    # Extra vendor-specific request fields (sent as the request body's extra fields).
    request_extras: dict[str, Any] = field(default_factory=lambda: {"chat_template_kwargs": {"thinking": False}})
    temperature: float = 0.2
    max_tokens: int = 2048                   # headroom for models that reason before answering
    timeout_sec: float = 60.0
    max_retries: int = 4
    max_concurrency: int = 8
    max_input_chars: int = 6000
    cache_dir: str = ".cache/llm"
    # Re-scoring: only candidates near the shortlist cutoff,
    # blended 50/50 with the rule-based experience score.
    rescore_near_cutoff: bool = True
    cutoff_band: int = 50                    # ranks within +/- band of the cutoff
    blend_weight: float = 0.5                # exp = (1-w)*rules + w*llm

    def apply_provider(self, provider: str) -> "LLMConfig":
        """Switch base_url / model / fallbacks / key name to a preset (in place)."""
        preset = LLM_PROVIDERS.get(provider)
        if preset is None:
            raise ValueError(f"Unknown LLM provider '{provider}'")
        self.provider = provider
        self.api_key_env = preset["key_env"]
        self.base_url = preset["base_url"]
        self.model = preset["model"]  # "" = let the backend use its own default
        self.fallback_models = list(preset["fallback_models"])
        self.backend_factory = preset.get("backend_factory", "")
        self.request_extras = dict(preset.get("request_extras", {}))
        return self


@dataclass
class ProcessingConfig:
    max_workers: int = 8
    max_file_mb: float = 10.0
    max_zip_members: int = 10000
    max_zip_total_mb: float = 1024.0
    # Pattern used to spot candidate IDs in resume file names / text when the TA
    # feed's own IDs are not found verbatim.
    candidate_id_pattern: str = r"\b[A-Z]{2,6}[-_]?\d{3,8}\b"


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

        Profiles are untrusted input: LLM *connection* settings (endpoint, key name, backend
        import path, fallback list, extra request fields, cache folder) are always taken from
        the code defaults, so a profile can never redirect the API key or candidate data.
        """
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("llm"), dict):
            data["llm"] = {k: v for k, v in data["llm"].items() if k not in LLM_CONNECTION_FIELDS}
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
        if self.shortlist.missing_resume_policy not in {"zero", "renormalize"}:
            problems.append(f"Unknown missing-resume policy '{self.shortlist.missing_resume_policy}'.")
        if not 0 <= self.llm.blend_weight <= 1:
            problems.append("LLM blend weight must be between 0 and 1.")
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
