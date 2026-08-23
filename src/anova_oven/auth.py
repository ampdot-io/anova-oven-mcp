"""Firebase refresh-token exchange with short-lived ID-token caching."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from .credentials import RefreshTokenStore
from .exceptions import AuthenticationError

# Firebase web API keys identify public app configuration; the user's separate
# refresh token is the credential and is never stored in this repository.
FIREBASE_WEB_API_KEY = "AIzaSyB0VNqmJVAeR1fn_NbqqhwSytyMOZ_JO9c"
FIREBASE_TOKEN_ENDPOINT = "https://securetoken.googleapis.com/v1/token"


@dataclass(frozen=True, slots=True)
class FirebaseIDToken:
    value: str
    expires_at_monotonic: float


class FirebaseTokenProvider:
    """Mint and cache Firebase ID tokens from an injected secret store."""

    def __init__(
        self,
        store: RefreshTokenStore,
        *,
        api_key: str = FIREBASE_WEB_API_KEY,
        http_client: httpx.AsyncClient | None = None,
        refresh_margin_seconds: int = 90,
    ) -> None:
        self._store = store
        self._api_key = api_key
        self._http = http_client or httpx.AsyncClient(timeout=20.0)
        self._owns_http_client = http_client is None
        self._refresh_margin_seconds = refresh_margin_seconds
        self._cached: FirebaseIDToken | None = None
        self._lock = asyncio.Lock()

    async def get_id_token(self, *, force_refresh: bool = False) -> str:
        cached = self._cached
        now = time.monotonic()
        if (
            not force_refresh
            and cached is not None
            and cached.expires_at_monotonic - self._refresh_margin_seconds > now
        ):
            return cached.value

        async with self._lock:
            cached = self._cached
            now = time.monotonic()
            if (
                not force_refresh
                and cached is not None
                and cached.expires_at_monotonic - self._refresh_margin_seconds > now
            ):
                return cached.value

            refresh_token = await asyncio.to_thread(self._store.load)
            try:
                response = await self._http.post(
                    FIREBASE_TOKEN_ENDPOINT,
                    params={"key": self._api_key},
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise AuthenticationError(
                    "Firebase rejected the stored Anova credential or could not be reached."
                ) from error

            id_token = payload.get("id_token")
            rotated_refresh_token = payload.get("refresh_token")
            try:
                expires_in = int(payload.get("expires_in", 0))
            except (TypeError, ValueError):
                expires_in = 0
            if not isinstance(id_token, str) or expires_in < 300:
                raise AuthenticationError("Firebase returned an unusable Anova ID token.")

            if (
                isinstance(rotated_refresh_token, str)
                and rotated_refresh_token
                and rotated_refresh_token != refresh_token
            ):
                await asyncio.to_thread(self._store.save, rotated_refresh_token)

            self._cached = FirebaseIDToken(
                value=id_token,
                expires_at_monotonic=time.monotonic() + expires_in,
            )
            return id_token

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()
