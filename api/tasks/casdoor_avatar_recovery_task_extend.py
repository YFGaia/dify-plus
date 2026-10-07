"""Default-off initial and explicitly approved retry navigation in the FlaskTask."""

from celery import shared_task
from configs import dify_config


@shared_task(
    name="tasks.casdoor_avatar_recovery_task_extend.dispatch_casdoor_avatar_initial_pending",
    queue="extend_low",
    ignore_result=True,
    autoretry_for=(),
    max_retries=0,
    acks_late=False,
    reject_on_worker_lost=False,
)
def dispatch_casdoor_avatar_initial_pending() -> dict[str, str]:
    if not dify_config.ENABLE_CASDOOR_AVATAR_INITIAL_RECOVERY_TASK:
        return {"code": "disabled"}
    service = None
    signal = None
    try:
        from extensions.ext_application_services import application_services

        service = application_services().casdoor_avatar_dispatch
    except KeyboardInterrupt:
        signal = "interrupt"
    except SystemExit:
        signal = "exit"
    except Exception:
        return {"code": "unknown"}
    if signal is not None:
        service = application_services = None
        if signal == "interrupt":
            raise KeyboardInterrupt("avatar dispatch interrupted")
        raise SystemExit(1)
    return service._dispatch_initial_pending()
