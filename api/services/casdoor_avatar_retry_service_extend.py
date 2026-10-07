"""Authenticated manager navigation and one durable pre-storage retry, no broker I/O."""

import time
from datetime import UTC, datetime
from uuid import UUID

from core.casdoor.auth_transactions import AuthTransactionError
from core.casdoor.request_safety import RequestAction
from models.account import Account
from repositories.casdoor_avatar_repository_extend import (
    CasdoorAvatarConflict,
    CasdoorAvatarRepository,
    _worker_state,
)

from services.casdoor_diagnostic_service_extend import CasdoorDiagnosticService


class CasdoorAvatarRetryService(CasdoorDiagnosticService):
    def __init__(self, *, now=lambda: datetime.now(UTC), **kwargs):
        super().__init__(**kwargs)
        self._now = now

    def _retry_repository(self, session):
        if (
            self._settings.RBAC_ENABLED is not False
            or self._configuration_service._rbac_enabled is not False
        ):
            raise AuthTransactionError("local_mode_required")
        return CasdoorAvatarRepository(
            session,
            configuration_repository=self._configuration_service._repository(session),
        )

    def _retry_final_source(self, session, source, refresh_token, client, deadline):
        fresh = session.get(Account, str(source.account_id), populate_existing=True)
        self._configuration_service.require_management(fresh)
        self._final_refresh(source, refresh_token, client)
        if time.monotonic() >= deadline:
            raise AuthTransactionError("deadline")

    def list_retry_targets(
        self, account, *, after=None, limit=20, refresh_token, server_ip
    ):
        if (
            (after is not None and type(after) is not UUID)
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise AuthTransactionError()

        def operation(client, deadline):
            source = self._source(account, refresh_token, client)
            with self._session_factory() as session, session.begin():
                ids, more = self._retry_repository(session).list_retry_target_ids(
                    after, limit
                )
            targets = []
            for intent_id in ids:
                try:
                    with self._session_factory() as session, session.begin():
                        target = self._retry_repository(session).inspect_retry_target(
                            intent_id, now=self._now()
                        )
                        self._retry_final_source(
                            session, source, refresh_token, client, deadline
                        )
                        targets.append(target)
                except CasdoorAvatarConflict:
                    continue
            with self._session_factory() as session, session.begin():
                self._retry_final_source(
                    session, source, refresh_token, client, deadline
                )
            return {
                "targets": targets,
                "next_after": str(ids[-1]) if ids and more else None,
                "has_more": more,
            }

        try:
            return self._run(RequestAction.DIAGNOSTIC, server_ip, operation)
        except CasdoorAvatarConflict:
            raise AuthTransactionError("retry_unavailable") from None

    def retry_avatar(self, account, *, intent_id, refresh_token, server_ip):
        if type(intent_id) is not UUID:
            raise AuthTransactionError()

        def operation(client, deadline):
            source = self._source(account, refresh_token, client)
            # First-SQL reader is factual; the later parent-locked writer rechecks every scalar.
            with self._session_factory() as session, session.begin():
                repository = self._retry_repository(session)
                record = repository.recover_pre_storage_failure(
                    intent_id, now=self._now()
                )
            with self._session_factory() as session, session.begin():
                repository = self._retry_repository(session)
                result = repository.prepare_confirmed_pre_storage_retry(
                    intent_id, actor_account_id=source.account_id, now=self._now()
                )
                if record is not None:
                    row = repository._worker_root(intent_id, lock=False)["intent"]
                    repository._read_retry_lineage(row, _worker_state(row))
                self._retry_final_source(
                    session, source, refresh_token, client, deadline
                )
            return result

        try:
            return self._run(RequestAction.DIAGNOSTIC, server_ip, operation)
        except CasdoorAvatarConflict:
            raise AuthTransactionError("retry_unavailable") from None
