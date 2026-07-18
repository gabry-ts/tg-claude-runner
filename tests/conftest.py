"""Shared test setup.

bot.main reads several env vars at import time, so they must be set before
any test module imports it. Everything points at throwaway temp dirs.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="tgcr-tests-"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER", "1")
os.environ.setdefault("DATA_DIR", str(_TMP / "data"))
os.environ.setdefault("WORKSPACE_DIR", str(_TMP / "workspace"))
os.environ.setdefault("CLAUDE_HOME", str(_TMP / "claude-home"))
os.environ.setdefault("TGCR_STATE_DIR", str(_TMP / "state"))
os.environ.pop("OPENAI_API_KEY", None)  # keep the OpenAI client disabled

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
