from uuid import UUID
from typing import Any
from datetime import timezone, timedelta

from sqlalchemy import select, exists, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession
from xconn_core.exception import ApplicationError

from deskconn import models, schemas, helpers, uris
from deskconn.database.backend import device as device_backend
from deskconn.database.backend import desktop as desktop_backend


async def create_user(db: AsyncSession, data: schemas.UserCreate) -> models.User:
    data.password, salt = helpers.hash_password_and_generate_salt(data.password)
    db_user = models.User(**data.model_dump(), salt=salt)

    await generate_and_save_otp(db, db_user, helpers.OTP_PURPOSE_VERIFY)

    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)

    return db_user


async def update_user(db: AsyncSession, db_user: models.User, data: dict[str, Any]) -> models.User:
    for field, value in data.items():
        if field == "password":
            db_user.password, db_user.salt = helpers.hash_password_and_generate_salt(value)
            continue

        if hasattr(db_user, field):
            setattr(db_user, field, value)

    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)

    return db_user


async def delete_user(db: AsyncSession, db_user: models.User) -> None:
    await desktop_backend.delete_user_desktop_access(db, db_user)
    await desktop_backend.delete_user_desktops(db, db_user)
    await device_backend.delete_user_devices(db, db_user)

    await db.delete(db_user)
    await db.commit()


async def get_user_by_email(db: AsyncSession, email: str) -> models.User | None:
    stmt = select(models.User).where(models.User.email == email)
    result = await db.execute(stmt)

    return result.scalar()


async def verify_user(db: AsyncSession, db_user: models.User) -> None:
    db_user.is_verified = True
    await db.commit()


async def generate_and_save_otp(db: AsyncSession, db_user: models.User, purpose: str) -> models.User:
    now = helpers.utcnow()

    last_sent_at = db_user.otp_last_sent_at
    if last_sent_at is not None:
        if last_sent_at.tzinfo is None:
            last_sent_at = last_sent_at.replace(tzinfo=timezone.utc)

        cooldown_ends_at = last_sent_at + timedelta(seconds=helpers.OTP_COOLDOWN_SECONDS)
        if now < cooldown_ends_at:
            retry_after = int((cooldown_ends_at - now).total_seconds())
            raise ApplicationError(
                uris.ERROR_USER_OTP_COOLDOWN, f"Please wait {retry_after}s before requesting another OTP"
            )

    # ponytail: read-modify-write on otp_send_count is not locked, so two concurrent
    # requests for the same user can both pass the check and exceed the cap by one.
    # Upgrade to `SELECT ... FOR UPDATE` on db_user if that race becomes worth closing.
    window_started_at = db_user.otp_window_started_at
    if window_started_at is not None and window_started_at.tzinfo is None:
        window_started_at = window_started_at.replace(tzinfo=timezone.utc)

    window_ends_at = None
    if window_started_at is not None:
        window_ends_at = window_started_at + timedelta(minutes=helpers.OTP_SEND_WINDOW_MINUTES)
    if window_ends_at is None or now >= window_ends_at:
        db_user.otp_window_started_at = now
        db_user.otp_send_count = 0
    elif (db_user.otp_send_count or 0) >= helpers.OTP_MAX_SENDS_PER_WINDOW:
        retry_after = int((window_ends_at - now).total_seconds())
        raise ApplicationError(
            uris.ERROR_USER_OTP_LIMIT_EXCEEDED, f"Too many OTP requests, try again in {retry_after}s"
        )

    db_user.otp_hash, db_user.otp_expires_at = helpers.generate_and_send_otp(db_user.email)
    db_user.otp_purpose = purpose
    db_user.otp_last_sent_at = now
    db_user.otp_send_count = (db_user.otp_send_count or 0) + 1
    db_user.otp_verify_attempts = 0
    await db.commit()

    return db_user


async def verify_otp(db: AsyncSession, db_user: models.User, code: str, expected_purpose: str) -> None:
    if (db_user.otp_verify_attempts or 0) >= helpers.OTP_MAX_VERIFY_ATTEMPTS:
        raise ApplicationError(uris.ERROR_USER_OTP_TOO_MANY_ATTEMPTS, "Too many incorrect attempts, request a new OTP")

    is_valid = helpers.verify_email_otp(
        db_user.otp_hash, db_user.otp_expires_at, code, db_user.otp_purpose, expected_purpose
    )
    if not is_valid:
        db_user.otp_verify_attempts = await increment_otp_verify_attempts(db, db_user)
        await db.commit()
        raise ApplicationError(uris.ERROR_USER_OTP_INVALID, "OTP invalid or expired")

    db_user.otp_verify_attempts = 0


async def increment_otp_verify_attempts(db: AsyncSession, db_user: models.User) -> int:
    # Atomic increment (not read-modify-write) so concurrent wrong guesses can't
    # undercount each other and let the attempt cap slip.
    stmt = (
        update(models.User)
        .where(models.User.id == db_user.id)
        .values(otp_verify_attempts=models.User.otp_verify_attempts + 1)
        .returning(models.User.otp_verify_attempts)
    )
    result = await db.execute(stmt)

    return result.scalar_one()


async def reset_password(db: AsyncSession, db_user: models.User, new_password: str) -> models.User:
    db_user.password, db_user.salt = helpers.hash_password_and_generate_salt(new_password)
    await db.commit()

    return db_user


async def user_exists(db: AsyncSession, email: str) -> bool:
    stmt = select(exists().where(models.User.email == email))
    result = await db.execute(stmt)

    return bool(result.scalar())


async def get_user_public_keys(db: AsyncSession, user_id: UUID) -> dict[str, list[str]]:
    principal_query = (
        select(models.User.email.label("authid"), models.Principal.public_key.label("public_key"))
        .join(models.User, models.User.id == models.Principal.user_id)
        .where(models.Principal.user_id == user_id)
    )

    device_query = (
        select(models.User.email.label("authid"), models.Device.public_key.label("public_key"))
        .join(models.User, models.User.id == models.Device.user_id)
        .where(models.Device.user_id == user_id)
    )

    desktop_query = select(models.Desktop.authid.label("authid"), models.Desktop.public_key.label("public_key")).where(
        models.Desktop.user_id == user_id
    )

    keys_union = union_all(principal_query, device_query, desktop_query).subquery()

    stmt = select(keys_union.c.authid, keys_union.c.public_key).distinct()
    result = await db.execute(stmt)

    authorized_keys: dict[str, list[str]] = {}

    for authid, public_key in result.all():
        authorized_keys.setdefault(authid, []).append(public_key)

    return authorized_keys
