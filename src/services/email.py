"""Email service for handling subscriptions and sending summaries."""

import email
import html
import imaplib
import logging
import os
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from email.policy import default
from email.utils import parseaddr
from typing import List

try:
    import markdown
except ImportError:
    markdown = None

from rich.console import Console

from ..ai.markdown_utils import clean_app_summary_markdown
from ..models import EmailConfig

logger = logging.getLogger(__name__)

# The summarizer HTML-escapes untrusted text and then Markdown-escapes it,
# which turns "&#x27;" into "&\#x27;". These helpers undo that once for email.
_ESCAPED_ENTITY_RE = re.compile(r"&\\#(x?[0-9A-Fa-f]+);")
_MARKDOWN_BACKSLASH_RE = re.compile(r"\\([\\`*_{}\[\]()<>#!|+.-])")
_LIST_ITEM = r"[ \t]*(?:\d+[.)]|[-*+])[ \t]"
_LIST_AFTER_TEXT_RE = re.compile(rf"(?m)^((?!{_LIST_ITEM}).*\S.*)\n(?={_LIST_ITEM})")
_LOCAL_SUMMARY_LINK_RE = re.compile(r"\[((?:\\.|[^\]])+)\]\(#item-[^)]+\)")


def _escape_email_text(value: str) -> str:
    """Block raw HTML without escaping the summarizer's entities a second time.

    Escaping < is enough to stop raw HTML. Existing entities such as &#x27;
    are left for Markdown to pass through, and bare & is escaped by Markdown.
    > is left alone so the app's own blockquotes still work; untrusted text
    reaches here as &gt;, so it can't start one. Python-Markdown doesn't treat
    \\| as an escape, so it is unescaped to avoid a stray backslash.
    """
    return value.replace("<", "&lt;").replace("\\|", "|")


def _prepare_email_markdown(value: str) -> str:
    """Make summary Markdown render cleanly, and safely, as email HTML.

    Summary text is already HTML-escaped by the summarizer, so escaping it
    again here showed entities such as &#x27; and &quot; in the email.
    """
    value = _ESCAPED_ENTITY_RE.sub(r"&#\1;", value)
    # Python-Markdown needs a blank line before a list that follows text.
    return _LIST_AFTER_TEXT_RE.sub(r"\1\n\n", _escape_email_text(value))


if markdown is not None:
    class _EmailCodeInlineProcessor(markdown.inlinepatterns.BacktickInlineProcessor):
        """Decode summary text only after Markdown identifies a real code span."""

        def handleMatch(self, match, data):
            element, start, end = super().handleMatch(match, data)
            if getattr(element, "tag", None) == self.tag:
                code_start, code_end = self.find_code_spans(match.start(0), data)
                if code_end is None:
                    return element, start, end
                code = html.unescape(
                    _MARKDOWN_BACKSLASH_RE.sub(
                        r"\1", data[code_start:code_end].strip()
                    )
                )
                element.text = markdown.util.AtomicString(markdown.util.code_escape(code))
            return element, start, end


def _render_email_markdown(value: str) -> str:
    parser = markdown.Markdown()
    parser.inlinePatterns.register(
        _EmailCodeInlineProcessor(markdown.inlinepatterns.BACKTICK_RE), "backtick", 190
    )
    return parser.convert(value)


def _apply_email_inline_styles(markup: str) -> str:
    """Apply inline styles because many mail clients drop document CSS."""
    styles = {
        "h1": "margin:0 0 10px;color:#14243a;font-size:26px;line-height:1.25;font-weight:700",
        "h2": "margin:0;color:#14243a;font-size:18px;line-height:1.35;font-weight:700;overflow-wrap:anywhere;word-break:break-word",
        "h3": "margin:0 0 12px;color:#14243a;font-size:16px;line-height:1.4;font-weight:700;overflow-wrap:anywhere;word-break:break-word",
        "p": "margin:0 0 13px;color:#26354a;font-size:14px;line-height:1.7",
        "a": "color:#006f68;text-decoration:underline",
        "ul": "margin:0 0 14px;padding-left:22px;color:#26354a;font-size:14px;line-height:1.7",
        "ol": "margin:0 0 14px;padding-left:22px;color:#26354a;font-size:14px;line-height:1.7",
        "li": "margin:0 0 6px;padding-left:2px",
        "blockquote": "margin:10px 0 18px;padding:10px 14px;border-left:3px solid #36a7a0;background:#eef7f6;color:#536477;font-size:13px;line-height:1.6",
        "code": "padding:2px 5px;border-radius:4px;background:#edf1f5;color:#23374d;font-family:Consolas,monospace;font-size:13px",
        "pre": "margin:0;padding:14px;border-radius:6px;background:#edf1f5;color:#23374d;white-space:pre-wrap;font-family:Consolas,monospace;font-size:13px;line-height:1.6",
        "details": "margin:12px 0;color:#536477;font-size:13px",
        "summary": "color:#087f8c;font-weight:600",
    }
    for tag, style in styles.items():
        pattern = re.compile(rf"<{tag}(\s[^>]*)?>", re.IGNORECASE)

        def add_style(match, tag=tag, style=style):
            attributes = match.group(1) or ""
            if re.search(r"\bstyle\s*=", attributes, re.IGNORECASE):
                return match.group(0)
            return f'<{tag}{attributes} style="{style}">'

        markup = pattern.sub(add_style, markup)
    return re.sub(
        r"<hr\s*/?>",
        '<div style="height:8px;line-height:8px;font-size:8px">&nbsp;</div>',
        markup,
        flags=re.IGNORECASE,
    )


def _layout_email_blocks(markup: str) -> str:
    """Lay out article cards in two columns, wrapping on narrow screens."""
    parts = re.split(r"(?=<h[23]\b)|(<hr\b[^>]*>)", markup, flags=re.IGNORECASE)
    output = []
    cards = []

    def flush_cards():
        if not cards:
            return
        output.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;table-layout:fixed">')
        for index in range(0, len(cards), 2):
            output.append('<tr>')
            for card in cards[index:index + 2]:
                output.append(
                    '<td class="digest-column" width="50%" valign="top" style="width:50%;padding:0 8px 16px;vertical-align:top">'
                    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                    'style="width:100%;background:#ffffff;border:1px solid #dfe5e7;border-top:3px solid #118579">'
                    '<tr><td style="padding:20px;overflow-wrap:anywhere;word-break:break-word">'
                    + _apply_email_inline_styles(card)
                    + '</td></tr></table></td>'
                )
            if index + 1 == len(cards):
                output.append('<td class="digest-empty" width="50%" style="width:50%"></td>')
            output.append('</tr>')
        output.append('</table>')
        cards.clear()

    for part in parts:
        if not part or not part.strip():
            continue
        if re.match(r"<h3\b", part, re.IGNORECASE):
            cards.append(part)
        elif re.match(r"<hr\b", part, re.IGNORECASE):
            # Dividers between articles do not interrupt the two-column flow.
            continue
        else:
            flush_cards()
            if re.match(r"<h2\b", part, re.IGNORECASE):
                output.append('<div style="margin:28px 8px 16px;padding:0 0 10px;border-bottom:2px solid #192b32">'
                              + _apply_email_inline_styles(part) + '</div>')
            else:
                output.append(_apply_email_inline_styles(part))
    flush_cards()
    return "".join(output)


def _render_email_document(
    content_html: str,
    sender_name: str,
    unsubscribe_keyword: str,
    language: str = "en",
) -> str:
    """Wrap the summary in a responsive, table-based email."""
    sender = html.escape(sender_name, quote=True)
    unsubscribe = html.escape(unsubscribe_keyword, quote=True)
    is_chinese = language == "zh"
    html_language = "zh-CN" if is_chinese else "en"
    sender_label = "发件人：" if is_chinese else "Sent by "
    unsubscribe_message = (
        f"退订请向此邮箱发送新邮件，主题必须为“{unsubscribe}”。"
        if is_chinese
        else f'To unsubscribe, send a new email to this address with the subject “{unsubscribe}”.'
    )
    return f"""<!DOCTYPE html>
<html lang="{html_language}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>@media only screen and (max-width:600px){{.digest-column{{display:block!important;width:100%!important;box-sizing:border-box!important}}.digest-empty{{display:none!important}}}}</style></head>
<body style="margin:0;padding:0;background:#f1f4f7;color:#26354a;font-family:Arial,'Microsoft YaHei',sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f1f4f7" style="width:100%;background:#f1f4f7">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="840" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:840px;border-collapse:separate;border-spacing:0">
<tr><td bgcolor="#14243a" style="padding:20px 24px;background:#14243a;border-top:4px solid #36a7a0;border-radius:10px 10px 0 0">
<div style="color:#ffffff;font-size:13px;letter-spacing:1.5px;font-weight:700">HORIZON · DAILY BRIEFING</div>
</td></tr>
<tr><td bgcolor="#ffffff" style="padding:24px;background:#ffffff;border-radius:0 0 10px 10px">
{content_html}
</td></tr>
<tr><td align="center" style="padding:18px 12px 0;color:#718096;font-size:12px;line-height:1.6">
<div>{sender_label}{sender}</div>
<div>{unsubscribe_message}</div>
</td></tr>
</table>
</td></tr></table>
</body></html>"""


class EmailManager:
    """Manages email subscriptions and sending summaries."""

    def __init__(self, config: EmailConfig, console=None):
        self.config = config
        self.pwd = os.getenv(self.config.password_env)
        self.console = console if console is not None else Console(stderr=True)

        if not self.pwd and self.config.enabled:
            logger.warning(
                f"Environment variable {self.config.password_env} not set. Email features may fail."
            )
            self.console.print(
                f"[yellow]Warning: Environment variable {self.config.password_env} not set. Email features may fail.[/yellow]"
            )

    def check_subscriptions(self, storage_manager):
        """Checks inbox for subscription requests and updates subscriber list."""
        if not self.config.enabled or not self.config.imap_enabled:
            return

        mail = None
        mailbox_selected = False
        try:
            mail = imaplib.IMAP4_SSL(self.config.imap_server, self.config.imap_port)
            mail.login(self.config.email_address, self.pwd)
            status, _ = mail.select("INBOX")
            mailbox_selected = status == "OK"

            keyword = self.config.subscribe_keyword
            search_crit = f'(UNSEEN SUBJECT "{keyword}")'

            status, messages = mail.search(None, search_crit)

            # Find both request types before RFC822 fetches mark overlapping matches as seen.
            unsub_keyword = self.config.unsubscribe_keyword
            search_crit_unsub = f'(UNSEEN SUBJECT "{unsub_keyword}")'
            unsub_status, unsub_messages = mail.search(None, search_crit_unsub)

            if status == "OK" and messages[0]:
                email_ids = messages[0].split()
                subscribers = storage_manager.load_subscribers()

                for e_id in email_ids:
                    _, msg_data = mail.fetch(e_id, "(RFC822)")
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1], policy=default)

                            subject = str(msg.get("Subject") or "").strip()
                            if subject.upper() != keyword.upper():
                                continue

                            sender = msg.get("From")

                            if sender:
                                _, email_addr = parseaddr(sender)
                                if email_addr and "@" in email_addr:
                                    if (
                                        "noreply" in email_addr.lower()
                                        or "no-reply" in email_addr.lower()
                                    ):
                                        continue

                                    if email_addr not in subscribers:
                                        storage_manager.add_subscriber(email_addr)
                                        subscribers = storage_manager.load_subscribers()
                                        self._send_reply(
                                            email_addr,
                                            "Subscribed to Horizon",
                                            "You have been successfully subscribed to Horizon daily summaries.",
                                        )
                                        logger.info(f"Added subscriber: {email_addr}")
                                    else:
                                        logger.info(f"Already subscribed: {email_addr}")

            if unsub_status == "OK" and unsub_messages[0]:
                email_ids = unsub_messages[0].split()
                subscribers = storage_manager.load_subscribers()

                for e_id in email_ids:
                    _, msg_data = mail.fetch(e_id, "(RFC822)")
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1], policy=default)

                            subject = str(msg.get("Subject") or "").strip()
                            if subject.upper() != unsub_keyword.upper():
                                continue

                            sender = msg.get("From")

                            if sender:
                                _, email_addr = parseaddr(sender)
                                if email_addr and "@" in email_addr:
                                    if (
                                        "noreply" in email_addr.lower()
                                        or "no-reply" in email_addr.lower()
                                    ):
                                        continue

                                    if email_addr in subscribers:
                                        storage_manager.remove_subscriber(email_addr)
                                        subscribers = storage_manager.load_subscribers()
                                        self._send_reply(
                                            email_addr,
                                            "Unsubscribed from Horizon",
                                            "You have been successfully unsubscribed from Horizon daily summaries.",
                                        )
                                        logger.info(f"Removed subscriber: {email_addr}")
                                    else:
                                        logger.info(f"Not subscribed: {email_addr}")

        except Exception as e:
            logger.error(f"Error checking subscriptions: {e}")
        finally:
            if mail is not None:
                if mailbox_selected:
                    try:
                        mail.close()
                    except Exception as e:
                        logger.debug(
                            "Could not close IMAP mailbox (%s)", type(e).__name__
                        )
                try:
                    mail.logout()
                except Exception as e:
                    logger.debug(
                        "Could not log out of IMAP connection (%s)", type(e).__name__
                    )

    def _open_smtp(self) -> smtplib.SMTP:
        """Open an implicit TLS or STARTTLS SMTP connection."""
        context = ssl.create_default_context()
        if self.config.smtp_port == 465:
            return smtplib.SMTP_SSL(
                self.config.smtp_server, self.config.smtp_port, context=context
            )

        server = smtplib.SMTP(self.config.smtp_server, self.config.smtp_port)
        try:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        except Exception:
            server.close()
            raise
        return server

    def send_daily_summary(
        self,
        summary_md: str,
        subject: str,
        subscribers: List[str],
        language: str = "en",
    ):
        """Sends the daily summary to all subscribers."""
        if not self.config.enabled or not subscribers:
            return

        cleaned_summary = clean_app_summary_markdown(summary_md)
        # Email clients do not keep the Markdown summary's generated anchor
        # elements, so turn its table-of-contents jumps into plain text.
        cleaned_summary = _LOCAL_SUMMARY_LINK_RE.sub(r"\1", cleaned_summary)
        try:
            safe_summary = _prepare_email_markdown(cleaned_summary)
            if markdown is None:
                raise RuntimeError("Markdown dependency is missing; install the project dependencies")
            rendered_summary = _render_email_markdown(safe_summary)
            email_content = _layout_email_blocks(rendered_summary)
        except Exception as exc:
            logger.error("Could not render HTML summary; email was not sent: %s", exc)
            return

        html_body = _render_email_document(
            email_content,
            self.config.sender_name,
            self.config.unsubscribe_keyword,
            language=language,
        )

        try:
            with self._open_smtp() as server:
                server.login(
                    self.config.smtp_username or self.config.email_address, self.pwd
                )

                for subscriber in subscribers:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = Header(subject, "utf-8")
                    msg["From"] = (
                        f"{self.config.sender_name} <{self.config.email_address}>"
                    )
                    msg["To"] = subscriber

                    text_part = MIMEText(cleaned_summary, "plain", "utf-8")
                    html_part = MIMEText(html_body, "html", "utf-8")

                    msg.attach(text_part)
                    msg.attach(html_part)

                    try:
                        server.send_message(msg)
                        logger.info(f"Sent summary to {subscriber}")
                    except Exception as e:
                        logger.error(f"Failed to send to {subscriber}: {e}")

        except Exception as e:
            logger.error(f"SMTP Error: {e}")

    def _send_reply(self, to_email: str, subject: str, body: str):
        """Helper to send a simple reply."""
        try:
            with self._open_smtp() as server:
                server.login(
                    self.config.smtp_username or self.config.email_address, self.pwd
                )

                msg = MIMEText(body)
                msg["Subject"] = subject
                msg["From"] = f"{self.config.sender_name} <{self.config.email_address}>"
                msg["To"] = to_email

                server.send_message(msg)
        except Exception as e:
            logger.error(f"Failed to send reply to {to_email}: {e}")
