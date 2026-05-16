import logging
import json
import os
import threading
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from .config import Config

logger = logging.getLogger("trader.email")

REPORTS_DIR = "/app/data/llm_reports"


def _send_via_smtp(smtp_host, smtp_port, user, password, from_addr, to_addr, subject, body):
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(user, password)
            msg = MIMEText(body, "plain")
            msg["Subject"] = subject
            msg["From"] = from_addr
            msg["To"] = to_addr
            server.send_message(msg)
        return True
    except Exception as e:
        logger.warning(f"SMTP failed: {e}")
        return False


def _send_via_resend(api_key, to_addr, subject, body):
    try:
        import requests
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": "Trading Bot <onboarding@resend.dev>",
                "to": [to_addr],
                "subject": subject,
                "text": body,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info("Email sent via Resend API")
            return True
        logger.warning(f"Resend API returned {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"Resend API failed: {e}")
        return False


def _send_via_ntfy(topic, title, body, click=None):
    try:
        import requests
        headers = {"Title": title, "Tags": "page_facing_up", "Priority": "4"}
        if click:
            headers["Click"] = click
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info("LLM report delivered via ntfy")
            return True
        logger.warning(f"ntfy returned {resp.status_code}")
        return False
    except Exception as e:
        logger.warning(f"ntfy send failed: {e}")
        return False


def _save_to_file(body, timestamp, prefix="llm_decision"):
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        path = os.path.join(REPORTS_DIR, f"{prefix}_{timestamp}.txt")
        with open(path, "w") as f:
            f.write(body)
        logger.info(f"Report saved to {path}")
        return path
    except Exception as e:
        logger.error(f"Failed to save report: {e}")
        return None


def _truncate_report(body, max_len=3500):
    if len(body) <= max_len:
        return body
    half = max_len // 2 - 50
    return body[:half] + "\n\n[... truncated ...]\n\n" + body[-half:]


class EmailNotifier:
    def __init__(self):
        self.enabled = Config.EMAIL_ENABLED
        self.smtp_host = Config.EMAIL_SMTP_HOST
        self.smtp_port = Config.EMAIL_SMTP_PORT
        self.user = Config.EMAIL_USER
        self.password = Config.EMAIL_PASS
        self.to_addr = Config.EMAIL_TO
        self.from_addr = Config.EMAIL_FROM or Config.EMAIL_USER
        self.email_api_key = Config.EMAIL_API_KEY

    def send_llm_report(self, system_prompt, user_prompt, raw_response, decisions, actions_taken):
        if not self.enabled:
            return

        def _send():
            try:
                body = self._format_report(system_prompt, user_prompt, raw_response, decisions, actions_taken)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                subject = f"LLM Trading Decision - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

                saved_path = _save_to_file(body, timestamp)

                _send_via_ntfy(
                    Config.NOTIFY_NTFY_TOPIC,
                    f"LLM Trading Decision - {datetime.now().strftime('%H:%M')}",
                    _truncate_report(body),
                )

                sent = False
                if self.email_api_key:
                    sent = _send_via_resend(self.email_api_key, self.to_addr, subject, body)
                elif self.smtp_host and self.password:
                    sent = _send_via_smtp(
                        self.smtp_host, self.smtp_port, self.user, self.password,
                        self.from_addr, self.to_addr, subject, body,
                    )

                if sent:
                    logger.info("LLM decision report delivered via email")
                else:
                    logger.info("LLM report sent via ntfy + saved to file")

            except Exception as e:
                logger.error(f"Failed to send LLM report: {e}")

        threading.Thread(target=_send, daemon=True).start()

    def send_weekly_report(self, total_deposits, starting_value, current_value, trade_log, trading_pnl):
        if not self.enabled:
            return

        from .tracker import build_weekly_report

        def _send():
            try:
                report_body, num_reports = build_weekly_report(total_deposits, starting_value)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                subject = f"Weekly Trading Summary - {datetime.now().strftime('%Y-%m-%d')}"

                _save_to_file(report_body, timestamp, prefix="weekly")
                _send_via_ntfy(
                    Config.NOTIFY_NTFY_TOPIC,
                    f"Weekly Summary ({num_reports} LLM reports)",
                    report_body[:3500],
                )

                sent = False
                if self.email_api_key:
                    sent = _send_via_resend(self.email_api_key, self.to_addr, subject, report_body)
                elif self.smtp_host and self.password:
                    sent = _send_via_smtp(
                        self.smtp_host, self.smtp_port, self.user, self.password,
                        self.from_addr, self.to_addr, subject, report_body,
                    )

                if sent:
                    logger.info("Weekly report delivered via email")
                else:
                    logger.info("Weekly report saved to file + ntfy")
            except Exception as e:
                logger.error(f"Failed to send weekly report: {e}")

        threading.Thread(target=_send, daemon=True).start()

    def send_daily_report(self, start_value, end_value, daily_pnl, total_deposits, true_trading_pnl, trades_today, win_count, loss_count, positions, spy_return_pct, apy, spy_apy):
        if not self.enabled:
            return

        from .tracker import build_daily_report

        def _send():
            try:
                today = datetime.now()
                report_body, num_reports = build_daily_report(
                    today, start_value, end_value, daily_pnl, total_deposits, true_trading_pnl,
                    trades_today, win_count, loss_count, positions, spy_return_pct, apy, spy_apy
                )
                timestamp = today.strftime("%Y%m%d_%H%M%S")
                subject = f"Daily Trading Summary - {today.strftime('%Y-%m-%d')}"

                _save_to_file(report_body, timestamp, prefix="daily")
                _send_via_ntfy(
                    Config.NOTIFY_NTFY_TOPIC,
                    f"Daily Summary ({num_reports} LLM cycles)",
                    report_body[:3500],
                )

                sent = False
                if self.email_api_key:
                    sent = _send_via_resend(self.email_api_key, self.to_addr, subject, report_body)
                elif self.smtp_host and self.password:
                    sent = _send_via_smtp(
                        self.smtp_host, self.smtp_port, self.user, self.password,
                        self.from_addr, self.to_addr, subject, report_body,
                    )

                if sent:
                    logger.info("Daily report delivered via email")
                else:
                    logger.info("Daily report saved to file + ntfy")
            except Exception as e:
                logger.error(f"Failed to send daily report: {e}")

        threading.Thread(target=_send, daemon=True).start()

    @staticmethod
    def send_email(to_addr, subject, body, api_key=None, smtp_config=None):
        if api_key:
            return _send_via_resend(api_key, to_addr, subject, body)
        if smtp_config:
            return _send_via_smtp(*smtp_config, to_addr, subject, body)
        return False

    def _format_report(self, system_prompt, user_prompt, raw_response, decisions, actions_taken):
        lines = []
        sep = "=" * 72
        lines.append(sep)
        lines.append(f"LLM TRADING DECISION REPORT - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(sep)
        lines.append("")

        lines.append(f"Outlook: {decisions.get('market_outlook', '?')}")
        lines.append(f"Summary: {decisions.get('summary', '')}")
        lines.append("")

        if decisions.get("decisions"):
            lines.append("DECISIONS:")
            for d in decisions["decisions"]:
                sym = d.get("symbol", "?")
                act = d.get("action", "?")
                qty = d.get("quantity", 0)
                strat = d.get("strategy", "?")
                conf = d.get("confidence", 0)
                reas = d.get("reasoning", "")
                lines.append(f"  {act} {qty} {sym} [{strat}] (conf: {conf})")
                if reas:
                    lines.append(f"    -> {reas}")
        lines.append("")

        if actions_taken:
            lines.append("ACTIONS:")
            for a in actions_taken:
                icon = {"executed": "OK", "rejected": "SKIP", "failed": "FAIL"}.get(a.get("status", ""), "?")
                lines.append(f"  [{icon}] {a.get('action', '?')} {a.get('symbol', '?')} x {a.get('quantity', 0):.2f} @ ${a.get('price', 0):.2f}")
                if a.get("reason"):
                    lines.append(f"    -> {a['reason']}")
        lines.append("")

        lines.append("-" * 40)
        lines.append("FULL LLM PROMPT:")
        lines.append("-" * 40)
        lines.append(system_prompt)
        lines.append("")
        lines.append("USER DATA:")
        lines.append(user_prompt)
        lines.append("")

        lines.append("-" * 40)
        lines.append("RAW LLM RESPONSE:")
        lines.append("-" * 40)
        lines.append(raw_response)
        lines.append("")

        lines.append(sep)
        lines.append("END OF REPORT")
        lines.append(sep)

        return "\n".join(lines)
