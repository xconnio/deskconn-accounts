from xconn_core import Component
from xconn_core.exception import ApplicationError
from sqlalchemy.ext.asyncio import AsyncSession
from xconn_core.types import Depends, CallDetails

from deskconn import schemas, uris, helpers
from deskconn.database.database import get_database
from deskconn.database.backend import user as user_backend
from deskconn.database.backend import device as device_backend
from deskconn.database.backend import desktop as desktop_backend

component = Component()


@component.register("io.xconn.deskconn.device.create", response_model=schemas.DeviceGet)
async def create(rs: schemas.DeviceCreate, details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    if await device_backend.device_exists_by_keys(db, rs):
        raise ApplicationError(
            uris.ERROR_DEVICE_EXISTS,
            f"Device with public key '{rs.public_key}' or device_id '{rs.device_id}' already exists",
        )

    device = await device_backend.create_device(db, rs, db_user)

    # publish new keys to desktops
    desktop_authorizations = await desktop_backend.get_user_desktops_authid_with_authrole(db, db_user.id)
    for desktop_authid, authrole in desktop_authorizations:
        await (
            component.session.publish(helpers.TOPIC_KEY_ADD.format(machine_id=desktop_authid))
            .args(db_user.email, device.public_key, authrole)
            .options(acknowledge=True)
            .execute()
        )

    return device


@component.register("io.xconn.deskconn.device.key.list", response_model=schemas.DeviceGet)
async def list_public_keys(details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    return await device_backend.get_user_public_keys(db, db_user.id)


@component.register("io.xconn.deskconn.device.list", response_model=schemas.DeviceGet)
async def list_devices(details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    return await device_backend.list_user_devices(db, db_user.id)


@component.register("io.xconn.deskconn.device.delete")
async def delete(device_id: str, details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    public_key = await device_backend.delete_device(db, device_id)

    # publish keys removal to desktops
    db_desktops = await desktop_backend.get_user_desktops(db, db_user.id)
    for desktop in db_desktops:
        await (
            component.session.publish(helpers.TOPIC_KEY_REMOVE.format(machine_id=desktop.authid))
            .args({db_user.email: [public_key]})
            .options(acknowledge=True)
            .execute()
        )
