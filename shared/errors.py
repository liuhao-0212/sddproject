"""自定义异常体系（design.md §6.1：任何失败都必须有日志记录，异常按层区分）。

所有自定义异常继承 ReportError，main.py 可通过捕获 ReportError 统一处理全局错误。
"""


class ReportError(Exception):
    """所有自定义异常的基类。"""


class ConfigError(ReportError):
    """配置读取或校验失败。"""


class CollectorError(ReportError):
    """采集层错误：外部 API 调用失败、超时、限流等。"""


class GeneratorError(ReportError):
    """生成层错误：日报组织或模板渲染失败。"""


class NotifierError(ReportError):
    """推送层错误：邮件或飞书推送失败。"""


class StorageError(ReportError):
    """存储层错误：SQLite 读写失败。"""
