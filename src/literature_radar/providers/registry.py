"""Explicit provider registration without implicit network activity."""
from __future__ import annotations
from literature_radar.providers.base import Provider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        if not isinstance(provider, Provider):
            raise TypeError("provider does not satisfy the Provider contract")
        if not provider.provider_id:
            raise ValueError("provider_id must be nonempty")
        if provider.provider_id in self._providers:
            raise ValueError(f"duplicate provider_id: {provider.provider_id}")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> Provider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"unknown provider_id: {provider_id}") from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))
