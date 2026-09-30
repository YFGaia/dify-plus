import json
from decimal import Decimal

from app_factory import create_migrations_app
from extensions.ext_database import db
from models.account import Account
from models.account_money_extend import AccountMoneyExtend
from models.model import App
from services.system_manage_extend import QuotaManageService

app = create_migrations_app()
with app.app_context():
    assert db.session.query(Account).count() == 0, "Dedicated database must have no accounts"
    assert db.session.query(App).count() == 0, "Dedicated database must have no apps"
    assert db.session.query(AccountMoneyExtend).count() == 0, "Dedicated database must have no balances"
    engine = db.session.get_bind().dialect.name
    account = Account(name="Disposable quota probe", email="quota-upsert@example.invalid")
    db.session.add(account)
    db.session.flush()
    account_id = account.id
    db.session.commit()
    result = {"engine": engine, "operation": "actual_service_set_user_quota"}
    try:
        QuotaManageService.set_user_quota(account_id, 12.3456789)
        db.session.expire_all()
        row = db.session.query(AccountMoneyExtend).filter_by(account_id=account_id).one()
        assert row.total_quota == Decimal("12.3456789")
        assert row.used_quota == 0
        result.update({"insert": "passed", "precision_readback": "passed"})
        row.used_quota = Decimal("0.1234567")
        db.session.commit()
        QuotaManageService.set_user_quota(account_id, 23.4567891)
        db.session.expire_all()
        row = db.session.query(AccountMoneyExtend).filter_by(account_id=account_id).one()
        assert row.total_quota == Decimal("23.4567891")
        assert row.used_quota == Decimal("0.1234567")
        result.update({"update": "passed", "preserves_used_quota": True,
                       "unique_balance": db.session.query(AccountMoneyExtend).filter_by(account_id=account_id).count() == 1})
    except Exception as error:
        result.update({"operation_result": "failed", "error_type": type(error).__name__,
                       "postgresql_conflict_on_mysql": "OnConflictDoUpdate" in str(error),
                       "first_failure": str(error) if type(error).__name__ == "UnsupportedCompilationError" else type(error).__name__})
    finally:
        db.session.rollback()
        db.session.query(AccountMoneyExtend).filter_by(account_id=account_id).delete()
        db.session.query(Account).filter_by(id=account_id).delete()
        db.session.commit()
        assert db.session.query(Account).count() == 0
        assert db.session.query(AccountMoneyExtend).count() == 0
        result["cleanup_restored_empty_tables"] = True
    print(json.dumps(result), flush=True)
    if result.get("operation_result") == "failed":
        raise SystemExit(1)
