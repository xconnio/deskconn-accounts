import uuid

from xconn_core import Component
from xconn_core.exception import ApplicationError
from sqlalchemy.ext.asyncio import AsyncSession
from xconn_core.types import Depends, CallDetails

from deskconn import schemas, uris, models, helpers
from deskconn.database.database import get_database
from deskconn.database.backend import user as user_backend
from deskconn.database.backend import desktop as desktop_backend

component = Component()

PROCEDURE_ADD_REALM = "io.xconn.deskconn.realm.add"
PROCEDURE_REMOVE_REALM = "io.xconn.deskconn.realm.remove"


@component.register("io.xconn.deskconn.desktop.attach", response_model=schemas.DesktopGet)
async def attach(rs: schemas.DesktopCreate, details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    if await desktop_backend.desktop_exists_by_authid(db, rs.authid):
        raise ApplicationError(uris.ERROR_DESKTOP_EXISTS, f"Desktop with authid '{rs.authid}' already exists")

    if await desktop_backend.desktop_name_unique_for_user(db, rs.name, db_user.id):
        raise ApplicationError(
            uris.ERROR_DESKTOP_EXISTS,
            f"Desktop with name '{rs.name}' already exists for this user",
        )

    realm = str(uuid.uuid4())

    # call router rpc to add realm
    await helpers.call_cloud_router_rpc(
        component.session, PROCEDURE_ADD_REALM, [realm, rs.authid], "Got error upon creating realm for desktop"
    )

    desktop = await desktop_backend.create_desktop(db, rs, db_user, realm)

    # publish new keys to desktops
    desktop_authorizations = await desktop_backend.get_user_desktops_authid_with_authrole(db, db_user.id)
    for desktop_authid, authrole in desktop_authorizations:
        await (
            component.session.publish(helpers.TOPIC_KEY_ADD.format(machine_id=desktop_authid))
            .args(desktop.authid, desktop.public_key, authrole)
            .options(acknowledge=True)
            .execute()
        )

    return desktop


@component.register("io.xconn.deskconn.desktop.list", response_model=schemas.DesktopWithRoleGet)
async def list_desktops(rs: schemas.DesktopList, details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    return await desktop_backend.get_user_desktops_with_role(db, db_user.id, rs.name)


@component.register("io.xconn.deskconn.desktop.update", response_model=schemas.DesktopGet)
async def update(rs: schemas.DesktopUpdate, details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    data = rs.model_dump(exclude_none=True)
    data.pop("id", None)
    if len(data) == 0:
        raise ApplicationError("wamp.error.invalid_argument", "No field to update")

    db_desktop = await desktop_backend.get_user_desktop_by_id(db, rs.id, db_user)
    if db_desktop is None:
        raise ApplicationError(uris.ERROR_DESKTOP_NOT_FOUND, f"Desktop with id '{rs.id}' not found")

    return await desktop_backend.update_desktop(db, db_desktop, data)


@component.register("io.xconn.deskconn.desktop.detach")
async def detach(rs: schemas.DesktopDetach, details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    db_desktop = await desktop_backend.get_desktop_by_authid(db, rs.authid)
    if db_desktop is None:
        raise ApplicationError(uris.ERROR_DESKTOP_NOT_FOUND, f"Desktop with authid '{rs.authid}' not found")

    if db_desktop.user_id != db_user.id:
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Cannot detach a desktop owned by another user")

    # call router rpc to remove realm
    await helpers.call_cloud_router_rpc(
        component.session,
        PROCEDURE_REMOVE_REALM,
        [db_desktop.realm, db_desktop.authid],
        "Got error upon deleting realm for desktop",
    )

    await desktop_backend.delete_desktop(db, db_desktop)

    await (
        component.session.publish(helpers.TOPIC_DESKTOP_DETACH.format(machine_id=db_desktop.authid))
        .options(acknowledge=True)
        .execute()
    )

    await helpers.call_cloud_router_rpc(
        component.session, helpers.RPC_KILL_SESSION, [db_desktop.authid], "Got error upon killing session for desktop"
    )

    # publish keys removal to desktops
    db_desktops = await desktop_backend.get_user_desktops(db, db_user.id)
    for desktop in db_desktops:
        await (
            component.session.publish(helpers.TOPIC_KEY_REMOVE.format(machine_id=desktop.authid))
            .args({db_desktop.authid: [db_desktop.public_key]})
            .options(acknowledge=True)
            .execute()
        )


@component.register("io.xconn.deskconn.desktop.invitation.user.create", response_model=schemas.DesktopInviteGet)
async def invite_user(
    rs: schemas.DesktopUserInviteCreate, details: CallDetails, db: AsyncSession = Depends(get_database)
):
    inviter = await user_backend.get_user_by_email(db, details.authid)
    if inviter is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    db_desktop = await desktop_backend.get_desktop_by_id(db, rs.desktop_id)
    if db_desktop is None:
        raise ApplicationError(uris.ERROR_DESKTOP_NOT_FOUND, f"Desktop with id '{rs.desktop_id}' not found")

    inviter_access = await desktop_backend.get_user_access_by_desktop_and_user(db, db_desktop.id, inviter.id)
    if inviter_access is None or inviter_access.role not in (
        models.DesktopAccessRole.owner,
        models.DesktopAccessRole.admin,
    ):
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Only the desktop owner or admin can send invitations")

    invitee = await user_backend.get_user_by_email(db, str(rs.email))
    if invitee is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with email '{rs.email}' not found")

    if inviter.id == invitee.id:
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Cannot invite yourself")

    if await desktop_backend.user_access_exists(db, db_desktop.id, invitee.id):
        raise ApplicationError(uris.ERROR_USER_ALREADY_MEMBER, "User already has access to this desktop")

    if await desktop_backend.get_pending_user_invite(db, db_desktop.id, invitee.id) is not None:
        raise ApplicationError(uris.ERROR_INVITATION_ALREADY_SENT, f"Invitation already sent to '{rs.email}'")

    invite = await desktop_backend.create_desktop_invite(
        db, inviter, db_desktop, rs.role, rs.expires_in_hours, invitee_user=invitee
    )

    helpers.send_desktop_invite_email(inviter.email, invitee.email, db_desktop.name, rs.role.value)

    return invite


@component.register("io.xconn.deskconn.desktop.invitation.inbox.list", response_model=schemas.DesktopInviteInboxGet)
async def list_inbox_invitations(details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    return await desktop_backend.list_desktop_invites_inbox(db, db_user)


@component.register("io.xconn.deskconn.desktop.invitation.outbox.list", response_model=schemas.DesktopInviteOutboxGet)
async def list_outbox_invitations(details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    return await desktop_backend.list_desktop_invites_outbox(db, db_user)


@component.register("io.xconn.deskconn.desktop.invitation.user.cancel")
async def cancel_desktop_invitation(
    rs: schemas.InviteCancel, details: CallDetails, db: AsyncSession = Depends(get_database)
):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    invite = await desktop_backend.get_desktop_invite_by_id(db, rs.invitation_id)
    if invite is None:
        raise ApplicationError(uris.ERROR_INVITATION_NOT_FOUND, f"Invitation '{rs.invitation_id}' not found")

    if invite.inviter_id != db_user.id:
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Only the inviter can cancel this invitation")

    if invite.status != models.InvitationStatus.pending:
        raise ApplicationError(uris.ERROR_INVITATION_INVALID, "Invitation is no longer pending")

    await desktop_backend.cancel_desktop_invite(db, invite)


@component.register("io.xconn.deskconn.desktop.access.user.me", response_model=schemas.DesktopUserAccessGet)
async def get_my_access(
    rs: schemas.DesktopAccessListRequest, details: CallDetails, db: AsyncSession = Depends(get_database)
):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    db_desktop = await desktop_backend.get_desktop_by_id(db, rs.desktop_id)
    if db_desktop is None:
        raise ApplicationError(uris.ERROR_DESKTOP_NOT_FOUND, f"Desktop with id '{rs.desktop_id}' not found")

    access = await desktop_backend.get_user_access_by_desktop_and_user(db, db_desktop.id, db_user.id)
    if access is None:
        raise ApplicationError(uris.ERROR_DESKTOP_ACCESS_NOT_FOUND, "No access record found for this desktop")

    return access


@component.register("io.xconn.deskconn.desktop.invitation.respond")
async def respond_invitation(
    rs: schemas.DesktopInviteRespond, details: CallDetails, db: AsyncSession = Depends(get_database)
):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    invite = await desktop_backend.get_desktop_invite_by_id(db, rs.invitation_id)
    if invite is None:
        raise ApplicationError(uris.ERROR_INVITATION_NOT_FOUND, f"Invitation '{rs.invitation_id}' not found")

    if invite.invitee_user_id != db_user.id:
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Not authorized to respond to this invitation")

    if invite.status != models.InvitationStatus.pending:
        raise ApplicationError(uris.ERROR_INVITATION_INVALID, "Invitation is no longer pending")

    if invite.expires_at < helpers.utcnow():
        await desktop_backend.change_desktop_invite_status(db, invite, models.InvitationStatus.expired)
        raise ApplicationError(uris.ERROR_INVITATION_EXPIRED, "Invitation has expired")

    await desktop_backend.respond_to_desktop_user_invite(db, invite, rs.status)


@component.register("io.xconn.deskconn.desktop.access.user.set", response_model=schemas.DesktopUserAccessGet)
async def set_user_access(
    rs: schemas.DesktopUserAccessSet, details: CallDetails, db: AsyncSession = Depends(get_database)
):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    db_desktop = await desktop_backend.get_desktop_by_id(db, rs.desktop_id)
    if db_desktop is None:
        raise ApplicationError(uris.ERROR_DESKTOP_NOT_FOUND, f"Desktop with id '{rs.desktop_id}' not found")

    if db_desktop.user_id != db_user.id:
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Only the desktop owner can set access roles")

    if rs.user_id == db_user.id:
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Cannot change the owner's role")

    return await desktop_backend.set_user_access(db, rs.desktop_id, rs.user_id, rs.role)


@component.register("io.xconn.deskconn.desktop.access.user.update", response_model=schemas.DesktopUserAccessGet)
async def update_user_access_role(
    rs: schemas.DesktopAccessRoleUpdate, details: CallDetails, db: AsyncSession = Depends(get_database)
):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    db_access = await desktop_backend.get_user_access_by_id(db, rs.access_id)
    if db_access is None:
        raise ApplicationError(uris.ERROR_DESKTOP_ACCESS_NOT_FOUND, f"Access record '{rs.access_id}' not found")

    db_desktop = await desktop_backend.get_desktop_by_id(db, db_access.desktop_id)
    if db_desktop.user_id != db_user.id:
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Only the desktop owner can change access roles")

    if db_access.role == models.DesktopAccessRole.owner:
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Cannot change the owner's role")

    return await desktop_backend.update_user_access_role(db, db_access, rs.role)


@component.register("io.xconn.deskconn.desktop.access.user.revoke")
async def revoke_user_access(
    rs: schemas.DesktopAccessRevoke, details: CallDetails, db: AsyncSession = Depends(get_database)
):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    db_access = await desktop_backend.get_user_access_by_id(db, rs.access_id)
    if db_access is None:
        raise ApplicationError(uris.ERROR_DESKTOP_ACCESS_NOT_FOUND, f"Access record '{rs.access_id}' not found")

    if db_access.role == models.DesktopAccessRole.owner:
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Cannot revoke the owner's access")

    db_desktop = await desktop_backend.get_desktop_by_id(db, db_access.desktop_id)
    if db_desktop.user_id != db_user.id:
        raise ApplicationError(uris.ERROR_USER_NOT_AUTHORIZED, "Only the desktop owner can revoke access")

    await desktop_backend.revoke_user_access(db, db_access)


@component.register("io.xconn.deskconn.desktop.access.user.list", response_model=schemas.DesktopUserAccessDetailGet)
async def list_user_accesses(
    rs: schemas.DesktopAccessListRequest, details: CallDetails, db: AsyncSession = Depends(get_database)
):
    db_user = await user_backend.get_user_by_email(db, details.authid)
    if db_user is None:
        raise ApplicationError(uris.ERROR_USER_NOT_FOUND, f"User with authid '{details.authid}' not found")

    db_desktop = await desktop_backend.get_desktop_by_id(db, rs.desktop_id)
    if db_desktop is None:
        raise ApplicationError(uris.ERROR_DESKTOP_NOT_FOUND, f"Desktop with id '{rs.desktop_id}' not found")

    requester_access = await desktop_backend.get_user_access_by_desktop_and_user(db, db_desktop.id, db_user.id)
    if requester_access is None or requester_access.role not in (
        models.DesktopAccessRole.owner,
        models.DesktopAccessRole.admin,
    ):
        raise ApplicationError(
            uris.ERROR_USER_NOT_AUTHORIZED, "Only the desktop owner or admin can view the access list"
        )

    return await desktop_backend.list_user_accesses(db, db_desktop.id)


@component.register("io.xconn.deskconn.desktop.access.key.list")
async def access_keys(details: CallDetails, db: AsyncSession = Depends(get_database)):
    db_desktop = await desktop_backend.get_desktop_by_authid(db, details.authid)
    if db_desktop is None:
        raise ApplicationError(uris.ERROR_DESKTOP_NOT_FOUND, f"Desktop with authid '{details.authid}' not found")

    return await desktop_backend.get_desktop_access_public_keys(db, db_desktop.id)
