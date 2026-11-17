"""Entry point: `python -m app` (and the PyInstaller sidecar). Binds to 127.0.0.1 only."""

from __future__ import annotations

import uvicorn

from app.config import load_config
from app.main import create_app


def main() -> None:
    cfg = load_config()
    app = create_app(cfg)
    config = uvicorn.Config(app, host="127.0.0.1", port=cfg.port, log_config=None, access_log=False,
                            lifespan="on", timeout_graceful_shutdown=3)
    server = uvicorn.Server(config)
    app.state.request_exit = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
