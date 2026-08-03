"""
plat:storage — Supabase Storage HTTP client.

Was duplicated three times (esign.py, documents.py, doc_templates/router.py),
each copy missing the `apikey` header that Supabase's gateway (Kong) requires
independently of `Authorization: Bearer` -- the two are checked separately:
`apikey` identifies the caller to the gateway itself, `Authorization` is the
credential the storage service verifies. Every one of these three call sites
has zero successful uploads recorded in production, ever, as a direct result.

Centralized here so there's one implementation to get right and one place to
fix if Supabase's API contract ever changes.
"""
from __future__ import annotations

import os

import httpx
from fastapi import HTTPException

BUCKET = "institution-docs"


def _creds() -> tuple[str, str]:
    url = os.environ.get("PORTAL_SUPABASE_URL")
    key = os.environ.get("PORTAL_SUPABASE_SERVICE_KEY")
    if not url or not key:
        # A clear HTTPException beats a bare KeyError -- this still gets a
        # proper CORS-correct response via the global exception handler
        # either way, but a bare KeyError gives the log line no clue this is
        # a config problem rather than a code bug.
        raise HTTPException(status_code=500, detail="Storage is not configured.")
    return url, key


def _headers(key: str, **extra: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}", **extra}


async def storage_upload(
    path: str, data: bytes, content_type: str, *, bucket: str = BUCKET,
) -> None:
    url, key = _creds()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{url}/storage/v1/object/{bucket}/{path}",
            headers=_headers(key, **{"Content-Type": content_type, "x-upsert": "true"}),
            content=data,
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="Could not store document.")


async def storage_download(path: str, *, bucket: str = BUCKET) -> bytes:
    url, key = _creds()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{url}/storage/v1/object/{bucket}/{path}",
            headers=_headers(key),
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not fetch document.")
    return resp.content


async def storage_signed_url(path: str, expires_in: int = 600, *, bucket: str = BUCKET) -> str:
    url, key = _creds()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{url}/storage/v1/object/sign/{bucket}/{path}",
            headers=_headers(key),
            json={"expiresIn": expires_in},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not sign document URL.")
    return f"{url}/storage/v1{resp.json()['signedURL']}"


async def storage_signed_upload_url(object_name: str, *, bucket: str = BUCKET) -> dict[str, str]:
    """For client-side direct uploads: returns {upload_url, storage_path}."""
    url, key = _creds()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{url}/storage/v1/object/upload/sign/{bucket}/{object_name}",
            headers=_headers(key, **{"Content-Type": "application/json"}),
            json={"upsert": True},
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="Could not generate upload URL.")
    data = resp.json()
    return {"upload_url": f"{url}/storage/v1{data['url']}", "storage_path": object_name}
