from xconn_core import Component
from xconn_core.exception import ApplicationError
from sqlalchemy.ext.asyncio import AsyncSession
from xconn_core.types import Depends, CallDetails

from deskconn import schemas, uris, helpers, models
from deskconn.database.database import get_database
from deskconn.database.backend import user as user_backend
from deskconn.database.backend import desktop as desktop_backend
from deskconn.database.backend import principal as principal_backend

component = Component()


async def create_and_notify_principal(
    db: AsyncSession, rs: schemas.PrincipalCreate, db_user: models.User
) -> models.Principal:
    if await principal_backend.principal_exists_by_public_key(db, rs.public_key):
        raise ApplicationError(
            uris.ERROR_PRINCIPAL_EXISTS, f"Principal with public key '{rs.public_key}' already exists"
        )

    principal = await principal_backend.create_principal(db, rs, db_user)

    # publish new keys to desktops
    desktop_authorizations = await desktop_backend.get_user_desktops_authid_with_authrole(db, db_user.id)
    for desktop_authid, authrole in desktop_authorizations:
        await (
            component.session.publish(helpers.TOPIC_KEY_ADD.format(machine_id=desktop_authid))
            .args(db_user.email, principal.public_key, authrole)
            .options(acknowledge=True)
            .execute()
        )

    return principal


@component.register("io.xconn.deskconn.account.principal.list", response_model=schemas.PrincipalGet)
async def list_principal(details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    return await principal_backend.list_principals(db, db_user)


@component.register("io.xconn.deskconn.account.principal.rotate", response_model=schemas.PrincipalGet)
async def rotate(rs: schemas.PrincipalRotate, details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    if not await principal_backend.user_owns_principal(db, rs.old_public_key, db_user):
        raise ApplicationError(
            uris.ERROR_PRINCIPAL_NOT_FOUND, f"Principal with public key '{rs.old_public_key}' not found"
        )

    # create the new principal first: if the new key is already taken, the old one stays
    # intact instead of leaving the user with neither.
    new_principal = await create_and_notify_principal(
        db, schemas.PrincipalCreate(public_key=rs.new_public_key), db_user
    )
    await principal_backend.delete_principal(db, schemas.PrincipalCreate(public_key=rs.old_public_key), db_user)

    # publish old key removal to desktops
    db_desktops = await desktop_backend.get_user_desktops(db, db_user.id)
    for desktop in db_desktops:
        await (
            component.session.publish(helpers.TOPIC_KEY_REMOVE.format(machine_id=desktop.authid))
            .args({db_user.email: [rs.old_public_key]})
            .options(acknowledge=True)
            .execute()
        )

    return new_principal


@component.register("io.xconn.deskconn.account.principal.delete")
async def delete(rs: schemas.PrincipalCreate, details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    await principal_backend.delete_principal(db, rs, db_user)

    # publish keys removal to desktops
    db_desktops = await desktop_backend.get_user_desktops(db, db_user.id)
    for desktop in db_desktops:
        await (
            component.session.publish(helpers.TOPIC_KEY_REMOVE.format(machine_id=desktop.authid))
            .args({db_user.email: [rs.public_key]})
            .options(acknowledge=True)
            .execute()
        )
