#!/usr/bin/env python3
"""Send a helmlog backup report by email.

Invoked by scripts/backup.sh at the end of a run (success or failure). Reads
SMTP credentials from an env file and the report body from a markdown file,
then sends a single multipart/alternative email (plain-text markdown + a
lightly-rendered HTML version so Gmail shows headings/bullets).

Usage:
    backup_report_mail.py --status ok|failed \
        --report /tmp/helmlog-backup-report-<ts>.md \
        --creds ~/.config/helmlog-backup/smtp.env \
        --to weaties@gmail.com \
        [--stderr /tmp/helmlog-backup-stderr-<ts>.log]

Exit codes:
    0  email sent
    2  bad arguments / missing files
    3  SMTP error
    4  creds file missing or incomplete

Stdlib only — runs on any macOS Python 3.
"""

from __future__ import annotations

import argparse
import html
import re
import smtplib
import socket
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file. Strips quotes and ignores comments."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        out[key] = value
    return out


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_BULLET = re.compile(r"^([-*])\s+(.*)$")
_MD_CODE_FENCE = re.compile(r"^```")
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _markdown_to_html(md: str) -> str:
    """Very small markdown renderer — enough for our report layout.

    Not trying to be a full implementation; just handles headings, bullets,
    fenced code blocks, inline code, and bold. Everything else is escaped
    and wrapped in <p>.
    """
    out: list[str] = []
    in_code = False
    in_list = False
    for raw_line in md.splitlines():
        if _MD_CODE_FENCE.match(raw_line):
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append(
                    '<pre style="background:#f4f4f4;border:1px solid #ddd;'
                    'padding:8px;border-radius:4px;font-size:12px;overflow-x:auto">'
                )
                in_code = True
            continue
        if in_code:
            out.append(html.escape(raw_line))
            continue

        heading = _MD_HEADING.match(raw_line)
        if heading:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = len(heading.group(1))
            text = _inline_md(heading.group(2))
            out.append(f"<h{level}>{text}</h{level}>")
            continue

        bullet = _MD_BULLET.match(raw_line)
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline_md(bullet.group(2))}</li>")
            continue

        if in_list:
            out.append("</ul>")
            in_list = False

        if raw_line.strip() == "":
            out.append("")
        else:
            out.append(f"<p>{_inline_md(raw_line)}</p>")

    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def _inline_md(text: str) -> str:
    """Apply inline markdown transforms (bold, code) after escaping."""
    escaped = html.escape(text)
    escaped = _MD_BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _MD_INLINE_CODE.sub(
        r'<code style="background:#f4f4f4;padding:1px 4px;border-radius:3px">\1</code>',
        escaped,
    )
    return escaped


def _build_message(
    creds: dict[str, str],
    to: str,
    subject: str,
    md_body: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = creds["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(md_body)
    msg.add_alternative(
        '<!doctype html><html><body style="font-family:-apple-system,BlinkMacSystemFont,'
        "'Segoe UI',Roboto,sans-serif;max-width:780px;margin:0 auto;padding:12px;"
        'color:#222">' + _markdown_to_html(md_body) + "</body></html>",
        subtype="html",
    )
    return msg


def _send(creds: dict[str, str], msg: EmailMessage) -> None:
    host = creds["SMTP_HOST"]
    port = int(creds["SMTP_PORT"])
    user = creds.get("SMTP_USER", "")
    password = creds.get("SMTP_PASSWORD", "")

    # Gmail and most modern relays use STARTTLS on port 587 or implicit TLS
    # on 465. Pick automatically based on port.
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as srv:
            if user and password:
                srv.login(user, password)
            srv.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as srv:
            srv.ehlo()
            try:
                srv.starttls(context=context)
                srv.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass  # Relay without TLS; fine for localhost / internal MTA
            if user and password:
                srv.login(user, password)
            srv.send_message(msg)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", choices=["ok", "failed"], required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--creds", type=Path, required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument(
        "--stderr",
        type=Path,
        default=None,
        help="Optional file whose contents are appended as a fenced code block "
        "(used to attach full stderr on failure).",
    )
    ap.add_argument(
        "--target",
        default="",
        help="Informational target label used in the subject line.",
    )
    args = ap.parse_args()

    if not args.report.is_file():
        print(f"backup-report-mail: report file missing: {args.report}", file=sys.stderr)
        return 2

    creds = _parse_env_file(args.creds)
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_FROM"]
    missing = [k for k in required if not creds.get(k)]
    if missing:
        print(
            f"backup-report-mail: creds missing {missing} in {args.creds}",
            file=sys.stderr,
        )
        return 4

    md_body = args.report.read_text()
    if args.stderr and args.stderr.is_file():
        stderr_text = args.stderr.read_text()
        if stderr_text.strip():
            md_body += "\n\n## Full stderr\n\n```\n" + stderr_text + "\n```\n"

    host_label = args.target or socket.gethostname()
    if args.status == "ok":
        subject = f"[helmlog backup OK] {host_label}"
    else:
        subject = f"[helmlog backup FAILED] {host_label}"

    # Prepend the status line so it's visible in mail previews
    preview_line = f"Status: {'SUCCESS' if args.status == 'ok' else 'FAILURE'}"
    md_body = f"{preview_line}\n\n{md_body}"

    msg = _build_message(creds, args.to, subject, md_body)

    try:
        _send(creds, msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        print(f"backup-report-mail: SMTP send failed: {e}", file=sys.stderr)
        return 3

    print(f"backup-report-mail: sent {subject!r} → {args.to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
