"""P0-04 部署安全：生产环境默认凭据拒绝启动。

运行：
    pytest backend/tests/test_deploy_security.py -q
"""
import pytest

from app.core.config import Settings


def _prod_settings(**overrides) -> Settings:
    base = dict(
        APP_ENV="production",
        AGENT_CHECKPOINT_DSN="sqlite:///./data/agent_checkpoints.db",
        SECRET_KEY="a" * 64,
        ADMIN_PASSWORD="S3cure-Initial-Pass!",
    )
    base.update(overrides)
    return Settings(**base)


class TestProductionSecrets:
    def test_default_secret_key_rejected(self):
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            _prod_settings(SECRET_KEY="change-me-in-production-min-32-chars-long!!").validate_startup()

    def test_short_secret_key_rejected(self):
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            _prod_settings(SECRET_KEY="short-key").validate_startup()

    def test_default_admin_password_rejected(self):
        with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
            _prod_settings(ADMIN_PASSWORD="Admin@123456").validate_startup()

    def test_weak_admin_password_rejected(self):
        with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
            _prod_settings(ADMIN_PASSWORD="password").validate_startup()

    def test_strong_secrets_accepted(self):
        _prod_settings().validate_startup()  # 不抛异常即通过

    def test_development_allows_defaults(self):
        """开发环境不受生产凭据校验影响（默认配置保持可用）"""
        Settings(APP_ENV="development").validate_startup()
