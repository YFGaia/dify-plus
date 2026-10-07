"""Bounded initial and explicitly approved retry navigation; original consumer claims."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
from sqlalchemy.orm import Session, sessionmaker

from services.casdoor_configuration_service_extend import CasdoorConfigurationService


class _DispatchInvocation:
    def __init__(self):
        self.signal = None

    def latch(self, error):
        if self.signal is None:
            if isinstance(error, KeyboardInterrupt):
                self.signal = "interrupt"
            elif isinstance(error, SystemExit):
                self.signal = "exit"


class CasdoorAvatarDispatchService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        configuration_service: CasdoorConfigurationService,
        now: Callable[[], datetime],
        publish: Callable[[str], None],
    ):
        self._session_factory = session_factory
        self._configuration_service = configuration_service
        self._now = now
        self._publish = publish

    def _root(self, invocation, operation):
        """Return a value only after acknowledged commit AND successful close."""
        session = repository = value = None
        acknowledged = False
        clean = True
        try:
            session = self._session_factory()
            session.begin()
            repository = CasdoorAvatarRepository(
                session, configuration_repository=self._configuration_service._repository(session)
            )
            value = operation(repository)
            session.commit()
            acknowledged = not session.in_transaction()
        except BaseException as error:
            invocation.latch(error)
        finally:
            if session is not None:
                try:
                    session.rollback()
                except BaseException as error:
                    invocation.latch(error)
                    clean = False
                try:
                    session.close()
                    clean = clean and not session.in_transaction()
                except BaseException as error:
                    invocation.latch(error)
                    clean = False
            session = repository = None
        if not acknowledged or not clean or invocation.signal is not None:
            return False, None
        return True, value

    def _dispatch_initial_pending(self) -> dict[str, str]:
        """A bounded scan is not a delivery receipt or a wall-clock deadline."""
        invocation = _DispatchInvocation()
        result = {"code": "unknown"}
        page = candidate = intent_id = None
        try:
            accepted, page = self._root(
                invocation, lambda repo: repo._scan_initial_dispatch_page(limit=100, now=self._now())
            )
            if accepted:
                result = {"code": "scanned"}
                for intent_id in page.intent_ids:
                    accepted, candidate = self._root(
                        invocation, lambda repo, key=intent_id: repo._initial_dispatch_candidate(key, now=self._now())
                    )
                    if not accepted:
                        result = {"code": "unknown"}
                        break
                    if type(candidate) is UUID:
                        try:
                            self._publish(str(candidate))
                        except Exception:
                            # An ACK may be lost after enqueue; never fabricate a sent receipt.
                            result = {"code": "unknown"}
        except BaseException as error:
            invocation.latch(error)
            result = {"code": "unknown"}
        if invocation.signal is not None:
            signal = invocation.signal
            page = candidate = intent_id = invocation = self = None
            if signal == "interrupt":
                raise KeyboardInterrupt("avatar dispatch interrupted")
            raise SystemExit(1)
        return result
