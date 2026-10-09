"""Branded system emails (verification, password reset, invitations, …).

Every email has a plain-text and an HTML part. The HTML is hand-built for email
clients: tables for layout, inline styles, a fluid 600px column, a bulletproof
button with a VML fallback for Outlook, dark-mode overrides for clients that
support them (Apple Mail, Outlook.com, iOS), and a hidden preview line.

Anything user-supplied (account names, email addresses) is escaped: an account
name must never be able to inject links or markup into someone else's inbox.
"""

from dataclasses import dataclass
from datetime import datetime
from html import escape
from urllib.parse import urlsplit

from app.config import get_settings
from app.security import utcnow

# Brand tokens (match the studio: zinc greys, blue-600 accent).
INK = "#09090b"
BODY = "#3f3f46"
MUTED = "#71717a"
LINE = "#e4e4e7"
PAGE = "#f4f4f5"
CARD = "#ffffff"
ACCENT = "#2563eb"
ACCENT_2 = "#7c3aed"
SOFT = "#eff6ff"
FONT = "'Inter','Segoe UI',-apple-system,BlinkMacSystemFont,Roboto,Helvetica,Arial,sans-serif"
MONO = "'SFMono-Regular',Menlo,Consolas,'Liberation Mono',monospace"
GRADIENT = f"linear-gradient(135deg,{ACCENT} 0%,{ACCENT_2} 100%)"
HERO_GRADIENT = "linear-gradient(135deg,#0b1020 0%,#1e1b4b 55%,#1d4ed8 140%)"


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


# ---- Emails ----


def verify_email(url: str, ttl_hours: int) -> RenderedEmail:
    expiry = f"{ttl_hours} hours"
    return RenderedEmail(
        subject="Confirm your email for Mailvender",
        text=(
            "Welcome to Mailvender! Confirm your email address to finish creating your account:\n\n"
            f"{url}\n\n"
            f"The link expires in {expiry} and works once.\n"
            "If you didn't sign up, ignore this email and no account will be created.\n"
        ),
        html=_layout(
            preheader="One click and you're in. Confirm your email to start building emails people love.",
            badge="✉️",
            eyebrow="Welcome aboard",
            title="Confirm your email",
            subtitle="You're one click away from your new email studio.",
            body=(
                _p("Thanks for signing up for Mailvender. Confirm that this is your email address and "
                   "we'll set up your workspace right away.")
                + _button("Confirm my email", url)
                + _pill(f"Expires in {expiry} · works once")
                + _fallback_link(url)
            ),
            note="Didn't sign up? Ignore this email. No account is created until the address is confirmed.",
            reason="someone signed up for Mailvender with this address",
        ),
    )


def password_reset(url: str, ttl_minutes: int) -> RenderedEmail:
    expiry = f"{ttl_minutes} minutes"
    return RenderedEmail(
        subject="Reset your Mailvender password",
        text=(
            "Use this link to choose a new password:\n\n"
            f"{url}\n\n"
            f"It expires in {expiry} and works once. If you didn't ask for this, ignore this email; "
            "your password stays the same.\n"
        ),
        html=_layout(
            preheader=f"Choose a new password. This link expires in {expiry}.",
            badge="🔑",
            eyebrow="Account security",
            title="Reset your password",
            subtitle="Let's get you back into your studio.",
            body=(
                _p("We received a request to reset the password for your Mailvender account. "
                   "Choose a new one with the button below.")
                + _button("Choose a new password", url)
                + _pill(f"Expires in {expiry} · works once")
                + _fallback_link(url)
            ),
            note="Didn't ask for this? You can safely ignore this email. Your password stays the same "
                 "and nobody can change it without this link.",
            reason="a password reset was requested for your Mailvender account",
        ),
    )


def password_changed(email: str, when: datetime, reset_url: str) -> RenderedEmail:
    stamp = when.strftime("%d %B %Y, %H:%M UTC")
    return RenderedEmail(
        subject="Your Mailvender password was changed",
        text=(
            f"The password for your Mailvender account ({email}) was changed on {stamp}.\n"
            "For your security, every device that was signed in has been signed out.\n\n"
            "Wasn't you? Reset your password right away:\n"
            f"{reset_url}\n"
        ),
        html=_layout(
            preheader=f"Your password was changed on {stamp}. Wasn't you? Act now.",
            badge="🔒",
            eyebrow="Security alert",
            title="Your password was changed",
            subtitle="Just making sure it was you.",
            body=(
                _p("The password for your Mailvender account was just changed. For your security, every "
                   "device that was signed in has been signed out.")
                + _details([("Account", email), ("When", stamp)])
                + _p("<strong>Wasn't you?</strong> Someone may have access to your email. Reset your "
                     "password now and secure your email account.", raw=True)
                + _button("Secure my account", reset_url)
            ),
            note="If you made this change, there's nothing else to do.",
            reason="it's an important notice about your Mailvender account",
        ),
    )


def already_registered(login_url: str, reset_url: str) -> RenderedEmail:
    return RenderedEmail(
        subject="You already have a Mailvender account",
        text=(
            "Someone (hopefully you) tried to sign up with this address, but it already has an account.\n\n"
            f"Sign in: {login_url}\n"
            f"Forgot your password? {reset_url}\n"
        ),
        html=_layout(
            preheader="Good news: you already have an account. Sign in to pick up where you left off.",
            badge="👋",
            eyebrow="Welcome back",
            title="You already have an account",
            subtitle="No need to sign up twice.",
            body=(
                _p("Someone (hopefully you) tried to sign up with this address, but it already belongs "
                   "to a Mailvender account. Sign in to pick up where you left off.")
                + _button("Sign in", login_url)
                + _p(f'Forgot your password? <a href="{escape(reset_url)}" class="t-link" '
                     f'style="color:{ACCENT};text-decoration:underline;">Reset it here</a>.', raw=True)
            ),
            note="Didn't try to sign up? Ignore this email; nothing about your account has changed.",
            reason="someone tried to sign up for Mailvender with this address",
        ),
    )


def welcome(studio_url: str) -> RenderedEmail:
    steps = [
        ("Design", "Drag and drop sections, widgets and templates in the studio. Every email is "
                   "responsive and checked for inbox quirks before you send."),
        ("Personalise", "Upload a dataset and use @variables to fill each email with your "
                        "recipient's name, order, or anything else."),
        ("Send", "Verify your domain, approve a sender and deliver from your own address, with "
                 "unsubscribes and bounces handled for you."),
    ]
    return RenderedEmail(
        subject="Welcome to Mailvender 🎉",
        text=(
            "Your Mailvender account is ready.\n\n"
            + "".join(f"{i}. {name}: {description}\n" for i, (name, description) in enumerate(steps, 1))
            + f"\nOpen the studio: {studio_url}\n"
        ),
        html=_layout(
            preheader="Your account is ready. Here's how to send your first great email in three steps.",
            badge="🚀",
            eyebrow="You're in",
            title="Welcome to Mailvender",
            subtitle="Your studio is ready. Let's make emails people actually open.",
            body=(
                _p("Your email is confirmed and your workspace is set up. Here's the fastest way from "
                   "a blank canvas to a great email in someone's inbox:")
                + _steps(steps)
                + _button("Open the studio", studio_url)
            ),
            note="Tip: invite your team from Settings → Members so you can design and send together.",
            reason="you created a Mailvender account",
        ),
    )


def invitation(inviter: str, account_name: str, invitee: str, url: str, ttl_days: int) -> RenderedEmail:
    expiry = f"{ttl_days} days"
    return RenderedEmail(
        subject=f"{inviter} invited you to {account_name} on Mailvender",
        text=(
            f"{inviter} invited you to join {account_name} on Mailvender.\n\n"
            f"Accept: {url}\n\n"
            f"Sign in (or sign up) with {invitee} to accept. The link expires in {expiry}.\n"
        ),
        html=_layout(
            preheader=f"Join {account_name} on Mailvender and design emails together.",
            badge="🤝",
            eyebrow="Team invitation",
            title="You're invited",
            subtitle="Your team is waiting for you in the studio.",
            body=(
                _team_card(account_name, inviter)
                + _p(f"<strong>{escape(inviter)}</strong> invited you to join "
                     f"<strong>{escape(account_name)}</strong> on Mailvender, so you can design, review and "
                     "send emails together.", raw=True)
                + _button("Accept invitation", url)
                + _pill(f"Expires in {expiry}")
                + _p(f"Sign in or sign up with <strong>{escape(invitee)}</strong> to accept.", raw=True,
                     small=True)
                + _fallback_link(url)
            ),
            note="Not expecting this? Ignore this email and you won't be added to the team.",
            reason=f"{inviter} invited this address to a Mailvender team",
        ),
    )


# ---- Building blocks ----


def _p(content: str, *, raw: bool = False, small: bool = False) -> str:
    size, line = ("14px", "22px") if small else ("16px", "26px")
    text = content if raw else escape(content)
    return (
        f'<p class="t-body" style="margin:0 0 20px;font-family:{FONT};font-size:{size};line-height:{line};'
        f'color:{BODY};">{text}</p>'
    )


def _button(label: str, url: str) -> str:
    href = escape(url)
    label = escape(label)
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" class="btn-table" style="margin:8px 0 24px;">
  <tr>
    <td align="center" bgcolor="{ACCENT}" style="border-radius:12px;background-color:{ACCENT};background-image:{GRADIENT};">
      <!--[if mso]>
      <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{href}" style="height:52px;v-text-anchor:middle;width:280px;" arcsize="23%" stroke="f" fillcolor="{ACCENT}">
        <w:anchorlock/>
        <center style="color:#ffffff;font-family:Arial,sans-serif;font-size:16px;font-weight:bold;">{label}</center>
      </v:roundrect>
      <![endif]-->
      <!--[if !mso]><!-- -->
      <a href="{href}" target="_blank" class="btn-a" style="display:inline-block;padding:16px 36px;font-family:{FONT};font-size:16px;line-height:20px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:12px;">{label}&nbsp;&rarr;</a>
      <!--<![endif]-->
    </td>
  </tr>
</table>"""


def _pill(text: str) -> str:
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 24px;">
  <tr>
    <td class="bg-soft" style="background-color:{SOFT};border-radius:999px;padding:6px 14px;font-family:{FONT};font-size:13px;line-height:18px;font-weight:600;color:#1d4ed8;">
      &#9201;&nbsp; {escape(text)}
    </td>
  </tr>
</table>"""


def _fallback_link(url: str) -> str:
    href = escape(url)
    return f"""
<p class="t-muted" style="margin:0 0 8px;font-family:{FONT};font-size:13px;line-height:20px;color:{MUTED};">Button not working? Paste this link into your browser:</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 8px;">
  <tr>
    <td class="bg-code b-line" style="background-color:{PAGE};border:1px solid {LINE};border-radius:10px;padding:12px 14px;font-family:{MONO};font-size:12px;line-height:18px;word-break:break-all;">
      <a href="{href}" target="_blank" class="t-link" style="color:{ACCENT};text-decoration:none;word-break:break-all;">{href}</a>
    </td>
  </tr>
</table>"""


def _details(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f"""
  <tr>
    <td class="t-muted b-line" style="padding:12px 16px;border-bottom:1px solid {LINE};font-family:{FONT};font-size:13px;line-height:20px;color:{MUTED};width:90px;">{escape(label)}</td>
    <td class="t-ink b-line" style="padding:12px 16px;border-bottom:1px solid {LINE};font-family:{FONT};font-size:14px;line-height:20px;font-weight:600;color:{INK};overflow-wrap:anywhere;word-break:break-word;">{escape(value)}</td>
  </tr>"""
        for label, value in rows
    )
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bg-code b-line" style="margin:0 0 24px;background-color:{PAGE};border:1px solid {LINE};border-radius:12px;border-collapse:separate;overflow:hidden;">{cells}
</table>"""


def _steps(steps: list[tuple[str, str]]) -> str:
    rows = "".join(
        f"""
  <tr>
    <td valign="top" style="padding:0 16px 20px 0;width:36px;">
      <div style="width:36px;height:36px;line-height:36px;border-radius:12px;background-color:{ACCENT};background-image:{GRADIENT};color:#ffffff;font-family:{FONT};font-size:15px;font-weight:800;text-align:center;">{number}</div>
    </td>
    <td valign="top" style="padding:0 0 20px;">
      <p class="t-ink" style="margin:0 0 4px;font-family:{FONT};font-size:16px;line-height:22px;font-weight:700;color:{INK};">{escape(name)}</p>
      <p class="t-body" style="margin:0;font-family:{FONT};font-size:14px;line-height:22px;color:{BODY};">{escape(description)}</p>
    </td>
  </tr>"""
        for number, (name, description) in enumerate(steps, 1)
    )
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 12px;">{rows}
</table>"""


def _team_card(account_name: str, inviter: str) -> str:
    initial = escape((account_name.strip()[:1] or "M").upper())
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bg-code b-line" style="margin:0 0 24px;background-color:{PAGE};border:1px solid {LINE};border-radius:14px;border-collapse:separate;">
  <tr>
    <td style="padding:16px;width:48px;" valign="middle">
      <div style="width:48px;height:48px;line-height:48px;border-radius:14px;background-color:{ACCENT};background-image:{GRADIENT};color:#ffffff;font-family:{FONT};font-size:22px;font-weight:800;text-align:center;">{initial}</div>
    </td>
    <td style="padding:16px 16px 16px 0;" valign="middle">
      <p class="t-ink" style="margin:0;font-family:{FONT};font-size:17px;line-height:24px;font-weight:700;color:{INK};word-break:break-word;">{escape(account_name)}</p>
      <p class="t-muted" style="margin:0;font-family:{FONT};font-size:13px;line-height:20px;color:{MUTED};word-break:break-all;">Invited by {escape(inviter)}</p>
    </td>
  </tr>
</table>"""


def _layout(*, preheader: str, badge: str, eyebrow: str, title: str, subtitle: str, body: str, note: str,
            reason: str) -> str:
    site = get_settings().app_base_url.rstrip("/")
    site_label = urlsplit(site).netloc or site
    year = utcnow().year
    # Padding after the preview text so clients don't pull body text into the inbox preview.
    spacer = "&#847;&zwnj;&nbsp;" * 60
    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="x-apple-disable-message-reformatting">
<meta name="format-detection" content="telephone=no, date=no, address=no, email=no, url=no">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{escape(title)}</title>
<!--[if mso]>
<noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
<style>* {{ font-family: Arial, sans-serif !important; }}</style>
<![endif]-->
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {{ color-scheme: light dark; supported-color-schemes: light dark; }}
  body {{ margin: 0 !important; padding: 0 !important; width: 100% !important; -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
  table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
  img {{ border: 0; outline: none; text-decoration: none; -ms-interpolation-mode: bicubic; }}
  a[x-apple-data-detectors] {{ color: inherit !important; text-decoration: none !important; }}
  u + #body a {{ color: inherit; text-decoration: none; }}
  @media only screen and (max-width: 620px) {{
    .px {{ padding-left: 24px !important; padding-right: 24px !important; }}
    .hero {{ padding: 36px 24px 32px !important; }}
    .h1 {{ font-size: 28px !important; line-height: 34px !important; }}
    .outer {{ padding: 12px 8px !important; }}
    .btn-table {{ width: 100% !important; }}
    .btn-a {{ display: block !important; text-align: center !important; padding-left: 16px !important; padding-right: 16px !important; }}
  }}
  @media (prefers-color-scheme: dark) {{
    .bg-page {{ background-color: #09090b !important; }}
    .bg-card {{ background-color: #18181b !important; }}
    .bg-code {{ background-color: #27272a !important; }}
    .bg-soft {{ background-color: #1e293b !important; color: #93c5fd !important; }}
    .b-line {{ border-color: #3f3f46 !important; }}
    .t-ink {{ color: #fafafa !important; }}
    .t-body {{ color: #d4d4d8 !important; }}
    .t-muted {{ color: #a1a1aa !important; }}
    .t-link {{ color: #93c5fd !important; }}
  }}
  [data-ogsc] .t-ink {{ color: #fafafa !important; }}
  [data-ogsc] .t-body {{ color: #d4d4d8 !important; }}
  [data-ogsc] .t-muted {{ color: #a1a1aa !important; }}
  [data-ogsc] .t-link {{ color: #93c5fd !important; }}
  [data-ogsb] .bg-page {{ background-color: #09090b !important; }}
  [data-ogsb] .bg-card {{ background-color: #18181b !important; }}
  [data-ogsb] .bg-code {{ background-color: #27272a !important; }}
</style>
</head>
<body id="body" class="bg-page" style="margin:0;padding:0;background-color:{PAGE};">
<div style="display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;mso-hide:all;font-size:1px;line-height:1px;color:{PAGE};">{escape(preheader)}{spacer}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bg-page" style="background-color:{PAGE};">
  <tr>
    <td align="center" class="outer" style="padding:32px 16px;">
      <!--[if mso]><table role="presentation" width="600" align="center" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
      <div style="max-width:600px;margin:0 auto;">

        <!-- Wordmark -->
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td style="padding:4px 8px 20px;">
              <a href="{escape(site)}" target="_blank" style="text-decoration:none;">
                <span style="display:inline-block;width:10px;height:10px;border-radius:5px;background-color:{ACCENT};background-image:{GRADIENT};vertical-align:middle;"></span>
                <span class="t-ink" style="font-family:{FONT};font-size:18px;line-height:24px;font-weight:800;letter-spacing:-0.3px;color:{INK};vertical-align:middle;">&nbsp;Mailvender</span>
              </a>
            </td>
          </tr>
        </table>

        <!-- Card -->
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bg-card" style="background-color:{CARD};border-radius:20px;border-collapse:separate;overflow:hidden;box-shadow:0 1px 3px rgba(9,9,11,0.06),0 12px 32px rgba(9,9,11,0.06);">
          <tr>
            <td class="hero" bgcolor="#0b1020" style="padding:44px 44px 40px;background-color:#0b1020;background-image:{HERO_GRADIENT};border-radius:20px 20px 0 0;">
              <div style="width:56px;height:56px;line-height:56px;border-radius:16px;background-color:rgba(255,255,255,0.12);border:1px solid rgba(255,255,255,0.18);text-align:center;font-size:28px;margin:0 0 24px;">{badge}</div>
              <p style="margin:0 0 10px;font-family:{FONT};font-size:12px;line-height:16px;font-weight:700;letter-spacing:1.6px;text-transform:uppercase;color:#93c5fd;">{escape(eyebrow)}</p>
              <h1 class="h1" style="margin:0 0 12px;font-family:{FONT};font-size:34px;line-height:40px;font-weight:800;letter-spacing:-0.8px;color:#ffffff;">{escape(title)}</h1>
              <p style="margin:0;font-family:{FONT};font-size:16px;line-height:24px;color:#c7d2fe;">{escape(subtitle)}</p>
            </td>
          </tr>
          <tr>
            <td class="px" style="padding:40px 44px 16px;">
{body}
            </td>
          </tr>
          <tr>
            <td class="px" style="padding:0 44px 36px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td class="b-line t-muted" style="border-top:1px solid {LINE};padding-top:20px;font-family:{FONT};font-size:13px;line-height:20px;color:{MUTED};">{escape(note)}</td>
                </tr>
              </table>
            </td>
          </tr>
        </table>

        <!-- Footer -->
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td align="center" style="padding:28px 24px 8px;">
              <p class="t-muted" style="margin:0 0 6px;font-family:{FONT};font-size:12px;line-height:18px;color:{MUTED};">You're receiving this because {escape(reason)}.</p>
              <p class="t-muted" style="margin:0;font-family:{FONT};font-size:12px;line-height:18px;color:{MUTED};">&copy; {year} Mailvender &middot; <a href="{escape(site)}" target="_blank" class="t-link" style="color:{MUTED};text-decoration:underline;">{escape(site_label)}</a></p>
            </td>
          </tr>
        </table>

      </div>
      <!--[if mso]></td></tr></table><![endif]-->
    </td>
  </tr>
</table>
</body>
</html>"""
