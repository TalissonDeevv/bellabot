from __future__ import annotations

from aiohttp import StreamReader


async def read_limited(stream: StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = await stream.read(min(64 * 1024, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)
