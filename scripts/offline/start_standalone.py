"""Start the same app in SQLite + in-process + single-process mode."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("AGENTFORGE_DATABASE_URL", f"sqlite+aiosqlite:///{(ROOT / 'backend' / 'agentforge.db').as_posix()}")
os.environ.setdefault("AGENTFORGE_TASK_BACKEND", "memory")
os.environ.setdefault("AGENTFORGE_CONTROL_BACKEND", "memory")
os.environ.setdefault("AGENTFORGE_VECTOR_BACKEND", "inprocess")
os.environ.setdefault("AGENTFORGE_EMBEDDING_PROVIDER", "hashing")
os.environ.setdefault("AGENTFORGE_EMBEDDING_LOCAL_FILES_ONLY", "true")
os.environ.setdefault("AGENTFORGE_LLM_PROVIDER", "mock")
os.environ.setdefault("AGENTFORGE_SANDBOX_REQUIRED", "false")

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")), workers=1)
