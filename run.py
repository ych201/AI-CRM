"""启动入口：python run.py"""
from __future__ import annotations

from app import create_app
from app.config import get_config

app = create_app()

if __name__ == "__main__":
    cfg = get_config()
    if cfg.flask_env == "development":
        # 单进程多线程：SSE + 后台画像线程在单 worker 下行为可控（SDD R6）
        app.run(host=cfg.host, port=cfg.port, debug=True, threaded=True, use_reloader=False)
    else:
        try:
            from waitress import serve

            serve(app, host=cfg.host, port=cfg.port, threads=8)
        except ImportError:  # pragma: no cover
            app.run(host=cfg.host, port=cfg.port, threaded=True)
