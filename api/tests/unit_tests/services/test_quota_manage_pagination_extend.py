"""Quota edits must not move equal-usage accounts between pagination results."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from models.account import Account
from models.account_money_extend import AccountMoneyExtend
from services import system_manage_extend
from services.system_manage_extend import QuotaManageService


@pytest.fixture
def quota_db(sqlite_session: Session, monkeypatch: pytest.MonkeyPatch) -> Session:
    monkeypatch.setattr(system_manage_extend, "db", SimpleNamespace(session=sqlite_session))
    # Reverse insertion order makes physical row order different from the account tie order.
    for index in reversed(range(12)):
        account_id = f"00000000-0000-0000-0000-{index:012d}"
        account = Account(name=f"Account {index:02d}", email=f"quota-{index:02d}@example.invalid")
        account.id = account_id
        sqlite_session.add(account)
        sqlite_session.add(
            AccountMoneyExtend(
                account_id=account_id,
                total_quota=Decimal("15.0000000"),
                used_quota=Decimal("0.1234567") if index == 11 else Decimal(0),
            )
        )
    sqlite_session.commit()
    return sqlite_session


def pages() -> list[dict]:
    return [QuotaManageService.get_quota_list(page=page, page_size=10) for page in (1, 2)]


def test_equal_usage_pages_remain_stable_after_total_quota_edit(quota_db: Session) -> None:
    before = pages()
    expected = [f"00000000-0000-0000-0000-{index:012d}" for index in [11, *range(11)]]
    assert [row["account_id"] for page in before for row in page["list"]] == expected
    assert [len(page["list"]) for page in before] == [10, 2]
    assert [row["ranking"] for page in before for row in page["list"]] == list(range(1, 13))

    target_id = expected[9]
    target = quota_db.query(AccountMoneyExtend).filter_by(account_id=target_id).one()
    target.total_quota = Decimal("23.1234567")
    quota_db.commit()
    after = pages()

    assert [[row["account_id"] for row in page["list"]] for page in after] == [
        [row["account_id"] for row in page["list"]] for page in before
    ]
    edited = after[0]["list"][9]
    assert edited["total_quota"] == 23.1234567
    assert edited["used_quota"] == 0
    assert after[0]["list"][0]["used_quota"] == 0.1234567
    assert {page["total"] for page in after} == {12}


@pytest.mark.usefixtures("quota_db")
def test_filtered_quota_list_keeps_usage_order_and_unique_accounts() -> None:
    result = QuotaManageService.get_quota_list(page=1, page_size=30, keyword="quota-0")
    assert result["total"] == 10
    assert [row["email"] for row in result["list"]] == [f"quota-{index:02d}@example.invalid" for index in range(10)]
    assert QuotaManageService.get_quota_list(page=1, page_size=10, keyword="absent")["total"] == 0
