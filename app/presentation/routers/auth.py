from fastapi import APIRouter, Depends, Response, status

from app.presentation.dependencies import AccountServiceDep, client_address, rate_limit
from app.presentation.errors import error_responses
from app.presentation.schemas.users import Registered, RegisterInput


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(tags=["auth"], dependencies=[Depends(no_store)])


@router.post(
    "/auth/register",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("auth:register", client_address))],
    responses=error_responses(
        status.HTTP_422_UNPROCESSABLE_CONTENT, status.HTTP_429_TOO_MANY_REQUESTS
    ),
    description=(
        "Create an account in the user pool and sign in afterwards with the pool directly "
        "(USER_PASSWORD_AUTH). The answer is the same whether or not the email already had an "
        "account. The password must satisfy the pool's policy (422 names the rule)."
    ),
)
async def register(data: RegisterInput, service: AccountServiceDep) -> Registered:
    await service.register(str(data.email), data.password, data.display_name, data.invitation_token)
    return Registered()
