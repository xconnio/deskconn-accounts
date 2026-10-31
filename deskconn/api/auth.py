from xconn_core import Component
from xconn_core.exception import ApplicationError
from sqlalchemy.ext.asyncio import AsyncSession
from xconn_core.types import Depends, Result

from deskconn import schemas, uris, helpers, models
from deskconn.database.database import get_database
from deskconn.database.backend import user as user_backend
from deskconn.database.backend import device as device_backend
from deskconn.database.backend import desktop as desktop_backend
from deskconn.database.backend import principal as principal_backend
from deskconn.api.principal import create_and_notify_principal

component = Component()


@component.register("io.xconn.deskconn.account.cra.verify", response_model=schemas.CRAUser)
async def verify_cra(authid: str, realm: str, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{authid}' not found")

    if not db_user.is_verified:
        raise ApplicationError(uris.ERROR_USER_NOT_VERIFIED, f"User with authid '{authid}' is not verified")

    await validate_user_connect_to_desktop(db, authid, realm, db_user)

    return db_user


@component.register("io.xconn.deskconn.account.login")
async def login(email: str, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, email)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with email '{email}' not found")

    if not db_user.is_verified:
        raise ApplicationError(uris.ERROR_USER_NOT_VERIFIED, f"User with email '{email}' is not verified")

    await user_backend.generate_and_save_otp(db, db_user, helpers.OTP_PURPOSE_LOGIN)


@component.register("io.xconn.deskconn.account.login.verify", response_model=schemas.PrincipalGet)
async def verify_login(rs: schemas.UserVerify, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, rs.email)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with email '{rs.email}' not found")

    await user_backend.verify_otp(db, db_user, rs.code, helpers.OTP_PURPOSE_LOGIN)

    db_user.otp_send_count = 0
    db_user.otp_window_started_at = None
    return await create_and_notify_principal(db, schemas.PrincipalCreate(public_key=rs.public_key), db_user)


@component.register("io.xconn.deskconn.account.cryptosign.verify")
async def verify_cryptosign(authid: str, public_key: str, realm: str, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, authid)
    if db_user is not None:
        if not db_user.is_verified:
            raise ApplicationError(uris.ERROR_USER_NOT_VERIFIED, f"User with authid '{authid}' is not verified")

        if not await principal_backend.user_principal_exists(db, public_key, db_user):
            db_device = await device_backend.get_device_by_public_key(db, public_key, db_user.id)
            if db_device is None:
                raise ApplicationError(
                    uris.ERROR_NOT_FOUND, f"Device/Principal with public key '{public_key}' not found"
                )

        await validate_user_connect_to_desktop(db, authid, realm, db_user)

        authrole = helpers.ROLE_CRYPTOSIGN_USER
        if realm != helpers.CLOUD_REALM:
            authrole = await desktop_access_role(db, realm, db_user)
    else:
        db_desktop = await desktop_backend.get_desktop_by_public_key(db, authid, public_key)
        if db_desktop is None:
            raise ApplicationError(
                uris.ERROR_DEVICE_NOT_FOUND, f"Desktop with authid '{authid}' public key '{public_key}' not found"
            )

        if realm not in (db_desktop.realm, helpers.CLOUD_REALM):
            raise ApplicationError(
                uris.ERROR_AUTHENTICATION_FAILED, f"Desktop is not authorized to access realm '{realm}'"
            )

        authrole = helpers.ROLE_DESKTOP.format(authid=db_desktop.authid)

    return Result(args=[{"authid": authid, "authrole": authrole}])


@component.register("io.xconn.deskconn.desktop.access")
async def desktop_access(authid: str, desktop_authid: str, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{authid}' not found")

    if not db_user.is_verified:
        raise ApplicationError(uris.ERROR_USER_NOT_VERIFIED, f"User with authid '{authid}' is not verified")

    db_desktop = await desktop_backend.get_desktop_by_authid(db, desktop_authid)
    if db_desktop is None:
        raise ApplicationError(uris.ERROR_DEVICE_NOT_FOUND, f"Desktop with authid '{desktop_authid}' not found")

    if not await desktop_backend.has_desktop_access(db, db_desktop.id, db_user.id):
        raise ApplicationError(
            uris.ERROR_USER_NOT_AUTHORIZED, f"User with authid '{authid}' is not authorized to access desktop"
        )

    return None


async def validate_user_connect_to_desktop(db: AsyncSession, authid: str, realm: str, user: models.User):
    if realm == helpers.CLOUD_REALM:
        return

    db_desktop = await desktop_backend.get_desktop_by_realm(db, realm)
    if db_desktop is None:
        raise ApplicationError(uris.ERROR_DEVICE_NOT_FOUND, f"Desktop with realm '{realm}' not found")

    if not await desktop_backend.has_desktop_access(db, db_desktop.id, user.id):
        raise ApplicationError(
            uris.ERROR_USER_NOT_AUTHORIZED, f"User with authid '{authid}' is not authorized to access desktop"
        )


async def desktop_access_role(db: AsyncSession, realm: str, user: models.User) -> str:
    """Returns user:owner, user:admin or user:member: the user's best access role on the desktop."""
    role = models.DesktopAccessRole.member
    for desktop in await desktop_backend.get_user_desktops_with_role(db, user.id):
        if desktop.realm == realm:
            role = desktop.role
    return helpers.ROLE_USER_DESKTOP.format(role=role.value)
