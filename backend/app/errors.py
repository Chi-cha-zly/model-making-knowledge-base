"""Exceptions shared across model adapters and their consumers."""


class ProviderError(Exception):
    """Safe user-visible upstream failure; never include response bodies or credentials."""
