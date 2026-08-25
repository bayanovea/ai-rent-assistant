"""Listing photos on disk: which ones to keep, how small, and where.

The photo pass is expensive per image, so the cheap decisions happen here
rather than in front of the model: at most :data:`config.PHOTO_MAX_COUNT`
shots per listing, each shrunk to :data:`config.PHOTO_LONG_EDGE_PX` on the
long edge. Facebook already serves ~960px, so the resize is a ceiling, not
an upscale.

:func:`photos_hash` is the idempotency key of the whole pass — it says
"these are still the pictures the model looked at", and a mismatch is what
sends a listing back into the queue.
"""

from __future__ import annotations

import hashlib
import shutil
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from . import config

_JPEG_NAME = "{:02d}.jpg"


def photos_dir(fb_id: str) -> Path:
    """Return the directory holding one listing's photos."""
    return config.PHOTOS_DIR / str(fb_id)


def pick_photo_urls(urls: list[str]) -> list[str]:
    """Return the cover shot plus an even spread over the rest of the gallery.

    Galleries walk the flat room by room and linger on whatever the seller
    liked most, so three angles of the same bedroom sit next to each other.
    Spreading the picks covers more rooms than the first six ever would.
    """
    urls = list(urls)
    limit = config.PHOTO_MAX_COUNT
    if len(urls) <= limit:
        return urls

    rest = urls[1:]
    take = limit - 1
    step = len(rest) / take
    return [urls[0]] + [rest[int(i * step)] for i in range(take)]


def download_and_resize(context: Any, urls: list[str], fb_id: str) -> list[Path]:
    """Download the picked photos into ``data/photos/<fb_id>/`` and shrink them.

    ``context`` is the Playwright browsing context: its ``request`` client
    shares cookies with the persistent profile, which is what makes the
    signed ``fbcdn`` links resolve at all. A URL that fails or decodes to
    garbage is skipped — a listing with five photos beats no listing.
    """
    picked = pick_photo_urls(urls)
    if not picked:
        return []

    directory = photos_dir(fb_id)
    directory.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    for url in picked:
        try:
            response = context.request.get(url)
            if not response.ok:
                continue
            image = Image.open(BytesIO(response.body()))
            image = image.convert("RGB")
            image.thumbnail(
                (config.PHOTO_LONG_EDGE_PX, config.PHOTO_LONG_EDGE_PX),
                Image.LANCZOS,
            )
            target = directory / _JPEG_NAME.format(len(saved) + 1)
            image.save(target, "JPEG", quality=config.PHOTO_JPEG_QUALITY)
        except Exception:
            continue
        saved.append(target)
    return saved


def photos_hash(fb_id: str) -> str | None:
    """Return a digest of the listing's photo set, or ``None`` if there is none.

    Names plus sizes are enough: the files are written once by
    :func:`download_and_resize` and never edited in place, so a different
    set of pictures always means a different name/size list.
    """
    directory = photos_dir(fb_id)
    if not directory.is_dir():
        return None

    entries = sorted(
        f"{path.name}:{path.stat().st_size}"
        for path in directory.iterdir()
        if path.is_file()
    )
    if not entries:
        return None
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def purge_photos(fb_id: str) -> None:
    """Delete the listing's photo directory; a missing one is not an error."""
    shutil.rmtree(photos_dir(fb_id), ignore_errors=True)
