import base64
import hashlib
import os
import uuid
import zlib
from collections.abc import Generator, Sequence  # Changed Iterator to Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from io import BytesIO
from tempfile import NamedTemporaryFile
from typing import Literal
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, features
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, SessionTransactionOrigin, sessionmaker
from werkzeug.exceptions import NotFound

from configs import dify_config
from constants import (
    AUDIO_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from core.rag.extractor.extract_processor import ExtractProcessor
from enums import DeploymentEdition
from extensions.ext_storage import storage
from extensions.storage.storage_type import StorageType
from graphon.file import helpers as file_helpers
from libs.datetime_utils import naive_utc_now
from libs.helper import extract_tenant_id
from models import Account
from models.enums import CreatorUserRole
from models.model import EndUser, UploadFile

from .errors.file import BlockedFileExtensionError, FileNotExistsError, FileTooLargeError, UnsupportedFileTypeError

PREVIEW_WORDS_LIMIT = 3000


_AVATAR_LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AvatarReservation:
    """Structural reference only; R must reconstruct and recheck database authority."""

    intent_id: str
    attempt_id: str
    file_id: str
    account_id: str
    tenant_id: str
    storage_key: str


@dataclass(frozen=True, slots=True)
class NormalizedAvatar:
    content: bytes = field(repr=False)
    sha3_256: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class AvatarNormalization:
    code: Literal["normalized", "invalid_image", "unsupported_image_codec"]
    image: NormalizedAvatar | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AvatarFileInsert:
    code: Literal["inserted", "invalid_input", "invalid_transaction", "insert_failed"]
    upload_file: UploadFile | None = field(default=None, repr=False)


class _AvatarPNGWriter:
    """Sequential PNG sink: refuse an oversized write before retaining any of it."""

    def __init__(self):
        self.data = bytearray()

    def write(self, chunk: bytes) -> int:
        if not isinstance(chunk, bytes) or len(chunk) > _AVATAR_LIMIT - len(self.data):
            raise ValueError("invalid_image")
        self.data.extend(chunk)
        return len(chunk)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.data.clear()


def _avatar_dimensions(width: int, height: int) -> tuple[int, int]:
    if not (0 < width <= 4096 and 0 < height <= 4096 and width * height <= 16_777_216):
        raise ValueError("invalid_image")
    # Reject before Pillow's warning path without changing its shared warning configuration.
    if Image.MAX_IMAGE_PIXELS is not None and width * height > Image.MAX_IMAGE_PIXELS:
        raise ValueError("invalid_image")
    return width, height


def _avatar_png_header(content: bytes) -> tuple[int, int]:
    offset = 8
    dimensions = None
    has_data = False
    while offset < len(content):
        if offset + 12 > len(content):
            raise ValueError("invalid_image")
        length = int.from_bytes(content[offset : offset + 4], "big")
        end = offset + 12 + length
        if end > len(content):
            raise ValueError("invalid_image")
        kind = content[offset + 4 : offset + 8]
        payload = content[offset + 8 : end - 4]
        if zlib.crc32(kind + payload) != int.from_bytes(content[end - 4 : end], "big"):
            raise ValueError("invalid_image")
        if dimensions is None and kind != b"IHDR":
            raise ValueError("invalid_image")
        if kind in (b"acTL", b"fcTL", b"fdAT"):
            raise ValueError("invalid_image")
        if kind == b"IHDR":
            if dimensions is not None or length != 13:
                raise ValueError("invalid_image")
            dimensions = _avatar_dimensions(int.from_bytes(payload[:4], "big"), int.from_bytes(payload[4:8], "big"))
        if kind == b"IDAT":
            has_data = True
        if kind == b"IEND":
            if length or not has_data or end != len(content) or dimensions is None:
                raise ValueError("invalid_image")
            return dimensions
        offset = end
    raise ValueError("invalid_image")


def _avatar_jpeg_header(content: bytes) -> tuple[int, int]:
    offset = 2
    dimensions = None
    in_scan = False
    has_scan = False
    # Every iteration consumes bytes; the outer input cap bounds the marker scan.
    while offset < len(content):
        if content[offset] != 0xFF:
            if not in_scan:
                raise ValueError("invalid_image")
            offset += 1
            continue
        offset += 1
        while offset < len(content) and content[offset] == 0xFF:
            offset += 1
        if offset == len(content):
            raise ValueError("invalid_image")
        marker = content[offset]
        offset += 1
        if in_scan and (marker == 0 or 0xD0 <= marker <= 0xD7):
            continue
        if marker == 0xD9:
            if offset != len(content) or dimensions is None or not has_scan:
                raise ValueError("invalid_image")
            return dimensions
        in_scan = False
        if marker in (0, 0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or offset + 2 > len(content):
            raise ValueError("invalid_image")
        length = int.from_bytes(content[offset : offset + 2], "big")
        end = offset + length
        if length < 2 or end > len(content):
            raise ValueError("invalid_image")
        payload = content[offset + 2 : end]
        if marker == 0xE2 and payload.startswith(b"MPF\0"):
            raise ValueError("invalid_image")
        if marker in (0xC0, 0xC1, 0xC2):
            if dimensions is not None or len(payload) < 6:
                raise ValueError("invalid_image")
            dimensions = _avatar_dimensions(int.from_bytes(payload[3:5], "big"), int.from_bytes(payload[1:3], "big"))
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            raise ValueError("invalid_image")
        if marker == 0xDA:
            if dimensions is None:
                raise ValueError("invalid_image")
            has_scan = in_scan = True
        offset = end
    raise ValueError("invalid_image")


def _avatar_webp_header(content: bytes) -> tuple[int, int]:
    if len(content) < 20 or int.from_bytes(content[4:8], "little") + 8 != len(content):
        raise ValueError("invalid_image")
    offset = 12
    canvas = None
    dimensions = None
    while offset < len(content):
        if offset + 8 > len(content):
            raise ValueError("invalid_image")
        kind = content[offset : offset + 4]
        length = int.from_bytes(content[offset + 4 : offset + 8], "little")
        end = offset + 8 + length
        padded_end = end + (length & 1)
        if padded_end > len(content) or (length & 1 and content[end] != 0):
            raise ValueError("invalid_image")
        payload = content[offset + 8 : end]
        if kind in (b"ANIM", b"ANMF"):
            raise ValueError("invalid_image")
        if kind == b"VP8X":
            if offset != 12 or length != 10 or payload[0] & 0xC3 or payload[1:4] != b"\0\0\0":
                raise ValueError("invalid_image")
            canvas = _avatar_dimensions(
                1 + int.from_bytes(payload[4:7], "little"), 1 + int.from_bytes(payload[7:10], "little")
            )
        if kind in (b"VP8 ", b"VP8L"):
            if dimensions is not None:
                raise ValueError("invalid_image")
            if kind == b"VP8 ":
                if length < 10 or payload[0] & 1 or payload[3:6] != b"\x9d\x01\x2a":
                    raise ValueError("invalid_image")
                dimensions = _avatar_dimensions(
                    int.from_bytes(payload[6:8], "little") & 0x3FFF,
                    int.from_bytes(payload[8:10], "little") & 0x3FFF,
                )
            else:
                if length < 5 or payload[0] != 0x2F or payload[4] & 0xE0:
                    raise ValueError("invalid_image")
                bits = int.from_bytes(payload[1:5], "little")
                dimensions = _avatar_dimensions(1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
        offset = padded_end
    if dimensions is None or (canvas is not None and canvas != dimensions):
        raise ValueError("invalid_image")
    return dimensions


def _avatar_header(content: bytes) -> tuple[str, tuple[int, int]]:
    if type(content) is not bytes or not 0 < len(content) <= _AVATAR_LIMIT:
        raise ValueError("invalid_image")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG", _avatar_png_header(content)
    if content.startswith(b"\xff\xd8"):
        return "JPEG", _avatar_jpeg_header(content)
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "WEBP", _avatar_webp_header(content)
    raise ValueError("invalid_image")


def _avatar_valid_reservation(reservation: AvatarReservation) -> bool:
    if type(reservation) is not AvatarReservation or type(reservation.storage_key) is not str:
        return False
    try:
        values = (
            reservation.intent_id,
            reservation.attempt_id,
            reservation.file_id,
            reservation.account_id,
            reservation.tenant_id,
        )
        if any(type(value) is not str or str(uuid.UUID(value)) != value for value in values):
            return False
        return reservation.storage_key == (
            f"casdoor-avatar/{reservation.intent_id}/{reservation.attempt_id}/{reservation.file_id}.png"
        )
    except (ValueError, TypeError, AttributeError):
        return False


def _avatar_valid_normalized(normalized: NormalizedAvatar) -> bool:
    if type(normalized) is not NormalizedAvatar:
        return False
    # Check internal normalization invariants without decoding inside the attachment root.
    try:
        content = normalized.content
        if type(normalized.width) is not int or type(normalized.height) is not int:
            return False
        if _avatar_header(content) != ("PNG", (normalized.width, normalized.height)):
            return False
        if content[24:29] not in (b"\x08\x02\0\0\0", b"\x08\x06\0\0\0"):
            return False
        offset = 8
        while offset < len(content):
            if content[offset + 4 : offset + 8] not in (b"IHDR", b"IDAT", b"IEND"):
                return False
            offset += 12 + int.from_bytes(content[offset : offset + 4], "big")
        return type(normalized.sha3_256) is str and hashlib.sha3_256(content).hexdigest() == normalized.sha3_256
    except Exception:
        return False


class FileService:
    @staticmethod
    def _cleanup_reserved_avatar(permit):
        """One-use exact orphan cleanup after original acknowledged/closed SQL roots."""
        from core.casdoor.avatar_cleanup_provider import _delete_native_avatar
        from core.casdoor.avatar_termination import _consume_avatar_cleanup_io, _observe_avatar_cleanup_deleted

        domain, record = _consume_avatar_cleanup_io(permit)
        if not _avatar_valid_reservation(record.reservation):
            raise ValueError("avatar_cleanup_invalid")
        if _delete_native_avatar(domain, record.reservation):
            return _observe_avatar_cleanup_deleted(domain, record)
        return None

    _session_maker: sessionmaker[Session]

    def __init__(self, session_factory: sessionmaker | Engine | None = None):
        match session_factory:
            case Engine():
                self._session_maker = sessionmaker(bind=session_factory)
            case sessionmaker():
                self._session_maker = session_factory
            case _:
                raise AssertionError("must be a sessionmaker or an Engine.")

    @staticmethod
    def normalize_avatar_image(content: bytes) -> AvatarNormalization:
        """Decode only bounded, single-frame PNG/JPEG/WebP and discard all source metadata."""
        try:
            detected, dimensions = _avatar_header(content)
            codec = {"PNG": "zlib", "JPEG": "jpg", "WEBP": "webp"}[detected]
            if not features.check(codec) or not features.check("zlib"):
                return AvatarNormalization("unsupported_image_codec")
            with BytesIO(content) as source, Image.open(source) as probe:
                if probe.format != detected or probe.size != dimensions or getattr(probe, "n_frames", 1) != 1:
                    return AvatarNormalization("invalid_image")
                probe.verify()
            with BytesIO(content) as source, Image.open(source) as decoded:
                if decoded.format != detected or decoded.size != dimensions or getattr(decoded, "n_frames", 1) != 1:
                    return AvatarNormalization("invalid_image")
                decoded.load()
                mode = "RGBA" if "A" in decoded.getbands() or "transparency" in decoded.info else "RGB"
                with decoded.convert(mode) as converted, Image.frombytes(
                    mode, dimensions, converted.tobytes()
                ) as clean:
                    writer = _AvatarPNGWriter()
                    try:
                        clean.save(writer, format="PNG")
                        output = bytes(writer.data)
                    finally:
                        writer.close()
            return AvatarNormalization(
                "normalized", NormalizedAvatar(output, hashlib.sha3_256(output).hexdigest(), *dimensions)
            )
        except Exception:
            return AvatarNormalization("invalid_image")

    @staticmethod
    def store_reserved_avatar(
        reservation: AvatarReservation, normalized: NormalizedAvatar
    ) -> Literal["stored", "invalid_input", "storage_unknown"]:
        """Stage outside SQL transactions, after R commits its reservation and releases locks.

        Full readback proves bytes, not a cancellable storage deadline. Any ambiguous outcome
        remains unknown for R/T reconciliation; this helper never retries or deletes.
        """
        if not _avatar_valid_reservation(reservation) or not _avatar_valid_normalized(normalized):
            return "invalid_input"
        stream = None
        result: Literal["stored", "storage_unknown"] = "storage_unknown"
        try:
            storage.save(reservation.storage_key, normalized.content)
            stream = storage.load_stream(reservation.storage_key)
            if not callable(getattr(stream, "close", None)) or iter(stream) is not stream:
                return "storage_unknown"
            digest = hashlib.sha3_256()
            size = 0
            # Each nonempty bytes chunk consumes at least one of the <=2MiB budget.
            for chunk in stream:
                if type(chunk) is not bytes or not chunk or len(chunk) > len(normalized.content) - size:
                    break
                size += len(chunk)
                digest.update(chunk)
            else:
                if size == len(normalized.content) and digest.hexdigest() == normalized.sha3_256:
                    result = "stored"
        except Exception:
            result = "storage_unknown"
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    result = "storage_unknown"
        return result

    @staticmethod
    def insert_reserved_avatar(
        session: Session, reservation: AvatarReservation, normalized: NormalizedAvatar
    ) -> AvatarFileInsert:
        """Add/flush only, after R rechecks full authority, lease, membership and avatar baseline.

        R owns attachment, audit, commit and rollback, including rollback on insert_failed.
        No storage or signed URL operation occurs inside this caller-owned root transaction.
        """
        if not _avatar_valid_reservation(reservation) or not _avatar_valid_normalized(normalized):
            return AvatarFileInsert("invalid_input")
        if not isinstance(session, Session):
            return AvatarFileInsert("invalid_transaction")
        transaction = session.get_transaction()
        if (
            transaction is None
            or not transaction.is_active
            or not session.is_active
            or transaction.origin is not SessionTransactionOrigin.BEGIN
            or session.in_nested_transaction()
            or session.new
            or session.dirty
            or session.deleted
        ):
            return AvatarFileInsert("invalid_transaction")
        try:
            upload_file = UploadFile(
                tenant_id=reservation.tenant_id,
                storage_type=StorageType(dify_config.STORAGE_TYPE),
                key=reservation.storage_key,
                name="avatar.png",
                size=len(normalized.content),
                extension="png",
                mime_type="image/png",
                created_by_role=CreatorUserRole.ACCOUNT,
                created_by=reservation.account_id,
                created_at=naive_utc_now(),
                used=True,
                used_by=reservation.account_id,
                used_at=naive_utc_now(),
                hash=normalized.sha3_256,
                source_url="",
            )
            upload_file.id = reservation.file_id
            session.add(upload_file)
            session.flush([upload_file])
            return AvatarFileInsert("inserted", upload_file)
        except Exception:
            return AvatarFileInsert("insert_failed")

    def upload_file(
        self,
        *,
        filename: str,
        content: bytes,
        mimetype: str,
        user: Account | EndUser,
        tenant_id: str | None = None,
        source: Literal["datasets"] | None = None,
        source_url: str = "",
        default_file_size_limit: int | None = None,
    ) -> UploadFile:
        # get file extension
        extension = os.path.splitext(filename)[1].lstrip(".").lower()

        # Only reject path separators here. The original filename is stored as metadata,
        # while the storage key is UUID-based.
        if any(c in filename for c in ["/", "\\"]):
            raise ValueError("Filename contains invalid characters")

        if len(filename) > 200:
            filename = filename.split(".")[0][:200] + "." + extension

        # check if extension is in blacklist
        if extension and extension in dify_config.UPLOAD_FILE_EXTENSION_BLACKLIST:
            raise BlockedFileExtensionError(f"File extension '.{extension}' is not allowed for security reasons")

        if source == "datasets" and extension not in DOCUMENT_EXTENSIONS:
            raise UnsupportedFileTypeError()

        # get file size
        file_size = len(content)

        # check if the file size is exceeded
        if not FileService.is_file_size_within_limit(
            extension=extension,
            file_size=file_size,
            default_file_size_limit=default_file_size_limit,
        ):
            raise FileTooLargeError

        # generate file key
        file_uuid = str(uuid.uuid4())

        resource_tenant_id = tenant_id if tenant_id is not None else extract_tenant_id(user)

        file_key = "upload_files/" + (resource_tenant_id or "") + "/" + file_uuid + "." + extension

        # save file to storage
        storage.save(file_key, content)

        # save file to db
        upload_file = UploadFile(
            tenant_id=resource_tenant_id or "",
            storage_type=StorageType(dify_config.STORAGE_TYPE),
            key=file_key,
            name=filename,
            size=file_size,
            extension=extension,
            mime_type=mimetype,
            created_by_role=(CreatorUserRole.ACCOUNT if isinstance(user, Account) else CreatorUserRole.END_USER),
            created_by=user.id,
            created_at=naive_utc_now(),
            used=False,
            hash=hashlib.sha3_256(content).hexdigest(),
            source_url=source_url,
        )

        with self._session_maker(expire_on_commit=False) as session:
            session.add(upload_file)
            session.commit()

        if not upload_file.source_url:
            upload_file.source_url = file_helpers.get_signed_file_url(upload_file_id=upload_file.id)

        return upload_file

    @staticmethod
    def is_file_size_within_limit(
        *,
        extension: str,
        file_size: int,
        default_file_size_limit: int | None = None,
    ) -> bool:
        return file_size <= FileService.file_size_limit(
            extension=extension,
            default_file_size_limit=default_file_size_limit,
        )

    @staticmethod
    def file_size_limit(
        *,
        extension: str,
        default_file_size_limit: int | None = None,
    ) -> int:
        """Return the size an extension is allowed, in bytes."""

        if extension in IMAGE_EXTENSIONS:
            file_size_limit = dify_config.UPLOAD_IMAGE_FILE_SIZE_LIMIT
        elif extension in VIDEO_EXTENSIONS:
            file_size_limit = dify_config.UPLOAD_VIDEO_FILE_SIZE_LIMIT
        elif extension in AUDIO_EXTENSIONS:
            file_size_limit = dify_config.UPLOAD_AUDIO_FILE_SIZE_LIMIT
        else:
            # Context-specific uploads may override the default limit without changing media-specific limits.
            file_size_limit = (
                default_file_size_limit if default_file_size_limit is not None else dify_config.UPLOAD_FILE_SIZE_LIMIT
            )

        return file_size_limit * 1024 * 1024

    def get_file_base64(self, file_id: str) -> str:
        with self._session_maker(expire_on_commit=False) as session:
            upload_file = session.scalar(select(UploadFile).where(UploadFile.id == file_id).limit(1))
            if not upload_file:
                raise NotFound("File not found")
            upload_file_key = upload_file.key

        blob = storage.load_once(upload_file_key)
        return base64.b64encode(blob).decode()

    def get_file_presigned_url(self, *, file_id: str, tenant_id: str) -> str:
        """Generate a direct storage URL for a tenant-owned upload file."""
        with self._session_maker(expire_on_commit=False) as session:
            upload_file = self.get_upload_file_by_id(tenant_id, file_id, session=session)
            if upload_file is None:
                raise NotFound("File not found")

            file_key = upload_file.key
            content_type = upload_file.mime_type

        return storage.generate_presigned_url(
            file_key,
            expires_in=dify_config.FILES_ACCESS_TIMEOUT,
            content_type=content_type,
        )

    def get_icon_url(self, file_id: str, tenant_id: str) -> str:
        try:
            if dify_config.DEPLOYMENT_EDITION == DeploymentEdition.CLOUD and (
                StorageType(dify_config.STORAGE_TYPE) == StorageType.S3
            ):
                return self.get_file_presigned_url(file_id=file_id, tenant_id=tenant_id)
            with self._session_maker(expire_on_commit=False) as session:
                upload_file = self.get_upload_file_by_id(tenant_id, file_id, session=session)
            if upload_file is None:
                raise NotFound("File not found")
        except NotFound as exc:
            raise FileNotExistsError("File reference not found") from exc
        return file_helpers.get_signed_file_url(upload_file_id=file_id)

    def upload_text(self, text: str, text_name: str, user_id: str, tenant_id: str) -> UploadFile:
        if len(text_name) > 200:
            text_name = text_name[:200]
        # user uuid as file name
        file_uuid = str(uuid.uuid4())
        file_key = "upload_files/" + tenant_id + "/" + file_uuid + ".txt"
        content = text.encode("utf-8")

        # save file to storage
        storage.save(file_key, content)

        # save file to db
        upload_file = UploadFile(
            tenant_id=tenant_id,
            storage_type=StorageType(dify_config.STORAGE_TYPE),
            key=file_key,
            name=text_name,
            size=len(content),
            extension="txt",
            mime_type="text/plain",
            created_by=user_id,
            created_by_role=CreatorUserRole.ACCOUNT,
            created_at=naive_utc_now(),
            used=True,
            used_by=user_id,
            used_at=naive_utc_now(),
        )

        with self._session_maker(expire_on_commit=False) as session:
            session.add(upload_file)
            session.commit()

        return upload_file

    def get_file_preview(self, file_id: str, tenant_id: str) -> str:
        """
        Return a short text preview extracted from a document file.
        """
        with self._session_maker(expire_on_commit=False) as session:
            upload_file = session.scalar(
                select(UploadFile).where(UploadFile.id == file_id, UploadFile.tenant_id == tenant_id).limit(1)
            )

        if not upload_file:
            raise NotFound("File not found")

        # extract text from file
        extension = upload_file.extension
        if extension.lower() not in DOCUMENT_EXTENSIONS:
            raise UnsupportedFileTypeError()

        text = ExtractProcessor.load_from_upload_file(upload_file, return_text=True)
        return text[0:PREVIEW_WORDS_LIMIT] if text else ""

    def get_file_content(self, file_id: str) -> str:
        with self._session_maker(expire_on_commit=False) as session:
            upload_file: UploadFile | None = session.scalar(select(UploadFile).where(UploadFile.id == file_id).limit(1))

        if not upload_file:
            raise NotFound("File not found")
        content = storage.load(upload_file.key)

        return content.decode("utf-8")

    def delete_file(self, file_id: str):
        with self._session_maker() as session, session.begin():
            upload_file = session.scalar(select(UploadFile).where(UploadFile.id == file_id))

            if not upload_file:
                return
            storage.delete(upload_file.key)
            session.delete(upload_file)

    @staticmethod
    def get_upload_file_by_id(tenant_id: str, upload_file_id: str, *, session: Session) -> UploadFile | None:
        return session.scalar(
            select(UploadFile)
            .where(
                UploadFile.tenant_id == tenant_id,
                UploadFile.id == upload_file_id,
            )
            .limit(1)
        )

    @staticmethod
    def get_upload_files_by_ids(
        tenant_id: str, upload_file_ids: Sequence[str], *, session: Session
    ) -> dict[str, UploadFile]:
        """
        Fetch `UploadFile` rows for a tenant in a single batch query.

        This is a generic `UploadFile` lookup helper (not dataset/document specific), so it lives in `FileService`.
        """
        if not upload_file_ids:
            return {}

        # Normalize and deduplicate ids before using them in the IN clause.
        upload_file_id_list: list[str] = [str(upload_file_id) for upload_file_id in upload_file_ids]
        unique_upload_file_ids: list[str] = list(set(upload_file_id_list))

        # Fetch upload files in one query for efficient batch access.
        upload_files: Sequence[UploadFile] = session.scalars(
            select(UploadFile).where(
                UploadFile.tenant_id == tenant_id,
                UploadFile.id.in_(unique_upload_file_ids),
            )
        ).all()
        return {str(upload_file.id): upload_file for upload_file in upload_files}

    @staticmethod
    def _sanitize_zip_entry_name(name: str) -> str:
        """
        Sanitize a ZIP entry name to avoid path traversal and weird separators.

        We keep this conservative: the upload flow already rejects `/` and `\\`, but older rows (or imported data)
        could still contain unsafe names.
        """
        # Drop any directory components and prevent empty names.
        base = os.path.basename(name).strip() or "file"

        # ZIP uses forward slashes as separators; remove any residual separator characters.
        return base.replace("/", "_").replace("\\", "_")

    @staticmethod
    def _dedupe_zip_entry_name(original_name: str, used_names: set[str]) -> str:
        """
        Return a unique ZIP entry name, inserting suffixes before the extension.
        """
        # Keep the original name when it's not already used.
        if original_name not in used_names:
            return original_name

        # Insert suffixes before the extension (e.g., "doc.txt" -> "doc (1).txt").
        stem, extension = os.path.splitext(original_name)
        suffix = 1
        while True:
            candidate = f"{stem} ({suffix}){extension}"
            if candidate not in used_names:
                return candidate
            suffix += 1

    @staticmethod
    @contextmanager
    def build_upload_files_zip_tempfile(
        *,
        upload_files: Sequence[UploadFile],
    ) -> Generator[str, None, None]:  # Changed from Iterator[str]
        """
        Build a ZIP from `UploadFile`s and yield a tempfile path.

        We yield a path (rather than an open file handle) to avoid "read of closed file" issues when Flask/Werkzeug
        streams responses. The caller is expected to keep this context open until the response is fully sent, then
        close it (e.g., via `response.call_on_close(...)`) to delete the tempfile.
        """
        used_names: set[str] = set()

        # Build a ZIP in a temp file and keep it on disk until the caller finishes streaming it.
        tmp_path: str | None = None
        try:
            with NamedTemporaryFile(mode="w+b", suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name
                with ZipFile(tmp, mode="w", compression=ZIP_DEFLATED) as zf:
                    for upload_file in upload_files:
                        # Ensure the entry name is safe and unique.
                        safe_name = FileService._sanitize_zip_entry_name(upload_file.name)
                        arcname = FileService._dedupe_zip_entry_name(safe_name, used_names)
                        used_names.add(arcname)

                        # Stream file bytes from storage into the ZIP entry.
                        with zf.open(arcname, "w") as entry:
                            for chunk in storage.load(upload_file.key, stream=True):
                                entry.write(chunk)

                # Flush so `send_file(path, ...)` can re-open it safely on all platforms.
                tmp.flush()

            assert tmp_path is not None
            yield tmp_path
        finally:
            # Remove the temp file when the context is closed (typically after the response finishes streaming).
            if tmp_path is not None:
                with suppress(FileNotFoundError):
                    os.remove(tmp_path)
