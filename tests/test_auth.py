from __future__ import annotations

import ctypes
import json
import os
from typing import Any

import httpx
import pytest

from anova_oven import credentials
from anova_oven.auth import FirebaseTokenProvider
from anova_oven.credentials import (
    DEFAULT_KEYCHAIN_ACCOUNT,
    DEFAULT_KEYCHAIN_SERVICE,
    FileRefreshTokenStore,
    MacOSKeychainRefreshTokenStore,
)
from anova_oven.exceptions import CredentialNotFoundError


class MemoryStore:
    def __init__(self) -> None:
        self.token = "refresh-secret"
        self.saved: list[str] = []
        self.loads = 0

    def load(self) -> str:
        self.loads += 1
        return self.token

    def save(self, refresh_token: str) -> None:
        self.saved.append(refresh_token)
        self.token = refresh_token


class FakeCoreFoundation:
    def __init__(self) -> None:
        self.released: list[int | None] = []

    def CFRelease(self, reference: ctypes.c_void_p) -> None:
        self.released.append(reference.value)


class FakeSecurityFramework:
    def __init__(
        self,
        *,
        stored_token: bytes = b"canonical-refresh-token",
        existing_item: bool = True,
    ) -> None:
        self._stored_token = ctypes.create_string_buffer(stored_token)
        self._existing_item = existing_item
        self.find_selectors: list[tuple[bytes, bytes]] = []
        self.freed_passwords = 0
        self.modified_passwords: list[bytes] = []
        self.added_passwords: list[bytes] = []

    def SecKeychainFindGenericPassword(
        self,
        _keychain: object,
        service_length: int,
        service: bytes,
        account_length: int,
        account: bytes,
        password_length: Any,
        password_data: Any,
        item_ref: Any,
    ) -> int:
        self.find_selectors.append(
            (service[:service_length], account[:account_length])
        )
        if password_length is not None and password_data is not None:
            password_length._obj.value = len(self._stored_token.value)
            password_data._obj.value = ctypes.addressof(self._stored_token)
            return 0
        if self._existing_item:
            item_ref._obj.value = 0x1234
            return 0
        return -25300

    def SecKeychainItemFreeContent(
        self, _attributes: object, _password_data: ctypes.c_void_p
    ) -> int:
        self.freed_passwords += 1
        return 0

    def SecKeychainItemModifyAttributesAndData(
        self,
        _item_ref: ctypes.c_void_p,
        _attributes: object,
        password_length: int,
        password_data: ctypes.c_void_p,
    ) -> int:
        self.modified_passwords.append(ctypes.string_at(password_data, password_length))
        return 0

    def SecKeychainAddGenericPassword(
        self,
        _keychain: object,
        _service_length: int,
        _service: bytes,
        _account_length: int,
        _account: bytes,
        password_length: int,
        password_data: ctypes.c_void_p,
        _item_ref: object,
    ) -> int:
        self.added_passwords.append(ctypes.string_at(password_data, password_length))
        return 0


@pytest.mark.asyncio
async def test_refreshes_once_caches_and_persists_rotation() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert b"refresh-secret" in request.content
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "id_token": "short-lived-id-token",
                    "refresh_token": "rotated-refresh-secret",
                    "expires_in": "3600",
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = MemoryStore()
    provider = FirebaseTokenProvider(store, http_client=http)
    try:
        first = await provider.get_id_token()
        second = await provider.get_id_token()
    finally:
        await http.aclose()
    assert first == second == "short-lived-id-token"
    assert requests == 1
    assert store.saved == ["rotated-refresh-secret"]


def test_file_store_requires_private_permissions_and_rotates_atomically(tmp_path) -> None:
    path = tmp_path / "anova-refresh-token"
    path.write_text("first-token\n", encoding="utf-8")
    os.chmod(path, 0o644)
    store = FileRefreshTokenStore(path)
    with pytest.raises(CredentialNotFoundError):
        store.load()

    os.chmod(path, 0o600)
    assert store.load() == "first-token"
    store.save("second-token")
    assert store.load() == "second-token"
    assert path.stat().st_mode & 0o777 == 0o600


def test_keychain_load_is_scoped_to_the_canonical_service_and_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = FakeSecurityFramework()
    core_foundation = FakeCoreFoundation()
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    monkeypatch.setattr(
        MacOSKeychainRefreshTokenStore,
        "_frameworks",
        staticmethod(lambda: (security, core_foundation)),
    )

    store = MacOSKeychainRefreshTokenStore()
    assert store.load() == "canonical-refresh-token"
    assert security.find_selectors == [
        (DEFAULT_KEYCHAIN_SERVICE.encode(), DEFAULT_KEYCHAIN_ACCOUNT.encode())
    ]
    assert security.freed_passwords == 1


@pytest.mark.parametrize("existing_item", [True, False])
def test_keychain_rotation_uses_security_framework_without_a_secret_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    existing_item: bool,
) -> None:
    security = FakeSecurityFramework(existing_item=existing_item)
    core_foundation = FakeCoreFoundation()
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    monkeypatch.setattr(
        MacOSKeychainRefreshTokenStore,
        "_frameworks",
        staticmethod(lambda: (security, core_foundation)),
    )

    MacOSKeychainRefreshTokenStore().save("rotated-refresh-token")

    assert "subprocess" not in credentials.__dict__
    assert security.find_selectors == [
        (DEFAULT_KEYCHAIN_SERVICE.encode(), DEFAULT_KEYCHAIN_ACCOUNT.encode())
    ]
    if existing_item:
        assert security.modified_passwords == [b"rotated-refresh-token"]
        assert security.added_passwords == []
        assert core_foundation.released == [0x1234]
    else:
        assert security.modified_passwords == []
        assert security.added_passwords == [b"rotated-refresh-token"]
        assert core_foundation.released == []
