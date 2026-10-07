import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from events.app_event import app_was_created
from models.model_extend import AppStatisticsExtend


@app_was_created.connect
def handle(sender, *, session: Session, **_kwargs) -> None:
    """Initialize usage ranking in the caller's signal session without committing.

    Preserve each creation owner's commit/signal ordering; this handler does not
    make the upstream commit-before-signal path atomic with its extension rows.
    """
    if session.scalar(select(AppStatisticsExtend.id).where(AppStatisticsExtend.app_id == sender.id).limit(1)):
        return
    session.add(AppStatisticsExtend(id=str(uuid.uuid4()), app_id=sender.id, number=0))
    session.flush()
