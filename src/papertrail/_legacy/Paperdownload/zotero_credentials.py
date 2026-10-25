"""Remember local Zotero grants in Windows Credential Manager, never in logs/state."""
from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes


class CredentialStoreError(RuntimeError):
    pass


class _Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR)]


class WindowsCredentialStore:
    def __init__(self):
        if os.name != "nt":
            raise CredentialStoreError("AUTH_STORE_UNAVAILABLE")
        self.api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.POINTER(ctypes.POINTER(_Credential))]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredDeleteW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]

    @staticmethod
    def _target(server_id, app_name):
        scope = f"http://127.0.0.1:23119/api\0{server_id}\0{app_name}"
        return "Trae/ZoteroLocal/" + hashlib.sha256(scope.encode()).hexdigest()

    def load(self, server_id, app_name):
        pointer = ctypes.POINTER(_Credential)()
        if not self.api.CredReadW(self._target(server_id, app_name), 1, 0,
                                  ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
                return None
            raise CredentialStoreError("AUTH_STORE_UNAVAILABLE")
        try:
            record = pointer.contents
            if record.CredentialBlobSize != 64:
                raise CredentialStoreError("AUTH_STORE_INVALID")
            try:
                return ctypes.string_at(record.CredentialBlob,
                                        record.CredentialBlobSize).decode("utf-16-le")
            except UnicodeError:
                raise CredentialStoreError("AUTH_STORE_INVALID") from None
        finally:
            self.api.CredFree(pointer)

    def save(self, server_id, app_name, key):
        blob = (ctypes.c_ubyte * 64).from_buffer_copy(key.encode("utf-16-le"))
        record = _Credential(Type=1, TargetName=self._target(server_id, app_name),
                             CredentialBlobSize=64, CredentialBlob=blob, Persist=2,
                             UserName="Zotero Local API")
        if not self.api.CredWriteW(ctypes.byref(record), 0):
            raise CredentialStoreError("AUTH_STORE_UNAVAILABLE")

    def delete(self, server_id, app_name):
        if not self.api.CredDeleteW(self._target(server_id, app_name), 1, 0):
            if ctypes.get_last_error() != 1168:
                raise CredentialStoreError("AUTH_STORE_UNAVAILABLE")
