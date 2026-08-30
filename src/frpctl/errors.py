class FrpCtlError(Exception):
    """Base error suitable for presentation to a CLI/TUI user."""


class ValidationError(FrpCtlError):
    pass


class RemoteError(FrpCtlError):
    pass


class DeploymentError(FrpCtlError):
    pass
