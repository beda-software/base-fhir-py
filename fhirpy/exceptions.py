class FHIRError(Exception):
    pass


class ResourceNotFound(FHIRError):
    pass


class InvalidResponse(FHIRError):
    pass


class AuthorizationError(FHIRError):
    pass


class OperationOutcome(FHIRError):
    pass


class NotSupportedVersionError(FHIRError):
    pass
