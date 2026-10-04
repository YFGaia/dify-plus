"""Independent collision check across the actual file writer and account gateway."""

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker
from test_casdoor_avatar_file_extend import MemoryStorage, normalized, reservation

from extensions.ext_storage import storage
from extensions.storage.storage_type import StorageType
from models.enums import CreatorUserRole
from models.model import UploadFile
from services.account_avatar_file_gateway import SQLAlchemyAccountAvatarFileGateway
from services.file_service import FileService


@pytest.fixture
def db_fixture(sqlite_engine):
    UploadFile.__table__.create(sqlite_engine, checkfirst=True)
    factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    with factory() as session:
        yield session, factory


def test_foreign_file_uuid_collision_rolls_back_without_changing_owner(db_fixture, monkeypatch):
    session, factory = db_fixture
    reserved = reservation()
    image = normalized()
    foreign_account_id = str(uuid4())
    foreign_key = f"foreign/{uuid4()}.png"
    foreign_row = UploadFile(
        tenant_id=reserved.tenant_id,
        storage_type=StorageType.LOCAL,
        key=foreign_key,
        name="original.png",
        size=19,
        extension="png",
        mime_type="image/png",
        created_by_role=CreatorUserRole.ACCOUNT,
        created_by=foreign_account_id,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        used=True,
        used_by=foreign_account_id,
        used_at=datetime.now(UTC).replace(tzinfo=None),
        hash="foreign-original-hash",
        source_url="",
    )
    foreign_row.id = reserved.file_id
    with session.begin():
        session.add(foreign_row)
    session.expunge(foreign_row)

    adapter = MemoryStorage(session)
    monkeypatch.setattr(storage, "storage_runner", adapter, raising=False)
    assert FileService.store_reserved_avatar(reserved, image) == "stored"
    assert adapter.objects == {reserved.storage_key: image.content}

    commits = []
    event.listen(session, "after_commit", lambda _: commits.append(True))
    session.begin()
    result = FileService.insert_reserved_avatar(session, reserved, image)
    assert result.code == "insert_failed"
    assert not session.is_active and session.in_transaction()
    assert commits == []
    session.rollback()
    assert commits == []

    with factory() as reader:
        persisted = reader.scalar(select(UploadFile).where(UploadFile.id == reserved.file_id))
        assert persisted is not None
        assert (
            persisted.id,
            persisted.key,
            persisted.name,
            persisted.size,
            persisted.extension,
            persisted.mime_type,
            persisted.created_by_role,
            persisted.created_by,
            persisted.used,
            persisted.used_by,
            persisted.hash,
            persisted.source_url,
        ) == (
            reserved.file_id,
            foreign_key,
            "original.png",
            19,
            "png",
            "image/png",
            CreatorUserRole.ACCOUNT,
            foreign_account_id,
            True,
            foreign_account_id,
            "foreign-original-hash",
            "",
        )

    gateway = SQLAlchemyAccountAvatarFileGateway(session_factory=factory)
    with patch(
        "services.account_avatar_file_gateway.file_helpers.get_signed_file_url", return_value="signed-local"
    ) as signer:
        assert gateway.get_owned_signed_url(account_id=reserved.account_id, upload_file_id=reserved.file_id) is None
        assert (
            gateway.get_owned_signed_url(account_id=foreign_account_id, upload_file_id=reserved.file_id)
            == "signed-local"
        )
    signer.assert_called_once_with(upload_file_id=reserved.file_id)
