"""Write the OpenAPI contract to docs/openapi.json (the reference for the JavaScript frontend, with docs/API.md).

    uv run python export_openapi.py
"""

import json
import sys
from pathlib import Path

from app.config import load_config
from app.main import create_app

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
spec = create_app(load_config(token="unused")).openapi()
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(spec['paths'])} paths)")
