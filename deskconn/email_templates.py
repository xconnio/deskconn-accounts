# ruff: noqa: E501
"""Resend email templates. Sync them to Resend with: python -m deskconn.email_templates"""

import os

import resend

from deskconn.helpers import (
    EMAIL_FROM,
    EMAIL_TEMPLATE_DESKTOP_INVITE,
    EMAIL_TEMPLATE_VERIFICATION_CODE,
)


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:8px;overflow:hidden;">
        <tr><td style="background:#111827;padding:20px 32px;color:#ffffff;font-size:20px;font-weight:600;">Deskconn</td></tr>
        <tr><td style="padding:32px;">
          <h1 style="margin:0 0 16px;font-size:22px;font-weight:600;">{title}</h1>
          {body}
        </td></tr>
        <tr><td style="padding:20px 32px;border-top:1px solid #e5e7eb;font-size:12px;color:#6b7280;">
          You received this email because of activity on your Deskconn account.
          If this wasn't you, you can safely ignore it.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


_P = 'style="margin:0 0 16px;font-size:15px;line-height:1.6;"'

TEMPLATES: list[resend.Templates.CreateParams] = [
    {
        "name": "Deskconn Verification Code",
        "alias": EMAIL_TEMPLATE_VERIFICATION_CODE,
        "from": EMAIL_FROM,
        "subject": "Your Deskconn verification code: {{{CODE}}}",
        "html": _layout(
            "Your verification code",
            f"""<p {_P}>Your Deskconn verification code is:</p>
          <div style="margin:24px 0;padding:16px;background:#f3f4f6;border-radius:6px;text-align:center;
                      font-size:32px;font-weight:700;letter-spacing:8px;font-family:Menlo,Consolas,monospace;-webkit-user-select:all;user-select:all;cursor:pointer;">{{{{{{CODE}}}}}}</div>
          <p {_P}>Never share this code with anyone.</p>""",
        ),
        "text": "Your Deskconn verification code is: {{{CODE}}}\n\nNever share this code with anyone.",
        "variables": [
            {"key": "CODE", "type": "string"},
        ],
    },
    {
        "name": "Deskconn Desktop Invite",
        "alias": EMAIL_TEMPLATE_DESKTOP_INVITE,
        "from": EMAIL_FROM,
        "subject": "{{{INVITER}}} invited you to access a desktop on Deskconn",
        "html": _layout(
            "You've been invited to a desktop",
            f"""<p {_P}><strong>{{{{{{INVITER}}}}}}</strong> has invited you to access the desktop
          <strong>{{{{{{DESKTOP_NAME}}}}}}</strong> as <strong>{{{{{{ROLE}}}}}}</strong>.</p>
          <p {_P}>Open the Deskconn app and sign in with this email address to accept or decline the invitation.</p>""",
        ),
        "text": "{{{INVITER}}} has invited you to access the desktop {{{DESKTOP_NAME}}} as {{{ROLE}}}.\n\n"
        "Open the Deskconn app and sign in with this email address to accept or decline the invitation.",
        "variables": [
            {"key": "INVITER", "type": "string"},
            {"key": "DESKTOP_NAME", "type": "string", "fallback_value": "a desktop"},
            {"key": "ROLE", "type": "string", "fallback_value": "member"},
        ],
    },
]


def sync() -> None:
    """Create or update every template in Resend, then publish it."""
    for template in TEMPLATES:
        try:
            template_id = resend.Templates.get(template["alias"])["id"]
            resend.Templates.update({"id": template_id, **template})
            print(f"updated {template['alias']}")
        except resend.exceptions.ResendError:
            template_id = resend.Templates.create(template)["id"]
            print(f"created {template['alias']}")
        resend.Templates.publish(template_id)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    resend.api_key = os.environ["RESEND_API_KEY"]
    sync()
