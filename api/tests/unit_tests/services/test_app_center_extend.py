"""App-center visibility uses installations/publication, not usage-history existence."""

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from models.model import App, AppMode, AppModelConfig, IconType, InstalledApp, Tag, TagBinding
from models.model_extend import AppStatisticsExtend
from models.workflow import Workflow, WorkflowKind, WorkflowType
from services import recommended_app_service_extend as module


@pytest.fixture
def center_db(sqlite_session: Session, monkeypatch: pytest.MonkeyPatch) -> Session:
    monkeypatch.setattr(module, "db", SimpleNamespace(session=sqlite_session))
    return sqlite_session


def persist_app(
    session: Session,
    app_id: str,
    *,
    mode: AppMode = AppMode.CHAT,
    tenant_id: str = "tenant-a",
    published: bool = True,
    installed: bool = True,
) -> App:
    app = App(
        id=app_id,
        tenant_id=tenant_id,
        name=app_id,
        description="Description",
        mode=mode,
        icon_type=IconType.EMOJI,
        icon="robot",
        icon_background="#ffffff",
        enable_site=True,
        enable_api=True,
        max_active_requests=0,
    )
    session.add(app)
    if published and mode in {AppMode.WORKFLOW, AppMode.ADVANCED_CHAT}:
        workflow = Workflow(
            id=f"workflow-{app_id}",
            tenant_id=tenant_id,
            app_id=app_id,
            type=WorkflowType.WORKFLOW,
            kind=WorkflowKind.STANDARD,
            version="1",
            graph='{"nodes":[],"edges":[]}',
            features="{}",
            created_by="user-a",
            environment_variables=[],
            conversation_variables=[],
            rag_pipeline_variables=[],
        )
        session.add(workflow)
        app.workflow_id = workflow.id
    elif published:
        config = AppModelConfig(app_id=app_id, pre_prompt="Published prompt")
        config.id = f"config-{app_id}"
        session.add(config)
        app.app_model_config_id = config.id
    if installed:
        installation = InstalledApp(tenant_id=tenant_id, app_id=app_id, app_owner_tenant_id=tenant_id)
        installation.id = f"installed-{app_id}"
        session.add(installation)
    session.commit()
    return app


@pytest.mark.parametrize("mode", [AppMode.CHAT, AppMode.ADVANCED_CHAT, AppMode.WORKFLOW])
def test_published_installation_visible_without_statistics(center_db: Session, mode: AppMode) -> None:
    persist_app(center_db, "first-use", mode=mode)
    result = module.RecommendedAppService.installed_app_list("tenant-a")
    assert [(row["app_id"], row["installed_id"], row["category"]) for row in result["recommended_apps"]] == [
        ("first-use", "installed-first-use", "未分类")
    ]
    assert center_db.query(AppStatisticsExtend).count() == 0  # Read path never repairs business data.


def test_statistics_sorting_keeps_missing_zero_and_duplicate_history(center_db: Session) -> None:
    for app_id in ["a-missing", "b-zero", "c-used"]:
        persist_app(center_db, app_id)
    center_db.add_all(
        [
            AppStatisticsExtend(id="s-zero", app_id="b-zero", number=0),
            AppStatisticsExtend(id="s-old", app_id="c-used", number=3),
            AppStatisticsExtend(id="s-new", app_id="c-used", number=7),
        ]
    )
    center_db.commit()
    result = module.RecommendedAppService.installed_app_list("tenant-a")
    assert [row["app_id"] for row in result["recommended_apps"]] == ["c-used", "a-missing", "b-zero"]
    assert [row.number for row in center_db.query(AppStatisticsExtend).order_by(AppStatisticsExtend.id)] == [7, 3, 0]


@pytest.mark.parametrize("mode", [AppMode.CHAT, AppMode.ADVANCED_CHAT, AppMode.WORKFLOW, AppMode.AGENT])
def test_unpublished_and_agent_apps_are_not_installed_webapps(center_db: Session, mode: AppMode) -> None:
    persist_app(center_db, "unavailable", mode=mode, published=mode == AppMode.AGENT)
    assert module.RecommendedAppService.installed_app_list("tenant-a")["recommended_apps"] == []


def test_missing_publish_target_is_unavailable(center_db: Session) -> None:
    app = persist_app(center_db, "deleted-target", published=False)
    app.app_model_config_id = "no-config"
    center_db.commit()
    assert module.RecommendedAppService.installed_app_list("tenant-a")["recommended_apps"] == []


def test_workspace_and_installation_owner_scope(center_db: Session) -> None:
    persist_app(center_db, "visible")
    persist_app(center_db, "other-workspace", tenant_id="tenant-b")
    persist_app(center_db, "not-installed", installed=False)
    persist_app(center_db, "bad-owner")
    center_db.query(InstalledApp).filter_by(app_id="bad-owner").one().app_owner_tenant_id = "tenant-b"
    foreign_installation = InstalledApp(tenant_id="tenant-b", app_id="visible", app_owner_tenant_id="tenant-a")
    foreign_installation.id = "foreign-install"
    center_db.add(foreign_installation)
    center_db.commit()
    result = module.RecommendedAppService.installed_app_list("tenant-a")
    assert [(row["app_id"], row["installed_id"]) for row in result["recommended_apps"]] == [
        ("visible", "installed-visible")
    ]


def test_session_tags_and_prompt_description_are_preserved(center_db: Session) -> None:
    app = persist_app(center_db, "tagged")
    app.description = ""
    for tag_id, tenant_id, name in [("tag-a", "tenant-a", "Writing"), ("tag-b", "tenant-b", "Private")]:
        tag = Tag(tenant_id=tenant_id, type="app", name=name, created_by="user-a")
        tag.id = tag_id
        binding = TagBinding(tenant_id=tenant_id, tag_id=tag_id, target_id=app.id, created_by="user-a")
        binding.id = f"binding-{tag_id}"
        center_db.add_all([tag, binding])
    center_db.commit()
    result = module.RecommendedAppService.installed_app_list("tenant-a")
    assert result["categories"] == ["Writing", "未分类"]
    assert [(row["category"], row["description"]) for row in result["recommended_apps"]] == [
        ("Writing", "Published prompt")
    ]
