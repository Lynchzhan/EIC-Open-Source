"""HTTP tests explicitly migrate their synthetic database before app startup."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture(scope="module", autouse=True)
def migrated_http_database(request):
    http_modules = {
        "test_catalog_messages_and_logs", "test_knowledge_retrieval_ops",
        "test_m3_m5_features", "test_personal_isolation", "test_rbac_and_organization",
        "test_skill_type_filter", "test_smoke", "test_user_profile_register_and_delete", "test_org_message_safety",
        "test_upload_safety", "test_p6_contracts", "test_acceptance_auth",
    }
    if request.module.__name__.split(".")[-1] not in http_modules:
        return
    from eic.core.config import get_settings
    from sqlalchemy.engine import make_url

    settings = get_settings()
    url = make_url(settings.database_url)
    assert url.drivername == "sqlite+aiosqlite" and url.database
    path = Path(url.database).resolve()
    # 仅接受测试库，不接触开发/发布数据库；不 stamp 不明 schema。
    assert (path.name in {"test_eic.db", "test_skill_filter.db"} and path.parent == Path(__file__).resolve().parent) or (
        path.name.startswith("idcrash-test_") and path.parent == Path("/tmp")
    ), "HTTP fixtures require an isolated synthetic database"
    environment = {**os.environ, "EIC_DATABASE_URL": settings.database_url}
    command = [sys.executable, "-B", "-m", "eic.core.schema", "upgrade"]
    result = subprocess.run(command, cwd=Path(__file__).parents[1], env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
