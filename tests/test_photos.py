"""Tests for the photo store: picking, downloading, hashing, purging.

Nothing here touches the network — the Playwright context is a stub whose
``request.get`` returns bytes we made ourselves, which is also how a broken
URL gets simulated.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from fb_rent import config, photos


@pytest.fixture()
def photos_home(tmp_path, monkeypatch):
    """Point the photo store at a temp directory."""
    root = tmp_path / "photos"
    monkeypatch.setattr(config, "PHOTOS_DIR", root)
    return root


def _png_bytes(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (width, height), (10, 120, 200, 255)).save(buffer, "PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self, body: bytes | None, ok: bool = True):
        self.ok = ok
        self._body = body or b""

    def body(self) -> bytes:
        return self._body


class _RequestClient:
    def __init__(self, replies: dict[str, _Response]):
        self._replies = replies
        self.calls: list[str] = []

    def get(self, url: str) -> _Response:
        self.calls.append(url)
        try:
            return self._replies[url]
        except KeyError:
            raise RuntimeError(f"unexpected url: {url}") from None


class _Context:
    def __init__(self, replies: dict[str, _Response]):
        self.request = _RequestClient(replies)


def test_pick_photo_urls_keeps_short_galleries_whole():
    assert photos.pick_photo_urls([]) == []
    assert photos.pick_photo_urls(["a"]) == ["a"]
    assert photos.pick_photo_urls(["a", "b", "c"]) == ["a", "b", "c"]

    six = [f"u{i}" for i in range(6)]
    assert photos.pick_photo_urls(six) == six


def test_pick_photo_urls_spreads_over_a_long_gallery():
    urls = [f"u{i}" for i in range(20)]
    picked = photos.pick_photo_urls(urls)

    assert len(picked) == config.PHOTO_MAX_COUNT
    assert picked[0] == "u0"
    assert len(set(picked)) == len(picked)
    # The picks walk the gallery instead of clustering at its head.
    assert picked == ["u0", "u1", "u4", "u8", "u12", "u16"]


def test_download_and_resize_writes_numbered_jpegs(photos_home):
    urls = [f"https://cdn/{i}.jpg" for i in range(3)]
    context = _Context(
        {
            urls[0]: _Response(_png_bytes(2000, 1000)),
            urls[1]: _Response(_png_bytes(400, 300)),
            urls[2]: _Response(_png_bytes(800, 1600)),
        }
    )

    saved = photos.download_and_resize(context, urls, "111")

    assert [path.name for path in saved] == ["01.jpg", "02.jpg", "03.jpg"]
    assert all(path.parent == photos_home / "111" for path in saved)
    for path in saved:
        with Image.open(path) as image:
            assert image.format == "JPEG"
            assert image.mode == "RGB"
            assert max(image.size) <= config.PHOTO_LONG_EDGE_PX
    # Small originals are left alone, not upscaled.
    with Image.open(saved[1]) as image:
        assert image.size == (400, 300)


def test_download_and_resize_skips_broken_urls(photos_home):
    urls = ["https://cdn/ok.jpg", "https://cdn/404.jpg", "https://cdn/junk.jpg"]
    context = _Context(
        {
            urls[0]: _Response(_png_bytes(1200, 900)),
            urls[1]: _Response(b"", ok=False),
            urls[2]: _Response(b"not an image at all"),
        }
    )

    saved = photos.download_and_resize(context, urls, "222")

    assert [path.name for path in saved] == ["01.jpg"]
    assert context.request.calls == urls


def test_download_and_resize_downloads_at_most_the_cap(photos_home):
    urls = [f"https://cdn/{i}.jpg" for i in range(20)]
    context = _Context({url: _Response(_png_bytes(300, 300)) for url in urls})

    saved = photos.download_and_resize(context, urls, "333")

    assert len(saved) == config.PHOTO_MAX_COUNT
    assert len(context.request.calls) == config.PHOTO_MAX_COUNT


def test_download_and_resize_with_no_urls_creates_nothing(photos_home):
    assert photos.download_and_resize(_Context({}), [], "444") == []
    assert not (photos_home / "444").exists()


def test_photos_hash_is_none_without_photos(photos_home):
    assert photos.photos_hash("555") is None

    (photos_home / "555").mkdir(parents=True)
    assert photos.photos_hash("555") is None


def test_photos_hash_is_stable_and_set_sensitive(photos_home):
    directory = photos_home / "666"
    directory.mkdir(parents=True)
    (directory / "01.jpg").write_bytes(b"a" * 100)
    (directory / "02.jpg").write_bytes(b"b" * 200)

    first = photos.photos_hash("666")
    assert first == photos.photos_hash("666")

    (directory / "03.jpg").write_bytes(b"c" * 300)
    with_extra = photos.photos_hash("666")
    assert with_extra != first

    (directory / "03.jpg").write_bytes(b"c" * 400)
    assert photos.photos_hash("666") != with_extra

    (directory / "03.jpg").unlink()
    assert photos.photos_hash("666") == first


def test_photos_hash_is_per_listing(photos_home):
    for fb_id in ("777", "888"):
        directory = photos_home / fb_id
        directory.mkdir(parents=True)
        (directory / "01.jpg").write_bytes(b"a" * 100)

    # Same file names and sizes: the hash keys the photo set, not the listing.
    assert photos.photos_hash("777") == photos.photos_hash("888")

    (photos_home / "888" / "02.jpg").write_bytes(b"b" * 50)
    assert photos.photos_hash("777") != photos.photos_hash("888")


def test_purge_photos_removes_the_directory(photos_home):
    directory = photos_home / "999"
    directory.mkdir(parents=True)
    (directory / "01.jpg").write_bytes(b"a")

    photos.purge_photos("999")

    assert not directory.exists()
    # A listing that never had photos purges without complaining.
    photos.purge_photos("nope")
