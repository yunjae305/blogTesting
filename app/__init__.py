"""Compatibility package for running the API from the repository root.

The real FastAPI package lives in ``apps/api/app``.  Adding that directory to
this package path lets commands such as ``python -m uvicorn app.main:app`` work
from the repo root without requiring ``--app-dir apps/api``.
"""

from pathlib import Path

_api_app_dir = Path(__file__).resolve().parent.parent / "apps" / "api" / "app"

if _api_app_dir.is_dir():
    __path__.append(str(_api_app_dir))
