"""Warden AI DevOps Self-Healing Agent Package."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Robust multi-path environment variable discovery
CANDIDATE_PATHS = [
    Path.cwd() / ".env",
    Path.cwd() / "warden" / ".env",
    Path(__file__).resolve().parent.parent / ".env",
    Path(__file__).resolve().parent / ".env",
    # Defensive fallback in case user pasted keys into .env.example
    Path.cwd() / ".env.example",
    Path(__file__).resolve().parent.parent / ".env.example",
]

for p in CANDIDATE_PATHS:
    if p.exists() and p.is_file():
        load_dotenv(dotenv_path=str(p), override=False)
