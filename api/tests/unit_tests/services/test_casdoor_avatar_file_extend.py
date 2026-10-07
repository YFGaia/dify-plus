"""Finite synthetic codec, original storage seam, and caller-owned SQLite checks."""

import hashlib
import zlib
from dataclasses import replace
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

import pytest
from PIL import Image, PngImagePlugin
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from extensions.ext_storage import storage
from models.model import UploadFile
from services.account_avatar_file_gateway import SQLAlchemyAccountAvatarFileGateway
from services.file_service import AvatarReservation, FileService, NormalizedAvatar, _AvatarPNGWriter

LIMIT = 2 * 1024 * 1024


def picture(fmt="PNG", *, size=(8, 7), **options):
    with Image.new("RGB", size, (21, 74, 125)) as image, BytesIO() as output:
        image.save(output, format=fmt, **options)
        return output.getvalue()


def normalized():
    result = FileService.normalize_avatar_image(picture())
    assert result.code == "normalized"
    assert result.image is not None
    return result.image


def reservation():
    ids = [str(uuid4()) for _ in range(5)]
    return AvatarReservation(*ids, f"casdoor-avatar/{ids[0]}/{ids[1]}/{ids[2]}.png")


def png_chunk(kind, payload):
    return len(payload).to_bytes(4, "big") + kind + payload + zlib.crc32(kind + payload).to_bytes(4, "big")


def webp_chunk(kind, payload):
    return kind + len(payload).to_bytes(4, "little") + payload + (b"\0" if len(payload) & 1 else b"")


def webp_body(chunks):
    return b"RIFF" + (4 + len(chunks)).to_bytes(4, "little") + b"WEBP" + chunks


@pytest.mark.parametrize(
    "fmt,options",
    [("PNG", {}), ("JPEG", {}), ("JPEG", {"progressive": True}), ("WEBP", {}), ("WEBP", {"lossless": True})],
)
def test_actual_codecs_normalize_and_strip_metadata(fmt, options):
    info = PngImagePlugin.PngInfo()
    info.add_text("private", "synthetic-metadata")
    exif = Image.Exif()
    exif[270] = "synthetic-metadata"
    content = picture(fmt, exif=exif, icc_profile=b"synthetic-icc", pnginfo=info, **options)
    result = FileService.normalize_avatar_image(content)
    assert result.code == "normalized"
    assert result.image.width == 8 and result.image.height == 7
    assert result.image.sha3_256 == hashlib.sha3_256(result.image.content).hexdigest()
    assert "synthetic" not in repr(result) + repr(result.image)
    assert "content=" not in repr(result.image)
    with Image.open(BytesIO(result.image.content)) as image:
        assert image.format == "PNG" and image.mode == "RGB"
        assert image.info == {} and not image.getexif()
        image.load()


def test_alpha_and_palette_transparency_preserved():
    for mode in ("RGBA", "P"):
        with Image.new(mode, (2, 2)) as source, BytesIO() as out:
            source.save(out, format="PNG", **({"transparency": 0} if mode == "P" else {}))
            result = FileService.normalize_avatar_image(out.getvalue())
        assert result.code == "normalized"
        with Image.open(BytesIO(result.image.content)) as image:
            assert image.mode == "RGBA" and image.getpixel((0, 0))[3] == 0
            assert image.info == {}


@pytest.mark.parametrize("content", [b"", b"x" * (LIMIT + 1), b"<svg/>", b"<html/>", bytearray(b"png")])
def test_invalid_input_before_decoder(content):
    with patch("services.file_service.Image.open", side_effect=AssertionError("decoder reached")) as decoder:
        assert FileService.normalize_avatar_image(content).code == "invalid_image"
        decoder.assert_not_called()


@pytest.mark.parametrize(
    "kind",
    [
        "png_bomb",
        "jpeg_bomb",
        "webp_x_bomb",
        "webp_l_bomb",
        "webp_v_bomb",
        "apng",
        "webp_flag",
        "webp_anim",
        "mpo",
        "png_tail",
        "jpeg_tail",
        "webp_tail",
    ],
)
def test_header_rejections_precede_native_decoder(kind):
    if kind.startswith("png") or kind == "apng":
        content = picture()
        if kind == "png_bomb":
            content = content[:8] + png_chunk(b"IHDR", (4097).to_bytes(4, "big") + content[20:29]) + content[33:]
        elif kind == "apng":
            content = content[:33] + png_chunk(b"acTL", b"\0\0\0\1\0\0\0\0") + content[33:]
        else:
            content += b"<html/>"
    elif kind.startswith("jpeg") or kind == "mpo":
        content = picture("JPEG")
        if kind == "mpo":
            content = content[:2] + b"\xff\xe2\0\6MPF\0" + content[2:]
        elif kind == "jpeg_bomb":
            data = bytearray(content)
            pos = data.index(b"\xff\xc0")
            data[pos + 7 : pos + 9] = (4097).to_bytes(2, "big")
            content = bytes(data)
        else:
            content += b"<html/>"
    else:
        content = picture("WEBP")
        if kind == "webp_x_bomb":
            content = webp_body(webp_chunk(b"VP8X", b"\0" * 4 + (4096).to_bytes(3, "little") + b"\0" * 3))
        elif kind == "webp_l_bomb":
            content = webp_body(webp_chunk(b"VP8L", b"\x2f" + (4096).to_bytes(4, "little")))
        elif kind == "webp_v_bomb":
            content = webp_body(webp_chunk(b"VP8 ", b"\0\0\0\x9d\x01\x2a" + (4097).to_bytes(2, "little") + b"\1\0"))
        elif kind == "webp_flag":
            content = webp_body(webp_chunk(b"VP8X", b"\2" + b"\0" * 9) + content[12:])
        elif kind == "webp_anim":
            content = webp_body(content[12:] + webp_chunk(b"ANIM", b"\0" * 6))
        else:
            content += b"<html/>"
    with patch("services.file_service.Image.open") as decoder:
        assert FileService.normalize_avatar_image(content).code == "invalid_image"
        decoder.assert_not_called()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_truncated_real_codecs_rejected(fmt):
    assert FileService.normalize_avatar_image(picture(fmt)[:-10]).code == "invalid_image"


def test_actual_animations_rejected():
    with Image.new("RGB", (3, 3), "red") as first, Image.new("RGB", (3, 3), "blue") as second:
        for fmt in ("PNG", "WEBP"):
            with BytesIO() as stream:
                first.save(stream, format=fmt, save_all=True, append_images=[second], duration=10, loop=0)
                assert FileService.normalize_avatar_image(stream.getvalue()).code == "invalid_image"


def test_missing_codec_is_closed():
    with patch("services.file_service.features.check", return_value=False):
        assert FileService.normalize_avatar_image(picture()).code == "unsupported_image_codec"


def test_wrong_decoded_format_and_bomb_warning_prevented():
    original_open = Image.open

    def wrong_format(*args, **kwargs):
        image = original_open(*args, **kwargs)
        image.format = "MPO"
        return image

    with patch("services.file_service.Image.open", side_effect=wrong_format):
        assert FileService.normalize_avatar_image(picture()).code == "invalid_image"
    with patch.object(Image, "MAX_IMAGE_PIXELS", 1), patch("services.file_service.Image.open") as decoder:
        assert FileService.normalize_avatar_image(picture()).code == "invalid_image"
        decoder.assert_not_called()


def test_writer_caps_before_append_and_clears():
    writer = _AvatarPNGWriter()
    writer.write(b"a" * LIMIT)
    with pytest.raises(ValueError, match="invalid_image"):
        writer.write(b"x")
    assert len(writer.data) == LIMIT
    writer.close()
    assert not writer.data


def test_encoder_output_cap_is_real_and_closed():
    # Deterministic incompressible RGB input; valid JPEG fits the download cap,
    # while the fresh PNG output exceeds it.
    import random

    data = random.Random(41).randbytes(1024 * 1024 * 3)
    with Image.frombytes("RGB", (1024, 1024), data) as image, BytesIO() as output:
        image.save(output, format="JPEG", quality=85, subsampling=0)
        content = output.getvalue()
    assert len(content) < LIMIT
    assert FileService.normalize_avatar_image(content).code == "invalid_image"


class MemoryStream:
    def __init__(self, chunks, calls, close_error=False):
        self.chunks = iter(chunks)
        self.calls = calls
        self.close_error = close_error

    def __iter__(self):
        return self

    def __next__(self):
        self.calls.append("next")
        chunk = next(self.chunks)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def close(self):
        self.calls.append("close")
        if self.close_error:
            raise RuntimeError("synthetic-private-error")


class MemoryStorage:
    def __init__(self, session=None, *, chunks=None, save_error=None, close_error=False):
        self.session = session
        self.chunks = chunks
        self.save_error = save_error
        self.close_error = close_error
        self.calls = []
        self.objects = {}

    def save(self, key, content):
        assert self.session is None or not self.session.in_transaction()
        self.calls.append("save")
        self.objects[key] = content
        if self.save_error:
            raise self.save_error
        return None

    def load_stream(self, key):
        assert self.session is None or not self.session.in_transaction()
        self.calls.append("load_stream")
        content = self.objects[key]
        return MemoryStream(
            self.chunks if self.chunks is not None else [content[:4], content[4:]], self.calls, self.close_error
        )


@pytest.mark.parametrize(
    "problem", ["short", "long", "hash", "empty", "nonbytes", "read_error", "close_error", "save_error"]
)
def test_storage_ambiguity_is_unknown_without_retry_or_delete(monkeypatch, problem):
    image = normalized()
    chunk_map = {
        "short": [image.content[:-1]],
        "long": [image.content, b"x"],
        "hash": [b"x" * len(image.content)],
        "empty": [b""],
        "nonbytes": [bytearray(image.content)],
        "read_error": [RuntimeError("synthetic-private-error")],
    }
    adapter = MemoryStorage(
        chunks=chunk_map.get(problem),
        close_error=problem == "close_error",
        save_error=RuntimeError("synthetic-private-error") if problem == "save_error" else None,
    )
    monkeypatch.setattr(storage, "storage_runner", adapter, raising=False)
    assert FileService.store_reserved_avatar(reservation(), image) == "storage_unknown"
    assert adapter.calls.count("save") == 1
    if problem != "save_error":
        assert adapter.calls[-1] == "close"


def test_stream_oversize_rejected_before_hash_update(monkeypatch):
    image = normalized()
    adapter = MemoryStorage(chunks=[b"x" * (len(image.content) + 1)])
    monkeypatch.setattr(storage, "storage_runner", adapter, raising=False)
    original = hashlib.sha3_256
    updates = []

    class Digest:
        def update(self, chunk):
            updates.append(chunk)

    def sha3(*args):
        return original(*args) if args else Digest()

    with patch("services.file_service.hashlib.sha3_256", side_effect=sha3):
        assert FileService.store_reserved_avatar(reservation(), image) == "storage_unknown"
    assert updates == [] and adapter.calls[-1] == "close"


@pytest.mark.parametrize("value", [[], iter([b"a"]), b"abc", None])
def test_nonclosable_stream_unknown(monkeypatch, value):
    adapter = MemoryStorage()
    adapter.load_stream = lambda key: value
    monkeypatch.setattr(storage, "storage_runner", adapter, raising=False)
    assert FileService.store_reserved_avatar(reservation(), normalized()) == "storage_unknown"


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_termination_propagates_and_closes_stream(monkeypatch, signal):
    adapter = MemoryStorage(chunks=[signal()])
    monkeypatch.setattr(storage, "storage_runner", adapter, raising=False)
    with pytest.raises(signal):
        FileService.store_reserved_avatar(reservation(), normalized())
    assert adapter.calls[-1] == "close"


@pytest.mark.parametrize(
    "field,value",
    [
        ("intent_id", "../other"),
        ("attempt_id", "bad"),
        ("file_id", "bad"),
        ("account_id", "bad"),
        ("tenant_id", "bad"),
        ("storage_key", "wrong"),
    ],
)
def test_invalid_reservation_never_reaches_storage_or_sql(field, value):
    bad = replace(reservation(), **{field: value})
    with patch("services.file_service.storage.save") as save:
        assert FileService.store_reserved_avatar(bad, normalized()) == "invalid_input"
        assert FileService.insert_reserved_avatar(None, bad, normalized()).code == "invalid_input"
        save.assert_not_called()


@pytest.mark.parametrize("change", [{"sha3_256": "wrong"}, {"width": 9}, {"height": 9}, {"content": b"bad"}])
def test_invalid_normalized_never_reaches_storage_or_sql(change):
    image = replace(normalized(), **change)
    with patch("services.file_service.storage.save") as save:
        assert FileService.store_reserved_avatar(reservation(), image) == "invalid_input"
        assert FileService.insert_reserved_avatar(None, reservation(), image).code == "invalid_input"
        save.assert_not_called()


def test_source_metadata_cannot_be_smuggled_into_normalized_dto():
    info = PngImagePlugin.PngInfo()
    info.add_text("private", "synthetic")
    content = picture(pnginfo=info)
    image = NormalizedAvatar(content, hashlib.sha3_256(content).hexdigest(), 8, 7)
    assert FileService.insert_reserved_avatar(None, reservation(), image).code == "invalid_input"


@pytest.fixture
def db(sqlite_engine):
    UploadFile.__table__.create(sqlite_engine, checkfirst=True)
    factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    with factory() as session:
        yield session, factory


def test_original_storage_then_actual_insert_gateway_and_rollback(db, monkeypatch):
    session, factory = db
    image, reserved = normalized(), reservation()
    adapter = MemoryStorage(session)
    monkeypatch.setattr(storage, "storage_runner", adapter, raising=False)
    assert FileService.store_reserved_avatar(reserved, image) == "stored"
    assert adapter.objects == {reserved.storage_key: image.content}
    assert adapter.calls == ["save", "load_stream", "next", "next", "next", "close"]
    commits = []
    event.listen(session, "after_commit", lambda s: commits.append(True))
    session.begin()
    result = FileService.insert_reserved_avatar(session, reserved, image)
    assert result.code == "inserted"
    row = result.upload_file
    assert row.id == reserved.file_id and row.tenant_id == reserved.tenant_id
    assert row.name == "avatar.png" and row.mime_type == "image/png" and row.extension == "png"
    assert row.size == len(image.content) and row.hash == image.sha3_256
    assert row.source_url == "" and row.used and row.used_by == reserved.account_id
    assert row.created_by == reserved.account_id
    assert not commits and session.in_transaction()
    session.rollback()
    assert session.get(UploadFile, reserved.file_id) is None
    session.rollback()
    with session.begin():
        assert FileService.insert_reserved_avatar(session, reserved, image).code == "inserted"
    gateway = SQLAlchemyAccountAvatarFileGateway(session_factory=factory)
    with patch("services.account_avatar_file_gateway.file_helpers.get_signed_file_url", return_value="signed-local"):
        assert (
            gateway.get_owned_signed_url(account_id=reserved.account_id, upload_file_id=reserved.file_id)
            == "signed-local"
        )
        assert gateway.get_owned_signed_url(account_id=str(uuid4()), upload_file_id=reserved.file_id) is None
    with factory() as reader:
        assert reader.get(UploadFile, reserved.file_id).source_url == ""


@pytest.mark.parametrize("state", ["absent", "autobegin", "nested", "new", "dirty", "deleted"])
def test_insert_rejects_unclean_or_unowned_transaction(db, state):
    session, _ = db
    image, reserved = normalized(), reservation()
    if state in ("dirty", "deleted"):
        with session.begin():
            row = FileService.insert_reserved_avatar(session, reservation(), image).upload_file
    if state == "autobegin":
        session.execute(select(UploadFile))
    elif state != "absent":
        session.begin()
    if state == "nested":
        session.begin_nested()
    elif state == "new":
        from models.account import Account

        session.add(Account(name="Synthetic", email="synthetic@example.test"))
    elif state == "dirty":
        row.name = "changed"
    elif state == "deleted":
        session.delete(row)
    with patch.object(session, "flush", side_effect=AssertionError("unrelated flush")) as flush:
        assert FileService.insert_reserved_avatar(session, reserved, image).code == "invalid_transaction"
        flush.assert_not_called()


def test_flush_failure_remains_caller_rollback_responsibility(db):
    session, _ = db
    image, reserved = normalized(), reservation()
    with session.begin():
        assert FileService.insert_reserved_avatar(session, reserved, image).code == "inserted"
    session.begin()
    assert FileService.insert_reserved_avatar(session, reserved, image).code == "insert_failed"
    assert not session.is_active and session.in_transaction()
    session.rollback()
    assert session.get(UploadFile, reserved.file_id) is not None


@pytest.mark.parametrize("stage", ["verify", "load", "encode"])
@pytest.mark.parametrize("signal", [ValueError, KeyboardInterrupt])
def test_decoder_encoder_handles_close_on_failure_or_interruption(stage, signal):
    content = picture()
    opened = []
    sources = []
    writers = []
    original_open = Image.open
    original_save = Image.Image.save

    def open_tracked(source):
        image = original_open(source)
        opened.append(image)
        sources.append(source)
        if stage == "verify":
            image.verify = lambda: (_ for _ in ()).throw(signal("synthetic"))
        elif stage == "load" and len(opened) == 2:
            image.load = lambda: (_ for _ in ()).throw(signal("synthetic"))
        return image

    def save_tracked(image, sink, **kwargs):
        writers.append(sink)
        if stage == "encode":
            sink.write(b"temporary")
            raise signal("synthetic")
        return original_save(image, sink, **kwargs)

    with patch("services.file_service.Image.open", side_effect=open_tracked), patch.object(
        Image.Image, "save", save_tracked
    ):
        if signal is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt):
                FileService.normalize_avatar_image(content)
        else:
            assert FileService.normalize_avatar_image(content).code == "invalid_image"
    assert all(source.closed for source in sources)
    assert all(image.fp is None for image in opened)
    assert all(not writer.data for writer in writers)


def test_webp_extended_canvas_mismatch_before_decoder():
    content = picture("WEBP")
    malformed = webp_body(webp_chunk(b"VP8X", b"\0" * 4 + b"\1\0\0\1\0\0") + content[12:])
    with patch("services.file_service.Image.open") as decoder:
        assert FileService.normalize_avatar_image(malformed).code == "invalid_image"
        decoder.assert_not_called()


def test_insert_does_not_decode_or_access_storage_or_commit(db):
    session, _ = db
    image, reserved = normalized(), reservation()
    with session.begin(), patch("services.file_service.Image.open") as decoder, patch(
        "services.file_service.storage.save"
    ) as save, patch.object(session, "commit") as commit, patch.object(session, "rollback") as rollback:
        assert FileService.insert_reserved_avatar(session, reserved, image).code == "inserted"
        decoder.assert_not_called()
        save.assert_not_called()
        commit.assert_not_called()
        rollback.assert_not_called()


def test_custom_storage_key_equality_cannot_reach_io():
    class PretendKey:
        def __eq__(self, other):
            return True

    bad = replace(reservation(), storage_key=PretendKey())
    with patch("services.file_service.storage.save") as save:
        assert FileService.store_reserved_avatar(bad, normalized()) == "invalid_input"
        assert FileService.insert_reserved_avatar(None, bad, normalized()).code == "invalid_input"
        save.assert_not_called()


@pytest.mark.parametrize("kind", ["none", "plain", "duck"])
def test_non_session_rejected_without_method_access(kind):
    class PretendSession:
        def get_transaction(self):
            raise AssertionError("non-session accessed")

    candidate = {"none": None, "plain": object(), "duck": PretendSession()}[kind]
    assert FileService.insert_reserved_avatar(candidate, reservation(), normalized()).code == "invalid_transaction"
