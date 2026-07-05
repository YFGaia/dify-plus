"""extend: workflow 节点计费 Celery 任务体单测。

1.14.2 合并回归教训：任务体的 node_type 判断（graphon 外部化后 NodeType 变 type alias）
在 import 冒烟与 persistence 层单测（.delay 被 mock）下均不可见，必须直接执行任务体覆盖。
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import tasks.extend.update_account_money_when_workflow_node_execution_created_extend as task_module
from models.enums import CreatorUserRole


def _run_task(payload: dict) -> None:
    """直接调用被 @shared_task 包裹的任务函数体（run 属性即原函数）。"""
    task_module.update_account_money_when_workflow_node_execution_created_extend.run(payload)


def _llm_node_payload(**overrides) -> dict:
    payload = {
        "id": "node-exec-1",
        "node_type": "llm",
        "outputs": {"usage": {"total_price": "0.0012", "currency": "USD"}},
        "created_by": "account-1",
        "created_by_role": CreatorUserRole.ACCOUNT.value,
        "workflow_run_id": "run-1",
    }
    payload.update(overrides)
    return payload


class TestUpdateAccountMoneyTaskBody:
    def test_non_llm_node_returns_without_db_access(self):
        with patch.object(task_module, "db") as mock_db:
            _run_task(_llm_node_payload(node_type="code"))
        mock_db.session.query.assert_not_called()

    def test_empty_payload_returns(self):
        with patch.object(task_module, "db") as mock_db:
            _run_task({})
        mock_db.session.query.assert_not_called()

    def test_zero_price_returns_without_db_access(self):
        with patch.object(task_module, "db") as mock_db:
            _run_task(_llm_node_payload(outputs={"usage": {"total_price": "0", "currency": "USD"}}))
        mock_db.session.query.assert_not_called()

    def test_llm_node_deducts_account_and_token_quota(self):
        with (
            patch.object(task_module, "db") as mock_db,
            patch.object(task_module, "_resolve_payer_id", return_value="account-1") as mock_resolver,
        ):
            account_update_chain = MagicMock()
            account_update_chain.update.return_value = 1  # 账号额度行存在，原子更新成功

            token_join_chain = MagicMock()
            token_join_chain.first.return_value = MagicMock(app_token_id="token-1")

            token_update_chain = MagicMock()

            def query_side_effect(model):
                if model is task_module.AccountMoneyExtend:
                    return account_update_chain
                if model is task_module.ApiTokenMoneyExtend:
                    return token_update_chain
                return token_join_chain

            mock_db.session.query.side_effect = lambda *args: (
                query_side_effect(args[0]) if len(args) == 1 else token_join_chain
            )
            account_update_chain.filter.return_value = account_update_chain
            token_join_chain.filter.return_value = token_join_chain
            token_join_chain.order_by.return_value = token_join_chain
            token_update_chain.filter.return_value = token_update_chain

            _run_task(_llm_node_payload())

        mock_resolver.assert_called_once_with("account-1", CreatorUserRole.ACCOUNT.value)
        account_update_chain.update.assert_called_once()
        token_update_chain.update.assert_called_once()
        mock_db.session.commit.assert_called_once()

    def test_currency_conversion_for_non_usd(self):
        with (
            patch.object(task_module, "db") as mock_db,
            patch.object(task_module, "_resolve_payer_id", return_value="account-1"),
        ):
            chain = MagicMock()
            chain.filter.return_value = chain
            chain.order_by.return_value = chain
            chain.update.return_value = 1
            chain.first.return_value = None
            mock_db.session.query.return_value = chain

            _run_task(_llm_node_payload(outputs={"usage": {"total_price": "7.26", "currency": "RMB"}}))

            # 非 USD 需按 RMB_TO_USD_RATE 折算：update 的入参中包含折算后的 price
            update_kwargs = chain.update.call_args.args[0]
            deducted = update_kwargs["used_quota"]
            expected = float(Decimal("7.26")) / float(task_module.dify_config.RMB_TO_USD_RATE)
            # used_quota 是 SQLAlchemy 表达式 AccountMoneyExtend.used_quota + price，取右操作数核对
            assert float(deducted.right.value) == expected
        mock_db.session.commit.assert_called_once()
