"""Public exception hierarchy for the Anova oven client."""


class AnovaError(Exception):
    """Base class for all package errors."""


class AuthenticationError(AnovaError):
    """Authentication or refresh-token failure."""


class CredentialNotFoundError(AuthenticationError):
    """No usable refresh credential could be loaded."""


class ConnectionError(AnovaError):
    """The Anova cloud connection failed."""


class DeviceNotFoundError(AnovaError):
    """No oven, or no uniquely selectable oven, was found."""


class CommandError(AnovaError):
    """An oven command was rejected or could not be acknowledged."""


class InvalidCookPlanError(AnovaError, ValueError):
    """A cook plan violates a device or safety constraint."""


class CameraError(AnovaError):
    """A camera session or frame capture failed."""


class CameraUnavailableError(CameraError):
    """Live video is not available for the current oven state/account."""


class MissingCameraDependencyError(CameraError):
    """The optional WebRTC camera dependencies are not installed."""
