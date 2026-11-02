"""Eenmalig ophalen uit de Tuya-cloud: de local key en het datamodel van een toestel. Daarna werkt alles lokaal.

Dit zijn dezelfde aanvragen als in de API Explorer van het Tuya IoT Platform (Query Device Details en Query Things Data
Model), met de Access ID en Access Secret van je cloudproject. Ondertekening: HMAC-SHA256 zoals Tuya het beschrijft.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import uuid

import aiohttp

from .model import ModelError

REGIONS = {
    "eu": "https://openapi.tuyaeu.com",  # Centraal-Europa
    "weu": "https://openapi-weaz.tuyaeu.com",  # West-Europa
    "us": "https://openapi.tuyaus.com",
    "eus": "https://openapi-ueaz.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}
TIMEOUT = 10


def sign(client_id: str, secret: str, t: str, nonce: str, method: str, path: str, body: bytes = b"", token: str = "") -> str:
    text = "\n".join((method, hashlib.sha256(body).hexdigest(), "", path))
    return hmac.new(secret.encode(), (client_id + token + t + nonce + text).encode(), hashlib.sha256).hexdigest().upper()


class TuyaCloud:
    def __init__(self, client_id: str, secret: str, region: str = "eu", base: str | None = None) -> None:
        self.client_id, self.secret = client_id, secret
        self.base = base or REGIONS.get(region) or REGIONS["eu"]

    async def _get(self, s: aiohttp.ClientSession, path: str, token: str = "") -> dict:
        t, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
        headers = {"client_id": self.client_id, "t": t, "nonce": nonce, "sign_method": "HMAC-SHA256",
                   "sign": sign(self.client_id, self.secret, t, nonce, "GET", path, token=token)}
        if token:
            headers["access_token"] = token
        try:
            async with s.get(self.base + path, headers=headers) as r:
                j = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise ModelError(f"Tuya-cloud niet bereikbaar: {e or type(e).__name__}")
        if not isinstance(j, dict) or not j.get("success"):
            msg = j.get("msg") if isinstance(j, dict) else None
            raise ModelError(f"Tuya-cloud weigert: {msg or j}")
        return j.get("result") or {}

    async def device(self, device_id: str) -> tuple[dict, str]:
        """(details, datamodel als tekst) van één toestel."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as s:
            token = (await self._get(s, "/v1.0/token?grant_type=1")).get("access_token")
            if not token:
                raise ModelError("Tuya-cloud gaf geen token")
            details = await self._get(s, f"/v2.0/cloud/thing/{device_id}", token)
            model = await self._get(s, f"/v2.0/cloud/thing/{device_id}/model", token)
        return details, model.get("model") or ""
