"""Refresh-token storage interfaces and portable adapters."""

from __future__ import annotations

import ctypes
import os
import secrets
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .exceptions import CredentialNotFoundError

DEFAULT_KEYCHAIN_SERVICE = "com.codex.anova-camera.firebase-refresh-token"
DEFAULT_KEYCHAIN_ACCOUNT = "anova-oven-mcp"
DEFAULT_ENVIRONMENT_VARIABLE = "ANOVA_FIREBASE_REFRESH_TOKEN"
DEFAULT_FILE_ENVIRONMENT_VARIABLE = "ANOVA_FIREBASE_REFRESH_TOKEN_FILE"
_ERR_SEC_SUCCESS = 0
_ERR_SEC_ITEM_NOT_FOUND = -25300
_SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"
_CORE_FOUNDATION_FRAMEWORK = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)


@runtime_checkable
class RefreshTokenStore(Protocol):
    """Minimal secret-store interface used by the authentication layer."""

    def load(self) -> str:
        """Return a Firebase refresh token without logging it."""

    def save(self, refresh_token: str) -> None:
        """Persist a rotated refresh token when supported."""


@dataclass(slots=True)
class EnvironmentRefreshTokenStore:
    """Read a token from an environment variable; intentionally read-only."""

    variable: str = DEFAULT_ENVIRONMENT_VARIABLE

    def load(self) -> str:
        token = os.environ.get(self.variable, "").strip()
        if not token:
            raise CredentialNotFoundError(
                f"Environment variable {self.variable} does not contain a credential."
            )
        return token

    def save(self, refresh_token: str) -> None:
        # An inherited environment is not a safe or durable write target.
        return


@dataclass(slots=True)
class FileRefreshTokenStore:
    """Read and atomically rotate a refresh token in a mode-0600 file."""

    path: Path

    def _resolved_path(self) -> Path:
        path = self.path.expanduser()
        if path.is_symlink():
            raise CredentialNotFoundError("Refusing a symlinked credential file.")
        return path

    def load(self) -> str:
        path = self._resolved_path()
        try:
            file_stat = path.stat()
        except FileNotFoundError as error:
            raise CredentialNotFoundError(f"Credential file {path} does not exist.") from error
        if not stat.S_ISREG(file_stat.st_mode):
            raise CredentialNotFoundError("The configured credential path is not a file.")
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise CredentialNotFoundError(
                "Credential file permissions are too broad; run chmod 600 on it."
            )
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise CredentialNotFoundError("The configured credential file is empty.")
        return token

    def save(self, refresh_token: str) -> None:
        path = self._resolved_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(refresh_token)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass(slots=True)
class MacOSKeychainRefreshTokenStore:
    """Read and update a generic-password item in the macOS login Keychain."""

    service: str = DEFAULT_KEYCHAIN_SERVICE
    account: str = DEFAULT_KEYCHAIN_ACCOUNT

    def _require_macos(self) -> None:
        if sys.platform != "darwin":
            raise CredentialNotFoundError("The macOS Keychain adapter requires macOS.")

    @staticmethod
    def _frameworks() -> tuple[ctypes.CDLL, ctypes.CDLL]:
        security = ctypes.CDLL(_SECURITY_FRAMEWORK)
        core_foundation = ctypes.CDLL(_CORE_FOUNDATION_FRAMEWORK)

        security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        security.SecKeychainItemFreeContent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        core_foundation.CFRelease.restype = None
        return security, core_foundation

    def load(self) -> str:
        self._require_macos()
        security, _ = self._frameworks()
        service = self.service.encode("utf-8")
        account = self.account.encode("utf-8")
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        status = security.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            None,
        )
        if status != _ERR_SEC_SUCCESS or not password_data.value:
            raise CredentialNotFoundError(
                f"No credential was found in Keychain service {self.service}."
            )
        try:
            token = ctypes.string_at(password_data, password_length.value).decode("utf-8")
        except UnicodeDecodeError as error:
            raise CredentialNotFoundError(
                "The credential stored in Keychain is not valid UTF-8."
            ) from error
        finally:
            security.SecKeychainItemFreeContent(None, password_data)
        if not token:
            raise CredentialNotFoundError("The credential stored in Keychain is empty.")
        return token

    def save(self, refresh_token: str) -> None:
        self._require_macos()
        if not refresh_token or "\n" in refresh_token or "\r" in refresh_token:
            return
        security, core_foundation = self._frameworks()
        service = self.service.encode("utf-8")
        account = self.account.encode("utf-8")
        password = refresh_token.encode("utf-8")
        password_buffer = ctypes.create_string_buffer(password)
        password_pointer = ctypes.cast(password_buffer, ctypes.c_void_p)
        item_ref = ctypes.c_void_p()
        status = security.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            None,
            None,
            ctypes.byref(item_ref),
        )
        try:
            if status == _ERR_SEC_SUCCESS and item_ref.value:
                status = security.SecKeychainItemModifyAttributesAndData(
                    item_ref,
                    None,
                    len(password),
                    password_pointer,
                )
            elif status == _ERR_SEC_ITEM_NOT_FOUND:
                status = security.SecKeychainAddGenericPassword(
                    None,
                    len(service),
                    service,
                    len(account),
                    account,
                    len(password),
                    password_pointer,
                    None,
                )
        finally:
            if item_ref.value:
                core_foundation.CFRelease(item_ref)
        if status != _ERR_SEC_SUCCESS:
            # A rotated token can still be used for the current process. Failing
            # closed here would interrupt an otherwise healthy connection.
            return


@dataclass(slots=True)
class ChainedRefreshTokenStore:
    """Try secret stores in order and remember the one that succeeded."""

    stores: tuple[RefreshTokenStore, ...]
    _active: RefreshTokenStore | None = field(default=None, init=False, repr=False)

    def load(self) -> str:
        errors: list[str] = []
        for store in self.stores:
            try:
                token = store.load()
            except CredentialNotFoundError as error:
                errors.append(str(error))
                continue
            self._active = store
            return token
        detail = " ".join(errors) if errors else "No credential stores were configured."
        raise CredentialNotFoundError(detail)

    def save(self, refresh_token: str) -> None:
        if self._active is not None:
            self._active.save(refresh_token)


def default_refresh_token_store() -> RefreshTokenStore:
    """Use an explicit environment secret first, then macOS Keychain."""

    stores: list[RefreshTokenStore] = [EnvironmentRefreshTokenStore()]
    file_path = os.environ.get(DEFAULT_FILE_ENVIRONMENT_VARIABLE, "").strip()
    if file_path:
        stores.append(FileRefreshTokenStore(Path(file_path)))
    if sys.platform == "darwin":
        stores.append(MacOSKeychainRefreshTokenStore())
    return ChainedRefreshTokenStore(tuple(stores))
