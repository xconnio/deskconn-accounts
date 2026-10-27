import os
import base64
import secrets
import hashlib
import threading
from typing import Tuple, Any
from datetime import datetime, timezone, timedelta

import resend
from dotenv import load_dotenv
from xconn.exception import ApplicationError
from xconn.async_session import AsyncSession
from wampproto.auth.wampcra import derive_cra_key

from deskconn import uris

load_dotenv()

ITERATIONS = 1000
KEY_LENGTH = 32

# A user's role on the system realm (io.xconn.deskconn), by how they authenticated. On a
# desktop realm they get ROLE_USER_DESKTOP instead.
ROLE_CRA_USER = "user:cra"
ROLE_CRYPTOSIGN_USER = "user:cryptosign"
# A user's role on a desktop realm is user:<their access role> (user:owner, user:admin or
# user:member). The router uses it to keep app management to owners and admins.
ROLE_USER_DESKTOP = "user:{role}"
ROLE_DESKTOP = "xconnio:deskconn:desktop:{authid}"
OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 5
OTP_COOLDOWN_SECONDS = 60
OTP_MAX_SENDS_PER_WINDOW = 3
OTP_SEND_WINDOW_MINUTES = 60
OTP_MAX_VERIFY_ATTEMPTS = 5
OTP_PURPOSE_VERIFY = "verify"
OTP_PURPOSE_LOGIN = "login"
OTP_PURPOSE_PASSWORD_RESET = "password_reset"
EMAIL_FROM = "Deskconn <noreply@deskconn.com>"
# Aliases of the Resend templates defined in deskconn/email_templates.py
EMAIL_TEMPLATE_VERIFICATION_CODE = "deskconn-verification-code"
EMAIL_TEMPLATE_DESKTOP_INVITE = "deskconn-desktop-invite"

CLOUD_REALM = "io.xconn.deskconn"
TOPIC_DESKTOP_DETACH = "io.xconn.deskconn.desktop.{machine_id}.detach"
RPC_KILL_SESSION = "wamp.session.kill_by_authid"
TOPIC_KEY_ADD = "io.xconn.deskconn.desktop.{machine_id}.key.add"
TOPIC_KEY_REMOVE = "io.xconn.deskconn.desktop.{machine_id}.key.remove"

X_DEBUG = os.getenv("X_DEBUG", "false").lower() in ("true", "1", "yes", "on")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", None)
if not X_DEBUG:
    if RESEND_API_KEY is None or RESEND_API_KEY == "":
        raise ValueError("'RESEND_API_KEY' missing in environment variables.")
    resend.api_key = RESEND_API_KEY


def utcnow():
    return datetime.now(timezone.utc)


def hash_password_and_generate_salt(password: str) -> Tuple[str, str]:
    salt = generate_salt()

    return hash_password(password, salt), salt


def hash_password(password: str, salt: str) -> str:
    return derive_cra_key(salt, password, ITERATIONS, KEY_LENGTH).decode()


def verify_password(password: str, salt: str, hashed_password: str) -> bool:
    return hash_password(password, salt) == hashed_password


def generate_salt(length: int = 16) -> str:
    salt_bytes = os.urandom(length)

    return base64.b64encode(salt_bytes).decode("utf-8")


def generate_email_otp() -> str:
    return f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"


def hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def otp_expiry_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)


def generate_and_send_otp(email: str) -> Tuple[str, datetime]:
    otp = generate_email_otp()
    otp_hash = hash_otp(otp)
    otp_expires_at = otp_expiry_time()
    send_user_verification_email(email, otp)

    return otp_hash, otp_expires_at


def verify_email_otp(
    stored_hash: str | None,
    expires_at: datetime | None,
    provided_code: str,
    stored_purpose: str | None,
    expected_purpose: str,
) -> bool:
    if stored_purpose != expected_purpose:
        return False

    if stored_hash is None or expires_at is None:
        return False

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if utcnow() > expires_at:
        return False

    return hash_otp(provided_code) == stored_hash


def send_user_verification_email(user_email: str, code: str) -> None:
    send_template_email(user_email, EMAIL_TEMPLATE_VERIFICATION_CODE, {"CODE": code})


def send_desktop_invite_email(inviter: str, invitee: str, desktop_name: str, role: str) -> None:
    send_template_email(
        invitee, EMAIL_TEMPLATE_DESKTOP_INVITE, {"INVITER": inviter, "DESKTOP_NAME": desktop_name, "ROLE": role}
    )


def send_template_email(to: str, template: str, variables: dict[str, str | int]) -> None:
    params: resend.Emails.SendParams = {
        "from": EMAIL_FROM,
        "to": [to],
        "template": {"id": template, "variables": variables},
    }

    thread = threading.Thread(target=send_email, args=(params,))
    thread.start()


def send_email(params: resend.Emails.SendParams) -> None:
    if X_DEBUG:
        print("-------------------------------------")
        print(f"[X_DEBUG] {params['template']['id']} email to {params['to']}: {params['template']['variables']}")
        print("-------------------------------------")
        return
    try:
        resend.Emails.send(params)
    except Exception as e:
        print("Failed to send email, reason:", e)


async def call_cloud_router_rpc(session: AsyncSession, uri: str, args: list[Any], error_message: str) -> None:
    try:
        await session.call(uri, args)
    except ApplicationError as app_err:
        raise ApplicationError(uris.ERROR_INTERNAL_ERROR, f"{error_message}. Error is: {app_err.args}")
    except Exception as err:
        raise ApplicationError(uris.ERROR_INTERNAL_ERROR, str(err))
