import aiohttp
from fastapi import status


async def aiohttp_get(session: aiohttp.ClientSession, url: str, params: dict = None) -> dict | list:
    async with session.get(url, params=params) as response:
        if response.status != status.HTTP_200_OK:
            msg = f"Got status {response.status} on {url}"
            raise RuntimeError(msg)
        return await response.json()
