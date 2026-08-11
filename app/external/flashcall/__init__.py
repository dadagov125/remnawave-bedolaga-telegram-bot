"""Phone verification providers: registry, factory and public types.

The registry mirrors how payment methods are handled in this project: a provider
is described by metadata (which settings it needs, whether they are filled in),
so the admin cabinet can list providers, show what is missing and let an
operator switch between them without a deploy.
"""

from typing import TypedDict

from app.config import settings

from .base import CallVerificationProvider, Verification, VerificationStatus
from .flashcall_ru import FlashcallRuProvider
from .mock import MockProvider


class ProviderInfo(TypedDict):
    name: str
    display_name: str
    #: Settings keys an operator must fill in before the provider can be used.
    required_settings: list[str]
    is_configured: bool


_REGISTRY: dict[str, type] = {
    FlashcallRuProvider.name: FlashcallRuProvider,
    MockProvider.name: MockProvider,
}

#: What each adapter needs. Empty list = nothing to configure (mock).
_REQUIRED: dict[str, list[str]] = {
    FlashcallRuProvider.name: ['PHONE_AUTH_API_KEY'],
    MockProvider.name: [],
}


def list_providers() -> list[ProviderInfo]:
    """Everything the admin UI needs to render the provider list."""
    result: list[ProviderInfo] = []
    for name, cls in _REGISTRY.items():
        required = _REQUIRED.get(name, [])
        result.append(
            ProviderInfo(
                name=name,
                display_name=cls.display_name,
                required_settings=required,
                is_configured=all(bool(getattr(settings, key, '')) for key in required),
            ),
        )
    return result


def get_provider(name: str | None = None) -> CallVerificationProvider:
    """Instantiate the selected adapter.

    An unknown name raises instead of silently falling back to the mock: a typo
    in the settings must never turn a production login into "any call works".
    """
    selected = (name or settings.PHONE_AUTH_PROVIDER or 'mock').strip().lower()
    provider_class = _REGISTRY.get(selected)
    if provider_class is None:
        raise ValueError(f"Unknown phone verification provider '{selected}'. Available: {', '.join(_REGISTRY)}")
    return provider_class()


__all__ = [
    'CallVerificationProvider',
    'ProviderInfo',
    'Verification',
    'VerificationStatus',
    'get_provider',
    'list_providers',
]
