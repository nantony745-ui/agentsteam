"""显式开启的脱敏协议诊断，包含活动文件最多三份。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.db.event_codec import compact


class ProtocolDiagnostics:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=2, encoding="utf-8")
        self.logger = logging.Logger("acp.protocol")
        self.logger.addHandler(self.handler)

    def record(self, direction: str, message: dict) -> None:
        self.logger.info("%s %s", direction, compact(message)[:1024 * 1024])

    def close(self) -> None:
        self.handler.close()
