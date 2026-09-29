"""统一日志：JSON lines 格式（design.md §6.3）。

- 每行一条 JSON 记录，基础字段：timestamp / level / logger / message
- 通过 logging 的 extra= 关键字可附加业务字段（如数据源名称、采集条数）
- 异常堆栈序列化为 exception 字段
"""

import json
import logging
import sys
from datetime import datetime
from typing import TextIO

# 标准 LogRecord 属性，遍历 __dict__ 时跳过（仅合并 extra= 传入的自定义字段）
_RESERVED_FIELDS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "taskName", "thread", "threadName",
})


class JsonLineFormatter(logging.Formatter):
    """将日志记录格式化为单行 JSON（ensure_ascii=False，中文原样输出）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED_FIELDS and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """配置根日志为 JSON lines 输出（幂等：重复调用不会叠加 handler）。"""
    root = logging.getLogger()
    if any(isinstance(handler.formatter, JsonLineFormatter) for handler in root.handlers):
        return
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonLineFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str | None = None) -> logging.Logger:
    """获取日志器；未指定名称时返回应用根日志器 daily_report。"""
    return logging.getLogger(name or "daily_report")
