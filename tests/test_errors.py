"""shared.errors 单元测试。"""

import pytest

from shared.errors import (
    CollectorError,
    ConfigError,
    GeneratorError,
    NotifierError,
    ReportError,
    StorageError,
)

ALL_ERRORS = (ConfigError, CollectorError, GeneratorError, NotifierError, StorageError)


def test_all_errors_inherit_report_error():
    for exc in ALL_ERRORS:
        assert issubclass(exc, ReportError)


@pytest.mark.parametrize("exc", ALL_ERRORS)
def test_error_carries_message(exc):
    assert str(exc("测试错误")) == "测试错误"


def test_catch_all_by_base_class():
    with pytest.raises(ReportError):
        raise NotifierError("飞书推送失败")
