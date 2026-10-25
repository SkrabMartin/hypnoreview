"""Isolated synthetic reviewer for Playwright; no real datasets or production DB."""

import os
import sys
import tempfile
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from test_reviewer_api import seed_recording  # noqa: E402

from apps.api.hypnoreview_api.database import Base, engine_for  # noqa: E402
from apps.api.hypnoreview_api.main import create_app  # noqa: E402

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        engine = engine_for(f"sqlite:///{root / 'e2e.sqlite3'}")
        Base.metadata.create_all(engine)
        seed_recording(engine, root)
        os.environ["HYPNOREVIEW_WEB_ORIGIN"] = "http://127.0.0.1:15173"
        uvicorn.run(create_app(engine), host="127.0.0.1", port=18000)
