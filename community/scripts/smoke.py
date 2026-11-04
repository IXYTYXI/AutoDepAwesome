"""Verify a running local backend without printing credentials or tokens."""
import httpx
from app.core.config import Settings


def main():
    settings = Settings(news_sync_enabled=False)
    if not settings.dev_login_enabled or settings.environment == "production":
        raise SystemExit("仅用于启用了开发登录的本地环境")
    with httpx.Client(base_url=f"http://127.0.0.1:{settings.port}", timeout=10) as client:
        client.get("/health/live").raise_for_status()
        assert client.get("/api/v1/me").status_code == 401
        login = client.post("/api/v1/auth/dev", headers={"X-Dev-Login-Key": settings.dev_login_key.get_secret_value()})
        login.raise_for_status()
        token = login.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        try:
            me = client.get("/api/v1/me", headers=headers)
            me.raise_for_status()
            assert me.json()["data"]["identity_source"] == "development"
            assert client.get("/api/v1/admin/sync/status", headers=headers).status_code == 403
            assert client.get("/api/v1/content/snapshot", headers=headers).status_code in (200, 503)
        finally:
            client.post("/api/v1/auth/logout", headers=headers).raise_for_status()
        assert client.get("/api/v1/me", headers=headers).status_code == 401
    print("HTTP smoke passed: health, login, me, roles, content status, logout, revocation")


if __name__ == "__main__":
    main()
