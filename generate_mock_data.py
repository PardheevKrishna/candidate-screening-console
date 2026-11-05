"""Synthetic campus-hiring dataset generator.  ALL DATA PRODUCED HERE IS SYNTHETIC.

Builds a demo / test dataset for the Candidate Screening & Integrity Console:

* ``ta_feed.csv``   - what a HackerRank-for-Work CSV export looks like after the TA team
  appends candidate details and a few TA columns (human headers, messy on purpose);
* ``resumes/``      - campus resumes as PDF / DOCX / TXT in three layouts (+ ``resumes.zip``);
* ``job_description.txt`` and ``README.md``;
* ``_ground_truth/`` - labels for the integrity patterns planted among the 100% scorers,
  the decoys that must NOT be flagged, and every injected data-quality problem.

Everything is fictional: people are random combinations of common Indian first and last
names, colleges and companies are invented (any resemblance to a real institution is
coincidental), e-mail addresses use only the reserved example.com / example.org /
example.net domains, phone numbers are random "+91 9xxxxxxxxx" strings and IP addresses
are random public-looking IPv4 addresses. Never treat the output as real candidate data.

Usage::

    python generate_mock_data.py [--n 3000] [--seed 42] [--out data/sample] [--no-zip]
                                 [--formats pdf,docx,txt] [--quick] [--workers N]

The same ``--n``/``--seed`` always produce byte-identical ``ta_feed.csv``, resumes and
ground truth (resume formats only change how resumes are rendered, never the TA feed).
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import stat
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from screening.config import RiskConfig

# ---------------------------------------------------------------------------
# Dataset shape
# ---------------------------------------------------------------------------
DEFAULT_N = 3000
QUICK_N = 300
MIN_N = 150
MAX_N = 12_000
DEFAULT_SEED = 42
DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "sample"
MARKER_FILE = ".synthetic_dataset"
GENERATED_ENTRIES = ("ta_feed.csv", "resumes", "resumes.zip", "job_description.txt",
                     "README.md", "_ground_truth")

FORMATS = ("pdf", "docx", "txt")
FORMAT_WEIGHTS = {"pdf": 0.60, "docx": 0.30, "txt": 0.10}
LAYOUTS = ("classic", "modern", "minimal")
PARALLEL_MIN_JOBS = 400          # below this, rendering in-process is faster than a pool
RENDER_CHUNK = 40
FIXED_ZIP_TIME = (2026, 10, 31, 12, 0, 0)

SET_B_RATE = 0.11
NO_ATTEMPT_RATE = 0.008
RESUME_RATE = 0.95
BOILERPLATE_RATE = 0.084         # of normal resumes -> ~8% of all resumes
ALT_DATETIME_RATE = 0.05
COLLEGE_VARIANT_RATE = 0.03
N_DUPLICATES = 8
N_MISSING_EMAILS = 3
N_SCORE_ABOVE_MAX = 2
N_TIME_HMS = 8
N_CGPA_PERCENT = 10
N_V2 = 5
WALK_INS_FULL = 25
LEGIT_INDIVIDUAL_CAP = 40.0      # legit candidates never carry more stray flags than this

# Planted integrity patterns (all members score 100%)
RING = "ring_shared_solution"
PROXY = "proxy_test_taker"
MILL = "resume_mill"
HOSTEL = "synchronized_hostel"
SPEEDRUN = "leaked_paper_speedrun"
IDENTITY = "identity_overlap"
PATTERN_SIZES = {RING: 8, PROXY: 6, MILL: 7, HOSTEL: 10, SPEEDRUN: 8, IDENTITY: 3}
PATTERN_KEYS = {RING: "ring_01", PROXY: "proxy_01", MILL: "mill_01", HOSTEL: "hostel_01",
                SPEEDRUN: "speedrun_01", IDENTITY: "identity_01"}
# Floor a sane engine should reach with default weights (with a safety margin).
PATTERN_MIN_LEVEL = {RING: "High", PROXY: "High", HOSTEL: "High", SPEEDRUN: "High",
                     MILL: "Medium", IDENTITY: "Medium"}
DECOY_SIZE = 15
LAB_SIZE_FULL = 40
DECOY_LAB = ("decoy_lab_01", "oncampus_lab_shared_ip")
DECOY_BOILERPLATE = ("decoy_boilerplate_01", "boilerplate_resume_bullets")
DECOY_MCQ = ("decoy_mcq_key_01", "common_mcq_answer_key")

# ---------------------------------------------------------------------------
# HackerRank-style test definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestSet:
    """One test variant: name, HackerRank test id, per-question max and score unit."""

    name: str
    test_id: int
    maxes: tuple[float, ...]
    units: tuple[float, ...]   # MCQ: marks per question; coding: marks per test case

    @property
    def total(self) -> float:
        return float(sum(self.maxes))


TEST_SETS = {
    "A": TestSet("Campus SWE Online Assessment 2026 - Set A", 1853214, (20, 20, 30, 30), (2, 2, 2, 2.5)),
    "B": TestSet("Campus SWE Online Assessment 2026 - Set B", 1853227, (30, 30, 45, 45), (3, 3, 3, 3.75)),
}
MCQ_QUESTIONS = 10
MCQ_KEY = "BDACADBCAB"    # the MCQ section is common to both sets -> one answer-key hash
TEST_DAYS = (date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3))
WINDOW_START_H, WINDOW_END_H = 9, 23
TIME_LIMIT_MIN = 90
COMPONENTS = ("low", "mid", "high", "perfect")
COMPONENT_RANGES = {"low": (0.0, 40.0), "mid": (40.0, 75.0), "high": (75.0, 100.0)}
TIER_COMPONENT_WEIGHTS = {1: (28, 36, 30, 6), 2: (35, 36, 23, 4), 3: (40, 35, 20, 3)}
PROCTOR_FLAG_CHOICES = (("Candidate not visible", 40), ("Multiple faces detected", 20),
                        ("Phone detected", 20), ("Background voices detected", 15),
                        ("Face mismatch with profile photo", 5))

TA_COLUMNS = [
    "Candidate ID", "Test Candidate ID", "Full Name", "Email", "Phone Number", "College",
    "College Tier", "Degree", "Branch", "Year of Passing", "CGPA", "City", "Test Name", "Status",
    "Invited On", "Invited By", "Attempt Start Time", "Attempt End Time", "Time Taken (mins)",
    "Score", "Max Score", "Percentage Score", "Q1 Score (MCQ - CS Fundamentals)",
    "Q2 Score (Coding - Arrays)", "Q3 Score (Coding - Dynamic Programming)",
    "Q4 Score (Coding - Graphs)", "Q2 Code Similarity %", "Q3 Code Similarity %",
    "Q4 Code Similarity %", "Max Code Similarity %", "Most Similar Candidate", "Plagiarism Status",
    "Out of Window Events", "Out of Window Duration (sec)", "Editor Paste Count",
    "Secure Mode Exit Count", "IP Address", "Integrity Status", "Proctoring Flags", "Test Mode",
    "Source", "Q1 Answer Hash", "Q2 Code Hash", "Q3 Code Hash", "Q4 Code Hash", "Report URL",
]
LABEL_COLUMNS = ["candidate_id", "label", "cluster_key", "pattern", "expected_min_level"]

EMAIL_DOMAINS = (("example.com", 45), ("example.org", 20), ("example.net", 15), ("mail.example.com", 20))
RESERVED_EMAIL_DOMAINS = tuple(d for d, _ in EMAIL_DOMAINS)
TA_INVITERS = ("ta.campus@example.com", "ta.engineering@example.com", "university.hiring@example.com")
SOURCES = (("Campus Drive", 70), ("Job Portal", 14), ("Referral", 9), ("Hackathon", 7))
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# First octets of public unicast space (no private / CGNAT / loopback / documentation ranges).
PUBLIC_FIRST_OCTETS = (14, 27, 36, 42, 43, 45, 49, 59, 61, 101, 103, 106, 110, 111, 112, 114, 115,
                       116, 117, 119, 120, 121, 122, 123, 124, 125, 139, 157, 163, 171, 175, 180,
                       182, 183, 202, 210, 219, 223)

# ---------------------------------------------------------------------------
# Vocabulary: people, places, organisations (all names invented or generic)
# ---------------------------------------------------------------------------
FIRST_NAMES = (
    "Aarav", "Aditya", "Akash", "Aman", "Ananya", "Anjali", "Ankit", "Arjun", "Arnav", "Ayesha",
    "Deepak", "Divya", "Gaurav", "Harsh", "Ishaan", "Kabir", "Kavya", "Kunal", "Manish", "Megha",
    "Mehak", "Mohit", "Nandini", "Neha", "Nikhil", "Pooja", "Pranav", "Priya", "Rahul", "Riya",
    "Rohit", "Sakshi", "Sanya", "Shivam", "Shreya", "Simran", "Sneha", "Tanvi", "Suresh", "Vivek",
    "Yash", "Zoya", "Harpreet", "Gurleen", "Jaspreet", "Manpreet", "Navjot", "Ishita", "Raghav",
    "Tushar", "Abhinav", "Aishwarya", "Anirudh", "Arun", "Bhavana", "Tarun", "Dinesh", "Gayathri",
    "Harini", "Janani", "Karthik", "Keerthana", "Lakshmi", "Madhavi", "Manoj", "Meenakshi",
    "Harsha", "Nithya", "Pavan", "Pradeep", "Ramya", "Mohan", "Sandeep", "Sangeetha", "Santhosh",
    "Shruthi", "Sindhu", "Srinivas", "Swathi", "Tejaswini", "Gautam", "Venkat", "Vignesh", "Vishnu",
    "Yamini", "Akhil", "Athira", "Jithin", "Reshma", "Nikhila", "Charan", "Sravani", "Lokesh",
    "Bharath", "Chaitanya", "Anoop", "Haritha", "Dhanush", "Kavin", "Pranitha", "Abhishek",
    "Ankita", "Arindam", "Debanjan", "Debasish", "Moumita", "Payel", "Rajdeep", "Ritika", "Sayan",
    "Soumya", "Subhajit", "Sudipta", "Tanmoy", "Trisha", "Anwesha", "Sagarika", "Biswajit",
    "Pritam", "Smruti", "Subham", "Ipsita", "Satyajit", "Rupsa", "Soham", "Aditi", "Chinmay",
    "Dhruv", "Hetal", "Jay", "Krupa", "Mihir", "Nidhi", "Omkar", "Parth", "Prachi", "Rutuja",
    "Sanket", "Shruti", "Siddhesh", "Tejas", "Urvi", "Vaibhav", "Yogesh", "Aniket", "Ketaki",
    "Mitali", "Rushikesh", "Saloni", "Kshitij", "Bhargav", "Dhriti", "Jahnavi", "Pallavi", "Rupam",
    "Mridul", "Lalit", "Tenzin", "Dorjee", "Anushka", "Mohammed", "Fathima", "Imran", "Sana",
    "Arif", "Zainab", "Joel", "Alisha", "Kevin", "Nivedita",
)
LAST_NAMES = (
    "Sharma", "Verma", "Gupta", "Singh", "Kumar", "Agarwal", "Bansal", "Chauhan", "Malhotra",
    "Kapoor", "Mehra", "Saxena", "Srivastava", "Tiwari", "Pandey", "Mishra", "Yadav", "Rastogi",
    "Bhatia", "Arora", "Gill", "Sandhu", "Dhillon", "Grewal", "Sethi", "Khanna", "Chaudhary",
    "Rawat", "Negi", "Bisht", "Reddy", "Bhatt", "Naidu", "Iyer", "Iyengar", "Nair", "Menon",
    "Pillai", "Krishnan", "Subramanian", "Raman", "Hegde", "Shetty", "Kamath", "Bhat", "Gowda",
    "Prasad", "Varma", "Chowdary", "Murthy", "Rajan", "Kurian", "Thomas", "Joseph", "Varghese",
    "Mathew", "Abraham", "Ramesh", "Sundaram", "Natarajan", "Banerjee", "Chatterjee", "Mukherjee",
    "Ghosh", "Bose", "Das", "Dutta", "Sen", "Roy", "Saha", "Chakraborty", "Mondal", "Mohanty",
    "Patnaik", "Panda", "Sahoo", "Nayak", "Behera", "Jena", "Pradhan", "Patel", "Shah", "Desai",
    "Mehta", "Joshi", "Kulkarni", "Deshpande", "Patil", "Pawar", "Jadhav", "Shinde", "Gaikwad",
    "Kale", "Bhosale", "Naik", "Sawant", "Parikh", "Trivedi", "Pandya", "Baruah", "Bora", "Gogoi",
    "Hazarika", "Kalita", "Saikia", "Sangma", "Lyngdoh", "Khan", "Ahmed", "Ansari", "Qureshi",
    "Shaikh", "Siddiqui", "Hussain", "Mirza", "Fernandes", "Pinto", "Rodrigues", "Lobo",
)


@dataclass(frozen=True)
class College:
    name: str
    city: str
    tier: int


COLLEGES = tuple(College(n, c, t) for n, c, t in (
    ("Shrivatsa Institute of Technology", "Bengaluru", 1),
    ("Meghdoot Institute of Technology", "Pune", 1),
    ("Trivikram Institute of Engineering and Technology", "Hyderabad", 1),
    ("Saptarishi Institute of Technology", "Chennai", 1),
    ("Kalpavriksha Institute of Science and Technology", "New Delhi", 1),
    ("Hemadri Institute of Technology", "Mumbai", 1),
    ("Varunodaya Institute of Technology", "Kolkata", 1),
    ("Pratibimb Institute of Technology", "Ahmedabad", 1),
    ("Deccan Plateau Institute of Technology", "Hyderabad", 2),
    ("Golconda Ridge College of Engineering", "Hyderabad", 2),
    ("Musi Valley Institute of Technology", "Hyderabad", 2),
    ("Nilachal College of Engineering", "Bhubaneswar", 2),
    ("Brahmaputra Valley Institute of Technology", "Guwahati", 2),
    ("Malabar Coast College of Engineering", "Kochi", 2),
    ("Godavari Delta Institute of Technology", "Visakhapatnam", 2),
    ("Sahyadri Ridge College of Engineering", "Pune", 2),
    ("Coromandel Institute of Technology", "Chennai", 2),
    ("Tapti Valley College of Engineering", "Surat", 2),
    ("Doon Heights Institute of Technology", "Dehradun", 2),
    ("Gomti Riverside Institute of Technology", "Lucknow", 2),
    ("Western Ghats Institute of Technology", "Mangaluru", 2),
    ("Sabarmati Institute of Engineering and Research", "Ahmedabad", 2),
    ("Satpura Institute of Technology", "Bhopal", 2),
    ("Cauvery Basin Institute of Engineering", "Mysuru", 2),
    ("Nandi Hills Institute of Technology", "Bengaluru", 2),
    ("Kovalam Institute of Technology", "Thiruvananthapuram", 2),
    ("Banjara Heights College of Engineering", "Hyderabad", 3),
    ("Aarohan College of Engineering", "Hyderabad", 3),
    ("Navodit Institute of Technology", "Hyderabad", 3),
    ("Sri Kshetra College of Engineering", "Hyderabad", 3),
    ("Anandvan Institute of Technology", "Nagpur", 3),
    ("Shri Ganeshram College of Engineering", "Nashik", 3),
    ("Vaigai Valley Engineering College", "Madurai", 3),
    ("Noyyal Institute of Technology", "Coimbatore", 3),
    ("Tamraparni College of Engineering", "Tirunelveli", 3),
    ("Mahanadi Institute of Engineering", "Cuttack", 3),
    ("Subarnarekha Institute of Technology", "Ranchi", 3),
    ("Pataliputra Heritage College of Engineering", "Patna", 3),
    ("Varuna Ghat Institute of Technology", "Varanasi", 3),
    ("Marudhara Institute of Technology", "Jodhpur", 3),
    ("Suryavanshi College of Engineering", "Jabalpur", 3),
    ("Riverbank College of Engineering", "Vijayawada", 3),
    ("Bhadrakali Lake Institute of Technology", "Warangal", 3),
    ("Malaprabha Institute of Technology", "Belagavi", 3),
    ("Panchganga College of Engineering", "Kolhapur", 3),
    ("Shivalik Foothills Institute of Technology", "Chandigarh", 3),
))
COLLEGE_BY_NAME = {c.name: c for c in COLLEGES}
COLLEGE_TIER_WEIGHT = {1: 1.0, 2: 1.6, 3: 1.5}
DECOY_COLLEGE = "Meghdoot Institute of Technology"
RING_COLLEGE = "Godavari Delta Institute of Technology"
HOSTEL_COLLEGE = "Anandvan Institute of Technology"
MILL_CITY = "Hyderabad"
# College -> (test-day index, hour, minute) of its scheduled on-campus lab session.
ONCAMPUS_SESSIONS = {
    DECOY_COLLEGE: (1, 9, 30),
    "Shrivatsa Institute of Technology": (0, 10, 0),
    "Doon Heights Institute of Technology": (0, 14, 0),
    "Coromandel Institute of Technology": (2, 10, 0),
}
OPEN_COLLEGES = tuple(c for c in COLLEGES if c.name != DECOY_COLLEGE)

CITY_STATE = {
    "Bengaluru": "Karnataka", "Pune": "Maharashtra", "Hyderabad": "Telangana", "Chennai": "Tamil Nadu",
    "New Delhi": "Delhi", "Mumbai": "Maharashtra", "Kolkata": "West Bengal", "Ahmedabad": "Gujarat",
    "Bhubaneswar": "Odisha", "Guwahati": "Assam", "Kochi": "Kerala", "Visakhapatnam": "Andhra Pradesh",
    "Surat": "Gujarat", "Dehradun": "Uttarakhand", "Lucknow": "Uttar Pradesh", "Mangaluru": "Karnataka",
    "Bhopal": "Madhya Pradesh", "Mysuru": "Karnataka", "Thiruvananthapuram": "Kerala",
    "Nagpur": "Maharashtra", "Nashik": "Maharashtra", "Madurai": "Tamil Nadu",
    "Coimbatore": "Tamil Nadu", "Tirunelveli": "Tamil Nadu", "Cuttack": "Odisha",
    "Ranchi": "Jharkhand", "Patna": "Bihar", "Varanasi": "Uttar Pradesh", "Jodhpur": "Rajasthan",
    "Jabalpur": "Madhya Pradesh", "Vijayawada": "Andhra Pradesh", "Warangal": "Telangana",
    "Belagavi": "Karnataka", "Kolhapur": "Maharashtra", "Chandigarh": "Chandigarh",
    "Jaipur": "Rajasthan", "Indore": "Madhya Pradesh", "Noida": "Uttar Pradesh", "Gurugram": "Haryana",
    "Vadodara": "Gujarat", "Kanpur": "Uttar Pradesh", "Ludhiana": "Punjab", "Raipur": "Chhattisgarh",
    "Siliguri": "West Bengal", "Hubballi": "Karnataka", "Tiruchirappalli": "Tamil Nadu",
    "Thrissur": "Kerala", "Rajkot": "Gujarat", "Udaipur": "Rajasthan",
}
HOMETOWNS = ("Jaipur", "Indore", "Noida", "Gurugram", "Vadodara", "Kanpur", "Ludhiana", "Raipur",
             "Siliguri", "Hubballi", "Tiruchirappalli", "Thrissur", "Rajkot", "Udaipur", "Bengaluru",
             "Hyderabad", "Pune", "Chennai", "Mumbai", "Kolkata")
COMPANIES = (
    "Nimbuscape Technologies", "Kavachstack FinServ Labs", "Tarangini Analytics", "Quillwave Systems",
    "Saffronbyte Labs", "Bluelotus Softworks", "Pinecrest Data Solutions", "Ambrosia Cloudworks",
    "Vyomika Digital Labs", "Kesariya Payments", "Arkayaa Robotics", "Monsoonware Studio",
    "Zenledger Technologies", "Indigobyte Solutions", "Mayurpankh Analytics", "Rangolika Retail Tech",
    "Sitarani Health Informatics", "Coralreef Software", "Garudaksh Security Labs",
    "Banyangrid Systems", "Nakshatra AI Labs", "Chandrakiran Fintech", "Pushpaka Route Tech",
    "Neelgagan Cloud Services", "Kalpavalli Infotech", "Trishul Cyber Labs", "Meenakari Commerce",
    "Ankur Edtech Labs", "Shikhara Insurtech", "Prakriti Agritech",
)
COMPANY_CITIES = ("Bengaluru", "Hyderabad", "Pune", "Chennai", "Gurugram", "Noida", "Mumbai", "Kochi",
                  "Ahmedabad", "Remote")
SCHOOLS = ("Gyandeep Senior Secondary School", "Suryoday Public School", "Aranya Vidyalaya",
           "Brightwood Senior Secondary School", "Shanti Sadan High School", "Navchetna Public School",
           "Saraswati Gyan Mandir", "Little Lotus Senior Secondary School",
           "Vidyanjali International School", "Prerna Public School", "Kesar Valley School",
           "Morningstar Senior Secondary School")
BOARDS = (("CBSE", 55), ("ICSE", 15), ("State Board", 30))

# Degrees, branches and skill tracks ------------------------------------------------
DEGREES = (("B.Tech", 60), ("B.E.", 24), ("M.Tech", 6), ("MCA", 6), ("B.Sc", 4))
DEGREE_LONG = {"B.Tech": "Bachelor of Technology (B.Tech)", "B.E.": "Bachelor of Engineering (B.E.)",
               "M.Tech": "Master of Technology (M.Tech)", "MCA": "Master of Computer Applications (MCA)",
               "B.Sc": "Bachelor of Science (B.Sc)"}
DEGREE_YEARS = {"B.Tech": 4, "B.E.": 4, "M.Tech": 2, "MCA": 2, "B.Sc": 3}
UG_BRANCHES = (("Computer Science and Engineering", 38), ("Information Technology", 16),
               ("Artificial Intelligence and Data Science", 8),
               ("Computer Science and Engineering (AI and ML)", 7),
               ("Electronics and Communication Engineering", 15),
               ("Electrical and Electronics Engineering", 8), ("Mechanical Engineering", 4),
               ("Electronics and Instrumentation Engineering", 2), ("Civil Engineering", 2))
MTECH_BRANCHES = (("Computer Science and Engineering", 50), ("Data Science", 20),
                  ("Software Engineering", 20), ("VLSI Design", 10))
BSC_BRANCHES = (("Computer Science", 60), ("Mathematics", 25), ("Statistics", 15))
BRANCH_FAMILY = {
    "Electronics and Communication Engineering": "electronics",
    "Electrical and Electronics Engineering": "electronics",
    "Electronics and Instrumentation Engineering": "electronics", "VLSI Design": "electronics",
    "Mechanical Engineering": "core", "Civil Engineering": "core",
    "Mathematics": "maths", "Statistics": "maths",
}
FAMILY_TRACKS = {
    "cs": (("backend_java", 22), ("python_backend", 18), ("ml", 20), ("data", 12), ("web", 15),
           ("cloud", 8), ("android", 5)),
    "electronics": (("embedded", 35), ("ml", 15), ("python_backend", 15), ("data", 15),
                    ("backend_java", 10), ("cloud", 10)),
    "core": (("data", 40), ("python_backend", 30), ("web", 30)),
    "maths": (("data", 60), ("ml", 40)),
}
TRACKS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {   # track -> (core, extra) skills
    "backend_java": (("Java", "Spring Boot", "SQL", "MySQL", "REST APIs", "Git"),
                     ("Hibernate", "PostgreSQL", "Docker", "Kubernetes", "AWS", "Microservices", "JUnit",
                      "Redis", "Apache Kafka", "Maven", "Linux", "Python", "JavaScript", "React", "Azure")),
    "python_backend": (("Python", "Django", "FastAPI", "SQL", "PostgreSQL", "REST APIs", "Git"),
                       ("Flask", "Docker", "AWS", "Redis", "Celery", "Linux", "Pandas", "JavaScript",
                        "React", "MongoDB", "Java", "Azure", "GitHub Actions", "GCP")),
    "ml": (("Python", "PyTorch", "scikit-learn", "NumPy", "Pandas", "SQL"),
           ("TensorFlow", "Keras", "OpenCV", "NLP", "Hugging Face Transformers", "Matplotlib", "Flask",
            "FastAPI", "Docker", "AWS", "GCP", "Azure", "Java", "C++", "MLflow", "LangChain")),
    "data": (("Python", "SQL", "Pandas", "NumPy", "Power BI", "Excel"),
             ("MySQL", "PostgreSQL", "Tableau", "Apache Spark", "Apache Airflow", "AWS", "Azure",
              "scikit-learn", "Statistics", "Matplotlib", "Java", "Snowflake", "BigQuery", "GCP")),
    "web": (("JavaScript", "React", "Node.js", "HTML", "CSS", "Git"),
            ("TypeScript", "Express.js", "MongoDB", "SQL", "Next.js", "Redux", "Tailwind CSS", "Docker",
             "AWS", "Firebase", "Java", "Python", "REST APIs", "PostgreSQL")),
    "cloud": (("AWS", "Docker", "Kubernetes", "Linux", "Python", "Git"),
              ("Azure", "GCP", "Terraform", "Jenkins", "GitHub Actions", "Ansible", "Bash", "SQL", "Java",
               "Prometheus", "Grafana", "Go", "Nginx")),
    "android": (("Kotlin", "Java", "Android SDK", "Firebase", "SQLite", "Git"),
                ("Jetpack Compose", "Retrofit", "MVVM", "REST APIs", "Flutter", "Dart", "Python", "AWS",
                 "SQL")),
    "embedded": (("C", "C++", "Embedded C", "Arduino", "Python"),
                 ("MATLAB", "Verilog", "Raspberry Pi", "IoT", "RTOS", "Linux", "MQTT", "Java", "SQL",
                  "AWS IoT", "ESP32")),
}
SKILL_GROUPS = (
    ("Languages", {"Java", "Python", "C", "C++", "Embedded C", "JavaScript", "TypeScript", "Kotlin",
                   "Dart", "Go", "SQL", "Bash", "HTML", "CSS", "MATLAB", "Verilog"}),
    ("Frameworks and Libraries", {"Spring Boot", "Hibernate", "Django", "FastAPI", "Flask", "Celery",
                                  "React", "Node.js", "Express.js", "Next.js", "Redux", "Tailwind CSS",
                                  "Jetpack Compose", "Retrofit", "Flutter", "JUnit", "Pandas", "NumPy",
                                  "PyTorch", "TensorFlow", "Keras", "scikit-learn", "OpenCV",
                                  "Hugging Face Transformers", "LangChain", "Matplotlib", "Android SDK",
                                  "MVVM"}),
    ("Databases", {"MySQL", "PostgreSQL", "MongoDB", "Redis", "SQLite", "Snowflake", "BigQuery",
                   "Firebase"}),
    ("Cloud and DevOps", {"AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform", "Jenkins",
                          "GitHub Actions", "Ansible", "Linux", "Nginx", "Prometheus", "Grafana",
                          "AWS IoT", "Microservices", "MLflow"}),
)
CORE_CS = "Data Structures and Algorithms, OOP, DBMS, Operating Systems, Computer Networks"
GRADUATE_TITLES = ("Associate Software Engineer", "Graduate Engineer Trainee", "Junior Software Developer")

CERTIFICATIONS = (
    ("AWS Certified Cloud Practitioner", ("cloud", "*")),
    ("AWS Certified Solutions Architect - Associate", ("cloud",)),
    ("Microsoft Azure Fundamentals (AZ-900)", ("*",)),
    ("Microsoft Azure AI Fundamentals (AI-900)", ("ml", "data")),
    ("Google Cloud Digital Leader", ("cloud", "data")),
    ("Oracle Certified Associate, Java SE 8", ("backend_java", "android")),
    ("Oracle Certified Professional, Java SE 11", ("backend_java",)),
    ("NPTEL: Programming in Java (Elite)", ("backend_java", "android")),
    ("NPTEL: Joy of Computing using Python", ("*",)),
    ("NPTEL: DSA using Python (Elite)", ("*",)),
    ("NPTEL: Introduction to Machine Learning (Elite)", ("ml", "data")),
    ("NPTEL: Database Management System (Elite)", ("*",)),
    ("NPTEL: Cloud Computing (Elite)", ("cloud",)),
    ("NPTEL: Embedded System Design with ARM", ("embedded",)),
    ("Coursera: Machine Learning Specialization (DeepLearning.AI)", ("ml",)),
    ("Coursera: Google Data Analytics Professional Certificate", ("data",)),
    ("Power BI Data Analyst (PL-300)", ("data",)),
    ("Certified Kubernetes Application Developer (CKAD)", ("cloud",)),
    ("HashiCorp Certified: Terraform Associate", ("cloud",)),
    ("HackerRank Certified: Problem Solving (Intermediate)", ("*",)),
    ("HackerRank Certified: SQL (Advanced)", ("data", "*")),
    ("Meta Front-End Developer Professional Certificate", ("web",)),
    ("Google Associate Android Developer", ("android",)),
    ("TensorFlow Developer Certificate", ("ml",)),
    ("Cisco: Introduction to Cybersecurity", ("*",)),
)
HACKATHONS = ("HackNova", "CodeKshetra", "DevSangam", "Hack-o-Monsoon", "FinForge Hackathon",
              "Code Utsav", "BuildSprint", "ByteBazaar Hackathon")
THEMES = ("fraud detection", "rural healthcare", "smart agriculture", "financial literacy",
          "accessible banking", "campus safety", "carbon tracking", "disaster response")
ACHIEVEMENTS = (
    "Winner, {hack} {year} ({teams} teams) for a {theme} prototype.",
    "Finalist (top {top}) at {hack} {year} among {teams} teams.",
    "Solved {lc}+ problems on LeetCode with a contest rating of {rating}.",
    "{stars}-star coder on CodeChef; global rank {rank} in a Starters contest.",
    "Codeforces rating {cf} ({cftitle}).",
    "5-star badge in Problem Solving on HackerRank.",
    "Organised {ws} coding workshops as technical lead of the college coding club.",
    "Merit scholarship for academic excellence ({year}).",
    "Mentored {mentees} juniors in data structures and competitive programming.",
    "Presented a paper on {theme} at a national student conference ({year}).",
)
SUMMARY_FOCUS = {
    "backend_java": ("with hands-on experience building REST APIs in Java and Spring Boot",
                     "who enjoys designing reliable backend services with Java and SQL"),
    "python_backend": ("with practical experience building web services in Python and SQL",
                       "skilled in Python, Django and FastAPI with relational databases"),
    "ml": ("with hands-on experience training and deploying machine learning models in Python",
           "passionate about applied machine learning with PyTorch and scikit-learn"),
    "data": ("with strong skills in SQL, Python and data visualisation",
             "who enjoys turning raw data into dashboards and decisions"),
    "web": ("with experience building full-stack web apps using React and Node.js",
            "focused on fast, accessible web applications"),
    "cloud": ("with hands-on experience in AWS, Docker and Kubernetes",
              "interested in cloud infrastructure, automation and CI/CD"),
    "android": ("with experience building Android apps in Kotlin",
                "focused on mobile development with Kotlin and Firebase"),
    "embedded": ("with experience in embedded C, IoT and sensor integration",
                 "interested in embedded systems and edge computing"),
}
SUMMARY_GOALS = (
    "Seeking a software engineering role to build secure and scalable systems.",
    "Looking to apply strong problem-solving and DSA skills in a product team.",
    "Eager to contribute to digital banking platforms with clean, well-tested code.",
    "Keen to learn from experienced engineers while delivering measurable impact.",
    "Interested in fintech, payments and large-scale distributed systems.",
    "Aiming to grow into a full-stack engineer who owns features end to end.",
)

# Resume building blocks --------------------------------------------------------------


@dataclass(frozen=True)
class ProjectTemplate:
    """Paraphrasable project: bullets are composed from objects x outcomes x verbs x metrics."""

    key: str
    tracks: tuple[str, ...]
    titles: tuple[str, ...]
    tech: tuple[str, ...]
    objects: tuple[str, ...]
    outcomes: tuple[str, ...]
    slugs: tuple[str, ...]


@dataclass(frozen=True)
class RoleTemplate:
    title: str
    tracks: tuple[str, ...]
    objects: tuple[str, ...]


_P = ProjectTemplate
PROJECT_TEMPLATES: tuple[ProjectTemplate, ...] = (
    _P("loan_default", ("ml", "data", "python_backend"),
       ("Loan Default Risk Predictor", "Credit Default Prediction System", "Loan Approval Risk Scorer"),
       ("Python", "scikit-learn", "XGBoost", "Pandas", "Flask", "SQL"),
       ("a gradient-boosted model that predicts loan default from {k}K historical applications",
        "a feature pipeline for income ratios, credit history and repayment behaviour",
        "a Flask scoring API that returns a risk band with the top contributing features",
        "SHAP-based explanations for each prediction to support credit officers"),
       ("reaching {acc}% accuracy and an AUC of 0.{auc}", "reducing false approvals by {pct}% on a held-out set",
        "cutting manual review effort by {pct}%"),
       ("loan-default-predictor", "credit-risk-scoring")),
    _P("card_fraud", ("ml", "data"),
       ("Credit Card Fraud Detection", "Real-time Transaction Fraud Detector", "Payment Anomaly Detector"),
       ("Python", "PyTorch", "scikit-learn", "Pandas", "Apache Kafka", "FastAPI"),
       ("an autoencoder-based anomaly detector for card transactions",
        "a streaming pipeline that scores {k}K synthetic transactions per hour",
        "class-imbalance handling with SMOTE and a cost-sensitive loss",
        "a FastAPI endpoint that flags risky payments in real time"),
       ("raising fraud recall from 0.{f1a} to 0.{f1}", "keeping p95 scoring latency under {ms} ms",
        "lowering false positives by {pct}%"),
       ("fraud-detection", "txn-anomaly-detector")),
    _P("expense_tracker", ("web", "python_backend", "android"),
       ("Personal Expense Tracker", "Smart Budget Planner", "Hostel Expense Splitter"),
       ("React", "Node.js", "Express.js", "MongoDB", "Chart.js", "JWT"),
       ("a responsive dashboard with category-wise monthly spending charts",
        "JWT authentication with refresh tokens and protected routes",
        "a REST API for transactions, budgets and recurring bills",
        "CSV import of bank statements with duplicate detection"),
       ("used by {users}+ students across {cls} hostels", "loading the dashboard in under {ms} ms",
        "helping users cut monthly overspending by {pct}%"),
       ("expense-tracker", "budget-planner")),
    _P("banking_microservices", ("backend_java",),
       ("Online Banking System", "Core Banking Microservices", "Digital Wallet Backend"),
       ("Java", "Spring Boot", "MySQL", "Hibernate", "Docker", "JUnit"),
       ("account, transfer and statement microservices with Spring Boot",
        "role-based access control with Spring Security and JWT",
        "idempotent fund-transfer APIs with optimistic locking",
        "an audit trail for every balance change stored in MySQL"),
       ("handling {rps} requests per second in JMeter load tests",
        "with {cov}% unit-test coverage using JUnit and Mockito",
        "cutting transfer API latency by {pct}% through query tuning"),
       ("online-banking-system", "wallet-backend")),
    _P("library", ("backend_java", "python_backend", "web"),
       ("Library Management System", "Smart Library Portal", "Campus Book Lending App"),
       ("Java", "Spring Boot", "PostgreSQL", "React", "Python", "Django"),
       ("book issue, return and reservation workflows with automatic fines",
        "a catalogue search with filters by author, subject and availability",
        "an admin dashboard for inventory and overdue tracking",
        "scheduled e-mail reminders for due dates"),
       ("serving {users}+ students and staff", "reducing manual record-keeping by {pct}%",
        "returning search results in under {ms} ms"),
       ("library-management", "smart-library")),
    _P("support_chatbot", ("ml", "python_backend"),
       ("Customer Support Chatbot", "Banking FAQ Assistant", "College Helpdesk Chatbot"),
       ("Python", "PyTorch", "Hugging Face Transformers", "FastAPI", "LangChain", "FAISS"),
       ("an intent classifier fine-tuned on {n} labelled customer queries",
        "retrieval-augmented answers over {n} policy documents using FAISS",
        "a FastAPI service with conversation memory and human hand-off",
        "an evaluation harness for answer relevance and latency"),
       ("achieving {acc}% intent accuracy", "resolving {pct}% of queries without escalation",
        "keeping median response time near {ms} ms"),
       ("support-chatbot", "faq-assistant")),
    _P("stock_forecast", ("ml", "data"),
       ("Stock Price Trend Forecaster", "Equity Trend Prediction using LSTM", "Time-Series Forecasting Dashboard"),
       ("Python", "TensorFlow", "Keras", "Pandas", "Streamlit", "Matplotlib"),
       ("an LSTM model on {n} trading days of OHLC data with technical indicators",
        "walk-forward validation to avoid look-ahead bias",
        "a Streamlit dashboard comparing forecasts with actual prices",
        "features such as moving averages, RSI and rolling volatility"),
       ("lowering RMSE by {pct}% over a naive baseline", "improving directional accuracy to {acc}%",
        "refreshing forecasts every {mins} minutes"),
       ("stock-trend-forecaster", "lstm-forecasting")),
    _P("resume_screener", ("ml", "python_backend"),
       ("Resume Screening Assistant", "Job Description and Resume Matcher", "Skill Extraction from Resumes"),
       ("Python", "spaCy", "scikit-learn", "Flask", "NLTK", "SQL"),
       ("a skill extractor with a custom taxonomy of {n} technical skills",
        "TF-IDF and embedding similarity between resumes and job descriptions",
        "a Flask interface for recruiters to upload and rank resumes",
        "PDF and DOCX text extraction with section detection"),
       ("shortlisting resumes {x}x faster than manual review",
        "matching recruiter rankings with {acc}% agreement", "processing {n} resumes per minute"),
       ("resume-screener", "jd-resume-matcher")),
    _P("face_attendance", ("ml", "embedded"),
       ("Face Recognition Attendance System", "Smart Classroom Attendance"),
       ("Python", "OpenCV", "TensorFlow", "Flask", "SQLite", "Raspberry Pi"),
       ("a face-embedding pipeline for {n} enrolled students",
        "liveness checks that block photo-based proxy attendance",
        "a faculty portal that exports attendance reports",
        "edge inference on a Raspberry Pi camera module"),
       ("recognising faces with {acc}% accuracy in classroom lighting",
        "saving {mins} minutes of roll-call per lecture", "processing {fps} frames per second on the edge device"),
       ("face-attendance", "smart-attendance")),
    _P("ecommerce_microservices", ("backend_java", "cloud"),
       ("E-commerce Microservices Platform", "Online Store Backend"),
       ("Java", "Spring Boot", "Apache Kafka", "PostgreSQL", "Docker", "Kubernetes", "Redis"),
       ("catalogue, cart and order services that communicate over Kafka",
        "Redis caching for product listings and user sessions",
        "a Kubernetes deployment with health checks and autoscaling",
        "an API gateway with rate limiting and centralised logging"),
       ("sustaining {rps} requests per second in load tests", "reducing catalogue response time by {pct}%",
        "enabling zero-downtime rolling deployments"),
       ("ecommerce-microservices", "store-backend")),
    _P("cicd_pipeline", ("cloud",),
       ("CI/CD Pipeline for Microservices", "Automated Deployment Pipeline on AWS"),
       ("GitHub Actions", "Jenkins", "Docker", "Kubernetes", "AWS", "Terraform"),
       ("a GitHub Actions pipeline with build, test, scan and deploy stages",
        "Terraform modules for VPC, EKS and RDS provisioning",
        "blue-green deployments with automated rollback",
        "Prometheus and Grafana dashboards for service health"),
       ("cutting release time from {hrs} hours to {mins} minutes", "reducing failed deployments by {pct}%",
        "provisioning a complete environment in under {mins} minutes"),
       ("cicd-pipeline", "aws-deploy-pipeline")),
    _P("serverless_api", ("cloud", "python_backend"),
       ("Serverless Event Registration API", "Serverless Registration Backend on AWS"),
       ("Python", "AWS Lambda", "API Gateway", "DynamoDB", "S3", "CloudWatch"),
       ("a serverless API on Lambda and API Gateway with DynamoDB storage",
        "an S3-hosted static front end with signed upload URLs",
        "CloudWatch alarms and structured logging for every function",
        "infrastructure as code with AWS SAM templates"),
       ("handling {k}K registrations during a college fest", "keeping cold starts below {ms} ms",
        "cutting hosting cost by {pct}% compared with a VM"),
       ("serverless-registration", "serverless-shortener")),
    _P("sales_dashboard", ("data",),
       ("Retail Sales Analytics Dashboard", "Sales Performance Dashboard"),
       ("Power BI", "SQL", "Python", "Pandas", "Excel", "DAX"),
       ("a star-schema data model over {rows} million sales records",
        "DAX measures for year-on-year growth, margin and basket size",
        "an automated Python ETL that cleans and loads daily sales files",
        "drill-down reports by region, store and product category"),
       ("reducing report preparation time by {pct}%", "helping identify {n} underperforming products",
        "refreshing data daily in under {mins} minutes"),
       ("sales-analytics-dashboard", "retail-dashboard")),
    _P("churn_prediction", ("ml", "data"),
       ("Customer Churn Prediction", "Telecom Churn Analysis"),
       ("Python", "scikit-learn", "XGBoost", "Pandas", "SQL", "Tableau"),
       ("a churn model on {k}K customer records with usage and billing features",
        "SQL queries that build monthly customer cohorts",
        "a Tableau story explaining churn drivers by segment",
        "hyperparameter tuning with cross-validated grid search"),
       ("achieving an F1-score of 0.{f1}", "identifying the top {cls} churn drivers for retention campaigns",
        "improving precision by {pct}% over logistic regression"),
       ("churn-prediction", "telecom-churn")),
    _P("air_quality", ("data", "embedded"),
       ("Air Quality Monitoring Dashboard", "Urban Air Pollution Analytics"),
       ("Python", "Pandas", "Plotly", "MQTT", "Flask", "PostgreSQL"),
       ("an ingestion service for readings from {cls} low-cost air sensors",
        "data-cleaning rules for drift, gaps and outliers",
        "an interactive Plotly dashboard with ward-level heatmaps",
        "an alerting job for unhealthy AQI levels"),
       ("processing {k}K readings per day", "cutting data gaps by {pct}% with interpolation",
        "sending alerts within {mins} minutes of threshold breaches"),
       ("air-quality-monitor", "aqi-dashboard")),
    _P("smart_home", ("embedded",),
       ("IoT Smart Home Automation", "Smart Energy Meter"),
       ("C++", "ESP32", "Arduino", "MQTT", "Firebase", "Node-RED"),
       ("ESP32 firmware for relays, sensors and over-the-air updates",
        "an MQTT broker setup with topic-level access control",
        "a mobile-friendly control panel built with Node-RED",
        "power-usage logging to Firebase with daily summaries"),
       ("cutting household energy use by {pct}%", "responding to commands in under {ms} ms",
        "running {cls} devices on a single gateway"),
       ("smart-home-iot", "energy-meter-iot")),
    _P("traffic_signal", ("embedded", "ml"),
       ("Adaptive Traffic Signal Controller", "Vehicle Density based Traffic Control"),
       ("Python", "OpenCV", "YOLO", "Raspberry Pi", "C++"),
       ("a YOLO-based vehicle counter for {cls} camera feeds",
        "a signal-timing algorithm that adapts green time to queue length",
        "a traffic simulation comparing fixed and adaptive timing",
        "an edge deployment on Raspberry Pi with a fallback timer"),
       ("reducing average waiting time by {pct}% in simulation", "detecting vehicles with {acc}% precision",
        "processing {fps} frames per second"),
       ("adaptive-traffic-signal", "traffic-density-control")),
    _P("canteen_app", ("android",),
       ("Campus Canteen Pre-order App", "Food Ordering Android App"),
       ("Kotlin", "Android SDK", "Firebase", "Room", "Retrofit", "MVVM"),
       ("an Android app with menu browsing, cart and checkout",
        "offline caching of menus with Room and background sync",
        "push notifications for order status using Firebase Cloud Messaging",
        "an admin view for canteen staff to manage orders"),
       ("used by {users}+ students in the first month", "cutting queue time at the counter by {pct}%",
        "keeping the crash-free rate above {acc}%"),
       ("canteen-preorder-app", "food-ordering-android")),
    _P("fitness_app", ("android",),
       ("Fitness and Habit Tracker App", "Workout Tracker"),
       ("Kotlin", "Jetpack Compose", "Room", "Firebase", "WorkManager"),
       ("a Jetpack Compose UI for workouts, streaks and reminders",
        "a step counter using the device sensor API with battery-friendly sampling",
        "weekly progress charts and goal recommendations",
        "cloud backup of user data with Firebase Authentication"),
       ("reaching {users}+ installs among friends and juniors",
        "reducing battery usage by {pct}% compared with the first version",
        "earning a {stars}-star average rating from beta testers"),
       ("fitness-tracker", "habit-tracker-android")),
    _P("realtime_chat", ("web", "android"),
       ("Real-time Chat Application", "Group Chat with WebSockets"),
       ("Node.js", "Socket.IO", "React", "MongoDB", "Redis", "JWT"),
       ("one-to-one and group chat over WebSockets with typing indicators",
        "message persistence in MongoDB with pagination",
        "Redis pub/sub to scale sockets across server instances",
        "media sharing with client-side image compression"),
       ("supporting {users} concurrent users in load tests", "delivering messages in under {ms} ms",
        "reducing payload size by {pct}%"),
       ("realtime-chat", "group-chat-websockets")),
    _P("url_shortener", ("python_backend", "backend_java"),
       ("URL Shortener Service", "Link Shortener with Click Analytics"),
       ("Python", "FastAPI", "Redis", "PostgreSQL", "Docker"),
       ("a base62 short-code generator with collision checks",
        "Redis caching for hot links and per-user rate limiting",
        "click analytics by country, device and referrer",
        "a Dockerised deployment with health checks"),
       ("serving redirects in under {ms} ms", "handling {rps} requests per second",
        "achieving a cache hit rate of {acc}%"),
       ("url-shortener", "link-analytics")),
    _P("hospital_booking", ("backend_java", "web"),
       ("Hospital Appointment Booking System", "Clinic Management Portal"),
       ("Java", "Spring Boot", "MySQL", "React", "JWT"),
       ("appointment booking with doctor availability and slot locking",
        "patient records with role-based access for doctors and staff",
        "reminder and cancellation workflows for appointments",
        "a React front end with accessible forms"),
       ("reducing no-shows by {pct}% in a pilot clinic", "booking {n} appointments during the pilot",
        "keeping API responses under {ms} ms"),
       ("hospital-appointments", "clinic-portal")),
    _P("sentiment", ("ml", "data"),
       ("Product Review Sentiment Analyzer", "Social Media Sentiment Analysis"),
       ("Python", "NLTK", "scikit-learn", "BERT", "Flask", "Pandas"),
       ("a BERT classifier fine-tuned on {k}K product reviews",
        "text preprocessing with lemmatisation and emoji handling",
        "a Flask API and dashboard showing sentiment trends",
        "aspect-level sentiment for price, delivery and quality"),
       ("achieving {acc}% accuracy on the test split", "outperforming a TF-IDF baseline by {pct}%",
        "analysing {k}K reviews in under {mins} minutes"),
       ("review-sentiment", "sentiment-analyzer")),
    _P("plant_disease", ("ml",),
       ("Plant Disease Detection from Leaf Images", "Crop Disease Classifier"),
       ("Python", "PyTorch", "OpenCV", "Flask", "CNN"),
       ("a transfer-learning CNN trained on {k}K labelled leaf images",
        "data augmentation for lighting, blur and rotation",
        "a lightweight web app for farmers to upload photos",
        "Grad-CAM heatmaps that explain each prediction"),
       ("reaching {acc}% validation accuracy across {cls} classes", "running inference in {ms} ms on a laptop CPU",
        "reducing model size by {pct}% with quantisation"),
       ("plant-disease-detection", "crop-disease-cnn")),
    _P("etl_airflow", ("data", "cloud"),
       ("Batch ETL Pipeline with Airflow", "Data Lake Ingestion Pipeline"),
       ("Python", "Apache Airflow", "Apache Spark", "AWS", "SQL", "Docker"),
       ("Airflow DAGs that ingest CSV and API data into an S3 data lake",
        "PySpark jobs for deduplication and slowly changing dimensions",
        "data-quality checks that alert on failed expectations",
        "a partitioned warehouse schema for analysts"),
       ("processing {rows} million rows per nightly run", "reducing pipeline runtime by {pct}%",
        "catching {n} bad records per week before they reached reports"),
       ("airflow-etl", "data-lake-pipeline")),
    _P("systems_c", ("embedded", "backend_java", "python_backend"),
       ("Mini Unix Shell in C", "Custom Unix Shell"),
       ("C", "C++", "Linux", "Make", "GDB"),
       ("a shell with pipes, redirection and background jobs",
        "built-in commands such as cd, history and alias with tab completion",
        "signal handling and job control for foreground processes",
        "a regression suite of {n} test scripts run in CI"),
       ("passing all {n} course test cases", "keeping memory leaks at zero under Valgrind",
        "handling {k}K commands in stress tests without a crash"),
       ("mini-shell", "unix-shell")),
    _P("kv_store", ("backend_java", "cloud"),
       ("Distributed Key-Value Store", "Raft-based Replicated Store"),
       ("Java", "gRPC", "Docker", "Raft", "JUnit"),
       ("leader election and log replication based on the Raft paper",
        "a gRPC API for get, put and delete with consistent reads",
        "snapshotting and log compaction to bound disk usage",
        "fault-injection tests for partitions and node crashes"),
       ("sustaining {k}K writes per second on a 3-node cluster", "recovering from leader failure in under {ms} ms",
        "with {cov}% test coverage"),
       ("raft-kv-store", "distributed-kv")),
    _P("placement_portal", ("web", "python_backend"),
       ("Campus Placement Portal", "Job Board with Applicant Tracking"),
       ("Python", "Django", "PostgreSQL", "React", "Celery", "Redis"),
       ("student, recruiter and admin roles with fine-grained permissions",
        "resume uploads and eligibility filters for placement drives",
        "background jobs for bulk e-mails and report exports",
        "an analytics page for placement statistics"),
       ("used by {users}+ students during placement season", "cutting coordinator workload by {pct}%",
        "generating reports in under {mins} minutes"),
       ("placement-portal", "job-board")),
    _P("weather_app", ("web", "android"),
       ("Weather Forecast App", "Weather Dashboard"),
       ("JavaScript", "React", "REST APIs", "Chart.js", "HTML", "CSS"),
       ("a weather app that consumes a public forecast API",
        "location search with autocomplete and recent history",
        "hourly and weekly charts with unit conversion",
        "a responsive layout for mobile and desktop"),
       ("loading forecasts in under {ms} ms", "used by {users}+ visitors after a college demo",
        "reducing API calls by {pct}% with caching"),
       ("weather-app", "weather-dashboard")),
    _P("games", ("python_backend", "backend_java", "web"),
       ("Tic-Tac-Toe with Minimax AI", "Connect Four with Minimax AI"),
       ("Python", "Pygame", "Java", "Swing"),
       ("a desktop game board with a clean, resizable interface",
        "an unbeatable AI opponent using minimax with alpha-beta pruning",
        "a scoreboard persisted in a local file",
        "difficulty levels that limit the search depth"),
       ("evaluating {k}K game states per second", "keeping AI moves under {ms} ms",
        "reducing AI move time by {pct}% with pruning"),
       ("minimax-game", "board-game-ai")),
    _P("portfolio", ("web",),
       ("Personal Portfolio Website", "Developer Portfolio with Blog"),
       ("HTML", "CSS", "JavaScript", "React", "Tailwind CSS"),
       ("a responsive portfolio with project pages and a contact form",
        "a markdown blog with tags and search",
        "dark mode and accessibility improvements",
        "continuous deployment on every push"),
       ("scoring {lh} on Lighthouse performance", "loading in under {ms} ms on 4G",
        "attracting {users}+ visitors in two months"),
       ("portfolio", "dev-portfolio")),
    _P("kyc_ocr", ("ml", "python_backend"),
       ("KYC Document Verification using OCR", "Automated KYC Form Extraction"),
       ("Python", "Tesseract OCR", "OpenCV", "FastAPI", "PostgreSQL"),
       ("an OCR pipeline that extracts fields from scanned application forms",
        "image preprocessing for skew, glare and low resolution",
        "validation rules for names, dates and document numbers",
        "a review queue for low-confidence extractions"),
       ("extracting fields with {acc}% accuracy", "reducing manual data entry by {pct}%",
        "processing a document in about {ms} ms"),
       ("kyc-ocr", "kyc-extraction")),
    _P("upi_simulator", ("backend_java", "python_backend"),
       ("UPI Payment Simulator", "Peer-to-Peer Payment Gateway Simulator"),
       ("Java", "Spring Boot", "Apache Kafka", "PostgreSQL", "Redis"),
       ("a payment switch simulator with collect and pay flows",
        "idempotency keys and retries for exactly-once settlement",
        "Kafka-based events for ledger and notification services",
        "a reconciliation job that matches debits and credits"),
       ("settling {k}K test payments with zero mismatches", "keeping end-to-end latency under {ms} ms",
        "handling {rps} transactions per second in load tests"),
       ("upi-simulator", "p2p-payments")),
    _P("recommender", ("ml", "data"),
       ("Movie Recommendation Engine", "Product Recommender for E-commerce"),
       ("Python", "PyTorch", "Pandas", "FastAPI", "Redis"),
       ("collaborative filtering with matrix factorisation on {k}K ratings",
        "a cold-start strategy using content-based features",
        "a FastAPI service that serves top-10 recommendations",
        "offline evaluation with precision@k and recall@k"),
       ("improving precision@10 by {pct}% over popularity ranking", "serving recommendations in under {ms} ms",
        "covering {acc}% of the catalogue"),
       ("movie-recommender", "product-recommender")),
    _P("log_analytics", ("cloud", "data"),
       ("Log Analytics Platform with ELK", "Server Log Anomaly Detector"),
       ("Elasticsearch", "Logstash", "Kibana", "Python", "Docker"),
       ("a log pipeline that parses and indexes application logs",
        "Kibana dashboards for error rates and slow endpoints",
        "an isolation-forest model that flags unusual log patterns",
        "retention policies and index lifecycle management"),
       ("indexing {k}K log lines per minute", "detecting incidents {mins} minutes earlier than manual checks",
        "reducing storage cost by {pct}%"),
       ("elk-log-analytics", "log-anomaly-detector")),
    _P("blockchain_voting", ("web", "backend_java"),
       ("Blockchain-based E-Voting System", "Decentralised Voting DApp"),
       ("Solidity", "Ethereum", "Web3.js", "React", "Node.js"),
       ("smart contracts for voter registration and vote casting",
        "a React front end that connects to wallets through Web3.js",
        "unit tests for contracts on a local test network",
        "an audit view that verifies results on-chain"),
       ("running a mock election with {n} voters", "reducing gas cost per vote by {pct}%",
        "confirming transactions in about {sec} seconds"),
       ("blockchain-evoting", "land-records-chain")),
    _P("smart_parking", ("embedded", "android"),
       ("Smart Parking System", "IoT Parking Slot Finder"),
       ("C++", "ESP32", "Firebase", "Android SDK", "Ultrasonic Sensors"),
       ("slot-occupancy sensing with ultrasonic sensors and ESP32 boards",
        "a Firebase backend that updates slot status in real time",
        "an Android app that guides drivers to free slots",
        "solar-powered nodes with low-power sleep cycles"),
       ("covering {n} parking slots in the pilot", "cutting search time for a slot by {pct}%",
        "updating status within {sec} seconds"),
       ("smart-parking", "parking-slot-finder")),
    _P("inventory", ("backend_java", "python_backend", "data"),
       ("Inventory Management System", "Warehouse Stock Tracker"),
       ("Java", "Spring Boot", "MySQL", "Angular", "Python"),
       ("stock-in, stock-out and transfer workflows with barcode support",
        "reorder-level alerts and supplier management",
        "a reporting module for stock ageing and valuation",
        "bulk upload of items from Excel templates"),
       ("tracking {k}K items across {cls} warehouses", "reducing stock-outs by {pct}%",
        "generating reports in under {sec} seconds"),
       ("inventory-management", "stock-tracker")),
    _P("k8s_monitoring", ("cloud",),
       ("Kubernetes Cluster Monitoring Stack", "Self-healing Kubernetes Deployment"),
       ("Kubernetes", "Helm", "Prometheus", "Grafana", "Python", "Docker"),
       ("Helm charts for a multi-service application with config maps and secrets",
        "Prometheus alert rules for latency, errors and saturation",
        "Grafana dashboards for node and pod health",
        "a Python operator that restarts unhealthy workloads"),
       ("reducing mean time to recovery by {pct}%", "monitoring {n} pods across {cls} namespaces",
        "keeping alert noise to fewer than {cls} alerts per day"),
       ("k8s-monitoring", "self-healing-k8s")),
    _P("statement_analyzer", ("data", "python_backend"),
       ("Bank Statement Analyzer", "Personal Finance Insights Tool"),
       ("Python", "Pandas", "pdfplumber", "Streamlit", "SQL"),
       ("a parser that extracts transactions from PDF bank statements",
        "rule-based and ML categorisation of spending",
        "a Streamlit app with monthly cash-flow insights",
        "anomaly alerts for unusual debits"),
       ("categorising transactions with {acc}% accuracy", "parsing a statement in under {sec} seconds",
        "used by {users}+ friends and family"),
       ("statement-analyzer", "finance-insights")),
    _P("quiz_platform", ("web", "python_backend"),
       ("Online Quiz and Assessment Platform", "Coding Practice Platform"),
       ("Python", "Django", "PostgreSQL", "JavaScript", "Docker"),
       ("timed quizzes with randomised question banks",
        "an auto-grader that runs code in sandboxed Docker containers",
        "a leaderboard with plagiarism checks",
        "instructor analytics for question difficulty"),
       ("hosting {n} quizzes for {users}+ students", "grading submissions in under {sec} seconds",
        "reducing evaluation effort by {pct}%"),
       ("quiz-platform", "code-practice")),
    _P("sign_language", ("ml",),
       ("Sign Language Recognition", "Hand Gesture Recognition"),
       ("Python", "TensorFlow", "OpenCV", "MediaPipe", "Keras"),
       ("a landmark-based gesture classifier for {cls} signs",
        "a data collection tool that recorded {k}K labelled frames",
        "real-time inference from a laptop webcam",
        "text-to-speech output for recognised words"),
       ("achieving {acc}% accuracy on unseen signers", "running at {fps} frames per second",
        "reducing misclassifications by {pct}% with temporal smoothing"),
       ("sign-language-recognition", "gesture-recognition")),
)
TEMPLATE_BY_KEY = {t.key: t for t in PROJECT_TEMPLATES}
TRACK_NEIGHBOURS = {
    "backend_java": ("cloud", "web", "python_backend"), "python_backend": ("web", "data", "backend_java"),
    "ml": ("data", "python_backend"), "data": ("ml", "python_backend", "cloud"),
    "web": ("python_backend", "android", "backend_java"), "cloud": ("backend_java", "python_backend", "data"),
    "android": ("web", "backend_java"), "embedded": ("ml", "python_backend"),
}
PROJECT_VERBS = ("Built", "Developed", "Designed", "Implemented", "Engineered", "Created")
BULLET_PREFIXES = ("", "", "", "", "", "In a team of {team}, ", "As project lead, ", "Over {weeks} weeks, ")

_R = RoleTemplate
ROLE_TEMPLATES: tuple[RoleTemplate, ...] = (
    _R("Software Engineering Intern", ("backend_java", "python_backend", "web", "cloud"),
       ("REST endpoints for an internal loan-servicing dashboard",
        "unit and integration tests for a payments reconciliation service",
        "a feature-flagged rollout of a customer onboarding flow",
        "pagination and caching for a slow reporting API",
        "a Dockerised local setup for the team's services")),
    _R("Backend Developer Intern", ("backend_java", "python_backend"),
       ("Spring Boot services for KYC status tracking", "SQL query optimisations for a transactions ledger",
        "a notification service on top of a message queue",
        "OpenAPI documentation and Postman collections for {cls} services")),
    _R("Data Science Intern", ("ml", "data"),
       ("a customer segmentation model using RFM features",
        "an A/B test analysis for a credit-card offer campaign",
        "a demand forecast for branch cash planning", "features for a collections propensity model")),
    _R("Machine Learning Intern", ("ml",),
       ("a document classification model for scanned forms",
        "a monitoring pipeline for model drift and data quality",
        "a PyTorch training script with mixed precision and early stopping",
        "an NLP model that tags support tickets by product")),
    _R("Data Analyst Intern", ("data",),
       ("SQL reports on branch-level deposits and withdrawals", "Power BI dashboards for operations KPIs",
        "data-cleaning scripts for monthly MIS files", "a cohort analysis of digital banking adoption")),
    _R("Cloud Engineering Intern", ("cloud",),
       ("Terraform scripts for a staging environment on AWS",
        "cost dashboards and tagging policies for cloud resources",
        "log shipping to a central ELK cluster", "automated database backups with monthly restore drills")),
    _R("DevOps Intern", ("cloud",),
       ("a Jenkins to GitHub Actions migration for {cls} services",
        "Helm charts for services moving to Kubernetes", "container image scanning in the CI pipeline",
        "runbooks and alerts for on-call engineers")),
    _R("Full Stack Developer Intern", ("web",),
       ("React pages for a merchant onboarding portal",
        "Node.js APIs for document uploads and verification status",
        "form validation and accessibility fixes across the portal",
        "end-to-end Cypress tests for critical user journeys")),
    _R("Android Developer Intern", ("android",),
       ("new Jetpack Compose screens for a field-agent app", "offline-first sync for visit reports",
        "fixes for crashes reported through Firebase Crashlytics", "a biometric login flow")),
    _R("Embedded Systems Intern", ("embedded",),
       ("firmware for a sensor node running FreeRTOS", "I2C and SPI drivers for new peripherals",
        "a test jig that automated board bring-up checks", "power profiling and sleep-mode optimisation")),
    _R("QA Automation Intern", ("*",),
       ("Selenium and PyTest suites for regression testing", "API tests with Postman and Newman in CI",
        "test-data generators for edge cases", "a flaky-test tracker for the team")),
)
INTERN_VERBS = ("Built", "Developed", "Implemented", "Delivered", "Created", "Designed")
INTERN_OUTCOMES = (
    "reducing manual effort by {pct}%", "saving the team about {hrs} hours every week",
    "improving response time by {pct}%", "adopted by {cls} internal teams",
    "catching {small} defects before release", "cutting processing time from {hrs} hours to {mins} minutes",
)
INTERN_TERMS = {   # graduation year -> (start, end) in (year, month); end None = Present
    2027: (((2026, 5), (2026, 7)), ((2026, 6), (2026, 8)), ((2025, 5), (2025, 7)), ((2025, 6), (2025, 8)),
           ((2025, 12), (2026, 1)), ((2025, 12), (2026, 2)), ((2026, 8), None), ((2026, 9), None)),
    2026: (((2025, 5), (2025, 7)), ((2025, 6), (2025, 8)), ((2024, 12), (2025, 1)), ((2026, 1), (2026, 6)),
           ((2026, 7), None)),
    2025: (((2024, 5), (2024, 7)), ((2024, 6), (2024, 8)), ((2025, 1), (2025, 6)), ((2025, 8), None)),
}
PRESENT = (2026, 11)

# Planted resume content ----------------------------------------------------------------
RING_REPO_SLUG = "campus-event-hub"
RING_PROJECTS = (
    ("Smart Campus Event Management Platform", ("Python", "Django", "PostgreSQL", "React"), (
        "Built a Django REST backend for event creation, registration and QR check-in.",
        "Designed a PostgreSQL schema for events, venues and attendee feedback records.",
        "Added a React dashboard showing live registrations and seat availability.")),
    ("Hostel Mess Feedback Analyzer", ("Python", "NLTK", "Flask", "Chart.js"), (
        "Collected 3,200 mess feedback entries through a Flask web form and cleaned them.",
        "Classified feedback sentiment with an NLTK model at 87% validation accuracy.",
        "Plotted weekly sentiment trends on a Chart.js dashboard for the hostel committee.")),
)
RING_WORD_EDITS = (("Built", "Developed"), ("Designed", "Created"), ("Added", "Implemented"),
                   ("showing", "displaying"), ("live", "real-time"), ("Collected", "Gathered"),
                   ("cleaned", "preprocessed"), ("Classified", "Categorised"), ("Plotted", "Visualised"),
                   ("trends", "patterns"), ("records", "details"), ("weekly", "monthly"))
MILL_HANDLE = "placementprep-hyd"
MILL_PROJECTS = (
    ("Bank Loan Eligibility Predictor", ("Python", "scikit-learn", "Flask", "SQL"), "bank-loan-eligibility"),
    ("Online Banking Portal with Fraud Alerts", ("Java", "Spring Boot", "MySQL", "React"),
     "online-banking-portal"),
)
MILL_SHARED_BULLETS = (
    "Trained a random forest model on 25,000 loan records with 91.4% validation accuracy.",
    "Exposed the model through a Flask REST API consumed by a React frontend.",
    "Built Spring Boot services for accounts, transfers and fraud alert notifications.",
)
BOILERPLATE_BULLETS = (
    "Collaborated with a team of four members to design, develop and test the application.",
    "Wrote clean, modular and well-documented code following industry best practices.",
    "Used Git and GitHub for version control and participated in regular code reviews.",
    "Gained hands-on experience in requirement analysis, system design, testing and deployment.",
)

LAYOUT_HEADINGS = {
    "classic": {"summary": ("SUMMARY", "CAREER OBJECTIVE", "OBJECTIVE"),
                "education": ("EDUCATION", "ACADEMIC QUALIFICATIONS"),
                "skills": ("TECHNICAL SKILLS", "SKILLS"), "projects": ("PROJECTS", "ACADEMIC PROJECTS"),
                "experience": ("INTERNSHIPS", "WORK EXPERIENCE", "INTERNSHIP EXPERIENCE"),
                "certifications": ("CERTIFICATIONS", "CERTIFICATIONS & COURSES"),
                "achievements": ("ACHIEVEMENTS", "AWARDS")},
    "modern": {"summary": ("Profile Summary", "Summary", "Profile"),
               "education": ("Academic Qualifications", "Education", "Academics"),
               "skills": ("Core Competencies", "Technical Skills", "Key Skills"),
               "projects": ("Academic Projects", "Key Projects", "Personal Projects", "Projects"),
               "experience": ("Professional Experience", "Internship Experience", "Experience"),
               "certifications": ("Licenses & Certifications", "Certifications"),
               "achievements": ("Accomplishments", "Achievements", "Honors")},
    "minimal": {"summary": ("Objective", "Career Objective"), "education": ("Education", "Qualifications"),
                "skills": ("Skills", "Technical Skills"), "projects": ("Projects",),
                "experience": ("Work Experience", "Internships", "Experience"),
                "certifications": ("Certifications", "Courses"),
                "achievements": ("Achievements", "Extra-Curricular")},
}
LAYOUT_ORDER = {
    "classic": ("summary", "education", "skills", "experience", "projects", "certifications", "achievements"),
    "modern": ("summary", "skills", "experience", "projects", "education", "certifications", "achievements"),
    "minimal": ("summary", "skills", "education", "projects", "experience", "certifications", "achievements"),
}
# Bullet glyphs per (format, layout); PDFs avoid glyphs the embedded font lacks.
GLYPHS = {
    ("pdf", "classic"): ("•",), ("pdf", "modern"): ("•", "-"), ("pdf", "minimal"): ("-", "*"),
    ("txt", "classic"): ("•",), ("txt", "modern"): ("▪", "•"), ("txt", "minimal"): ("-", "*"),
    ("docx", "classic"): ("",), ("docx", "modern"): ("",), ("docx", "minimal"): ("-", "•"),
}
NAMING_STYLES = (("id", 0.50), ("name", 0.25), ("cv", 0.15), ("generic", 0.10))

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _rng(*parts: object) -> random.Random:
    """Independent, reproducible RNG stream for the given key (stable across processes)."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _sha12(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


MCQ_KEY_HASH = _sha12(f"mcq|{MCQ_KEY}")


def _weighted(rng: random.Random, options: Sequence[tuple[Any, float]]) -> Any:
    return rng.choices([o for o, _ in options], weights=[w for _, w in options])[0]


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, rng.random()
    while p > limit:
        k += 1
        p *= rng.random()
    return k


def _binomial(rng: random.Random, n: int, p: float) -> int:
    return sum(rng.random() < p for _ in range(n))


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _fmt_num(x: float | None, nd: int = 2) -> str:
    """'80' for 80.0, '72.5' for 72.5, '' for None."""
    if x is None:
        return ""
    r = round(float(x), nd)
    if r == int(r):
        return str(int(r))
    return f"{r:.{nd}f}".rstrip("0").rstrip(".")


def _at(day_index: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    d = TEST_DAYS[day_index]
    return datetime(d.year, d.month, d.day, hour, minute, second)


def _fmt_dt(dt: datetime, style: str) -> str:
    if style == "dmy":
        return f"{dt.day:02d}-{MONTHS[dt.month - 1]}-{dt.year} {dt:%H:%M:%S}"
    if style == "iso":
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_duration(seconds: int, hms: bool) -> str:
    if hms:
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    return _fmt_num(seconds / 60, 1)


def _join_words(items: Sequence[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _fmt_ym(ym: tuple[int, int], style: int) -> str:
    year, month = ym
    return f"{month:02d}/{year}" if style == 2 else f"{MONTHS[month - 1]} {year}"


def _fmt_range(start: tuple[int, int], end: tuple[int, int] | None, style: int) -> str:
    sep = " – " if style == 1 else " - "
    return f"{_fmt_ym(start, style)}{sep}{_fmt_ym(end, style) if end else 'Present'}"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class _Registry:
    """Uniqueness bookkeeping shared by every generation step."""

    names: set[tuple[str, str]] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    handles: set[str] = field(default_factory=lambda: {MILL_HANDLE})
    subnets: set[tuple[int, int, int]] = field(default_factory=set)
    files: set[str] = field(default_factory=set)          # lower-cased (case-insensitive file systems)
    bullets: set[str] = field(default_factory=set)        # normalised honest bullets (no chance overlaps)


def _bullet_key(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _unique_bullet(make: Callable[[], str], reg: _Registry) -> str:
    """Draw a bullet until it differs from every honest bullet generated so far, so only the
    planted / boilerplate bullets are ever shared between resumes."""
    text = make()
    for _ in range(30):
        if _bullet_key(text) not in reg.bullets:
            break
        text = make()
    reg.bullets.add(_bullet_key(text))
    return text


@dataclass
class Candidate:
    """One synthetic person plus their test attempt and ground-truth labels."""

    idx: int
    cid: str
    first: str
    last: str
    email: str
    phone: str                      # 10 digits
    college: College
    degree: str
    branch: str
    grad_year: int
    cgpa: float
    city: str
    track: str
    github: str                     # handle
    linkedin: str                   # "linkedin.com/in/..."
    source: str = "Campus Drive"
    invited_on: date = date(2026, 10, 20)
    invited_by: str = TA_INVITERS[0]
    phone_style: int = 0
    test_set: str = "A"
    status: str = "Completed"
    component: str = "mid"
    q_scores: list[float] = field(default_factory=list)
    mcq_answers: str = ""
    start: datetime | None = None
    duration_s: int = 0
    sims: list[float | None] = field(default_factory=lambda: [None, None, None])   # Q2..Q4
    hashes: list[str] = field(default_factory=lambda: ["", "", "", ""])            # Q1..Q4
    most_similar: str = ""
    plagiarism: bool = False
    oow_events: int = 0
    oow_sec: int = 0
    paste: int = 0
    secure_exits: int = 0
    ip: str = ""
    integrity: str = "Clear"
    proctor_flags: list[str] = field(default_factory=list)
    mode: str = "Remote"
    test_cid: int = 0
    timing_fixed: bool = False      # timing is part of a pattern / schedule: never shifted
    # ground truth
    label: str = "normal"
    cluster_key: str = ""
    pattern: str = ""
    expected_min_level: str = "None"
    # presentation-level data-quality injections (TA feed only)
    dt_style: str = "std"
    time_taken_hms: bool = False
    cgpa_as_pct: bool = False
    college_display: str | None = None
    recorded_score: float | None = None
    email_missing: bool = False

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"

    @property
    def attempted(self) -> bool:
        return self.status == "Completed"

    @property
    def max_score(self) -> float:
        return TEST_SETS[self.test_set].total

    @property
    def score(self) -> float | None:
        return float(sum(self.q_scores)) if self.attempted else None

    @property
    def pct(self) -> float | None:
        """Percentage as exported (uses the recorded score when a data error was injected)."""
        if not self.attempted:
            return None
        shown = self.recorded_score if self.recorded_score is not None else self.score
        return round(float(shown) / self.max_score * 100, 2)

    @property
    def is_perfect(self) -> bool:
        return self.attempted and self.score == self.max_score

    @property
    def in_focus(self) -> bool:
        """Will land in the 100% focus group after ingest (score > max is clipped to max)."""
        return self.attempted and (self.pct or 0.0) >= 100.0

    @property
    def end(self) -> datetime | None:
        return self.start + timedelta(seconds=self.duration_s) if self.start else None

    @property
    def max_sim(self) -> float | None:
        vals = [s for s in self.sims if s is not None]
        return max(vals) if vals else None


@dataclass
class Project:
    title: str
    tech: list[str]
    bullets: list[str]
    repo: str | None = None


@dataclass
class Experience:
    role: str
    company: str
    location: str
    start: tuple[int, int]
    end: tuple[int, int] | None
    bullets: list[str]


@dataclass
class ResumeContent:
    """Layout-independent resume content (rendered later as PDF / DOCX / TXT)."""

    name: str
    email: str
    phone: str
    city: str
    state: str
    linkedin: str | None
    github: str | None
    summary: str
    degree: str
    branch: str
    college: str
    college_city: str
    start_year: int
    grad_year: int
    cgpa: float
    school: str
    board12: str
    pct12: float
    board10: str
    pct10: float
    skills: list[str]
    projects: list[Project]
    experiences: list[Experience]
    certifications: list[str]
    achievements: list[str]
    date_style: int = 0

    def section_text(self, key: str) -> str:
        """Plain text of the 'projects' or 'experience' section (used for similarity checks)."""
        if key == "projects":
            parts = [f"{p.title} {' '.join(p.tech)} {' '.join(p.bullets)}" for p in self.projects]
        elif key == "experience":
            parts = [f"{e.role} {e.company} {' '.join(e.bullets)}" for e in self.experiences]
        else:
            raise ValueError(key)
        return "\n".join(parts)

    def all_bullets(self) -> list[str]:
        return [b for p in self.projects for b in p.bullets] + [b for e in self.experiences for b in e.bullets]


@dataclass
class ResumeFile:
    """One file in resumes/ (content is None for corrupt / empty / image-only files)."""

    file_name: str
    kind: str                       # primary | v2 | walk_in | corrupt_pdf | empty_docx | image_only_pdf
    candidate_id: str               # "" for walk-ins
    fmt: str                        # pdf | docx | txt
    layout: str = ""
    naming: str = ""
    glyph: str = ""
    content: ResumeContent | None = None
    lines: tuple[tuple[str, ...], ...] = ()
    blob_seed: int = 0


@dataclass(frozen=True)
class RenderJob:
    """Picklable rendering instruction for worker processes."""

    file_name: str
    kind: str                       # pdf | docx | txt | corrupt_pdf | empty_docx | image_only_pdf
    layout: str
    glyph: str
    author: str
    lines: tuple[tuple[str, ...], ...]
    blob_seed: int = 0


@dataclass
class Dataset:
    """Everything generate() writes, built in memory first (no I/O)."""

    n: int
    seed: int
    formats: tuple[str, ...]
    candidates: list[Candidate]
    ta_rows: list[list[str]]
    clusters: list[dict[str, Any]]
    issues: dict[str, Any]
    resumes: list[ResumeFile]

    def ta_csv_bytes(self) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(TA_COLUMNS)
        writer.writerows(self.ta_rows)
        return buf.getvalue().encode("utf-8")

    def label_rows(self) -> list[dict[str, str]]:
        return [{"candidate_id": c.cid, "label": c.label, "cluster_key": c.cluster_key,
                 "pattern": c.pattern, "expected_min_level": c.expected_min_level}
                for c in sorted(self.candidates, key=lambda c: c.cid)]


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


def _unique_name(rng: random.Random, reg: _Registry) -> tuple[str, str]:
    for _ in range(20_000):
        pair = (rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES))
        if pair not in reg.names:
            reg.names.add(pair)
            return pair
    raise RuntimeError("name space exhausted; lower --n")


def _unique_email(rng: random.Random, reg: _Registry, first: str, last: str) -> str:
    f, l = first.lower(), last.lower()
    for attempt in range(1000):
        local = rng.choice((f"{f}.{l}", f"{f}{l}", f"{f[0]}{l}", f"{l}.{f}", f"{f}_{l}", f"{f}.{l[0]}"))
        if attempt or rng.random() < 0.55:
            local += str(rng.randint(1, 99) if rng.random() < 0.7 else rng.randint(2001, 2006))
        email = f"{local}@{_weighted(rng, EMAIL_DOMAINS)}"
        if email not in reg.emails:
            reg.emails.add(email)
            return email
    raise RuntimeError("could not create a unique e-mail")


def _unique_phone(rng: random.Random, reg: _Registry) -> str:
    while True:
        phone = f"9{rng.randrange(10 ** 9):09d}"
        if phone not in reg.phones:
            reg.phones.add(phone)
            return phone


def _unique_handle(rng: random.Random, reg: _Registry, first: str, last: str) -> str:
    f, l = first.lower(), last.lower()
    for attempt in range(1000):
        handle = rng.choice((f"{f}{l}", f"{f}-{l}", f"{f[0]}{l}", f"{f}{l[0]}", f"{l}-{f}",
                             f"{f}-codes", f"{f}{l}-dev"))
        if attempt or rng.random() < 0.6:
            handle += str(rng.randint(1, 999))
        if handle not in reg.handles:
            reg.handles.add(handle)
            return handle
    raise RuntimeError("could not create a unique GitHub handle")


def _new_subnet(rng: random.Random, reg: _Registry) -> tuple[int, int, int]:
    while True:
        net = (rng.choice(PUBLIC_FIRST_OCTETS), rng.randint(0, 255), rng.randint(0, 255))
        if net not in reg.subnets:
            reg.subnets.add(net)
            return net


def _new_ip(rng: random.Random, reg: _Registry) -> str:
    """Random public-looking IPv4 in a /24 nobody else uses."""
    a, b, c = _new_subnet(rng, reg)
    return f"{a}.{b}.{c}.{rng.randint(2, 254)}"


def _draw_degree(rng: random.Random) -> tuple[str, str, str, int]:
    degree = _weighted(rng, DEGREES)
    if degree == "MCA":
        branch = "Computer Applications"
    elif degree == "M.Tech":
        branch = _weighted(rng, MTECH_BRANCHES)
    elif degree == "B.Sc":
        branch = _weighted(rng, BSC_BRANCHES)
    else:
        branch = _weighted(rng, UG_BRANCHES)
    track = _weighted(rng, FAMILY_TRACKS[BRANCH_FAMILY.get(branch, "cs")])
    if degree in ("M.Tech", "MCA"):
        grad_year = 2027 if rng.random() < 0.9 else 2026
    else:
        grad_year = _weighted(rng, ((2027, 85), (2026, 12), (2025, 3)))
    return degree, branch, track, grad_year


def _draw_cgpa(rng: random.Random, component: str, tier: int) -> float:
    base = {"low": 6.9, "mid": 7.45, "high": 8.05, "perfect": 8.55}[component]
    base += {1: 0.3, 2: 0.0, 3: -0.2}[tier]
    return round(_clip(rng.gauss(base, 0.55), 5.2, 9.85), 2)


def _new_person(rng: random.Random, reg: _Registry, idx: int, cid: str, college: College,
                component: str) -> Candidate:
    first, last = _unique_name(rng, reg)
    email = _unique_email(rng, reg, first, last)
    phone = _unique_phone(rng, reg)
    github = _unique_handle(rng, reg, first, last)
    linkedin = f"linkedin.com/in/{first.lower()}-{last.lower()}-{rng.getrandbits(24):06x}"
    degree, branch, track, grad_year = _draw_degree(rng)
    return Candidate(
        idx=idx, cid=cid, first=first, last=last, email=email, phone=phone, college=college,
        degree=degree, branch=branch, grad_year=grad_year, cgpa=_draw_cgpa(rng, component, college.tier),
        city=college.city if rng.random() < 0.85 else rng.choice(HOMETOWNS), track=track,
        github=github, linkedin=linkedin, source=_weighted(rng, SOURCES),
        invited_on=date(2026, 10, 20) + timedelta(days=rng.randint(0, 11)),
        invited_by=rng.choice(TA_INVITERS), phone_style=_weighted(rng, ((0, 70), (1, 15), (2, 15))),
        component=component)


# ---------------------------------------------------------------------------
# Test attempts
# ---------------------------------------------------------------------------


def _mcq_answers(rng: random.Random, k_correct: int) -> str:
    correct = set(rng.sample(range(MCQ_QUESTIONS), k_correct))
    out = []
    for i, key in enumerate(MCQ_KEY):
        if i in correct:
            out.append(key)
            continue
        wrong = [o for o in "ABCD" if o != key]
        out.append(wrong[i % 3] if rng.random() < 0.55 else rng.choice(wrong))   # popular distractor
    return "".join(out)


def _draw_scores(rng: random.Random, ts: TestSet, component: str) -> tuple[list[float], str]:
    """Per-question scores (MCQ, Q2..Q4) whose total falls in the component's % range."""
    if component == "perfect":
        return [float(m) for m in ts.maxes], MCQ_KEY
    lo, hi = COMPONENT_RANGES[component]
    scores: list[float] = []
    k_mcq = 0
    for _ in range(200):
        target = rng.uniform(lo, hi) / 100.0
        k_mcq = _binomial(rng, MCQ_QUESTIONS, _clip(rng.gauss(target + 0.06, 0.12), 0.05, 0.98))
        scores = [k_mcq * ts.units[0]]
        for m, u in zip(ts.maxes[1:], ts.units[1:]):
            scores.append(round(_clip(rng.gauss(target, 0.2), 0.0, 1.0) * m / u) * u)
        if lo <= sum(scores) / ts.total * 100 < hi:
            break
    else:   # pragma: no cover - statistically unreachable; nudge coding scores into range
        for _ in range(500):
            pct = sum(scores) / ts.total * 100
            if lo <= pct < hi:
                break
            q = max((1, 2, 3), key=lambda i: scores[i]) if pct >= hi else min((1, 2, 3), key=lambda i: scores[i])
            scores[q] = _clip(scores[q] + (-ts.units[q] if pct >= hi else ts.units[q]), 0, ts.maxes[q])
    return [float(s) for s in scores], _mcq_answers(rng, k_mcq)


def _draw_duration_s(rng: random.Random, perfect: bool) -> int:
    if perfect:
        return rng.randint(50 * 60, 85 * 60)
    return int(round(_clip(rng.gauss(70, 12), 22, TIME_LIMIT_MIN) * 60))


def _remote_start(rng: random.Random, duration_s: int) -> datetime:
    day = _weighted(rng, ((0, 36), (1, 34), (2, 30)))
    room = (WINDOW_END_H - WINDOW_START_H) * 3600 - duration_s
    return _at(day, WINDOW_START_H) + timedelta(seconds=rng.randint(0, max(0, room)))


def _session_start(rng: random.Random, college_name: str) -> datetime:
    day, hour, minute = ONCAMPUS_SESSIONS[college_name]
    return _at(day, hour, minute) + timedelta(seconds=rng.randint(0, 150))


def _unique_code_hash(rng: random.Random, seed: int, c: Candidate, q: int) -> str:
    return _sha12(f"{seed}|{c.cid}|q{q}|{rng.getrandbits(48)}")


def _draw_code_answers(rng: random.Random, c: Candidate, seed: int) -> None:
    """Answer hashes and HackerRank code similarity for an honest attempt."""
    c.hashes[0] = _sha12(f"mcq|{c.mcq_answers}")
    for q in (1, 2, 3):
        if c.q_scores[q] > 0 or rng.random() < 0.55:
            c.hashes[q] = _unique_code_hash(rng, seed, c, q + 1)
            easy_q2_match = q == 1 and rng.random() < 0.08
            sim = rng.uniform(60, 75) if easy_q2_match else rng.triangular(5, 55, 24 if q == 1 else 20)
            c.sims[q - 1] = round(sim, 1)
        else:
            c.hashes[q], c.sims[q - 1] = "", None


def _draw_honest_flags(rng: random.Random, c: Candidate) -> None:
    c.oow_events = _poisson(rng, 0.8)
    c.oow_sec = sum(int(rng.expovariate(1 / 22)) + 3 for _ in range(c.oow_events))
    c.paste = _poisson(rng, 0.6)
    r = rng.random()
    c.secure_exits = 0 if r < 0.96 else (1 if r < 0.995 else 2)
    r = rng.random()
    c.integrity = "Clear" if r < 0.95 else ("Review" if r < 0.99 else "Suspicious")
    if c.integrity == "Clear":
        c.proctor_flags = ["Candidate not visible"] if rng.random() < 0.01 else []
    else:
        n_flags = 1 if c.integrity == "Review" else 2
        flags: list[str] = []
        while len(flags) < n_flags:
            flag = _weighted(rng, PROCTOR_FLAG_CHOICES)
            if flag not in flags:
                flags.append(flag)
        c.proctor_flags = flags
    c.plagiarism = rng.random() < 0.01


def _individual_points(c: Candidate, rc: RiskConfig) -> float:
    """Individual-flag score the risk engine would assign with default weights (no speed flag)."""
    ms = c.max_sim
    hits = (
        (c.plagiarism, "plagiarism_flag"),
        (ms is not None and ms >= rc.code_similarity_threshold, "high_code_similarity"),
        (c.integrity in ("Review", "Suspicious"), "integrity_status"),
        (bool(c.proctor_flags), "proctoring_flags"),
        (c.oow_events >= rc.tab_switch_threshold, "tab_switches"),
        (c.oow_sec >= rc.out_of_window_sec_threshold, "out_of_window_time"),
        (c.paste >= rc.copy_paste_threshold, "copy_paste"),
        (c.secure_exits >= rc.secure_mode_exit_threshold, "secure_mode_exits"),
    )
    return float(sum(rc.individual_weights.get(key, 0.0) for hit, key in hits if hit))


def _cap_honest_flags(c: Candidate, rc: RiskConfig) -> None:
    """Keep stray proctoring noise on an honest candidate below the Medium/High boundary."""
    if _individual_points(c, rc) >= LEGIT_INDIVIDUAL_CAP:
        c.proctor_flags = []
    if _individual_points(c, rc) >= LEGIT_INDIVIDUAL_CAP:
        c.integrity = "Clear"
    if _individual_points(c, rc) >= LEGIT_INDIVIDUAL_CAP:
        c.secure_exits = 0
    if _individual_points(c, rc) >= LEGIT_INDIVIDUAL_CAP:
        c.paste = min(c.paste, rc.copy_paste_threshold - 1)
    if _individual_points(c, rc) >= LEGIT_INDIVIDUAL_CAP:
        c.oow_events, c.oow_sec = min(c.oow_events, 2), min(c.oow_sec, 60)


# ---------------------------------------------------------------------------
# TA feed generation
# ---------------------------------------------------------------------------


@dataclass
class _Plan:
    groups: dict[str, list[int]]           # role -> candidate indices (role order matters)
    role_of: dict[int, str]
    forced_college: dict[int, College]
    nat_ip: dict[str, str]                 # on-campus college -> lab NAT IP
    lab_size: int


def _distinct_city_colleges(rng: random.Random, pool: Sequence[College], k: int) -> list[College]:
    picked: list[College] = []
    for col in rng.sample(list(pool), len(pool)):
        if col.city not in {p.city for p in picked}:
            picked.append(col)
        if len(picked) == k:
            return picked
    raise RuntimeError("not enough cities for the speed-run pattern")


def pattern_sizes(n: int) -> dict[str, int]:
    """Planted group sizes for an n-candidate dataset. The resume mill's shared bullets / repos
    must stay "rare" (held by <= max_bullet_population_share of all resumes, per the risk
    contract), so very small datasets (--quick) get a smaller mill."""
    sizes = dict(PATTERN_SIZES)
    rare_cap = int(RiskConfig().max_bullet_population_share * RESUME_RATE * n)
    sizes[MILL] = max(3, min(sizes[MILL], rare_cap))
    return sizes


def _make_plan(n: int, seed: int, reg: _Registry) -> _Plan:
    """Decide which candidate IDs carry each planted pattern / decoy and their colleges."""
    rng = _rng(seed, "plan")
    lab_size = max(DECOY_SIZE + 5, min(LAB_SIZE_FULL, round(LAB_SIZE_FULL * n / DEFAULT_N)))
    sizes = [*pattern_sizes(n).items(), ("decoy", DECOY_SIZE), ("lab", lab_size - DECOY_SIZE)]
    picked = rng.sample(range(1, n + 1), sum(size for _, size in sizes))
    groups: dict[str, list[int]] = {}
    pos = 0
    for role, size in sizes:
        groups[role] = picked[pos:pos + size]
        pos += size
    special = {RING_COLLEGE, HOSTEL_COLLEGE, *ONCAMPUS_SESSIONS}
    remote_pool = [c for c in COLLEGES if c.name not in special]
    mill_pool = [c for c in remote_pool if c.city == MILL_CITY]
    forced: dict[int, College] = {}
    for i in groups[RING]:
        forced[i] = COLLEGE_BY_NAME[RING_COLLEGE]
    for i in groups[HOSTEL]:
        forced[i] = COLLEGE_BY_NAME[HOSTEL_COLLEGE]
    for i in groups["decoy"] + groups["lab"]:
        forced[i] = COLLEGE_BY_NAME[DECOY_COLLEGE]
    forced.update(zip(groups[MILL], rng.sample(mill_pool, len(groups[MILL]))))
    forced.update(zip(groups[PROXY], rng.sample(remote_pool, len(groups[PROXY]))))
    forced.update(zip(groups[SPEEDRUN], _distinct_city_colleges(rng, remote_pool, len(groups[SPEEDRUN]))))
    forced.update(zip(groups[IDENTITY], rng.sample(remote_pool, len(groups[IDENTITY]))))
    nat_ip = {name: _new_ip(rng, reg) for name in ONCAMPUS_SESSIONS}
    role_of = {i: role for role, ids in groups.items() for i in ids}
    return _Plan(groups, role_of, forced, nat_ip, lab_size)


def _component_for(rng: random.Random, role: str, college: College) -> str:
    if role in PATTERN_SIZES or role == "decoy":
        return "perfect"
    if role == "lab":   # the decoy college's other lab candidates never score 100%
        return _weighted(rng, list(zip(COMPONENTS[:3], TIER_COMPONENT_WEIGHTS[1][:3])))
    return _weighted(rng, list(zip(COMPONENTS, TIER_COMPONENT_WEIGHTS[college.tier])))


def _generate_candidates(n: int, seed: int, reg: _Registry, plan: _Plan) -> list[Candidate]:
    weights = [COLLEGE_TIER_WEIGHT[c.tier] for c in OPEN_COLLEGES]
    out: list[Candidate] = []
    for idx in range(1, n + 1):
        rng = _rng(seed, "candidate", idx)
        role = plan.role_of.get(idx, "normal")
        college = plan.forced_college.get(idx) or rng.choices(OPEN_COLLEGES, weights=weights)[0]
        component = _component_for(rng, role, college)
        c = _new_person(rng, reg, idx, f"CAND-{idx:05d}", college, component)
        if role == "normal":
            c.test_set = "B" if rng.random() < SET_B_RATE else "A"
            r = rng.random()
            c.status = ("Expired" if r < NO_ATTEMPT_RATE * 0.6 else
                        "Did Not Attempt" if r < NO_ATTEMPT_RATE else "Completed")
        c.mode = "On-campus" if college.name in ONCAMPUS_SESSIONS else "Remote"
        if c.attempted:
            c.q_scores, c.mcq_answers = _draw_scores(rng, TEST_SETS[c.test_set], component)
            c.duration_s = _draw_duration_s(rng, component == "perfect")
            if c.mode == "On-campus":
                c.start, c.ip, c.timing_fixed = _session_start(rng, college.name), plan.nat_ip[college.name], True
            else:
                c.start, c.ip = _remote_start(rng, c.duration_s), _new_ip(rng, reg)
            _draw_code_answers(rng, c, seed)
            _draw_honest_flags(rng, c)
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Planted integrity patterns and decoys
# ---------------------------------------------------------------------------


@dataclass
class _PlantContext:
    seed: int
    reg: _Registry
    plan: _Plan

    def shared_hash(self, key: str, q: int) -> str:
        return _sha12(f"{self.seed}|planted|{key}|q{q}")


def _make_perfect_colluder(c: Candidate, pattern: str) -> None:
    ts = TEST_SETS["A"]
    c.test_set, c.status, c.component = "A", "Completed", "perfect"
    c.q_scores, c.mcq_answers = [float(m) for m in ts.maxes], MCQ_KEY
    c.hashes[0] = MCQ_KEY_HASH
    c.mode, c.timing_fixed = "Remote", True
    c.most_similar, c.plagiarism, c.secure_exits = "", False, 0
    c.integrity, c.proctor_flags = "Clear", []
    c.label, c.cluster_key, c.pattern = "colluder", PATTERN_KEYS[pattern], pattern
    c.expected_min_level = PATTERN_MIN_LEVEL[pattern]


def _sync_offsets(rng: random.Random, n: int, start_span: float, submit_span: float,
                  dur_span: float) -> list[tuple[float, float]]:
    """(start offset, duration offset) pairs: starts within start_span, submissions within
    submit_span and durations within dur_span seconds of each other."""
    for _ in range(2000):
        starts = sorted(rng.uniform(0, start_span) for _ in range(n))
        submits = sorted(rng.uniform(0, submit_span) for _ in range(n))
        durs = [s - o for o, s in zip(starts, submits)]
        if max(durs) - min(durs) <= dur_span:
            pairs = list(zip(starts, durs))
            rng.shuffle(pairs)
            return pairs
    raise RuntimeError("could not satisfy synchronisation constraints")


def _quiet_flags(rng: random.Random, c: Candidate) -> None:
    c.oow_events = rng.randint(0, 1)
    c.oow_sec = rng.randint(4, 30) if c.oow_events else 0
    c.paste = rng.randint(0, 1)
    c.secure_exits, c.integrity, c.proctor_flags, c.plagiarism = 0, "Clear", [], False


def _honest_sims(rng: random.Random, c: Candidate, seed: int, cap: float = 60.0) -> None:
    for q in (1, 2, 3):
        c.hashes[q] = _unique_code_hash(rng, seed, c, q + 1)
        c.sims[q - 1] = round(min(cap, rng.triangular(5, 55, 22)), 1)


def _plant_ring(members: list[Candidate], rng: random.Random, ctx: _PlantContext) -> dict[str, Any]:
    key = PATTERN_KEYS[RING]
    shared = [ctx.shared_hash(key, q) for q in (2, 3, 4)]
    base = datetime(2026, 11, 1, 19, 30) + timedelta(minutes=rng.randint(0, 40), seconds=rng.randint(0, 59))
    base_dur = rng.randint(56 * 60, 64 * 60)
    offsets = _sync_offsets(rng, len(members), start_span=170, submit_span=85, dur_span=115)
    suspicious = set(rng.sample(range(len(members)), 6))
    review = set(rng.sample(range(len(members)), 3))
    for i, (c, (s_off, d_off)) in enumerate(zip(members, offsets)):
        c.ip = _new_ip(rng, ctx.reg)
        c.start, c.duration_s = base + timedelta(seconds=round(s_off)), base_dur + round(d_off)
        c.hashes[1:] = shared
        c.sims = [round(rng.uniform(95, 100), 1) for _ in range(3)]
        c.most_similar = members[1 if i == 0 else 0].email
        c.plagiarism = i in suspicious
        c.oow_events = rng.randint(6, 12)
        c.oow_sec = sum(rng.randint(18, 45) for _ in range(c.oow_events))
        c.paste, c.secure_exits = rng.randint(4, 9), rng.randint(0, 1)
        c.integrity = "Review" if i in review else "Clear"
        c.proctor_flags = ["Candidate not visible"] if i in review else []
    return {"college": RING_COLLEGE, "shared_hashes": dict(zip(("q2", "q3", "q4"), shared)),
            "github_repo": f"github.com/{members[0].github}/{RING_REPO_SLUG}",
            "copied_projects": [p[0] for p in RING_PROJECTS], "plagiarism_suspicious": len(suspicious)}


def _plant_proxy(members: list[Candidate], rng: random.Random, ctx: _PlantContext) -> dict[str, Any]:
    key = PATTERN_KEYS[PROXY]
    q3, q4 = ctx.shared_hash(key, 3), ctx.shared_hash(key, 4)
    ip = _new_ip(rng, ctx.reg)
    t = datetime(2026, 11, 1, 15, 0) + timedelta(minutes=rng.randint(0, 20), seconds=rng.randint(0, 59))
    face = set(rng.sample(range(len(members)), 4))
    suspicious = set(rng.sample(range(len(members)), 3))
    members = rng.sample(members, len(members))          # session order is unrelated to candidate IDs
    sessions = []
    for i, c in enumerate(members):
        c.ip, c.start, c.duration_s = ip, t, rng.randint(18 * 60, 26 * 60)
        t = c.end + timedelta(seconds=rng.randint(5 * 60, 15 * 60))      # type: ignore[operator]
        c.hashes[1] = _unique_code_hash(rng, ctx.seed, c, 2)
        c.hashes[2], c.hashes[3] = q3, q4
        c.sims = [round(rng.uniform(30, 65), 1), round(rng.uniform(90, 100), 1), round(rng.uniform(90, 100), 1)]
        c.most_similar = members[i - 1 if i else 1].email
        c.plagiarism = i in suspicious
        c.oow_events = _poisson(rng, 0.8)
        c.oow_sec = sum(rng.randint(3, 30) for _ in range(c.oow_events))
        c.paste = min(2, _poisson(rng, 0.6))
        c.integrity = "Review"
        c.proctor_flags = ["Face mismatch with profile photo"] if i in face else []
        sessions.append({"candidate_id": c.cid, "start": _fmt_dt(c.start, "std"),
                         "end": _fmt_dt(c.end, "std")})        # type: ignore[arg-type]
    return {"ip": ip, "colleges": [c.college.name for c in members],
            "shared_hashes": {"q3": q3, "q4": q4}, "sessions": sessions,
            "face_mismatch": len(face), "plagiarism_suspicious": len(suspicious)}


def _plant_mill(members: list[Candidate], rng: random.Random, ctx: _PlantContext) -> dict[str, Any]:
    for c in members:
        c.city, c.source, c.timing_fixed = MILL_CITY, "Referral", False
        c.ip, c.duration_s = _new_ip(rng, ctx.reg), rng.randint(50 * 60, 85 * 60)
        c.start = _remote_start(rng, c.duration_s)
        _honest_sims(rng, c, ctx.seed)
        _quiet_flags(rng, c)
    return {"city": MILL_CITY, "colleges": [c.college.name for c in members],
            "github_repos": [f"github.com/{MILL_HANDLE}/{slug}" for _, _, slug in MILL_PROJECTS],
            "shared_bullets": list(MILL_SHARED_BULLETS)}


def _plant_hostel(members: list[Candidate], rng: random.Random, ctx: _PlantContext) -> dict[str, Any]:
    key = PATTERN_KEYS[HOSTEL]
    a, b, net_c = _new_subnet(rng, ctx.reg)
    hosts = rng.sample(range(2, 255), len(members))
    base = datetime(2026, 11, 2, 20, 0) + timedelta(minutes=rng.randint(0, 30), seconds=rng.randint(0, 59))
    base_dur = rng.randint(56 * 60, 66 * 60)
    offsets = _sync_offsets(rng, len(members), start_span=110, submit_span=110, dur_span=1e9)
    groups = [members[:5], members[5:]]
    q4 = [ctx.shared_hash(f"{key}-a", 4), ctx.shared_hash(f"{key}-b", 4)]
    suspicious = set(rng.sample(range(len(members)), 4))
    multi_face = set(rng.sample(range(len(members)), 2))
    for i, (c, (s_off, d_off)) in enumerate(zip(members, offsets)):
        g = groups[0 if i < 5 else 1]
        c.ip = f"{a}.{b}.{net_c}.{hosts[i]}"
        c.start, c.duration_s = base + timedelta(seconds=round(s_off)), base_dur + round(d_off)
        c.hashes[1] = _unique_code_hash(rng, ctx.seed, c, 2)
        c.hashes[2] = _unique_code_hash(rng, ctx.seed, c, 3)
        c.hashes[3] = q4[0 if i < 5 else 1]
        c.sims = [round(rng.triangular(10, 55, 25), 1), round(rng.triangular(10, 55, 25), 1),
                  round(rng.uniform(95, 100), 1)]
        c.most_similar = (g[1] if c is g[0] else g[0]).email
        c.plagiarism = i in suspicious
        c.paste, c.oow_events = rng.randint(5, 10), rng.randint(5, 9)
        c.oow_sec = sum(rng.randint(15, 40) for _ in range(c.oow_events))
        c.integrity = "Review" if i in multi_face else "Clear"
        c.proctor_flags = ["Multiple faces detected"] if i in multi_face else []
    return {"college": HOSTEL_COLLEGE, "subnet": f"{a}.{b}.{net_c}.0/24",
            "subgroups": [[c.cid for c in g] for g in groups], "q4_hashes": q4,
            "plagiarism_suspicious": len(suspicious)}


def _plant_speedrun(members: list[Candidate], rng: random.Random, ctx: _PlantContext) -> dict[str, Any]:
    key = PATTERN_KEYS[SPEEDRUN]
    shared = [ctx.shared_hash(key, q) for q in (2, 3, 4)]
    offsets = sorted(rng.randint(0, 105 * 60) for _ in members)
    suspicious = set(rng.sample(range(len(members)), 5))
    for i, (c, off) in enumerate(zip(members, offsets)):
        c.city, c.ip = c.college.city, _new_ip(rng, ctx.reg)
        c.start, c.duration_s = datetime(2026, 11, 3, 21, 0) + timedelta(seconds=off), rng.randint(9 * 60, 15 * 60)
        c.hashes[1:] = shared
        c.sims = [100.0, 100.0, 100.0]
        c.most_similar = members[1 if i == 0 else 0].email
        c.plagiarism = i in suspicious
        c.paste = rng.randint(3, 8)
        c.oow_events = _poisson(rng, 0.5)
        c.oow_sec = sum(rng.randint(3, 20) for _ in range(c.oow_events))
    return {"shared_hashes": dict(zip(("q2", "q3", "q4"), shared)),
            "colleges": [c.college.name for c in members], "cities": [c.city for c in members],
            "window": ["2026-11-03 21:00:00", "2026-11-03 23:00:00"], "duration_min_range": [9, 15],
            "plagiarism_suspicious": len(suspicious)}


def _plant_identity(members: list[Candidate], rng: random.Random, ctx: _PlantContext) -> dict[str, Any]:
    phone = members[0].phone
    shared_ip, other_ip = _new_ip(rng, ctx.reg), _new_ip(rng, ctx.reg)
    slots = ((0, 11), (1, 16), (2, 11))      # one attempt per day, hours apart: not "sequential"
    for i, c in enumerate(members):
        c.phone = phone
        c.ip = shared_ip if i < 2 else other_ip
        c.duration_s = rng.randint(55 * 60, 85 * 60)
        day, hour = slots[i]
        c.start = _at(day, hour) + timedelta(seconds=rng.randint(0, 90 * 60))
        _honest_sims(rng, c, ctx.seed)
        _quiet_flags(rng, c)
    return {"phone_last10": phone, "shared_ip": shared_ip, "shared_ip_members": [c.cid for c in members[:2]]}


def _plant_decoys(decoys: list[Candidate], rng: random.Random, ctx: _PlantContext) -> dict[str, Any]:
    """Legit 100% scorers who sat one scheduled on-campus lab session behind a NAT IP."""
    for c in decoys:
        ts = TEST_SETS["A"]
        c.test_set, c.status, c.component = "A", "Completed", "perfect"
        c.q_scores, c.mcq_answers, c.hashes[0] = [float(m) for m in ts.maxes], MCQ_KEY, MCQ_KEY_HASH
        c.mode, c.ip, c.timing_fixed = "On-campus", ctx.plan.nat_ip[DECOY_COLLEGE], True
        c.start, c.duration_s = _session_start(rng, DECOY_COLLEGE), rng.randint(45 * 60, 85 * 60)
        _honest_sims(rng, c, ctx.seed, cap=48.0)
        _quiet_flags(rng, c)
        c.most_similar = ""
        c.label, (c.cluster_key, c.pattern) = "decoy", DECOY_LAB
    day, hour, minute = ONCAMPUS_SESSIONS[DECOY_COLLEGE]
    return {
        "key": DECOY_LAB[0], "pattern": DECOY_LAB[1], "label": "decoy", "size": len(decoys),
        "members": sorted(c.cid for c in decoys), "expected_min_level": "None", "must_not_exceed": "Medium",
        "description": (f"{len(decoys)} legitimate perfect scorers from {DECOY_COLLEGE} (Tier 1) sat one "
                        f"scheduled on-campus lab session ({_fmt_dt(_at(day, hour, minute), 'std')}) behind a "
                        f"NAT IP shared by {ctx.plan.lab_size} candidates of the college; submissions spread "
                        "over 45-85 minutes, unique code, similarity < 50%. Shared IP and synchronised "
                        "start are expected here and must not be flagged High."),
        "expected_signals": [],
        "details": {"college": DECOY_COLLEGE, "ip": ctx.plan.nat_ip[DECOY_COLLEGE],
                    "lab_size": ctx.plan.lab_size, "session_start": _fmt_dt(_at(day, hour, minute), "std")},
    }


PATTERN_DOCS: dict[str, tuple[str, list[str]]] = {
    RING: ("Perfect scorers from one college (remote) submitted byte-identical Q2-Q4 code within 90 s of "
           "each other after starting within 3 min; HackerRank similarity 95-100% naming each other, most "
           "flagged Suspicious; heavy out-of-window / paste activity; resumes share two near-verbatim "
           "copied projects and the same GitHub repository.",
           ["shared_answer_hash", "plagiarism_match", "sync_start", "sync_submission", "duration_match",
            "shared_github_repo", "resume_text_high", "shared_bullets", "plagiarism_flag",
            "high_code_similarity", "tab_switches", "out_of_window_time", "copy_paste", "integrity_status",
            "proctoring_flags"]),
    PROXY: ("Perfect scorers from six different colleges tested from one residential IP in back-to-back "
            "sessions (each starting 5-15 min after the previous ended), 18-26 min each, with identical "
            "Q3/Q4 code; AI proctoring marked all for review (face mismatch for four) - consistent with a "
            "proxy test-taker.",
            ["same_ip", "sequential_same_ip", "shared_answer_hash", "plagiarism_match", "speed_anomaly",
             "integrity_status", "proctoring_flags", "high_code_similarity", "plagiarism_flag"]),
    MILL: ("Perfect scorers from different colleges in one city, all referrals, with clean test "
           "telemetry; their resumes reuse a coaching-institute template: the same two GitHub repositories "
           "and three identical project bullets.",
           ["shared_github_repo", "shared_bullets", "resume_text_medium"]),
    HOSTEL: ("Perfect scorers from one college tested remotely from ten IPs in one /24 subnet, starting "
             "within 2 min and submitting within 2 min; two sub-groups of five share an identical Q4 "
             "submission; elevated paste and out-of-window activity.",
             ["shared_answer_hash", "plagiarism_match", "same_subnet", "sync_start", "sync_submission",
              "duration_match", "copy_paste", "tab_switches", "out_of_window_time", "high_code_similarity",
              "plagiarism_flag", "integrity_status", "proctoring_flags"]),
    SPEEDRUN: ("Perfect scorers from eight colleges in eight cities finished in 9-15 minutes, all starting "
               "between 21:00 and 23:00 on 3 Nov, with byte-identical Q2-Q4 code (100% similarity) - "
               "consistent with a leaked solution set.",
               ["shared_answer_hash", "plagiarism_match", "speed_anomaly", "plagiarism_flag",
                "high_code_similarity", "copy_paste"]),
    IDENTITY: ("Three candidate IDs with different names and e-mails share one phone number and all scored "
               "100%; two of them tested from the same IP on different days.",
               ["same_phone", "same_ip"]),
}


def _plant_patterns(cands: list[Candidate], plan: _Plan, seed: int, reg: _Registry) -> list[dict[str, Any]]:
    by_idx = {c.idx: c for c in cands}
    ctx = _PlantContext(seed, reg, plan)
    planters = ((RING, _plant_ring), (PROXY, _plant_proxy), (MILL, _plant_mill), (HOSTEL, _plant_hostel),
                (SPEEDRUN, _plant_speedrun), (IDENTITY, _plant_identity))
    clusters: list[dict[str, Any]] = []
    for pattern, planter in planters:
        members = [by_idx[i] for i in sorted(plan.groups[pattern])]   # members[0] = lowest ID ("source")
        for c in members:
            _make_perfect_colluder(c, pattern)
        details = planter(members, _rng(seed, "plant", pattern), ctx)
        description, signals = PATTERN_DOCS[pattern]
        clusters.append({
            "key": PATTERN_KEYS[pattern], "pattern": pattern, "label": "colluder", "size": len(members),
            "members": sorted(c.cid for c in members), "expected_min_level": PATTERN_MIN_LEVEL[pattern],
            "description": description, "expected_signals": signals, "details": details})
    clusters.append(_plant_decoys([by_idx[i] for i in sorted(plan.groups["decoy"])], _rng(seed, "plant", "decoy"), ctx))
    return clusters


# ---------------------------------------------------------------------------
# Honest-candidate clean-up and data-quality injections
# ---------------------------------------------------------------------------


def _choose_core_injections(cands: list[Candidate], seed: int) -> dict[str, Any]:
    """Injections that change who is in the focus group / who can be referenced (before clean-up)."""
    rng = _rng(seed, "dq-core")
    normal = [c for c in cands if c.label == "normal" and c.attempted]
    high = [c for c in normal if c.test_set == "A" and c.mode == "Remote" and 85 <= (c.pct or 0) < 100]
    over = rng.sample(high if len(high) >= N_SCORE_ABOVE_MAX else
                      [c for c in normal if c.mode == "Remote" and not c.in_focus], N_SCORE_ABOVE_MAX)
    records = []
    for c in over:
        true_score = c.score
        c.recorded_score = c.max_score + rng.choice((2.0, 4.0, 5.0, 7.5, 10.0))
        records.append({"candidate_id": c.cid, "recorded_score": c.recorded_score,
                        "max_score": c.max_score, "true_score": true_score})
    over_ids = {c.cid for c in over}
    missing = rng.sample([c for c in normal if not c.in_focus and c.cid not in over_ids], N_MISSING_EMAILS)
    for c in missing:
        c.email_missing = True
    return {"scores_above_max": records, "missing_emails": sorted(c.cid for c in missing)}


def _deconflict_focus_times(cands: list[Candidate], rng: random.Random, window_s: float = 300.0) -> int:
    """Move honest remote 100% scorers whose start AND submission coincide with another focus
    candidate (pure chance) so synchronised timing only appears where it is planted / scheduled."""
    focus = [c for c in cands if c.in_focus and c.start is not None]
    placed = [c for c in focus if c.timing_fixed]
    moved = 0
    for c in (c for c in focus if not c.timing_fixed):
        for _ in range(500):
            clash = any(abs((c.start - o.start).total_seconds()) <= window_s          # type: ignore[operator]
                        and abs((c.end - o.end).total_seconds()) <= window_s          # type: ignore[operator]
                        for o in placed)
            if not clash:
                break
            c.start = _remote_start(rng, c.duration_s)
            moved += 1
        placed.append(c)
    return moved


def _cleanup_honest(cands: list[Candidate], seed: int, rc: RiskConfig) -> None:
    """Honest candidates: cap focus-group similarity, cap stray flags, name similar candidates,
    de-conflict timings."""
    rng = _rng(seed, "cleanup")
    for c in cands:
        if c.label == "colluder" or not c.attempted:
            continue
        if c.in_focus:
            c.sims = [s if s is None or s < 69.0 else round(rng.uniform(56, 68), 1) for s in c.sims]
        _cap_honest_flags(c, rc)
    pools: dict[str, list[Candidate]] = {}
    for c in cands:
        if c.label != "colluder" and c.attempted and not c.in_focus and not c.email_missing:
            pools.setdefault(c.test_set, []).append(c)
    for c in cands:
        ms = c.max_sim
        if c.label == "colluder" or ms is None or ms < 70:
            continue
        pool = pools[c.test_set]
        other = rng.choice(pool)
        while other is c:
            other = rng.choice(pool)
        c.most_similar = other.email
    _deconflict_focus_times(cands, rng)


def _assign_test_candidate_ids(cands: list[Candidate], seed: int) -> None:
    rng = _rng(seed, "hackerrank-ids")
    ids = sorted(rng.sample(range(113_200_000, 113_999_999), len(cands)))
    for c, tid in zip(sorted(cands, key=lambda c: (c.invited_on, c.idx)), ids):
        c.test_cid = tid


def _college_variant(rng: random.Random, name: str) -> str:
    """Same college, differing only by case / extra spaces."""
    kind = rng.randrange(4)
    if kind == 0:
        return name.upper()
    if kind == 1:
        return name.lower()
    if kind == 2:
        words = name.split(" ")
        i = rng.randrange(len(words) - 1)
        return " ".join(words[:i + 1]) + "  " + " ".join(words[i + 1:])
    return f" {name}  "


def _stale_snapshot(rng: random.Random, c: Candidate, seed: int) -> Candidate:
    """Earlier re-export of the same attempt: earlier end time, fewer passing test cases."""
    s = replace(c, q_scores=list(c.q_scores), hashes=list(c.hashes), sims=list(c.sims),
                proctor_flags=list(c.proctor_flags))
    s.duration_s = max(10 * 60, c.duration_s - rng.randint(4 * 60, 18 * 60))
    ts = TEST_SETS[c.test_set]
    coded = [q for q in (1, 2, 3) if s.q_scores[q] > 0]
    if coded:
        q = rng.choice(coded)
        s.q_scores[q] = max(0.0, s.q_scores[q] - ts.units[q] * rng.randint(1, 3))
        s.hashes[q] = _sha12(f"{seed}|{c.cid}|q{q + 1}|snapshot")
    return s


def _inject_presentation_issues(cands: list[Candidate], seed: int) -> tuple[dict[str, Any], list[Candidate]]:
    """Formatting problems that only affect how rows are written (plus duplicate re-exports)."""
    rng = _rng(seed, "dq-presentation")
    normal = [c for c in cands if c.label == "normal"]
    attempted = [c for c in normal if c.attempted and c.recorded_score is None]
    alt = rng.sample(attempted, round(ALT_DATETIME_RATE * len(cands)))
    for i, c in enumerate(alt):
        c.dt_style = "dmy" if i % 2 == 0 else "iso"
    hms = rng.sample([c for c in attempted if c.dt_style == "std"], N_TIME_HMS)
    for c in hms:
        c.time_taken_hms = True
    cgpa = rng.sample(normal, N_CGPA_PERCENT)
    for c in cgpa:
        c.cgpa_as_pct = True
    variant_pool = [c for c in normal if c.mode == "Remote" and not c.in_focus]
    variants = rng.sample(variant_pool, round(COLLEGE_VARIANT_RATE * len(cands)))
    for c in variants:
        c.college_display = _college_variant(rng, c.college.name)
    dup_pool = [c for c in attempted if any(s > 0 for s in c.q_scores[1:]) and not c.email_missing]
    stale = [_stale_snapshot(rng, c, seed) for c in rng.sample(dup_pool, N_DUPLICATES)]
    issues = {
        "alt_datetime_formats": [{"candidate_id": c.cid, "format": "%d-%b-%Y %H:%M:%S" if c.dt_style == "dmy"
                                  else "%Y-%m-%dT%H:%M:%S"} for c in sorted(alt, key=lambda c: c.cid)],
        "time_taken_hhmmss": [{"candidate_id": c.cid, "value": _fmt_duration(c.duration_s, True)}
                              for c in sorted(hms, key=lambda c: c.cid)],
        "cgpa_as_percentage": [{"candidate_id": c.cid, "value": f"{c.cgpa * 10:.1f}", "cgpa_10pt": c.cgpa}
                               for c in sorted(cgpa, key=lambda c: c.cid)],
        "college_name_variants": [{"candidate_id": c.cid, "value": c.college_display, "canonical": c.college.name}
                                  for c in sorted(variants, key=lambda c: c.cid)],
    }
    return issues, stale


def _ta_row(c: Candidate) -> list[str]:
    ts = TEST_SETS[c.test_set]
    done = c.attempted
    dt = (lambda x: _fmt_dt(x, c.dt_style) if x else "")
    phone = (f"+91 {c.phone}", f"+91-{c.phone}", c.phone)[c.phone_style]
    cgpa = f"{c.cgpa * 10:.1f}" if c.cgpa_as_pct else f"{c.cgpa:.2f}"
    score = c.recorded_score if c.recorded_score is not None else c.score
    sims = [_fmt_num(s, 1) for s in c.sims] if done else ["", "", ""]
    return [
        c.cid, str(c.test_cid), c.name, "" if c.email_missing else c.email, phone,
        c.college_display if c.college_display is not None else c.college.name, str(c.college.tier),
        c.degree, c.branch, str(c.grad_year), cgpa, c.city, ts.name, c.status,
        c.invited_on.isoformat(), c.invited_by, dt(c.start) if done else "", dt(c.end) if done else "",
        _fmt_duration(c.duration_s, c.time_taken_hms) if done else "",
        _fmt_num(score) if done else "", _fmt_num(ts.total), _fmt_num(c.pct) if done else "",
        *([_fmt_num(s) for s in c.q_scores] if done else ["", "", "", ""]),
        *sims, _fmt_num(c.max_sim, 1) if done else "", c.most_similar if done else "",
        ("Suspicious" if c.plagiarism else "Not Suspicious") if done else "",
        str(c.oow_events) if done else "", str(c.oow_sec) if done else "", str(c.paste) if done else "",
        str(c.secure_exits) if done else "", c.ip if done else "", c.integrity if done else "",
        "; ".join(c.proctor_flags) if done else "", c.mode, c.source,
        *(c.hashes if done else ["", "", "", ""]),
        f"https://reports.example.com/hackerrank/tests/{ts.test_id}/candidates/{c.test_cid}",
    ]


def _ta_rows(cands: list[Candidate], stale: list[Candidate], seed: int) -> tuple[list[list[str]], list[dict]]:
    rng = _rng(seed, "dq-duplicate-positions")
    rows = [_ta_row(c) for c in cands]
    records = []
    for s in stale:
        pos = rng.randint(0, len(rows))
        original_pos = next(i for i, r in enumerate(rows) if r[0] == s.cid)   # only the final row exists yet
        rows.insert(pos, _ta_row(s))
        final = next(c for c in cands if c.cid == s.cid)
        records.append({"candidate_id": s.cid, "kept_end_time": _fmt_dt(final.end, "std"),   # type: ignore[arg-type]
                        "stale_end_time": _fmt_dt(s.end, "std"),                              # type: ignore[arg-type]
                        "kept_score": final.score, "stale_score": s.score,
                        "stale_row_first": pos <= original_pos})
    return rows, sorted(records, key=lambda r: r["candidate_id"])


# ---------------------------------------------------------------------------
# Resume content
# ---------------------------------------------------------------------------


def _metrics(rng: random.Random) -> dict[str, str]:
    return {
        "pct": str(rng.randint(8, 65)), "acc": f"{rng.uniform(82, 97.9):.1f}", "auc": str(rng.randint(82, 95)),
        "f1": str(rng.randint(78, 93)), "f1a": str(rng.randint(55, 74)), "n": str(rng.randint(12, 480)),
        "k": str(rng.randint(2, 90)), "users": f"{rng.randint(150, 5000):,}", "rps": f"{rng.randint(120, 2500):,}",
        "ms": str(rng.randint(35, 400)), "mins": str(rng.randint(2, 30)), "hrs": str(rng.randint(2, 12)),
        "sec": str(rng.randint(2, 15)), "cov": str(rng.randint(72, 95)), "x": str(rng.randint(2, 12)),
        "rows": str(rng.randint(1, 25)), "cls": str(rng.randint(4, 38)), "fps": str(rng.randint(18, 60)),
        "stars": f"4.{rng.randint(2, 8)}", "lh": str(rng.randint(88, 100)), "team": str(rng.randint(2, 5)),
        "weeks": str(rng.randint(3, 12)), "small": str(rng.randint(5, 60)),
    }


def _project_bullet(rng: random.Random, obj: str, outcome: str | None, tech: Sequence[str], quality: float,
                    m: dict[str, str]) -> str:
    """'<Verb> <object> [using|with <tech>][, <measurable outcome>].' with an optional lead-in."""
    verb = rng.choice(PROJECT_VERBS)
    fresh = [t for t in tech if t.lower() not in obj.lower()] or list(tech)   # avoid "SQL queries ... with SQL"
    tech_phrase = _join_words(rng.sample(fresh, min(len(fresh), rng.randint(1, 2))))
    shape = rng.random()
    if outcome is None or shape < 0.45 - 0.25 * quality:
        body = f"{verb} {obj} using {tech_phrase}"
    elif shape < 0.75:
        body = f"{verb} {obj} with {tech_phrase}, {outcome}"
    else:
        body = f"{verb} {obj}, {outcome}"
    prefix = rng.choice(BULLET_PREFIXES)
    if prefix:
        body = prefix + body[0].lower() + body[1:]
    return (body + ".").format(**m)


def _repo_slug(rng: random.Random, title: str, slugs: Sequence[str]) -> str:
    """Repository name derived from the project title (or one of the template's short names)."""
    if rng.random() < 0.4:
        return rng.choice(slugs)
    words = re.sub(r"[^a-z0-9 ]", " ", title.lower()).split()
    words = [w for w in words if w not in {"a", "an", "the", "for", "of", "with", "using", "in", "on", "and"}]
    return "-".join(words[:4])


def _make_project(rng: random.Random, tpl: ProjectTemplate, handle: str, quality: float,
                  reg: _Registry) -> Project:
    title_idx = rng.randrange(len(tpl.titles))
    tech = rng.sample(list(tpl.tech), min(len(tpl.tech), rng.randint(2, 4)))
    n_bullets = min(len(tpl.objects), 2 + (quality > 0.45) + (quality > 0.85 and rng.random() < 0.4))
    objs = rng.sample(list(tpl.objects), n_bullets)
    outs: list[str | None] = list(rng.sample(list(tpl.outcomes), len(tpl.outcomes)))
    outs += [None] * (len(objs) - len(outs))       # never repeat an outcome inside one project

    def bullet(obj: str, outcome: str | None) -> str:
        return _unique_bullet(lambda: _project_bullet(rng, obj, outcome, tech, quality, _metrics(rng)), reg)

    bullets = [bullet(o, oc) for o, oc in zip(objs, outs)]
    title = tpl.titles[title_idx]
    repo = (f"github.com/{handle}/{_repo_slug(rng, title, tpl.slugs[title_idx:title_idx + 1] or tpl.slugs)}"
            if rng.random() < 0.3 + 0.45 * quality else None)
    return Project(title, tech, bullets, repo)


def _make_projects(rng: random.Random, c: Candidate, quality: float, reg: _Registry) -> list[Project]:
    """2-4 projects: mostly from the candidate's track, some from neighbouring tracks (keeps
    honest resumes textually diverse even within small tracks)."""
    n = 2 + (quality > 0.45) + (quality > 0.78)
    primary = [t for t in PROJECT_TEMPLATES if c.track in t.tracks]
    neighbours = [t for t in PROJECT_TEMPLATES
                  if t not in primary and any(nb in t.tracks for nb in TRACK_NEIGHBOURS[c.track])]
    chosen: list[ProjectTemplate] = []
    for i in range(n):
        pool = primary if i == 0 or rng.random() < 0.6 else neighbours
        options = [t for t in pool if t not in chosen] or [t for t in primary + neighbours if t not in chosen]
        chosen.append(rng.choice(options))
    return [_make_project(rng, t, c.github, quality, reg) for t in chosen]


def _overlaps(a: tuple, b: tuple) -> bool:
    a_end, b_end = a[1] or PRESENT, b[1] or PRESENT
    return not (a_end < b[0] or b_end < a[0])


def _make_experiences(rng: random.Random, c: Candidate, quality: float, reg: _Registry) -> list[Experience]:
    r = rng.random()
    if quality < 0.3:
        count = 0 if r < 0.85 else 1
    elif quality < 0.6:
        count = 0 if r < 0.4 else (1 if r < 0.9 else 2)
    else:
        count = 0 if r < 0.15 else (1 if r < 0.65 else 2)
    terms: list[tuple] = []
    pool = INTERN_TERMS.get(c.grad_year, INTERN_TERMS[2027])
    for term in rng.sample(list(pool), len(pool)):
        if len(terms) == count:
            break
        if not any(_overlaps(term, t) for t in terms):
            terms.append(term)
    roles = [t for t in ROLE_TEMPLATES if c.track in t.tracks or "*" in t.tracks]
    tech = TRACKS[c.track][0]

    def bullet(obj: str, outcome: str) -> str:
        verb = rng.choice(INTERN_VERBS)
        if rng.random() < 0.3 - 0.2 * quality:
            text = f"{verb} {obj} using {_join_words(rng.sample(list(tech), 2))}."
        else:
            text = f"{verb} {obj}, {outcome}."
        return text.format(**_metrics(rng))

    out = []
    for start, end in terms:
        role = rng.choice(roles)
        title = rng.choice(GRADUATE_TITLES) if end is None and c.grad_year < 2027 else role.title
        n_b = min(len(role.objects), 2 + (quality > 0.55) + (quality > 0.85 and rng.random() < 0.5))
        pairs = zip(rng.sample(list(role.objects), n_b), rng.sample(INTERN_OUTCOMES, n_b))
        bullets = [_unique_bullet(lambda o=obj, oc=outcome: bullet(o, oc), reg) for obj, outcome in pairs]
        out.append(Experience(title, rng.choice(COMPANIES), rng.choice(COMPANY_CITIES), start, end, bullets))
    return sorted(out, key=lambda e: e.start, reverse=True)


def _make_achievements(rng: random.Random, quality: float, reg: _Registry) -> list[str]:
    count = rng.choice((0, 1)) if quality < 0.4 else rng.choice((1, 2, 2, 3))

    def achievement() -> str:
        cf = rng.randint(1200, 1850)
        return rng.choice(ACHIEVEMENTS).format(
            hack=rng.choice(HACKATHONS), year=rng.choice((2024, 2025, 2026)), teams=rng.randint(40, 600),
            theme=rng.choice(THEMES), top=rng.randint(5, 25), lc=rng.randint(150, 900),
            rating=rng.randint(1450, 2150), stars=rng.randint(3, 5), rank=f"{rng.randint(100, 9999):,}",
            cf=cf, cftitle="Pupil" if cf < 1400 else ("Specialist" if cf < 1600 else "Expert"),
            ws=rng.randint(3, 12), mentees=rng.randint(10, 60))

    out: list[str] = []
    for _ in range(count):
        text = _unique_bullet(achievement, reg)
        if text not in out:
            out.append(text)
    return out


def _make_certs(rng: random.Random, track: str, quality: float) -> list[str]:
    r = rng.random()
    count = 0 if r < 0.45 - 0.35 * quality else (1 if r < 0.75 - 0.2 * quality else (2 if r < 0.92 else 3))
    pool = [name for name, tracks in CERTIFICATIONS if track in tracks or "*" in tracks]
    return [f"{name} ({rng.choice((2024, 2025, 2025, 2026))})" for name in rng.sample(pool, min(count, len(pool)))]


def _make_skills(rng: random.Random, track: str, quality: float) -> list[str]:
    core, extra = TRACKS[track]
    skills = list(core)
    if rng.random() < 0.3:
        skills.remove(rng.choice(skills[2:]))
    k = int(_clip(round(2 + 7 * quality + rng.uniform(-1, 1)), 1, len(extra)))
    return list(dict.fromkeys(skills + rng.sample(list(extra), k)))


def _short_branch(branch: str) -> str:
    return (branch.replace(" and Engineering", "").replace("Engineering", "Engg.")
            if len(branch) > 32 else branch)


def _make_summary(rng: random.Random, c: Candidate) -> str:
    branch = _short_branch(c.branch)
    if c.grad_year >= 2027:
        opener = rng.choice((f"Final-year {c.degree} student in {branch}",
                             f"{c.degree} ({branch}) student graduating in {c.grad_year}",
                             f"Aspiring software engineer pursuing {c.degree} in {branch}"))
    else:
        opener = rng.choice((f"{c.degree} graduate in {branch} ({c.grad_year})",
                             f"Recent {c.degree} graduate in {branch}"))
    return f"{opener} {rng.choice(SUMMARY_FOCUS[c.track])}. {rng.choice(SUMMARY_GOALS)}"


def _resume_phone(rng: random.Random, digits: str) -> str:
    return rng.choice((f"+91 {digits[:5]} {digits[5:]}", f"+91-{digits}", f"+91 {digits}", digits))


def _resume_content(c: Candidate, rng: random.Random, quality: float, reg: _Registry) -> ResumeContent:
    """Realistic campus resume for a person; quality (0..1) drives depth, not wording."""
    projects = _make_projects(rng, c, quality, reg)
    years = DEGREE_YEARS[c.degree]
    start_year = c.grad_year - years
    year12 = start_year if years >= 3 else start_year - 4
    ability = _clip(0.25 + quality * 0.75, 0, 1)
    pct12 = round(_clip(rng.gauss(70 + 22 * ability, 6), 55, 98.8), 1)
    pct10 = round(_clip(rng.gauss(73 + 20 * ability, 6), 58, 99.2), 1)
    board = _weighted(rng, BOARDS)
    has_github = bool([p for p in projects if p.repo]) or rng.random() < 0.7
    return ResumeContent(
        name=c.name, email=c.email, phone=_resume_phone(rng, c.phone), city=c.city,
        state=CITY_STATE.get(c.city, ""), linkedin=c.linkedin if rng.random() < 0.85 else None,
        github=f"github.com/{c.github}" if has_github else None, summary=_make_summary(rng, c),
        degree=c.degree, branch=c.branch, college=c.college.name, college_city=c.college.city,
        start_year=start_year, grad_year=c.grad_year, cgpa=c.cgpa, school=rng.choice(SCHOOLS),
        board12=board if board != "ICSE" else "ISC", pct12=pct12, board10=board, pct10=pct10,
        skills=_make_skills(rng, c.track, quality), projects=projects,
        experiences=_make_experiences(rng, c, quality, reg), certifications=_make_certs(rng, c.track, quality),
        achievements=_make_achievements(rng, quality, reg), date_style=_weighted(rng, ((0, 60), (1, 25), (2, 15))))


def _word_edits(rng: random.Random, bullets: Sequence[str], n_edits: int) -> list[str]:
    """Near-verbatim copy: swap n_edits words (synonyms) somewhere in the bullets."""
    out = list(bullets)
    options = [(i, a, b) for i, text in enumerate(out) for a, b in RING_WORD_EDITS if re.search(rf"\b{a}\b", text)]
    for i, a, b in rng.sample(options, min(n_edits, len(options))):
        out[i] = re.sub(rf"\b{a}\b", b, out[i], count=1)
    return out


def _apply_ring(rc: ResumeContent, rng: random.Random, member_index: int, source_handle: str) -> None:
    copied = []
    for j, (title, tech, bullets) in enumerate(RING_PROJECTS):
        text = list(bullets) if member_index == 0 else _word_edits(rng, bullets, rng.randint(1, 2))
        repo = f"github.com/{source_handle}/{RING_REPO_SLUG}" if j == 0 else None
        copied.append(Project(title, list(tech), text, repo))
    rc.projects = copied + rc.projects[:rng.randint(0, 1)]


def _apply_mill(rc: ResumeContent, rng: random.Random, member_index: int) -> None:
    """Coaching-institute template: same titles, repos and three verbatim bullets, plus one short
    bullet of the student's own per project (distinct across members)."""
    own = []
    for key, step in (("loan_default", 5), ("banking_microservices", 7)):
        combos = [(v, o) for v in PROJECT_VERBS for o in TEMPLATE_BY_KEY[key].objects]
        verb, obj = combos[(step * member_index + 1) % len(combos)]
        own.append(f"{verb} {obj}.".format(**_metrics(rng)))
    own_a, own_b = own
    (title_a, tech_a, slug_a), (title_b, tech_b, slug_b) = MILL_PROJECTS
    mill = [Project(title_a, list(tech_a), [MILL_SHARED_BULLETS[0], MILL_SHARED_BULLETS[1], own_a],
                    f"github.com/{MILL_HANDLE}/{slug_a}"),
            Project(title_b, list(tech_b), [MILL_SHARED_BULLETS[2], own_b], f"github.com/{MILL_HANDLE}/{slug_b}")]
    rc.projects = mill + (rc.projects[:1] if rng.random() < 0.15 else [])


def _apply_boilerplate(rc: ResumeContent, rng: random.Random) -> None:
    targets: list[Any] = rc.projects + rc.experiences
    for bullet in rng.sample(BOILERPLATE_BULLETS, rng.randint(2, 3)):
        rng.choice(targets).bullets.append(bullet)


def _make_v2(rc: ResumeContent, rng: random.Random, track: str) -> ResumeContent:
    """A later version of the same resume: one more certification and a refreshed summary."""
    v2 = copy.deepcopy(rc)
    have = " ".join(v2.certifications)
    pool = [n for n, tracks in CERTIFICATIONS if (track in tracks or "*" in tracks) and n not in have]
    if pool:
        v2.certifications.append(f"{rng.choice(pool)} (2026)")
    v2.summary = v2.summary.replace("Seeking", "Actively seeking", 1)
    return v2


# ---------------------------------------------------------------------------
# Resume layout -> abstract lines
# ---------------------------------------------------------------------------
Line = tuple[str, ...]   # ("name"|"contact"|"heading"|"entry"|"text"|"bullet", text) or ("inline"|"inline_heading", label, text)


def _header_lines(rc: ResumeContent, layout: str, rng: random.Random) -> list[Line]:
    links = [x for x in (rc.linkedin, rc.github) if x]
    if layout == "classic":
        lines: list[Line] = [("name", rc.name.upper() if rng.random() < 0.5 else rc.name),
                             ("contact", f"{rc.city}, {rc.state} | {rc.phone} | {rc.email}")]
        if links:
            lines.append(("contact", " | ".join(links)))
        return lines
    if layout == "modern":
        lines = [("name", rc.name), ("contact", f"Email: {rc.email} | Phone: {rc.phone} | Location: {rc.city}")]
        parts = ([f"LinkedIn: https://www.{rc.linkedin}"] if rc.linkedin else []) + \
                ([f"GitHub: https://{rc.github}"] if rc.github else [])
        if parts:
            lines.append(("contact", " | ".join(parts)))
        return lines
    lines = [("name", rc.name), ("contact", f"{rc.email} | {rc.phone} | {rc.city}")]
    if links:
        lines.append(("contact", " | ".join(reversed(links))))
    return lines


def _skill_groups(skills: Sequence[str]) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for s in skills:
        label = next((g for g, members in SKILL_GROUPS if s in members), "Tools and Platforms")
        groups.setdefault(label, []).append(s)
    order = [g for g, _ in SKILL_GROUPS] + ["Tools and Platforms"]
    return [(g, groups[g]) for g in order if g in groups]


def _education_lines(rc: ResumeContent, layout: str) -> list[Line]:
    years = DEGREE_YEARS.get(rc.degree, 4)
    year12 = rc.start_year if years >= 3 else rc.start_year - 4
    if layout == "classic":
        return [("entry", f"{rc.degree} in {rc.branch} - {rc.college}, {rc.college_city}"),
                ("text", f"{rc.start_year} - {rc.grad_year} | CGPA: {rc.cgpa:.2f}/10"),
                ("text", f"Class XII ({rc.board12}) - {rc.school}, {year12} | {rc.pct12}%"),
                ("text", f"Class X ({rc.board10}) - {year12 - 2} | {rc.pct10}%")]
    if layout == "modern":
        return [("entry", f"{rc.college}, {rc.college_city}"),
                ("text", f"{DEGREE_LONG[rc.degree]}, {rc.branch} | Aug {rc.start_year} - May {rc.grad_year} | "
                         f"CGPA {rc.cgpa:.2f}/10"),
                ("text", f"Senior Secondary (XII), {rc.board12}: {rc.pct12}% ({year12})"),
                ("text", f"Secondary (X), {rc.board10}: {rc.pct10}% ({year12 - 2})")]
    return [("text", f"{rc.degree} {rc.branch}, {rc.college} ({rc.start_year}-{rc.grad_year}), "
                     f"CGPA {rc.cgpa:.1f}/10"),
            ("text", f"12th: {rc.pct12}% ({rc.board12}), 10th: {rc.pct10}% ({rc.board10})")]


def _section_lines(rc: ResumeContent, key: str, layout: str, rng: random.Random) -> list[Line]:
    if key == "summary":
        return [("text", rc.summary)]
    if key == "education":
        return _education_lines(rc, layout)
    if key == "skills":
        if layout == "classic":
            lines: list[Line] = [("inline", g, ", ".join(items)) for g, items in _skill_groups(rc.skills)]
            return lines + ([("inline", "Core CS", CORE_CS)] if rng.random() < 0.7 else [])
        return [("text", f"{g}: {', '.join(items)}") for g, items in _skill_groups(rc.skills)]
    if key == "experience":
        lines = []
        for e in rc.experiences:
            dates = _fmt_range(e.start, e.end, rc.date_style)
            if layout == "classic":
                lines.append(("entry", f"{e.role} | {e.company}, {e.location} | {dates}"))
            elif layout == "modern":
                lines += [("entry", f"{e.role}, {e.company}"), ("text", f"{dates} | {e.location}")]
            else:
                lines.append(("entry", f"{e.role} - {e.company} ({dates})"))
            lines += [("bullet", b) for b in e.bullets]
        return lines
    if key == "projects":
        lines = []
        for p in rc.projects:
            tech = ", ".join(p.tech)
            if layout == "classic":
                lines.append(("entry", f"{p.title} | {tech}" + (f" | {p.repo}" if p.repo else "")))
                lines += [("bullet", b) for b in p.bullets]
            elif layout == "modern":
                lines += [("entry", p.title), ("text", f"Technologies: {tech}")]
                lines += [("bullet", b) for b in p.bullets]
                lines += [("text", f"Repository: https://{p.repo}")] if p.repo else []
            else:
                lines.append(("entry", f"{p.title} ({tech})"))
                lines += [("bullet", b) for b in p.bullets]
                lines += [("text", f"GitHub: {p.repo}")] if p.repo else []
        return lines
    items = rc.certifications if key == "certifications" else rc.achievements
    return [("bullet", x) for x in items]


def _layout_lines(rc: ResumeContent, layout: str, rng: random.Random) -> tuple[Line, ...]:
    heads = {key: rng.choice(opts) for key, opts in LAYOUT_HEADINGS[layout].items()}
    lines = _header_lines(rc, layout, rng)
    for key in LAYOUT_ORDER[layout]:
        if layout == "minimal" and key in ("summary", "skills"):
            text = rc.summary if key == "summary" else ", ".join(rc.skills)
            lines.append(("inline_heading", heads[key], text))
            continue
        body = _section_lines(rc, key, layout, rng)
        if body:
            lines.append(("heading", heads[key] + (":" if layout == "minimal" else "")))
            lines.extend(body)
    return tuple(lines)


# ---------------------------------------------------------------------------
# Resume plan (who gets which file)
# ---------------------------------------------------------------------------


def _pick_format(u: float, formats: Sequence[str]) -> str:
    allowed = [f for f in FORMATS if f in formats]
    total = sum(FORMAT_WEIGHTS[f] for f in allowed)
    acc = 0.0
    for f in allowed:
        acc += FORMAT_WEIGHTS[f] / total
        if u < acc:
            return f
    return allowed[-1]


def _pick_naming(u: float) -> str:
    acc = 0.0
    for style, w in NAMING_STYLES:
        acc += w
        if u < acc:
            return style
    return NAMING_STYLES[-1][0]


def _claim_file_name(reg: _Registry, stem: str, ext: str) -> str:
    name, k = f"{stem}{ext}", 2
    while name.lower() in reg.files:
        name, k = f"{stem}_{k}{ext}", k + 1
    reg.files.add(name.lower())
    return name


def _file_stem(c: Candidate, style: str, generic_no: int) -> str:
    if style == "id":
        return f"{c.cid}_{c.first}_{c.last}"
    if style == "name":
        return f"{c.first}_{c.last}_Resume"
    if style == "cv":
        return f"{c.first.lower()}{c.last.lower()}_cv"
    return f"Resume ({generic_no})"


def _resume_file(reg: _Registry, seed: int, c: Candidate, content: ResumeContent, kind: str, style: str,
                 fmt: str, layout: str, glyph_u: float, stem: str) -> ResumeFile:
    glyphs = GLYPHS[(fmt, layout)]
    lines = _layout_lines(content, layout, _rng(seed, "layout", stem))
    return ResumeFile(file_name=_claim_file_name(reg, stem, f".{fmt}"), kind=kind, candidate_id=c.cid,
                      fmt=fmt, layout=layout, naming=style, glyph=glyphs[int(glyph_u * len(glyphs))],
                      content=content, lines=lines)


def _build_resumes(cands: list[Candidate], n: int, seed: int, formats: Sequence[str],
                   reg: _Registry) -> tuple[list[ResumeFile], dict[str, Any]]:
    """Resume files for TA candidates, walk-ins and the special broken files."""
    rng = _rng(seed, "resume-plan")
    has_resume = {c.idx: c.label in ("colluder", "decoy") or rng.random() < RESUME_RATE for c in cands}
    no_resume = [c for c in cands if not has_resume[c.idx]]
    broken_owners = rng.sample([c for c in no_resume if c.label == "normal"], 3)
    plans = []
    for c in cands:
        if not has_resume[c.idx]:
            continue
        style_u, fmt_u, glyph_u = rng.random(), rng.random(), rng.random()
        layout = rng.choice(LAYOUTS)
        boilerplate = c.label == "normal" and rng.random() < BOILERPLATE_RATE
        style = _pick_naming(style_u)
        if style == "generic" and c.email_missing:
            style = "id"
        plans.append((c, style, fmt_u, glyph_u, layout, boilerplate))
    v2_pool = [p for p in plans if p[0].label == "normal" and not p[5] and p[1] != "generic"]
    v2_ids = {p[0].cid for p in rng.sample(v2_pool, N_V2)}

    by_cluster: dict[str, list[Candidate]] = {}
    for c in cands:
        if c.label == "colluder":
            by_cluster.setdefault(c.pattern, []).append(c)
    ring_members = sorted(by_cluster.get(RING, []), key=lambda c: c.idx)
    ring_index = {c.cid: i for i, c in enumerate(ring_members)}
    ring_source = ring_members[0].github if ring_members else ""
    mill_index = {c.cid: i for i, c in enumerate(sorted(by_cluster.get(MILL, []), key=lambda c: c.idx))}

    files: list[ResumeFile] = []
    generic_no = 0
    v2_records = []
    for c, style, fmt_u, glyph_u, layout, boilerplate in plans:
        crng = _rng(seed, "resume", c.cid)
        if c.pattern == MILL:           # template resumes from a coaching institute: thin otherwise
            quality = crng.uniform(0.1, 0.3)
        elif c.label == "colluder":     # 100% on the test, unremarkable resume
            quality = crng.uniform(0.25, 0.7)
        else:
            quality = _clip(0.55 * ((c.pct or 35.0) / 100) + 0.45 * crng.random(), 0, 1)
        content = _resume_content(c, crng, quality, reg)
        if c.pattern == RING:
            _apply_ring(content, crng, ring_index[c.cid], ring_source)
        elif c.pattern == MILL:
            _apply_mill(content, crng, mill_index[c.cid])
        if boilerplate:
            _apply_boilerplate(content, crng)
            c.label, (c.cluster_key, c.pattern) = "decoy", DECOY_BOILERPLATE
        if style == "generic":
            generic_no += 1
        fmt = _pick_format(fmt_u, formats)
        stem = _file_stem(c, style, generic_no)
        files.append(_resume_file(reg, seed, c, content, "primary", style, fmt, layout, glyph_u, stem))
        if c.cid in v2_ids:
            v2 = _make_v2(content, _rng(seed, "resume-v2", c.cid), c.track)
            files.append(_resume_file(reg, seed, c, v2, "v2", style, fmt, layout, glyph_u, f"{stem}_v2"))
            v2_records.append({"candidate_id": c.cid, "primary": files[-2].file_name, "v2": files[-1].file_name})

    broken = []
    for (kind, fmt, style, suffix), c in zip((("corrupt_pdf", "pdf", "id", ""), ("empty_docx", "docx", "name", ""),
                                              ("image_only_pdf", "pdf", "id", "_scan")), broken_owners):
        name = _claim_file_name(reg, _file_stem(c, style, 0) + suffix, f".{fmt}")
        files.append(ResumeFile(name, kind, c.cid, fmt, naming=style, blob_seed=_rng(seed, "blob", c.cid).getrandbits(32)))
        broken.append({"file_name": name, "candidate_id": c.cid, "kind": kind})

    walk_ins = []
    wrng = _rng(seed, "walk-ins")
    for k in range(max(3, round(WALK_INS_FULL * n / DEFAULT_N))):
        college = wrng.choice(OPEN_COLLEGES)
        person = _new_person(wrng, reg, -(k + 1), "", college, wrng.choice(COMPONENTS[:3]))
        style = _weighted(wrng, (("name", 50), ("cv", 30), ("generic", 20)))
        fmt_u, glyph_u, layout = wrng.random(), wrng.random(), wrng.choice(LAYOUTS)
        content = _resume_content(person, _rng(seed, "walk-in-resume", k), wrng.random(), reg)
        if style == "generic":
            generic_no += 1
        stem = _file_stem(person, style, generic_no)
        rf = _resume_file(reg, seed, person, content, "walk_in", style, _pick_format(fmt_u, formats), layout,
                          glyph_u, stem)
        files.append(rf)
        walk_ins.append(rf.file_name)

    issues = {
        "no_resume": sorted(c.cid for c in no_resume),
        "corrupt_or_unreadable_files": broken,
        "v2_resumes": sorted(v2_records, key=lambda r: r["candidate_id"]),
        "walk_in_resumes": sorted(walk_ins),
        "generic_resume_names": sorted(({"file_name": f.file_name, "candidate_id": f.candidate_id}
                                        for f in files if f.naming == "generic" and f.candidate_id),
                                       key=lambda r: r["file_name"]),
    }
    return files, issues


# ---------------------------------------------------------------------------
# Rendering (runs in worker processes; module-level and picklable)
# ---------------------------------------------------------------------------
GLYPH_FONT = "SynthGlyph"
_PDF_CACHE: dict[str, Any] = {}
_ASCII_FALLBACK = {"•": "-", "–": "-", "—": "-", "‣": "-", "▪": "-", "’": "'"}


def _glyph_font() -> str | None:
    """Embedded TTF used only for non-ASCII glyphs (bullets, en dashes) so text extraction
    yields the real characters; standard fonts keep files small."""
    if "glyph" not in _PDF_CACHE:
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            pdfmetrics.registerFont(TTFont(GLYPH_FONT, "Vera.ttf", asciiReadable=False))
            _PDF_CACHE["glyph"] = GLYPH_FONT
        except Exception:   # font missing / unreadable -> ASCII fallbacks keep PDFs parseable
            _PDF_CACHE["glyph"] = None
    return _PDF_CACHE["glyph"]


def _pdf_markup(text: str) -> str:
    font = _glyph_font()
    out = []
    for ch in text:
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ord(ch) < 128:
            out.append(ch)
        elif font:
            out.append(f'<font name="{font}">{ch}</font>')
        else:
            out.append(_ASCII_FALLBACK.get(ch, "?"))
    return "".join(out)


def _pdf_styles(layout: str) -> dict[str, Any]:
    key = f"styles:{layout}"
    if key in _PDF_CACHE:
        return _PDF_CACHE[key]
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle

    if layout == "classic":
        font, bold, size, accent = "Times-Roman", "Times-Bold", 10.3, HexColor("#000000")
    elif layout == "modern":
        font, bold, size, accent = "Helvetica", "Helvetica-Bold", 9.6, HexColor("#1F3A5F")
    else:
        font, bold, size, accent = "Helvetica", "Helvetica-Bold", 9.4, HexColor("#000000")
    lead = size * 1.3
    centered = layout == "classic"
    styles = {
        "name": ParagraphStyle("name", fontName=bold, fontSize=size + (8 if layout != "minimal" else 4),
                               leading=size + 12, textColor=accent, alignment=TA_CENTER if centered else 0),
        "contact": ParagraphStyle("contact", fontName=font, fontSize=size - 0.8, leading=lead - 1,
                                  alignment=TA_CENTER if centered else 0, textColor=HexColor("#333333")),
        "heading": ParagraphStyle("heading", fontName=bold, fontSize=size + (1.2 if layout != "minimal" else 0.4),
                                  leading=lead + 2, spaceBefore=7 if layout != "minimal" else 5, spaceAfter=1,
                                  textColor=accent),
        "entry": ParagraphStyle("entry", fontName=bold, fontSize=size, leading=lead, spaceBefore=3),
        "text": ParagraphStyle("text", fontName=font, fontSize=size, leading=lead),
        "inline_heading": ParagraphStyle("inline_heading", fontName=font, fontSize=size, leading=lead,
                                         spaceBefore=5),
        "bullet": ParagraphStyle("bullet", fontName=font, fontSize=size, leading=lead, leftIndent=12,
                                 firstLineIndent=-8),
    }
    _PDF_CACHE[key] = styles
    return styles


def _render_pdf(job: RenderJob) -> bytes:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate
    from reportlab.platypus.flowables import HRFlowable

    st = _pdf_styles(job.layout)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=14 * mm,
                            bottomMargin=14 * mm, title=f"Resume - {job.author}", author=job.author,
                            subject="Synthetic resume (fictional data)", creator="generate_mock_data.py",
                            invariant=1)
    story: list[Any] = []
    for item in job.lines:
        kind = item[0]
        if kind in ("inline", "inline_heading"):
            style = st["inline_heading"] if kind == "inline_heading" else st["text"]
            story.append(Paragraph(f"<b>{_pdf_markup(item[1])}:</b> {_pdf_markup(item[2])}", style))
        elif kind == "bullet":
            story.append(Paragraph(f"{_pdf_markup(job.glyph or '-')} {_pdf_markup(item[1])}", st["bullet"]))
        else:
            story.append(Paragraph(_pdf_markup(item[1]), st[kind]))
            if kind == "heading" and job.layout == "classic":
                story.append(HRFlowable(width="100%", thickness=0.6, color=HexColor("#444444"),
                                        spaceBefore=1, spaceAfter=3))
    doc.build(story)
    return buf.getvalue()


def _render_image_pdf(job: RenderJob) -> bytes:
    """A 'scanned' resume: only shapes, no text layer (needs OCR)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    rng = random.Random(job.blob_seed)
    buf = io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=A4, invariant=1)
    w, h = A4
    cv.setFillColorRGB(0.95, 0.94, 0.92)
    cv.rect(0, 0, w, h, fill=1, stroke=0)
    cv.setFillColorRGB(0.15, 0.15, 0.15)
    cv.rect(56, h - 92, 230, 20, fill=1, stroke=0)
    cv.circle(w - 96, h - 84, 34, fill=0, stroke=1)
    y = h - 140
    while y > 70:
        if rng.random() < 0.15:
            cv.setFillColorRGB(0.25, 0.25, 0.25)
            cv.rect(56, y, 140, 9, fill=1, stroke=0)
            y -= 22
            continue
        cv.setFillColorRGB(0.5, 0.5, 0.5)
        cv.rect(66, y, rng.randint(180, 470), 5, fill=1, stroke=0)
        y -= 13
    cv.showPage()
    cv.save()
    return buf.getvalue()


def _normalise_zip(data: bytes) -> bytes:
    """Rewrite a zip container with fixed timestamps so DOCX bytes are reproducible."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            zi = zipfile.ZipInfo(info.filename, date_time=FIXED_ZIP_TIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(zi, src.read(info.filename))
    return out.getvalue()


def _render_docx(job: RenderJob) -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    d = docx.Document()
    centered = job.layout == "classic"
    for item in job.lines:
        kind = item[0]
        if kind == "name":
            if job.layout == "minimal":
                p = d.add_paragraph()
                run = p.add_run(item[1])
                run.bold, run.font.size = True, Pt(16)
            else:
                p = d.add_heading(item[1], 0)
            if centered:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif kind == "contact":
            p = d.add_paragraph(item[1])
            if centered:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif kind == "heading":
            if job.layout == "minimal":
                d.add_paragraph().add_run(item[1]).bold = True
            else:
                d.add_heading(item[1], 1 if job.layout == "classic" else 2)
        elif kind in ("inline", "inline_heading"):
            p = d.add_paragraph()
            p.add_run(f"{item[1]}: ").bold = True
            p.add_run(item[2])
        elif kind == "entry":
            d.add_paragraph().add_run(item[1]).bold = True
        elif kind == "bullet":
            if job.glyph:
                d.add_paragraph(f"{job.glyph} {item[1]}")
            else:
                d.add_paragraph(item[1], style="List Bullet")
        else:
            d.add_paragraph(item[1])
    props = d.core_properties
    props.author = props.last_modified_by = job.author
    props.title = f"Resume - {job.author}"
    props.comments = "Synthetic resume (fictional data)"
    props.created = props.modified = datetime(2026, 10, 15, 9, 0, 0)
    props.revision = 1
    buf = io.BytesIO()
    d.save(buf)
    return _normalise_zip(buf.getvalue())


def _render_txt(job: RenderJob) -> bytes:
    out: list[str] = []
    for item in job.lines:
        kind = item[0]
        if kind == "heading":
            out += ["", item[1]]
            if job.layout != "minimal":
                out.append(("=" if job.layout == "classic" else "-") * len(item[1]))
        elif kind == "inline_heading":
            out += ["", f"{item[1]}: {item[2]}"]
        elif kind == "inline":
            out.append(f"{item[1]}: {item[2]}")
        elif kind == "bullet":
            out.append(f"{job.glyph or '-'} {item[1]}")
        else:
            out.append(item[1])
    return ("\n".join(out) + "\n").encode("utf-8")


def _render(job: RenderJob) -> bytes:
    if job.kind == "pdf":
        return _render_pdf(job)
    if job.kind == "docx":
        return _render_docx(job)
    if job.kind == "txt":
        return _render_txt(job)
    if job.kind == "image_only_pdf":
        return _render_image_pdf(job)
    if job.kind == "corrupt_pdf":
        rng = random.Random(job.blob_seed)
        return rng.randbytes(rng.randint(4096, 9000))
    if job.kind == "empty_docx":
        return b""
    raise ValueError(f"unknown render kind {job.kind!r}")


def _render_batch(jobs: Sequence[RenderJob], out_dir: str) -> list[tuple[str, int]]:
    """Render and write a batch of resumes; returns (file name, bytes written)."""
    written = []
    for job in jobs:
        data = _render(job)
        Path(out_dir, job.file_name).write_bytes(data)
        written.append((job.file_name, len(data)))
    return written


def _render_all(jobs: list[RenderJob], out_dir: Path, workers: int) -> list[tuple[str, int]]:
    if workers <= 1 or len(jobs) < PARALLEL_MIN_JOBS:
        return _render_batch(jobs, str(out_dir))
    chunks = [jobs[i:i + RENDER_CHUNK] for i in range(0, len(jobs), RENDER_CHUNK)]
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results: list[tuple[str, int]] = []
            for part in pool.map(_render_batch, chunks, [str(out_dir)] * len(chunks)):
                results.extend(part)
            return results
    except (BrokenProcessPool, OSError, RuntimeError) as exc:   # e.g. restricted sandboxes
        print(f"warning: process pool unavailable ({type(exc).__name__}); rendering serially", file=sys.stderr)
        return _render_batch(jobs, str(out_dir))


def _render_jobs(resumes: Sequence[ResumeFile]) -> list[RenderJob]:
    jobs = []
    for f in resumes:
        kind = f.kind if f.content is None else f.fmt
        author = f.content.name if f.content else ""
        jobs.append(RenderJob(f.file_name, kind, f.layout, f.glyph, author, f.lines, f.blob_seed))
    return jobs


# ---------------------------------------------------------------------------
# Dataset assembly
# ---------------------------------------------------------------------------


def _decoy_groups(cands: list[Candidate], resumes: Sequence[ResumeFile]) -> list[dict[str, Any]]:
    boiler = sorted(c.cid for c in cands if c.cluster_key == DECOY_BOILERPLATE[0])
    holders = [c for c in cands if c.attempted and c.hashes[0] == MCQ_KEY_HASH]
    n_resumes = sum(1 for f in resumes if f.content is not None)
    return [
        {"key": DECOY_BOILERPLATE[0], "pattern": DECOY_BOILERPLATE[1], "label": "decoy", "size": len(boiler),
         "members": boiler, "expected_min_level": "None", "must_not_exceed": "Medium",
         "description": ("Honest candidates whose resumes reuse generic boilerplate bullets (template phrases "
                         f"found in about {len(boiler) / max(1, n_resumes):.0%} of all resumes); boilerplate must "
                         "be filtered out of shared-bullet evidence."),
         "expected_signals": [], "details": {"bullets": list(BOILERPLATE_BULLETS)}},
        {"key": DECOY_MCQ[0], "pattern": DECOY_MCQ[1], "label": "decoy", "size": len(holders),
         "members": sorted(c.cid for c in holders), "expected_min_level": "None", "must_not_exceed": "Medium",
         "description": ("Every candidate with a fully correct MCQ section (Q1) has the same Q1 answer hash - "
                         "an answer key shared by all perfect scorers, not evidence of collusion. Members overlap "
                         "with other groups; labels.csv does not mark them as decoys."),
         "expected_signals": [],
         "details": {"q1_hash": MCQ_KEY_HASH, "holders": len(holders),
                     "focus_holders": sum(1 for c in holders if c.in_focus)}},
    ]


def _validate(ds: Dataset) -> None:
    """Cheap invariants; a failure is a generator bug, never a data problem."""
    problems = []
    domains = set(RESERVED_EMAIL_DOMAINS)
    for c in ds.candidates:
        if c.email.rsplit("@", 1)[-1] not in domains:
            problems.append(f"{c.cid}: non-reserved e-mail domain")
        if c.label == "colluder" and not c.is_perfect:
            problems.append(f"{c.cid}: planted member is not a 100% scorer")
        if c.label != "colluder" and c.attempted and c.in_focus and (c.max_sim or 0) >= 70:
            problems.append(f"{c.cid}: honest focus candidate with high similarity")
    if len({c.cid for c in ds.candidates}) != ds.n:
        problems.append("candidate ids are not unique")
    if any(len(r) != len(TA_COLUMNS) for r in ds.ta_rows):
        problems.append("TA row width mismatch")
    names = [f.file_name.lower() for f in ds.resumes]
    if len(names) != len(set(names)):
        problems.append("duplicate resume file names")
    if problems:
        raise RuntimeError("generator invariant failed: " + "; ".join(problems[:10]))


def build_dataset(n: int = DEFAULT_N, seed: int = DEFAULT_SEED, formats: Sequence[str] = FORMATS) -> Dataset:
    """Build the whole synthetic dataset in memory (deterministic for a given n / seed)."""
    if not MIN_N <= n <= MAX_N:
        raise ValueError(f"n must be between {MIN_N} and {MAX_N} (planted patterns need room)")
    formats = tuple(f for f in FORMATS if f in formats) or FORMATS
    reg = _Registry()
    plan = _make_plan(n, seed, reg)
    cands = _generate_candidates(n, seed, reg, plan)
    clusters = _plant_patterns(cands, plan, seed, reg)
    core_issues = _choose_core_injections(cands, seed)
    _cleanup_honest(cands, seed, RiskConfig())
    _assign_test_candidate_ids(cands, seed)
    presentation, stale = _inject_presentation_issues(cands, seed)
    rows, duplicates = _ta_rows(cands, stale, seed)
    resumes, resume_issues = _build_resumes(cands, n, seed, formats, reg)
    clusters += _decoy_groups(cands, resumes)
    issues = {
        "duplicate_rows": duplicates,
        "missing_emails": core_issues["missing_emails"],
        "scores_above_max": core_issues["scores_above_max"],
        "blank_scores": [{"candidate_id": c.cid, "status": c.status} for c in cands if not c.attempted],
        **presentation,
        **resume_issues,
        "mcq_answer_key_hash": MCQ_KEY_HASH,
    }
    ds = Dataset(n, seed, formats, cands, rows, clusters, issues, resumes)
    _validate(ds)
    return ds


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------
JOB_DESCRIPTION = """\
Software Engineering Program - Campus Hire, Batch of 2027 (Technology)
Location: Bengaluru / Hyderabad / Pune (hybrid)

About the role
Our bank's Technology division is hiring graduating engineers into a two-year Software
Engineering Program. You will rotate across teams that build digital banking, payments,
lending and risk platforms used by millions of customers.

What you will do
- Design, build and test backend services and REST APIs in Python or Java (Spring Boot,
  Flask / FastAPI) on top of SQL databases (PostgreSQL, Oracle, MySQL).
- Build data pipelines and analytics on cloud platforms (AWS, Azure or GCP) using
  containers (Docker, Kubernetes) and CI/CD.
- Apply machine learning (PyTorch, scikit-learn) to fraud detection, credit risk and
  customer analytics, working with data scientists to take models to production.
- Write secure, well-tested code, take part in code reviews and work in agile teams.

What we look for
- B.Tech / B.E. / M.Tech / MCA (2027 batch) in Computer Science, IT, Electronics or a
  related discipline.
- Strong data structures, algorithms and problem-solving skills.
- Programming skill in Python and/or Java and a solid grasp of SQL.
- Hands-on projects or internships with measurable outcomes; exposure to cloud
  (AWS / Azure / GCP) and ML frameworks (PyTorch / TensorFlow) is a plus.
- Fundamentals: OOP, DBMS, operating systems and computer networks.

Selection process
Online assessment (HackerRank, 90 minutes: MCQ + 3 coding problems) -> technical
interviews -> HR conversation. Shortlisted candidates may be asked to walk through their
projects and solve a problem live.

(Synthetic job description for a demo dataset.)
"""

COLUMN_DOCS = (
    ("Candidate ID", "TA identifier CAND-00001..; 8 IDs appear twice (stale re-export of the same attempt)."),
    ("Test Candidate ID", "HackerRank numeric candidate id (increases with invite date)."),
    ("Full Name / Email / Phone Number", "Fictional; e-mails only on example.com/.org/.net and "
                                         "mail.example.com; phones +91 9xxxxxxxxx in mixed formats."),
    ("College / College Tier / City", "Invented colleges (tiers 1-3) in real Indian cities; a few college "
                                      "names differ only by case or extra spaces."),
    ("Degree / Branch / Year of Passing / CGPA", "CGPA on a 10-point scale; a few rows give a percentage "
                                                 "instead (e.g. 78.5)."),
    ("Test Name", "Set A (max 100, ~90% of rows) or Set B (max 150)."),
    ("Status", "Completed, or Expired / Did Not Attempt with blank scores and times."),
    ("Invited On / Invited By", "Invite date and the TA mailbox that sent it."),
    ("Attempt Start Time / Attempt End Time", "Local time, 1-3 Nov 2026, 09:00-23:00; ~5% of rows use "
                                              "'01-Nov-2026 10:32:15' or ISO '2026-11-01T10:32:15'."),
    ("Time Taken (mins)", "Minutes (90-minute limit); a few rows use HH:MM:SS."),
    ("Score / Max Score / Percentage Score", "Score equals the sum of the per-question scores (two rows "
                                             "deliberately exceed Max Score)."),
    ("Q1..Q4 Score", "Q1 MCQ - CS Fundamentals, Q2 Coding - Arrays, Q3 Coding - Dynamic Programming, "
                     "Q4 Coding - Graphs (Set A 20/20/30/30, Set B 30/30/45/45)."),
    ("Q2..Q4 Code Similarity % / Max Code Similarity %", "HackerRank plagiarism-engine similarity to the "
                                                         "closest other submission."),
    ("Most Similar Candidate", "E-mail of the most similar other candidate when max similarity >= 70."),
    ("Plagiarism Status", "Suspicious / Not Suspicious (the engine also produces false flags)."),
    ("Out of Window Events / Duration (sec)", "Tab or window switches and total time away."),
    ("Editor Paste Count / Secure Mode Exit Count", "Proctoring telemetry."),
    ("IP Address", "Public-looking IPv4; on-campus candidates share their lab's NAT IP."),
    ("Integrity Status / Proctoring Flags", "AI-proctoring verdict (Clear / Review / Suspicious) and notes."),
    ("Test Mode / Source", "Remote or On-campus; Campus Drive / Referral / Job Portal / Hackathon."),
    ("Q1 Answer Hash / Q2..Q4 Code Hash", "TA-derived sha1[:12] of each submission (blank = no submission). "
                                          "Every fully correct MCQ attempt has the same Q1 hash."),
    ("Report URL", "https://reports.example.com/hackerrank/tests/<test_id>/candidates/<test_candidate_id>."),
)


def _readme(ds: Dataset, counts: dict[str, Any]) -> str:
    cols = "\n".join(f"| {name} | {doc} |" for name, doc in COLUMN_DOCS)
    fmt_counts = ", ".join(f"{k} {v:,}" for k, v in counts["by_kind"].items())
    return f"""# Synthetic campus-hiring dataset

**Everything in this folder is synthetic and fictional.** People are random combinations of
common Indian first and last names, colleges and companies are invented (any resemblance to a
real institution is coincidental), e-mail addresses use only the reserved `example.com`,
`example.org`, `example.net` and `mail.example.com` domains, phone numbers and IP addresses are
random. Do not use it as real candidate data.

Regenerate with `python generate_mock_data.py --n {ds.n} --seed {ds.seed}` (same arguments give
byte-identical output).

## Files

| Path | Contents |
|---|---|
| `ta_feed.csv` | {len(ds.ta_rows):,} rows / {ds.n:,} candidates: a HackerRank-for-Work CSV export after the TA team appended candidate details (UTF-8, comma separated, human headers). |
| `resumes/` | {counts["resume_files"]:,} files ({fmt_counts}). |
| `resumes.zip` | The same files under `resumes/` (absent when generated with `--no-zip`). |
| `job_description.txt` | Campus software-engineering role at a bank (Python / Java / SQL / cloud / ML). |
| `_ground_truth/labels.csv` | One row per candidate: `candidate_id, label (colluder/decoy/normal), cluster_key, pattern, expected_min_level`. |
| `_ground_truth/clusters.json` | Planted groups and decoy groups: members, description, expected signals. |
| `_ground_truth/injected_issues.json` | Every injected data-quality problem, with the affected IDs / files. |
| `_ground_truth/resume_files.csv` | Every resume file and the candidate it belongs to. |

The `_ground_truth/` folder is for evaluating the app; never upload it as input.

## TA feed columns

| Column | Notes |
|---|---|
{cols}

Roughly 35% of attempts score 0-40%, 35% score 40-75%, 22% score 75-99% and about 4% are
legitimate 100% scores; durations follow ~N(70, 12) minutes capped at the 90-minute limit.

## Resumes

About 95% of TA candidates have a resume. Resumes are realistic campus CVs (header with
contact details and LinkedIn / GitHub, summary, education with CGPA and 12th / 10th marks,
skills, 2-4 projects, 0-2 internships with date ranges, certifications, achievements) in three
layouts (classic CAPS headings, modern, minimal with inline "Skills: ..." headings) rendered as
PDF (~60%), DOCX (~30%) and TXT (~10%). File names vary: `CAND-00012_First_Last.pdf`,
`First_Last_Resume.docx`, `firstlast_cv.pdf` and `Resume (12).pdf` (no name or ID - match by the
e-mail inside). Also included: walk-in resumes with no TA record, a few second `_v2` versions,
one random-bytes PDF, one zero-byte DOCX and one image-only (scanned) PDF without a text layer.

## Planted integrity patterns (among 100% scorers)

Each planted group is fully described in `_ground_truth/clusters.json`. Pattern types:

* **Shared-solution ring** - one college, identical code for the coding questions, tightly
  synchronised start and submission times, near-verbatim copied resume projects.
* **Proxy test-taker** - candidates from different colleges testing back-to-back from one
  residential IP with identical answers and short durations.
* **Resume mill** - different colleges in one city whose resumes share template projects,
  bullets and repositories while their test telemetry looks clean.
* **Synchronised hostel group** - one college, neighbouring IPs in a /24, synchronised timing,
  identical answers within sub-groups.
* **Leaked-paper speed run** - candidates from many colleges finishing implausibly fast late at
  night with identical solutions.
* **Identity overlap** - several candidate IDs sharing one phone number.

## Decoys and traps (must not be flagged High)

* Legitimate 100% scorers who tested on campus in one lab session behind a shared NAT IP.
* Resumes that reuse generic boilerplate bullets (about 8% of all resumes).
* The MCQ answer-key hash shared by every fully correct MCQ attempt.
* HackerRank false plagiarism flags (~1%) and high similarity on the easy array question.

## Data-quality problems (all listed in `injected_issues.json`)

Duplicate candidate rows (stale re-export with an earlier end time), missing e-mails, scores
above the maximum, blank scores for Expired / Did Not Attempt, mixed date-time formats,
`HH:MM:SS` durations, CGPA given as a percentage, college names differing only by case or
spacing, candidates without resumes, unreadable resume files and walk-in resumes.
"""


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _rmtree(path: Path) -> None:
    def _retry(func: Any, target: str, _exc: Any) -> None:   # read-only files / sync-client locks
        os.chmod(target, stat.S_IWRITE)
        func(target)

    handler = {"onexc": _retry} if sys.version_info >= (3, 12) else {"onerror": _retry}
    for attempt in range(3):
        try:
            shutil.rmtree(path, **handler)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.5)


def _prepare_out_dir(out: Path) -> None:
    """Create the output folder; only ever clears entries this generator wrote before."""
    if out.exists():
        if not out.is_dir():
            raise FileExistsError(f"{out} exists and is not a directory")
        if any(out.iterdir()) and not (out / MARKER_FILE).exists():
            raise FileExistsError(f"{out} is not empty and was not created by this generator; "
                                  "choose another --out or empty it first")
        for name in GENERATED_ENTRIES:
            target = out / name
            if target.is_dir():
                _rmtree(target)
            elif target.exists():
                target.unlink()
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER_FILE).write_text("Synthetic dataset written by generate_mock_data.py\n", encoding="utf-8")


def _write_zip(resume_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(resume_dir.iterdir(), key=lambda p: p.name):
            info = zipfile.ZipInfo(f"resumes/{path.name}", date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())


def write_dataset(ds: Dataset, out: Path, *, make_zip: bool = True, workers: int = 0) -> dict[str, Any]:
    """Render and write every output file; returns counts for the summary."""
    _prepare_out_dir(out)
    resume_dir, truth_dir = out / "resumes", out / "_ground_truth"
    resume_dir.mkdir()
    truth_dir.mkdir()
    (out / "ta_feed.csv").write_bytes(ds.ta_csv_bytes())

    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
    written = _render_all(_render_jobs(ds.resumes), resume_dir, workers)
    if make_zip:
        _write_zip(resume_dir, out / "resumes.zip")

    by_kind: dict[str, int] = {}
    for f in ds.resumes:
        key = f.fmt if f.content is not None else f.kind
        by_kind[key] = by_kind.get(key, 0) + 1
    counts = {"resume_files": len(written), "resume_bytes": sum(size for _, size in written),
              "by_kind": dict(sorted(by_kind.items()))}

    (out / "job_description.txt").write_text(JOB_DESCRIPTION, encoding="utf-8")
    (out / "README.md").write_text(_readme(ds, counts), encoding="utf-8")
    _write_csv(truth_dir / "labels.csv", LABEL_COLUMNS, [list(r.values()) for r in ds.label_rows()])
    _write_json(truth_dir / "clusters.json", {"generator": "generate_mock_data.py", "seed": ds.seed,
                                              "n_candidates": ds.n, "clusters": ds.clusters})
    _write_json(truth_dir / "injected_issues.json", ds.issues)
    _write_csv(truth_dir / "resume_files.csv",
               ["file_name", "candidate_id", "kind", "format", "layout", "naming"],
               [[f.file_name, f.candidate_id, f.kind, f.fmt, f.layout, f.naming]
                for f in sorted(ds.resumes, key=lambda f: f.file_name)])
    return counts


def generate(n: int = DEFAULT_N, seed: int = DEFAULT_SEED, out: str | Path = DEFAULT_OUT, *,
             formats: Sequence[str] = FORMATS, make_zip: bool = True, workers: int = 0) -> dict[str, Any]:
    """Build and write the dataset; returns a summary dict (counts only, no personal data)."""
    t0 = time.perf_counter()
    ds = build_dataset(n, seed, formats)
    out_path = Path(out)
    counts = write_dataset(ds, out_path, make_zip=make_zip, workers=workers)
    labels = [c.label for c in ds.candidates]
    return {
        "out": str(out_path), "seed": seed, "n_candidates": ds.n, "ta_rows": len(ds.ta_rows),
        "duplicate_rows": len(ds.issues["duplicate_rows"]),
        "no_score_rows": len(ds.issues["blank_scores"]),
        "perfect_scorers": sum(1 for c in ds.candidates if c.is_perfect),
        "score_above_max": len(ds.issues["scores_above_max"]),
        "colluders": labels.count("colluder"), "decoys": labels.count("decoy"),
        "lab_decoys": sum(1 for c in ds.candidates if c.cluster_key == DECOY_LAB[0]),
        "clusters": len(ds.clusters), **counts,
        "zip": make_zip, "elapsed_sec": round(time.perf_counter() - t0, 1),
    }


def _print_summary(s: dict[str, Any]) -> None:
    kinds = " | ".join(f"{k} {v:,}" for k, v in s["by_kind"].items())
    print(f"Synthetic dataset written to {s['out']} (seed {s['seed']}, {s['n_candidates']:,} candidates)")
    print(f"  ta_feed.csv        {s['ta_rows']:,} rows ({s['duplicate_rows']} duplicate re-exports, "
          f"{s['no_score_rows']} without scores)")
    print(f"  perfect scorers    {s['perfect_scorers']:,} (+{s['score_above_max']} rows with score > max); "
          f"planted colluders {s['colluders']}, lab decoys {s['lab_decoys']}")
    print(f"  resumes/           {s['resume_files']:,} files ({s['resume_bytes'] / 1e6:.1f} MB): {kinds}")
    print(f"  ground truth       _ground_truth/ labels.csv, clusters.json ({s['clusters']} groups), "
          "injected_issues.json, resume_files.csv")
    print(f"  also               job_description.txt, README.md{', resumes.zip' if s['zip'] else ''}")
    print(f"  elapsed            {s['elapsed_sec']:.1f} s")


def _parse_formats(text: str) -> tuple[str, ...]:
    parts = [p.strip().lower() for p in text.split(",") if p.strip()]
    bad = [p for p in parts if p not in FORMATS]
    if bad or not parts:
        raise argparse.ArgumentTypeError(f"formats must be a comma list of {', '.join(FORMATS)}")
    return tuple(p for p in FORMATS if p in parts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a synthetic (fictional) campus-hiring dataset.")
    parser.add_argument("--n", type=int, default=None, help=f"number of TA candidates (default {DEFAULT_N})")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="random seed (default 42)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output folder (default data/sample)")
    parser.add_argument("--no-zip", action="store_true", help="skip resumes.zip")
    parser.add_argument("--formats", type=_parse_formats, default=FORMATS,
                        help="resume formats to render, e.g. pdf,docx,txt")
    parser.add_argument("--quick", action="store_true", help=f"small dataset ({QUICK_N} candidates)")
    parser.add_argument("--workers", type=int, default=0, help="render processes (0 = auto)")
    args = parser.parse_args(argv)
    n = args.n if args.n is not None else (QUICK_N if args.quick else DEFAULT_N)
    if not MIN_N <= n <= MAX_N:
        parser.error(f"--n must be between {MIN_N} and {MAX_N}")
    try:
        summary = generate(n, args.seed, args.out, formats=args.formats, make_zip=not args.no_zip,
                           workers=args.workers)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
