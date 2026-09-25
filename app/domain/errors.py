"""Rules the domain refuses to break, by kind. The API maps each kind to one HTTP status
(`app.presentation.errors`); the message reaches the client verbatim as `detail`."""


class DomainError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    @property
    def detail(self) -> object:
        return self.message


class InvalidError(DomainError): ...


class UnauthenticatedError(DomainError): ...


class ForbiddenError(DomainError): ...


class NotFoundError(DomainError): ...


class ConflictError(DomainError): ...


class GoneError(DomainError): ...


class PreconditionFailedError(DomainError): ...


class ContentTooLargeError(DomainError): ...


class UnsupportedMediaTypeError(DomainError): ...


class OnboardingIncompleteError(ForbiddenError):
    """The account has not finished the onboarding step this action needs."""

    def __init__(self, onboarding_status: str) -> None:
        super().__init__("Onboarding incomplete")
        self.onboarding_status = onboarding_status

    @property
    def detail(self) -> object:
        return {"code": "ONBOARDING_INCOMPLETE", "onboarding_status": self.onboarding_status}


def found[T](value: T | None, message: str) -> T:
    if value is None:
        raise NotFoundError(message)
    return value
