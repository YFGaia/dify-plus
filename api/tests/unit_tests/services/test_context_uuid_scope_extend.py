"""Legacy text marker IDs must receive text identities from the owner subquery."""

from uuid import uuid4

import pytest
from sqlalchemy import String
from sqlalchemy.dialects import mysql, postgresql

from models.model_extend import MessageContextExtend
from services.recommended_app_service_extend import RecommendedAppService


@pytest.mark.parametrize(
    ("dialect", "text_identity_sql"),
    [
        (postgresql.dialect(), "CAST(conversations.id AS VARCHAR(36))"),
        (mysql.dialect(), "CAST(conversations.id AS CHAR(36))"),
    ],
)
def test_context_owner_subquery_matches_legacy_marker_text_type(dialect, text_identity_sql: str):
    """PostgreSQL cannot compare the fork varchar conversation ID with an upstream UUID."""
    owner_query = RecommendedAppService._context_conversations(
        tenant_id=str(uuid4()), app_id=str(uuid4()), conversation_id=str(uuid4())
    )
    selected_identity = next(iter(owner_query.selected_columns))
    assert isinstance(MessageContextExtend.conversation_id.type, String)
    assert isinstance(selected_identity.type, String)
    assert selected_identity.type.length == MessageContextExtend.conversation_id.type.length == 36
    assert text_identity_sql in str(owner_query.compile(dialect=dialect))
