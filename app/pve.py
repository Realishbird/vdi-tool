"""Minimal async client for the Proxmox VE API using an API token."""
from __future__ import annotations

import asyncio
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

    async def request(self, method: str, path: str, timeout: float | None = None, **data: Any) -> Any:
        data = {k: (int(v) if isinstance(v, bool) else v) for k, v in data.items() if v is not None}
        try:
            r = await self._client.request(method, path, data=data or None, timeout=timeout or 30.0)
        except httpx.HTTPError as e:
            raise PVEError(f"cannot reach Proxmox at {self.host}:{self.port}: {e}") from e
        if r.status_code >= 400:
            detail = r.text[:300] or r.reason_phrase
            raise PVEError(f"{method} {path} failed ({r.status_code} {r.reason_phrase}): {detail}")
        return r.json().get("data")

    async def post(self, path: str, **data: Any) -> Any:
        return await self.request("POST", path, **data)

    async def put(self, path: str, **data: Any) -> Any:
        return await self.request("PUT", path, **data)

    async def delete(self, path: str, **params: Any) -> Any:
        try:
            r = await self._client.delete(path, params=params or None)
        except httpx.HTTPError as e:
            raise PVEError(f"cannot reach Proxmox: {e}") from e
        if r.status_code >= 400:
            raise PVEError(f"DELETE {path} failed ({r.status_code}): {r.text[:300] or r.reason_phrase}")
        return r.json().get("data")

    async def upload(self, node: str, storage: str, content: str, filename: str, fileobj) -> Any:
        try:
            r = await self._client.post(
                f"/nodes/{node}/storage/{storage}/upload",
                data={"content": content},
                files={"filename": (filename, fileobj, "application/octet-stream")},
                timeout=3600.0,
            )
        except httpx.HTTPError as e:
            raise PVEError(f"upload failed: {e}") from e
        if r.status_code >= 400:
            raise PVEError(f"upload failed ({r.status_code}): {r.text[:300] or r.reason_phrase}")
        return r.json().get("data")

    async def wait_task(self, node: str, upid: Any, timeout: float = 1800.0) -> None:
        """Wait for a Proxmox task (UPID) to finish; raise if it did not end OK."""
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return
        waited = 0.0
        while waited < timeout:
            st = await self.get(f"/nodes/{node}/tasks/{upid}/status")
            if st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise PVEError(f"task failed: {st.get('exitstatus')}")
                return
            await asyncio.sleep(1.0)
            waited += 1.0
        raise PVEError("task timed out")

    async def close(self) -> None:
        await self._client.aclose()
