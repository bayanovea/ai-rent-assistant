"""Tests for the photo fingerprints: two hashes per shot, nothing on disk.

The network never gets touched — the Playwright context is a stub whose
``request.get`` hands back bytes we made ourselves, which is also how a
broken URL and a garbage body get simulated.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from fb_rent import config, fingerprint


def _gradient(width: int = 240, height: int = 180, shift: int = 0) -> Image.Image:
    """A smooth two-axis gradient — a picture with structure to hash."""
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for x in range(width):
        for y in range(height):
            value = (x * 255 // width + y * 128 // height + shift) % 256
            pixels[x, y] = (value, (value * 2) % 256, 255 - value)
    return image


def _jpeg_bytes(image: Image.Image, quality: int = 90) -> bytes:
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


class _Response:
    def __init__(self, body: bytes = b"", ok: bool = True):
        self.ok = ok
        self._body = body

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


def test_dhash_is_sixteen_hex_characters():
    digest = fingerprint.dhash(_gradient())
    assert len(digest) == 16
    assert set(digest) <= set("0123456789abcdef")


def test_dhash_is_stable_for_the_same_picture():
    assert fingerprint.dhash(_gradient()) == fingerprint.dhash(_gradient())


def test_dhash_survives_a_recompression():
    # The whole point of the perceptual hash: the same picture through
    # somebody else's phone still matches.
    original = _gradient()
    recompressed = Image.open(BytesIO(_jpeg_bytes(original, quality=35)))
    assert fingerprint.dhash(recompressed) == fingerprint.dhash(original)


def test_dhash_survives_a_resize():
    original = _gradient()
    smaller = original.resize((120, 90), Image.LANCZOS)
    assert fingerprint.dhash(smaller) == fingerprint.dhash(original)


def test_dhash_differs_for_a_different_picture():
    other = _gradient().rotate(90, expand=True)
    assert fingerprint.dhash(other) != fingerprint.dhash(_gradient())


def test_fingerprint_returns_md5_and_dhash():
    raw = _jpeg_bytes(_gradient())
    result = fingerprint.fingerprint(raw)
    assert result is not None

    md5, digest = result
    assert len(md5) == 32
    assert digest == fingerprint.dhash(Image.open(BytesIO(raw)))


def test_fingerprint_md5_is_of_the_raw_bytes():
    import hashlib

    raw = _jpeg_bytes(_gradient())
    assert fingerprint.fingerprint(raw)[0] == hashlib.md5(raw).hexdigest()


def test_fingerprint_of_garbage_is_none():
    assert fingerprint.fingerprint(b"not an image at all") is None
    assert fingerprint.fingerprint(b"") is None


def test_fetch_fingerprints_stops_at_the_limit():
    urls = [f"u{i}" for i in range(5)]
    context = _Context({url: _Response(_jpeg_bytes(_gradient(shift=i)))
                        for i, url in enumerate(urls)})

    found = fingerprint.fetch_fingerprints(context, urls, limit=3)

    assert [position for position, _, _ in found] == [1, 2, 3]
    assert context.request.calls == ["u0", "u1", "u2"]


def test_fetch_fingerprints_defaults_to_the_configured_count():
    urls = [f"u{i}" for i in range(6)]
    context = _Context({url: _Response(_jpeg_bytes(_gradient(shift=i)))
                        for i, url in enumerate(urls)})

    found = fingerprint.fetch_fingerprints(context, urls)

    assert len(found) == config.PHOTO_FINGERPRINT_COUNT
    assert len(context.request.calls) == config.PHOTO_FINGERPRINT_COUNT


def test_fetch_fingerprints_keeps_positions_across_a_hole():
    # The middle shot 404s; the third one must stay position 3, because
    # position is what pins a hash to a shot of the gallery.
    context = _Context({
        "a": _Response(_jpeg_bytes(_gradient(shift=0))),
        "b": _Response(b"", ok=False),
        "c": _Response(_jpeg_bytes(_gradient(shift=90))),
    })

    found = fingerprint.fetch_fingerprints(context, ["a", "b", "c"], limit=3)

    assert [position for position, _, _ in found] == [1, 3]


def test_fetch_fingerprints_skips_undecodable_bodies():
    context = _Context({
        "a": _Response(b"<html>login</html>"),
        "b": _Response(_jpeg_bytes(_gradient())),
    })

    found = fingerprint.fetch_fingerprints(context, ["a", "b"], limit=2)

    assert [position for position, _, _ in found] == [2]


def test_fetch_fingerprints_survives_a_raising_client():
    class _Boom:
        request = type("_R", (), {"get": staticmethod(
            lambda url: (_ for _ in ()).throw(RuntimeError("network")))})()

    assert fingerprint.fetch_fingerprints(_Boom(), ["a"], limit=1) == []


def test_fetch_fingerprints_handles_an_empty_gallery():
    context = _Context({})
    assert fingerprint.fetch_fingerprints(context, [], limit=3) == []
    assert context.request.calls == []


def test_fetch_fingerprints_writes_nothing_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PHOTOS_DIR", tmp_path / "photos")
    context = _Context({"a": _Response(_jpeg_bytes(_gradient()))})

    fingerprint.fetch_fingerprints(context, ["a"], limit=1)

    assert not (tmp_path / "photos").exists()
