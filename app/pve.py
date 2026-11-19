"""Minimal async client for the Proxmox VE API using an API token."""
from __future__ import annotations

import os
from typing import Any

import httpx


class PVEError(RuntimeError):
    pass


class PVE:
    def __init__(self) -> None:
        self.host = os.environ.get("PVE_HOST", "")
        self.port = int(os.environ.get("PVE_PORT", "8006"))
        self.token_id = os.environ.get("PVE_TOKEN_ID", "")
        self.secret = os.environ.get("PVE_TOKEN_SECRET", "")
        verify = os.environ.get("PVE_VERIFY_TLS", "false").lower() in ("1", "true", "yes")
        self._client = httpx.AsyncClient(
            base_url=f"https://{self.host}:{self.port}/api2/json",
            headers={"Authorization": f"PVEAPIToken={self.token_id}={self.secret}"},
            verify=verify,
            timeout=15.0,
        )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.token_id and self.secret)

    async def get(self, path: str, **params: Any) -> Any:
        try:
            r = await self._client.get(path, params=params or None)
        except httpx.HTTPError as e:
            raise PVEError(f"cannot reach Proxmox at {self.host}:{self.port}: {e}") from e
        if r.status_code >= 400:
            raise PVEError(f"Proxmox API {path} returned {r.status_code}: {r.text[:200]}")
        return r.json().get("data")

    async def close(self) -> None:
        await self._client.aclose()
