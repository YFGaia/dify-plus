"""Registered initial-only avatar entry; publication and recovery remain separate."""

from uuid import UUID

from celery import shared_task


@shared_task(
    name="tasks.casdoor_avatar_initial_task_extend.consume_casdoor_avatar_initial_task",
    queue="extend_low",
    ignore_result=True,
    autoretry_for=(),
    max_retries=0,
    acks_late=False,
    reject_on_worker_lost=False,
)
def consume_casdoor_avatar_initial_task(intent_id: str) -> dict[str, str]:
    if type(intent_id) is not str or len(intent_id) != 36:
        return {"code": "unknown"}
    try:
        parsed = UUID(intent_id)
    except ValueError:
        return {"code": "unknown"}
    if str(parsed) != intent_id:
        return {"code": "unknown"}

    consumer = None
    signal = None
    try:
        from extensions.ext_application_services import application_services

        consumer = application_services().casdoor_avatar_consumer
    except KeyboardInterrupt:
        signal = "interrupt"
    except SystemExit:
        signal = "exit"
    except Exception:
        return {"code": "unknown"}
    if signal is not None:
        consumer = parsed = intent_id = application_services = None
        if signal == "interrupt":
            raise KeyboardInterrupt("avatar task interrupted")
        raise SystemExit(1)

    # The accepted consumer owns transaction, I/O, and sanitized shutdown semantics.
    result = consumer._consume_initial(parsed)
    if result.code == "applied" and type(result.file_id) is UUID:
        return {"code": "applied", "file_id": str(result.file_id)}
    return {"code": "unknown"}
