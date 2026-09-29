"""shared.logger 单元测试。"""

import io
import json
import logging

from shared.logger import JsonLineFormatter, get_logger, setup_logging


def _capture(method: str, message: str, **extra) -> str:
    """用 StringIO 收集一条日志的 JSON lines 输出，返回原始文本。"""
    logger = get_logger(f"test.logger.{method}")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    kwargs = {"extra": extra} if extra else {}
    getattr(logger, method)(message, **kwargs)
    logger.removeHandler(handler)
    return stream.getvalue()


def test_info_log_is_single_json_line():
    raw = _capture("info", "采集开始")
    lines = raw.strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["level"] == "INFO"
    assert parsed["message"] == "采集开始"
    assert "timestamp" in parsed
    assert parsed["logger"].startswith("test.logger")


def test_multiple_logs_produce_one_json_per_line():
    raw = _capture("info", "第一条") + _capture("info", "第二条")
    parsed = [json.loads(line) for line in raw.strip().splitlines()]
    assert [p["message"] for p in parsed] == ["第一条", "第二条"]


def test_extra_fields_are_merged():
    parsed = json.loads(_capture("info", "github 采集完成", source="github", count=3))
    assert parsed["source"] == "github"
    assert parsed["count"] == 3


def test_error_log_includes_exception_traceback():
    logger = get_logger("test.logger.exception")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("采集失败")
    logger.removeHandler(handler)
    parsed = json.loads(stream.getvalue())
    assert parsed["level"] == "ERROR"
    assert parsed["message"] == "采集失败"
    assert "Traceback" in parsed["exception"]


def test_chinese_message_not_ascii_escaped():
    raw = _capture("info", "中文日志")
    assert "中文日志" in raw
    assert json.loads(raw)["message"] == "中文日志"


def test_setup_logging_is_idempotent():
    root = logging.getLogger()
    try:
        setup_logging()
        setup_logging()
        ours = [h for h in root.handlers if isinstance(h.formatter, JsonLineFormatter)]
        assert len(ours) == 1
    finally:
        for handler in list(root.handlers):
            if isinstance(handler.formatter, JsonLineFormatter):
                root.removeHandler(handler)
