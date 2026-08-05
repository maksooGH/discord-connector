"""Media downloader — saves Discord attachments locally."""

from __future__ import annotations

import logging
from pathlib import Path

import aiohttp

from .db import get_db

log = logging.getLogger(__name__)

MEDIA_DIR = Path(__file__).parent.parent / "data" / "media"


async def download_pending(limit: int = 100) -> int:
    """Download media files that haven't been saved locally yet.

    Returns the number of files downloaded.
    """
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    count = 0

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, message_id, url, filename FROM media WHERE downloaded = 0 LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()

        if not rows:
            log.info("No pending media to download.")
            return 0

        log.info("Downloading %d media files...", len(rows))

        async with aiohttp.ClientSession() as session:
            for row in rows:
                media_id, message_id, url, filename = row["id"], row["message_id"], row["url"], row["filename"]
                dest = MEDIA_DIR / str(message_id)
                dest.mkdir(parents=True, exist_ok=True)
                filepath = dest / (filename or f"attachment_{media_id}")

                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            filepath.write_bytes(await resp.read())
                            await db.execute(
                                "UPDATE media SET downloaded = 1, local_path = ? WHERE id = ?",
                                (str(filepath), media_id),
                            )
                            count += 1
                        else:
                            log.warning("  Failed to download %s: HTTP %d", filename, resp.status)
                except Exception as e:
                    log.error("  Error downloading %s: %s", filename, e)

                if count % 50 == 0 and count > 0:
                    await db.commit()

        await db.commit()
    finally:
        await db.close()

    log.info("Downloaded %d / %d media files.", count, len(rows))
    return count
