from collections.abc import Mapping


def import_error_messages(error):
    """Flatten validation errors into readable messages without ErrorDetail reprs."""
    detail = getattr(error, "detail", getattr(error, "messages", str(error)))

    def flatten(value, prefix=""):
        if isinstance(value, Mapping):
            return [message for key, nested in value.items() for message in flatten(nested, f"{prefix}{key}: ")]
        if isinstance(value, (list, tuple)):
            return [message for nested in value for message in flatten(nested, prefix)]
        return [prefix + str(value)]

    return flatten(detail)
