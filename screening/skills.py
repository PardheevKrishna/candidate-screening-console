"""Skill taxonomy, skill extraction from resume text, and required-skill matching.

Design notes
------------
* ``SKILL_TAXONOMY`` maps a canonical display name to regex synonyms. Synonyms are
  matched case-insensitively (wrap a fragment in ``(?-i:...)`` to make it
  case-sensitive, e.g. ``React`` the library vs. "react" the verb) and must not
  contain capturing groups. Word-boundary guards are added at compile time, so
  "Java" never matches "JavaScript", "SQL" never matches "MySQL" and ".NET" never
  matches "example.net".
* All synonyms are compiled once, at import, into a single alternation ordered
  longest-first, so a resume is scanned in one pass and the most specific phrase
  wins ("SQL Server" before "SQL", "Google Cloud" before "Cloud").
* Ambiguous short names ("C", "R", "Go", "Oracle", "Spring", ...) are only accepted
  as standalone items of a skills / tech-stack list (``LIST_ONLY_TOKENS``), never in
  prose ("go-to person", "R&D", "Oracle (employer)").
* ``SKILL_IMPLIES`` encodes the hierarchy (AWS -> Cloud, Spring Boot -> Java, ...);
  it is applied transitively by ``match_required`` - extraction itself only reports
  skills that are literally present.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache

__all__ = [
    "SKILL_TAXONOMY",
    "SKILL_CATEGORY",
    "SKILL_IMPLIES",
    "LIST_ONLY_TOKENS",
    "CANONICAL_SKILLS",
    "extract_skills",
    "skill_evidence",
    "canonicalize_skill",
    "implied_skills",
    "match_required",
]

# ---------------------------------------------------------------------------
# Taxonomy: category -> canonical skill -> regex synonyms
# ---------------------------------------------------------------------------
_TAXONOMY_BY_CATEGORY: dict[str, dict[str, list[str]]] = {
    "Programming Languages": {
        "Python": [r"python"],
        "Java": [r"java(?!\s?script)", r"j2ee", r"jdk"],
        "JavaScript": [r"java\s?script", r"js", r"ecma\s?script", r"es6"],
        "TypeScript": [r"type\s?script"],
        "C": [r"c\s(?:programming|language)", r"embedded\sc", r"ansi\sc",
              r"c(?=\s?(?:/|,|&|and)\s?c\s?\+\+)"],
        "C++": [r"c\s?\+\+", r"cpp", r"c\splus\splus"],
        "C#": [r"c\s?#", r"c\s?sharp", r"csharp"],
        "Go": [r"golang", r"go\s(?:programming|language|lang)"],
        "Rust": [r"(?-i:Rust)", r"rust\s(?:programming|language|lang)"],
        "Kotlin": [r"kotlin"],
        "Swift": [r"(?-i:Swift)", r"swift\s?ui"],
        "Dart": [r"(?-i:Dart)"],
        "PHP": [r"php"],
        "Ruby": [r"ruby\son\srails", r"ruby\s(?:programming|language|scripting)", r"ror"],
        "Scala": [r"scala"],
        "R": [r"r\s(?:programming|language)", r"r\s?studio", r"ggplot2?", r"tidyverse", r"r\sshiny"],
        "MATLAB": [r"matlab", r"simulink"],
        "Perl": [r"perl"],
        "Bash": [r"bash"],
        "Shell Scripting": [r"shell\s?script(?:ing|s)?", r"unix\sshell"],
        "PowerShell": [r"power\s?shell"],
        "Haskell": [r"haskell"],
        "Julia": [r"julia\s(?:programming|language|lang)"],
        "Assembly": [r"assembly\s(?:language|programming)", r"x86\sassembly", r"arm\sassembly",
                     r"mips\sassembly", r"808[56]\sassembly"],
        "Verilog": [r"verilog", r"system\s?verilog"],
        "VHDL": [r"vhdl"],
        "Solidity": [r"solidity"],
        "HTML": [r"html"],
        "CSS": [r"css", r"scss", r"sass"],
    },
    "Web & Mobile": {
        "React": [r"react\.?\s?js", r"(?-i:React|REACT)(?!\s?native)"],
        "React Native": [r"react\s?native"],
        "Angular": [r"angular\.?\s?js",
                    r"(?-i:Angular|ANGULAR)(?!\s(?:momentum|velocity|frequency|acceleration|displacement))"],
        "Vue.js": [r"vue(?:\.?\s?js)?", r"nuxt(?:\.?\s?js)?"],
        "Next.js": [r"next\.?\s?js"],
        "Node.js": [r"node\.?\s?js"],
        "Express.js": [r"express\.?\s?js"],
        "jQuery": [r"jquery"],
        "Redux": [r"redux"],
        "Bootstrap": [r"bootstrap"],
        "Tailwind CSS": [r"tailwind(?:\s?css)?"],
        "Spring Boot": [r"spring[\s-]?boot"],
        "Spring": [r"spring\s(?:framework|mvc|security|data|cloud|jpa|batch|core)"],
        "Hibernate": [r"hibernate"],
        "Django": [r"django"],
        "Flask": [r"flask"],
        "FastAPI": [r"fast\s?api"],
        ".NET": [r"\.net(?:\s?core|\s?framework)?", r"asp\.net(?:\s?(?:core|mvc))?", r"ado\.net",
                 r"vb\.net", r"dot\s?net"],
        "Laravel": [r"laravel"],
        "REST APIs": [r"rest(?:ful)?\s?apis?", r"rest(?:ful)?\s(?:services|web\sservices|endpoints)", r"restful"],
        "GraphQL": [r"graph\s?ql"],
        "Microservices": [r"micro[\s-]?services?"],
        "gRPC": [r"grpc"],
        "WebSockets": [r"web\s?sockets?", r"socket\.io"],
        "MERN Stack": [r"mern(?:\s?stack)?"],
        "Full Stack Development": [r"full[\s-]?stack"],
        "Android": [r"android"],
        "iOS": [r"ios"],
        "Flutter": [r"flutter"],
    },
    "Data Science & AI": {
        "Machine Learning": [r"machine[\s-]?learning", r"(?-i:ML)"],
        "Deep Learning": [r"deep[\s-]?learning", r"neural\snetworks?", r"deep\sneural\snetworks?",
                          r"artificial\sneural\snetworks?", r"convolutional\sneural\snetworks?",
                          r"recurrent\sneural\snetworks?", r"cnns?", r"rnns?", r"lstms?"],
        "Artificial Intelligence": [r"artificial\sintelligence", r"(?-i:AI)"],
        "Natural Language Processing": [r"natural\slanguage\sprocessing", r"nlp"],
        "Computer Vision": [r"computer\svision", r"image\sprocessing"],
        "Generative AI": [r"generative\sai", r"gen\s?ai", r"llms?", r"large\slanguage\smodels?",
                          r"chat\s?gpt", r"gpt-?\d(?:\.\d)?", r"prompt\sengineering"],
        "LangChain": [r"lang\s?chain"],
        "Hugging Face": [r"hugging\s?face"],
        "Reinforcement Learning": [r"reinforcement\slearning"],
        "Data Science": [r"data\sscience"],
        "Data Analysis": [r"data\sanaly(?:sis|tics|zing|sing)", r"exploratory\sdata\sanalysis", r"(?-i:EDA)"],
        "Data Visualization": [r"data\svisuali[sz]ation"],
        "Statistics": [r"statistics", r"statistical\s(?:analysis|modell?ing|methods|inference)", r"probability"],
        "Pandas": [r"pandas"],
        "NumPy": [r"numpy"],
        "SciPy": [r"scipy"],
        "Matplotlib": [r"matplotlib"],
        "Seaborn": [r"seaborn"],
        "Plotly": [r"plotly"],
        "scikit-learn": [r"scikit[\s-]?learn", r"sci-kit[\s-]?learn", r"sklearn"],
        "TensorFlow": [r"tensor\s?flow"],
        "Keras": [r"keras"],
        "PyTorch": [r"py\s?torch", r"torch\s?vision"],
        "OpenCV": [r"open\s?cv", r"cv2"],
        "XGBoost": [r"xg\s?boost"],
        "NLTK": [r"nltk"],
        "spaCy": [r"spacy"],
        "MLOps": [r"ml\s?ops", r"mlflow", r"kubeflow"],
        "Big Data": [r"big\sdata"],
        "Spark": [r"apache\sspark", r"py\s?spark", r"spark\s?sql", r"(?-i:Spark)"],
        "Hadoop": [r"hadoop", r"hdfs", r"map\s?reduce"],
        "Kafka": [r"kafka"],
        "Airflow": [r"airflow"],
        "ETL": [r"etl", r"elt\spipelines?", r"data\spipelines?"],
        "Data Engineering": [r"data\sengineering"],
        "Data Warehousing": [r"data\s?warehous(?:e|es|ing)", r"star\sschema", r"snowflake\sschema"],
        "Databricks": [r"databricks"],
        "Power BI": [r"power\s?bi"],
        "Tableau": [r"tableau"],
        "Excel": [r"ms\s?excel", r"microsoft\s?excel", r"advanced\s?excel", r"(?-i:Excel|EXCEL)", r"vba"],
        "SAS": [r"sas\s(?:programming|base|enterprise\sminer)", r"base\ssas"],
        "SPSS": [r"spss"],
    },
    "Cloud & DevOps": {
        "Cloud": [r"cloud(?<!word\scloud)(?<!point\scloud)(?<!tag\scloud)"],
        "AWS": [r"aws", r"amazon\sweb\sservices", r"amazon\s(?:ec2|s3|rds|lambda|sagemaker|redshift)", r"ec2",
                r"s3\sbuckets?", r"sagemaker", r"cloud\s?formation", r"cloud\s?watch", r"redshift"],
        "Azure": [r"azure", r"microsoft\sazure", r"az-\d{3}"],
        "GCP": [r"gcp", r"google\scloud(?:\splatform)?", r"app\s?engine", r"vertex\sai"],
        "BigQuery": [r"big\s?query"],
        "Firebase": [r"firebase", r"firestore"],
        "Serverless": [r"serverless"],
        "Docker": [r"docker(?:\s?(?:compose|hub|files?))?", r"containeri[sz]ation"],
        "Kubernetes": [r"kubernetes", r"k8s", r"kubectl", r"openshift", r"helm\scharts?"],
        "Jenkins": [r"jenkins"],
        "CI/CD": [r"ci\s?/\s?cd", r"ci-cd", r"cicd", r"continuous\s(?:integration|delivery|deployment)",
                  r"github\sactions", r"gitlab\sci", r"circle\s?ci", r"travis\s?ci"],
        "Terraform": [r"terraform"],
        "Ansible": [r"ansible"],
        "DevOps": [r"dev\s?ops", r"devsecops"],
        "Linux": [r"linux", r"ubuntu", r"unix", r"centos", r"red\s?hat", r"debian", r"fedora"],
        "Nginx": [r"nginx"],
        "Prometheus": [r"prometheus"],
        "Grafana": [r"grafana"],
    },
    "Databases": {
        "SQL": [r"sql", r"structured\squery\slanguage", r"pl\s?/\s?sql", r"plsql"],
        "MySQL": [r"mysql", r"my-sql", r"(?-i:My\s?SQL|MY\sSQL)"],
        "PostgreSQL": [r"postgre\s?sql", r"postgres(?:ql)?", r"psql"],
        "Oracle": [r"oracle\s?(?:db|database|sql|rdbms|apex|\d{1,2}[gc])"],
        "SQL Server": [r"ms\s?sql\s?server", r"microsoft\s?sql\s?server", r"sql\s?server", r"mssql",
                       r"t-?sql", r"ssms"],
        "SQLite": [r"sqlite"],
        "MariaDB": [r"maria\s?db"],
        "NoSQL": [r"no-?sql"],
        "MongoDB": [r"mongo\s?db", r"mongo", r"mongoose"],
        "Redis": [r"redis"],
        "Cassandra": [r"cassandra"],
        "DynamoDB": [r"dynamo\s?db"],
        "Elasticsearch": [r"elastic\s?search", r"elk\sstack", r"kibana"],
        "Neo4j": [r"neo4j"],
        "Snowflake": [r"snowflake"],
        "SQLAlchemy": [r"sql\s?alchemy"],
    },
    "Tools & Practices": {
        "Git": [r"git"],
        "Jira": [r"jira"],
        "Postman": [r"postman"],
        "Maven": [r"maven"],
        "Gradle": [r"gradle"],
        "JUnit": [r"junit"],
        "Selenium": [r"selenium"],
        "Pytest": [r"py\.?test"],
        "Unit Testing": [r"unit\stest(?:s|ing)?", r"tdd", r"test[\s-]driven\sdevelopment"],
        "Agile": [r"agile", r"scrum", r"kanban"],
        "Jupyter": [r"jupyter", r"ipython", r"colab", r"google\scolab"],
        "Figma": [r"figma"],
        "UI/UX Design": [r"ui\s?/\s?ux", r"ux\sdesign", r"ui\sdesign", r"user\sexperience\sdesign"],
        "Microsoft Office": [r"ms[\s-]?office", r"microsoft[\s-]?office"],
        "Arduino": [r"arduino"],
        "Raspberry Pi": [r"raspberry\s?pi"],
        "IoT": [r"iot", r"internet\sof\sthings"],
        "Embedded Systems": [r"embedded\s(?:systems?|programming|software)", r"microcontrollers?"],
        "Blockchain": [r"block\s?chain", r"ethereum", r"web3", r"smart\scontracts?"],
        "Cybersecurity": [r"cyber[\s-]?security", r"information\ssecurity", r"network\ssecurity",
                          r"ethical\shacking", r"penetration\stesting", r"owasp"],
        "Cryptography": [r"cryptography"],
        "SAP": [r"(?-i:SAP)", r"abap"],
        "Salesforce": [r"salesforce"],
        "AutoCAD": [r"auto\s?cad"],
    },
    "CS Fundamentals": {
        "Data Structures & Algorithms": [
            r"data\s?structures?(?:\s?(?:&|and|,)\s?algorithms?)?",
            r"algorithms?\s?(?:&|and)\s?data\s?structures?",
            r"dsa", r"design\s(?:and|&)\sanalysis\sof\salgorithms", r"daa",
        ],
        "OOP": [r"oops?(?:\sconcepts)?", r"object[\s-]oriented(?:\s(?:programming|design|analysis|concepts))?",
                r"ooad"],
        "DBMS": [r"dbms", r"rdbms", r"database\smanagement(?:\ssystems?)?"],
        "Operating Systems": [r"operating\ssystems?"],
        "Computer Networks": [r"computer\snetwork(?:s|ing)?", r"tcp\s?/\s?ip", r"osi\smodel"],
        "Computer Architecture": [r"computer\s(?:architecture|organi[sz]ation)"],
        "System Design": [r"system\sdesign", r"low[\s-]level\sdesign", r"high[\s-]level\sdesign"],
        "Design Patterns": [r"design\spatterns?"],
        "Distributed Systems": [r"distributed\s(?:systems?|computing)"],
        "Multithreading": [r"multi[\s-]?threading", r"concurrency", r"concurrent\sprogramming",
                           r"parallel\sprogramming"],
        "Competitive Programming": [r"competitive\s(?:programming|coding)"],
        "Problem Solving": [r"problem[\s-]solving"],
        "SDLC": [r"sdlc", r"software\sdevelopment\slife\s?cycle"],
        "Compiler Design": [r"compiler\sdesign"],
    },
}

SKILL_TAXONOMY: dict[str, list[str]] = {
    skill: synonyms
    for category in _TAXONOMY_BY_CATEGORY.values()
    for skill, synonyms in category.items()
}
SKILL_CATEGORY: dict[str, str] = {
    skill: category
    for category, skills in _TAXONOMY_BY_CATEGORY.items()
    for skill in skills
}
CANONICAL_SKILLS: list[str] = sorted(SKILL_TAXONOMY, key=str.lower)

# Short / ambiguous names accepted only as a standalone item of a skills or
# tech-stack list ("Languages: C, Python", "Tech: React | Node").
LIST_ONLY_TOKENS: dict[str, list[str]] = {
    "C": ["c"],
    "R": ["r"],
    "Go": ["go"],
    "Oracle": ["oracle"],
    "Spring": ["spring"],
    "React": ["react"],
    "Angular": ["angular"],
    "Node.js": ["node"],
    "Express.js": ["express"],
    "Ruby": ["ruby", "rails"],
    "Julia": ["julia"],
    "Rust": ["rust"],
    "Swift": ["swift"],
    "Dart": ["dart"],
    "Spark": ["spark"],
    "Excel": ["excel"],
    "SAS": ["sas"],
    "Assembly": ["assembly", "asm"],
    "Shell Scripting": ["shell"],
    "TypeScript": ["ts"],
    "Machine Learning": ["ml"],
    "Deep Learning": ["dl"],
    "Artificial Intelligence": ["ai"],
    "Computer Vision": ["cv"],
    "Operating Systems": ["os"],
    "Computer Networks": ["cn"],
}

# Hierarchy: having the key skill implies the listed skills (applied transitively).
SKILL_IMPLIES: dict[str, list[str]] = {
    # Cloud platforms
    "AWS": ["Cloud"],
    "Azure": ["Cloud"],
    "GCP": ["Cloud"],
    "Serverless": ["Cloud"],
    "BigQuery": ["GCP", "SQL"],
    "DynamoDB": ["AWS", "NoSQL"],
    # Relational databases -> SQL
    "MySQL": ["SQL"],
    "PostgreSQL": ["SQL"],
    "Oracle": ["SQL"],
    "SQL Server": ["SQL"],
    "SQLite": ["SQL"],
    "MariaDB": ["SQL"],
    "Snowflake": ["SQL"],
    "SQLAlchemy": ["SQL", "Python"],
    # Non-relational databases
    "MongoDB": ["NoSQL"],
    "Cassandra": ["NoSQL"],
    "Redis": ["NoSQL"],
    "Neo4j": ["NoSQL"],
    # Java ecosystem
    "Spring Boot": ["Java", "Spring"],
    "Spring": ["Java"],
    "Hibernate": ["Java"],
    "JUnit": ["Java"],
    "Maven": ["Java"],
    # Python ecosystem
    "Django": ["Python"],
    "Flask": ["Python"],
    "FastAPI": ["Python"],
    "Pandas": ["Python"],
    "NumPy": ["Python"],
    "SciPy": ["Python"],
    "Matplotlib": ["Python"],
    "Seaborn": ["Python"],
    "Pytest": ["Python"],
    "NLTK": ["Python", "Natural Language Processing"],
    "spaCy": ["Python", "Natural Language Processing"],
    # ML frameworks
    "PyTorch": ["Machine Learning", "Deep Learning", "Python"],
    "TensorFlow": ["Machine Learning", "Deep Learning"],
    "Keras": ["Machine Learning", "Deep Learning", "Python"],
    "scikit-learn": ["Machine Learning", "Python"],
    "XGBoost": ["Machine Learning"],
    "Deep Learning": ["Machine Learning"],
    "Reinforcement Learning": ["Machine Learning"],
    "MLOps": ["Machine Learning"],
    "OpenCV": ["Computer Vision"],
    "Hugging Face": ["Natural Language Processing", "Deep Learning"],
    "LangChain": ["Generative AI"],
    # JavaScript ecosystem
    "React": ["JavaScript"],
    "Angular": ["JavaScript"],
    "Node.js": ["JavaScript"],
    "Vue.js": ["JavaScript"],
    "jQuery": ["JavaScript"],
    "TypeScript": ["JavaScript"],
    "Redux": ["JavaScript"],
    "Express.js": ["Node.js"],
    "Next.js": ["React"],
    "React Native": ["React"],
    "MERN Stack": ["MongoDB", "Express.js", "React", "Node.js"],
    # Misc
    "Tailwind CSS": ["CSS"],
    "Flutter": ["Dart"],
    "Laravel": ["PHP"],
    "Jenkins": ["CI/CD"],
    "Databricks": ["Spark"],
    "Spark": ["Big Data"],
    "Hadoop": ["Big Data"],
    "Solidity": ["Blockchain"],
}

# Extra user-input spellings for canonicalize_skill (lowercase keys).
_ALIASES: dict[str, str] = {
    "rest": "REST APIs",
    "rest api": "REST APIs",
    "apis": "REST APIs",
    "cloud computing": "Cloud",
    "golang": "Go",
    "ds&a": "Data Structures & Algorithms",
    "ds & algo": "Data Structures & Algorithms",
    "algorithms": "Data Structures & Algorithms",
    "data structures": "Data Structures & Algorithms",
    "object oriented programming": "OOP",
    "ms excel": "Excel",
    "nodejs": "Node.js",
    "reactjs": "React",
    "vuejs": "Vue.js",
    "dotnet": ".NET",
    "postgres": "PostgreSQL",
    "k8s": "Kubernetes",
    "gcp": "GCP",
    "google cloud": "GCP",
    "amazon web services": "AWS",
    "microsoft azure": "Azure",
}

# ---------------------------------------------------------------------------
# Compiled matchers (built once at import)
# ---------------------------------------------------------------------------
# Left: not inside a word, and not right after "word." (rejects "three.js" -> js,
# "example.net" -> .NET). Right: no trailing letters; a version suffix is fine
# ("Python3", "HTML5", "Java8") as long as no letters follow it.
_LEFT_GUARD = r"(?<![A-Za-z0-9_])(?<![A-Za-z]\.)"
_RIGHT_GUARD = r"(?![A-Za-z_]|\d+[A-Za-z_])"
_LEFT_GUARD_LC = r"(?<![a-z0-9_])(?<![a-z]\.)"
_RIGHT_GUARD_LC = r"(?![a-z_]|\d+[a-z_])"
_REGEX_SYNTAX_RE = re.compile(r"\(\?<?[!=][^)]*\)|\(\?-?i:|\\[sdwb]|[\\^$.*+?()\[\]{}|:<>!=-]")
_CASE_SENSITIVE_PREFIX = "(?-i:"


def _literal_len(pattern: str) -> int:
    """Approximate number of literal characters in a synonym (longest-first ordering)."""
    return len(_REGEX_SYNTAX_RE.sub("", pattern))


def _has_top_level_alternation(pattern: str) -> bool:
    depth, escaped, in_class = 0, False, False
    for ch in pattern:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            return True
    return False


def _split_head(pattern: str) -> tuple[str, str] | None:
    """('p', 'ython') for 'python'; None when the first char is not a plain literal."""
    if pattern[:1] == "\\" and len(pattern) > 2 and not pattern[1].isalnum():
        head, rest = pattern[:2], pattern[2:]
    elif pattern[:1].isalnum():
        head, rest = pattern[0], pattern[1:]
    else:
        return None
    if rest[:1] in {"?", "*", "+", "{"} or _has_top_level_alternation(rest):
        return None
    return head, rest


def _build_scanners() -> tuple[re.Pattern[str], list[tuple[str, re.Pattern[str]]], re.Pattern[str], dict[str, str]]:
    """Two single-pass scanners.

    1. Case-insensitive synonyms, matched on lower-cased text *without* IGNORECASE and
       factored by first character - this keeps sre's fast first-char branch check
       (named groups / IGNORECASE would disable it and make scanning ~10x slower).
       The matched text is mapped back to its skill afterwards (cached).
    2. The few case-sensitive synonyms ("React", "ML", ...) on the original text.
    """
    entries = [(skill, pat) for skill, pats in SKILL_TAXONOMY.items() for pat in pats]
    entries.sort(key=lambda sp: -_literal_len(sp[1]))  # stable: taxonomy order breaks ties
    ci = [(s, p) for s, p in entries if not p.startswith(_CASE_SENSITIVE_PREFIX)]
    cs = [(s, p) for s, p in entries if p.startswith(_CASE_SENSITIVE_PREFIX)]

    heads: dict[str, list[str]] = {}
    unfactored: list[str] = []
    for _, pat in ci:
        split = _split_head(pat)
        if split is None:
            unfactored.append(f"(?:{pat})")
        else:
            heads.setdefault(split[0], []).append(split[1])
    branches = unfactored + [f"{head}(?:{'|'.join(rests)})" for head, rests in heads.items()]
    ci_re = re.compile(_LEFT_GUARD_LC + "(?:" + "|".join(branches) + ")" + _RIGHT_GUARD_LC)
    ci_each = [(s, re.compile(_LEFT_GUARD_LC + f"(?:{p})" + _RIGHT_GUARD_LC)) for s, p in ci]

    cs_groups = {f"c{i}": s for i, (s, _) in enumerate(cs)}
    cs_re = re.compile(
        _LEFT_GUARD + "(?:" + "|".join(f"(?P<c{i}>{p})" for i, (_, p) in enumerate(cs)) + ")" + _RIGHT_GUARD,
        re.IGNORECASE,
    )
    return ci_re, ci_each, cs_re, cs_groups


_CI_RE, _CI_EACH, _CS_RE, _CS_GROUP_SKILL = _build_scanners()
_HIT_CACHE: dict[str, str] = {}   # matched lower-case text -> skill (bounded below)
_HIT_CACHE_MAX = 20_000


def _resolve_hit(lower: str, start: int, end: int) -> str | None:
    hit = lower[start:end]
    skill = _HIT_CACHE.get(hit)
    if skill is None:
        for cand, rx in _CI_EACH:
            m = rx.match(lower, start)
            if m and m.end() == end:
                skill = cand
                break
        if skill is not None:
            if len(_HIT_CACHE) >= _HIT_CACHE_MAX:
                _HIT_CACHE.clear()
            _HIT_CACHE[hit] = skill
    return skill


def _scan(text: str) -> list[tuple[str, str]]:
    """(skill, matched text) for every taxonomy hit in ``text`` (single pass per scanner)."""
    lower = text.lower()
    hits: list[tuple[str, str]] = []
    for m in _CI_RE.finditer(lower):
        skill = _resolve_hit(lower, m.start(), m.end())
        if skill:
            hits.append((skill, m.group()))
    for m in _CS_RE.finditer(text):
        if m.lastgroup:
            hits.append((_CS_GROUP_SKILL[m.lastgroup], m.group()))
    return hits


_LIST_TOKEN_SKILL: dict[str, str] = {
    tok: skill for skill, toks in LIST_ONLY_TOKENS.items() for tok in toks
}
_LIST_PREFILTER_RE = re.compile(
    r"(?<![A-Za-z0-9_.+#])(?:"
    + "|".join(re.escape(t) for t in sorted(_LIST_TOKEN_SKILL, key=len, reverse=True))
    + r")(?![A-Za-z0-9_+#])",
    re.IGNORECASE,
)
# "Languages: C, Java" / "Tech Stack - React | Node" -> label + rest
_LABEL_RE = re.compile(
    r"^\s*(?P<label>[A-Za-z][A-Za-z0-9 &/+().'-]{0,40}?)\s*(?::|\s[-–—|]\s|\|)\s*(?P<rest>.*)$"
)
_TECH_LABEL_RE = re.compile(
    r"skill|language|tech|stack|tool|framework|librar|platform|database|\bdbs?\b|software|"
    r"environment|built|using|used|competenc|proficien|expert|programming|coding|core|subject|"
    r"course|familiar|knowledge|known|\bide|cloud|devops|web|back\s?end|front\s?end|mobile|"
    r"\bdata\b|\bml\b|\bai\b|interest|other|misc|concept|fundamental|areas",
    re.IGNORECASE,
)
_ITEM_SPLIT_RE = re.compile(r"\s*(?:[,;|/•·●▪]|\s[-–—]\s|\t|\s{2,}|\band\b|&)\s*", re.IGNORECASE)
_PAREN_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_FILLER_RE = re.compile(
    r"^(?:(?:basic|basics|working|good|strong|sound|hands-on|advanced|intermediate)\s+)?"
    r"(?:knowledge|proficien\w*|familiar\w*|experience[d]?|expertise|understanding|skilled|exposure|"
    r"basics|fundamentals|programming|coding)\s+(?:in|with|of|on)\s+",
    re.IGNORECASE,
)
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[•●▪‣◦⁃∙·⦁○◆◇■□►▶➢➤✓✔❖♦★→*>–—-]+|o(?=\s))\s*")
_WS_RE = re.compile(r"\s+")
# URLs / e-mails are removed before scanning ("github.com/u/python-notes" is not evidence).
# Lookbehinds anchor each alternative at the start of a token: without them a long run
# of non-space characters (pasted base64, minified code) backtracks quadratically.
_NOISE_RE = re.compile(
    r"(?<![\w.%+-])[\w.%+-]+@[\w-]+(?:\.[\w-]+)+|(?<![\w/.:-])(?:https?://|www\.)\S+|"
    r"(?<![\w.-])[\w-]+(?:\.[\w-]+)*\.(?:com|org|co\.in|in|edu|gov)\b(?:/\S*)?",
    re.IGNORECASE,
)


def _ancestors_closure() -> dict[str, frozenset[str]]:
    closure: dict[str, frozenset[str]] = {}
    for skill in set(SKILL_IMPLIES) | set(SKILL_TAXONOMY):
        seen: set[str] = set()
        stack = list(SKILL_IMPLIES.get(skill, []))
        while stack:
            parent = stack.pop()
            if parent in seen or parent == skill:
                continue
            seen.add(parent)
            stack.extend(SKILL_IMPLIES.get(parent, []))
        closure[skill] = frozenset(seen)
    return closure


_IMPLIED: dict[str, frozenset[str]] = _ancestors_closure()
_CANON_BY_LOWER: dict[str, str] = {s.lower(): s for s in SKILL_TAXONOMY}
_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that may surround a skill name in user input without changing its meaning.
_FILLER_WORDS = frozenset(
    "programming language languages framework frameworks library libraries basic basics advanced "
    "development developer computing platform platforms services service database databases db "
    "tool tools concepts fundamentals skills skill and with using in of core version cloud "
    "certified certification v".split()
)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _clean_item(item: str) -> str:
    item = _PAREN_RE.sub(" ", item)
    item = _FILLER_RE.sub("", item.strip())
    item = item.strip(" .:;'\"*•–—-").lower()
    return _WS_RE.sub(" ", item)


def _list_token_skills(text: str, assume_list: bool) -> set[str]:
    """Skills from LIST_ONLY_TOKENS that appear as standalone items of a tech list."""
    found: set[str] = set()
    for line in text.split("\n"):
        if not _LIST_PREFILTER_RE.search(line):
            continue
        content, labelled = line, False
        lm = _LABEL_RE.match(line)
        if lm and _TECH_LABEL_RE.search(lm.group("label")):
            content, labelled = lm.group("rest"), True
        items = [x for x in (_clean_item(p) for p in _ITEM_SPLIT_RE.split(_BULLET_PREFIX_RE.sub("", content))) if x]
        hits = {_LIST_TOKEN_SKILL[x] for x in items if x in _LIST_TOKEN_SKILL}
        if not hits:
            continue
        if assume_list or labelled:
            found |= hits
            continue
        # Unlabelled line: accept only if it really is a short-item list of tech names.
        if len(items) >= 2 and all(len(x.split()) <= 4 for x in items):
            others = [x for x in items if x not in _LIST_TOKEN_SKILL]
            if len(hits) >= 2 or any(_scan(x) for x in others):
                found |= hits
    return found


def _extract(text: str, *, assume_list: bool = False) -> set[str]:
    if not text or not text.strip():
        return set()
    clean = _NOISE_RE.sub(" ", text)
    found = {skill for skill, _ in _scan(clean)}
    found |= _list_token_skills(clean, assume_list)
    return found


def extract_skills(text: str) -> list[str]:
    """Canonical skills literally present in ``text`` (sorted, case-insensitive order).

    Implied skills are *not* added (see ``implied_skills`` / ``match_required``).
    """
    return sorted(_extract(text or ""), key=str.lower)


def skill_evidence(sections: Mapping[str, str]) -> dict[str, list[str]]:
    """skill -> section keys where it was seen (section order preserved, skills sorted).

    The "skills" section is treated as a list, so bare "C" / "R" / "Go" count there.
    """
    evidence: dict[str, list[str]] = {}
    for key, body in (sections or {}).items():
        for skill in _extract(body or "", assume_list=(key == "skills")):
            evidence.setdefault(skill, []).append(key)
    return {skill: evidence[skill] for skill in sorted(evidence, key=str.lower)}


# ---------------------------------------------------------------------------
# Canonicalisation & matching
# ---------------------------------------------------------------------------
@lru_cache(maxsize=4096)
def canonicalize_skill(name: str) -> str:
    """Map user input to a canonical skill ("postgres" -> "PostgreSQL", "k8s" -> "Kubernetes").

    Unknown names are returned stripped (whitespace collapsed); all-lowercase input is
    title-cased ("kafka streams" -> "Kafka Streams"), anything else is kept as typed.
    """
    s = _WS_RE.sub(" ", str(name or "")).strip().strip(",;:")
    if not s:
        return ""
    key = s.lower()
    if key in _CANON_BY_LOWER:
        return _CANON_BY_LOWER[key]
    if key in _ALIASES:
        return _ALIASES[key]
    if key in _LIST_TOKEN_SKILL:
        return _LIST_TOKEN_SKILL[key]
    hits = _scan(s)
    if len(hits) == 1 and len(hits[0][1]) == len(s):
        return hits[0][0]
    # "Python programming", "Java 8", "AWS Cloud": known skills plus generic filler only.
    leftover = key
    for _, matched in hits:
        leftover = leftover.replace(matched.lower(), " ", 1)
    if hits and all(w in _FILLER_WORDS or w.isdigit() for w in _WORD_RE.findall(leftover)):
        found = {skill for skill, _ in hits}
        if len(found) == 1:
            return next(iter(found))
        # "AWS Cloud" -> AWS: the unique skill that implies all the others.
        specific = [f for f in found if (found - {f}) <= _IMPLIED.get(f, frozenset())]
        if len(specific) == 1:
            return specific[0]
    return s.title() if s.islower() else s


def implied_skills(found: Iterable[str]) -> set[str]:
    """``found`` (canonicalised) plus everything they imply, transitively."""
    out: set[str] = set()
    for f in found or ():
        c = canonicalize_skill(f)
        if c:
            out.add(c)
            out |= _IMPLIED.get(c, frozenset())
    return out


@lru_cache(maxsize=1024)
def _literal_skill_re(skill: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in skill.split()]
    return re.compile(r"(?<![A-Za-z0-9_])" + r"\s+".join(words) + r"(?![A-Za-z0-9_])", re.IGNORECASE)


def match_required(required: list[str], found: list[str], text: str = "") -> tuple[list[str], list[str]]:
    """Split ``required`` into (matched, missing), preserving the order of ``required``.

    A required skill matches if it was found directly, is implied by a found skill
    (transitively, e.g. AWS -> Cloud), or - for skills outside the taxonomy - occurs
    in ``text`` as a whole word (case-insensitive). Returned names are canonical and
    de-duplicated.
    """
    have = implied_skills(found or ())
    matched: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for req in required or ():
        canon = canonicalize_skill(req)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        ok = canon in have
        if not ok and canon not in SKILL_TAXONOMY and text:
            raw = _WS_RE.sub(" ", str(req)).strip()
            ok = bool(_literal_skill_re(raw).search(text)) or (
                canon != raw and bool(_literal_skill_re(canon).search(text)))
        (matched if ok else missing).append(canon)
    return matched, missing
