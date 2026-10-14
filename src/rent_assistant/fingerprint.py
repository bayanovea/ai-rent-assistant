"""Two hashes per shot: the cheap duplicate signal taken at scrape time.

A flat handed to two agents comes back as two listings with different text
and different prices — nothing the text or the facts signal can join. Its
photographs, though, are usually the same files, so the first few shots of a
gallery are enough to say "this is the same flat".

Two hashes, because there are two kinds of repost:

* ``md5`` over the raw CDN bytes catches the literal one — the same file
  posted again, by the same agent or the next.
* ``dhash``, a 64-bit difference hash, catches the re-upload: the same
  picture after somebody else's phone, a resize or a recompression.

Both are compared exactly, string to string. Hamming distance would need a
threshold nobody can pick blind, and empty interiors all look alike.

This is not :mod:`rent_assistant.photos`: that module is about files on disk for
the AI pass and about :func:`photos.photos_hash`, the key that re-queues a
listing. Here nothing is written at all — the bytes are downloaded, hashed
and dropped. Writing three previews into ``data/photos/<fb_id>/`` would tell
the photo pass forever that the gallery is already on disk.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from typing import Any

from PIL import Image

from . import config

# 8 rows of 9 pixels: comparing neighbours along a row gives 8x8 = 64 bits.
_DHASH_WIDTH = 9
_DHASH_HEIGHT = 8


def dhash(image: Image.Image) -> str:
    """Return the 16-hex-character difference hash of ``image``.

    Grayscale, squashed to 9x8, then each pixel compared with its right-hand
    neighbour. What survives is the direction of the brightness gradients —
    stable across resizes and JPEG quality, different as soon as the picture
    is a different picture.
    """
    small = image.convert("L").resize((_DHASH_WIDTH, _DHASH_HEIGHT), Image.LANCZOS)
    pixels = small.tobytes()  # mode "L", one byte per pixel, row-major

    bits = 0
    for row in range(_DHASH_HEIGHT):
        base = row * _DHASH_WIDTH
        for col in range(_DHASH_WIDTH - 1):
            bits <<= 1
            if pixels[base + col] > pixels[base + col + 1]:
                bits |= 1
    return f"{bits:016x}"


def fingerprint(raw: bytes) -> tuple[str, str] | None:
    """Return ``(md5, dhash)`` for the image bytes, or ``None`` if undecodable.

    The md5 is of the bytes exactly as the CDN served them — before any
    resize, so that it stays comparable between runs.
    """
    try:
        image = Image.open(BytesIO(raw))
        image.load()
    except Exception:
        return None
    return hashlib.md5(raw).hexdigest(), dhash(image)


def fetch_fingerprints(
    context: Any,
    urls: list[str],
    limit: int | None = None,
) -> list[tuple[int, str, str]]:
    """Hash the first ``limit`` gallery shots; return ``(position, md5, dhash)``.

    ``context`` is the Playwright browsing context: its ``request`` client
    shares cookies with the persistent profile, which is what makes the
    signed ``fbcdn`` links resolve. A URL that fails or decodes to garbage is
    skipped, but the positions of the ones that survive stay as they were in
    the gallery — position is what pins a hash to a shot, so a hole in the
    middle must not shift the rest.

    Nothing is written to disk.
    """
    if limit is None:
        limit = config.PHOTO_FINGERPRINT_COUNT

    found: list[tuple[int, str, str]] = []
    for position, url in enumerate(list(urls)[:limit], start=1):
        try:
            response = context.request.get(url)
            if not response.ok:
                continue
            hashes = fingerprint(response.body())
        except Exception:
            continue
        if hashes is None:
            continue
        found.append((position, hashes[0], hashes[1]))
    return found
