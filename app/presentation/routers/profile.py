from fastapi import APIRouter, status

from app.presentation.dependencies import AccountServiceDep, CurrentUserDep
from app.presentation.errors import AUTHENTICATED, error_responses
from app.presentation.schemas.uploads import PresignedUploadRead, UploadConfirm, UploadRequest
from app.presentation.schemas.users import ProfileRead, ProfileUpdate

router = APIRouter(tags=["users"], responses=error_responses(*AUTHENTICATED))


@router.get("/me")
async def me(user: CurrentUserDep, service: AccountServiceDep) -> ProfileRead:
    return ProfileRead.of(await service.profile(user.id))


@router.patch("/me")
async def update_me(
    data: ProfileUpdate, user: CurrentUserDep, service: AccountServiceDep
) -> ProfileRead:
    return ProfileRead.of(await service.rename(user.id, data.display_name))


@router.post(
    "/me/avatar/uploads",
    responses=error_responses(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE),
    description=(
        "A presigned POST for a new profile photo (JPEG, PNG or WebP, up to `max_bytes`). "
        "Upload straight to `url` with `fields` plus the file, then call `confirm`."
    ),
)
async def presign_avatar(
    data: UploadRequest, user: CurrentUserDep, service: AccountServiceDep
) -> PresignedUploadRead:
    return PresignedUploadRead.of(await service.request_avatar_upload(user.id, data.content_type))


@router.post(
    "/me/avatar/confirm",
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
    description=(
        "Make the uploaded object the profile photo: it is validated, cropped to a 256px "
        "WebP square and stored; the pending object is deleted, so a second confirm of the "
        "same key answers `404`."
    ),
)
async def confirm_avatar(
    data: UploadConfirm, user: CurrentUserDep, service: AccountServiceDep
) -> ProfileRead:
    return ProfileRead.of(await service.set_avatar(user.id, data.key))


@router.delete("/me/avatar")
async def delete_avatar(user: CurrentUserDep, service: AccountServiceDep) -> ProfileRead:
    return ProfileRead.of(await service.set_avatar(user.id, None))


@router.delete(
    "/me",
    status_code=204,
    # 409 while the account still owns a workspace; clients show its detail.
    responses=error_responses(status.HTTP_403_FORBIDDEN, status.HTTP_409_CONFLICT),
)
async def delete_me(user: CurrentUserDep, service: AccountServiceDep) -> None:
    await service.delete(user.id)
