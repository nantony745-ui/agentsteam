import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_codex_plan import wire_client
from app.main import create_app

app = create_app(Path(sys.argv[1]) / "plan-browser.db", wire_client)
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
