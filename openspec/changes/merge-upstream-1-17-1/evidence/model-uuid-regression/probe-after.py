import json
from uuid import UUID, uuid4

from app_factory import create_migrations_app
from extensions.ext_database import db
from models.account import Account
from models.model import App
from models.model_extend import AppExtend, AppStatisticsExtend, EndUserAccountJoinsExtend, MessageContextExtend

app = create_migrations_app()
results = []
with app.app_context():
    engine = db.session.get_bind().dialect.name
    assert engine in {"postgresql", "mysql"}
    assert db.session.query(Account).count() == 0, "Dedicated migration-test database must have no user accounts"
    assert db.session.query(App).count() == 0, "Dedicated migration-test database must have no user applications"
    for cls, make in [
        (AppStatisticsExtend, lambda: AppStatisticsExtend(app_id=str(uuid4()), number=0)),
        (EndUserAccountJoinsExtend, lambda: EndUserAccountJoinsExtend(
            end_user_id=str(uuid4()), account_id=str(uuid4()), app_id=str(uuid4()))),
        (AppExtend, lambda: AppExtend(app_id=str(uuid4()), retention_number=2, webapp_auth_enabled=True)),
        (MessageContextExtend, lambda: MessageContextExtend(conversation_id=str(uuid4()), message_id=str(uuid4()))),
    ]:
        before = db.session.query(cls).count()
        result = {"engine": engine, "model": cls.__name__, "omitted_id": True, "before_rows": before}
        try:
            row = make()
            assert row.id is None
            db.session.add(row)
            db.session.flush()
            identifier = row.id
            assert identifier and UUID(identifier).version == 4
            db.session.expunge(row)
            read = db.session.get(cls, identifier)
            assert read is not None
            result.update({"flush": "passed", "readback": "passed", "uuidv4": True})
        except Exception as error:
            result.update({"flush": "failed", "error_type": type(error).__name__, "null_identity": "NULL identity key" in str(error)})
        finally:
            db.session.rollback()
        after = db.session.query(cls).count()
        result.update({"after_rollback_rows": after, "rollback_preserved_count": before == after})
        results.append(result)
        print(json.dumps(result), flush=True)
    assert all(r["rollback_preserved_count"] for r in results)
    print(json.dumps({"engine": engine, "overall": "passed" if all(r["flush"] == "passed" for r in results) else "failed"}), flush=True)
    if any(r["flush"] != "passed" for r in results):
        raise SystemExit(1)
with app.app_context():
    for cls, values in [
        (AppStatisticsExtend, {"app_id": str(uuid4()), "number": 5}),
        (EndUserAccountJoinsExtend, {"end_user_id": str(uuid4()), "account_id": str(uuid4()), "app_id": str(uuid4())}),
        (AppExtend, {"app_id": str(uuid4()), "retention_number": 3, "webapp_auth_enabled": False}),
        (MessageContextExtend, {"conversation_id": str(uuid4()), "message_id": str(uuid4())}),
    ]:
        before = db.session.query(cls).count()
        identifier = str(uuid4())
        try:
            row = cls(id=identifier, **values)
            db.session.add(row)
            db.session.flush()
            assert row.id == identifier
            db.session.expunge(row)
            read = db.session.get(cls, identifier)
            assert read is not None and all(getattr(read, key) == value for key, value in values.items())
        finally:
            db.session.rollback()
        after = db.session.query(cls).count()
        assert after == before
        print(json.dumps({"engine": engine, "model": cls.__name__, "explicit_id": True, "id_preserved": True,
                          "payload_readback": "passed", "before_rows": before, "after_rollback_rows": after,
                          "rollback_preserved_count": True}), flush=True)
