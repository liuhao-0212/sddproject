"""邮件推送模块：通过 SMTP 发送 HTML 格式日报（design.md §4.3）。

接口契约: send(report, recipients) -> bool

- 邮件主题包含日期与团队名称；正文为 report.html
- 发送失败重试 2 次，仍失败返回 False + 错误日志（design.md §6.1；
  跨渠道告警由编排层 main.py 负责）
- SMTP 服务器地址/端口读 config.yaml 的 notifier.email 段；
  账号/密码/发件人等密钥经环境变量注入（design.md §6.2）
"""

from __future__ import annotations

import smtplib
import time
from email.message import EmailMessage

from generator import DailyReport
from shared.config import env, load_config
from shared.errors import NotifierError
from shared.logger import get_logger

logger = get_logger("notifier.email")

CONFIG_PATH = "config.yaml"
SEND_TIMEOUT = 30.0    # 单次 SMTP 连接/发送超时（秒）
MAX_RETRIES = 2        # 发送失败最多重试次数（不含首次尝试）
RETRY_INTERVAL = 5.0   # 重试间隔（秒）

# 模块级别名，便于测试中替换，避免影响全局 time 模块
_sleep = time.sleep


def send(report: DailyReport, recipients: list[str]) -> bool:
    """将 HTML 格式日报通过 SMTP 发送给指定收件人。

    成功返回 True；发送失败重试 2 次，仍失败记录错误日志并返回 False。
    """
    if not recipients:
        logger.error("邮件推送失败：收件人为空")
        return False
    try:
        host, port, user, password, sender = _smtp_config()
    except Exception as exc:  # 配置不可用同样降级为推送失败（§6.1）
        logger.error("邮件推送失败：SMTP 配置不可用",
                     extra={"path": CONFIG_PATH, "reason": str(exc)}, exc_info=exc)
        return False
    message = _build_message(report, recipients, sender)

    attempt = 0
    while True:
        try:
            _send_once(host, port, user, password, sender, recipients, message)
            break
        except (smtplib.SMTPException, OSError) as exc:
            if attempt >= MAX_RETRIES:
                logger.error("邮件推送失败",
                             extra={"recipients": recipients, "reason": str(exc)},
                             exc_info=exc)
                return False
            attempt += 1
            logger.warning("邮件发送失败，稍后重试",
                           extra={"recipients": recipients, "attempt": attempt,
                                  "error": str(exc)})
            _sleep(RETRY_INTERVAL)
    logger.info("邮件推送成功", extra={"recipients": recipients,
                                       "team": report.team_name,
                                       "date": report.date.isoformat()})
    return True


def _smtp_config() -> tuple[str, int, str, str, str]:
    """返回 (host, port, user, password, sender)。host/port 读配置，凭据读环境变量。"""
    cfg = load_config(CONFIG_PATH)
    email_cfg = cfg.get("notifier", {}).get("email", {})
    host = str(email_cfg.get("smtp_host") or "")
    port = email_cfg.get("smtp_port")
    if not host or not isinstance(port, int):
        raise NotifierError("notifier.email.smtp_host/smtp_port 未正确配置")
    user = env("SMTP_USER")
    password = env("SMTP_PASSWORD")
    sender = env("SMTP_FROM") or user or "daily-report@localhost"
    return host, int(port), user, password, sender


def _build_message(report: DailyReport, recipients: list[str], sender: str) -> EmailMessage:
    """构造 HTML 邮件：主题含日期与团队名称，正文为 report.html。"""
    msg = EmailMessage()
    msg["Subject"] = f"{report.team_name} 日报（{report.date.isoformat()}）"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(report.html, subtype="html")
    return msg


def _send_once(host: str, port: int, user: str, password: str, sender: str,
               recipients: list[str], message: EmailMessage) -> None:
    """完成一次完整发送：连接 →（TLS/登录）→ sendmail → 断开。失败抛出交由重试层处理。"""
    server = _create_smtp(host, port)
    try:
        if port != 465:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPException as exc:
                logger.warning("SMTP STARTTLS 不可用，继续发送",
                               extra={"host": host, "error": str(exc)})
        if user:
            server.login(user, password)
        server.sendmail(sender, recipients, message.as_string())
    finally:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            pass  # 连接已断开；发送成功时不应因 quit 失败触发重发


def _create_smtp(host: str, port: int) -> smtplib.SMTP:
    """按端口选择连接方式：465 为隐式 SSL，其余为明文连接（随后 STARTTLS）。"""
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=SEND_TIMEOUT)
    return smtplib.SMTP(host, port, timeout=SEND_TIMEOUT)
