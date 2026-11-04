import logging
from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.core.errors import AppError
from app.core.error_codes import ErrorCode
from app.schemas.auth import Department, Principal

PROFILE_SCOPES = ("contact:contact.base:readonly",
                  "contact:user.email:readonly", "contact:user.department:readonly",
                  "contact:department.base:readonly")


class FeishuIdentityProvider:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client, self.settings = client, settings

    def check_config(self):
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret.get_secret_value():
            raise AppError(ErrorCode.FEISHU_NOT_CONFIGURED)

    @staticmethod
    def payload(response: httpx.Response):
        try:
            data = response.json()
        except ValueError:
            raise AppError(ErrorCode.UPSTREAM_ERROR) from None
        if not isinstance(data, dict):
            raise AppError(ErrorCode.UPSTREAM_ERROR)
        if response.status_code >= 500 or response.status_code == 429:
            raise AppError(ErrorCode.UPSTREAM_ERROR)
        if not response.is_success or data.get("code", 0) != 0 or data.get("error"):
            code = data.get("code")
            logging.getLogger("ows_community").warning("feishu_oauth_rejected", extra={"upstream_code": code})
            if code in {20008, 20010, 20021, 20022, 20023, 20066}:
                raise AppError(ErrorCode.FORBIDDEN)
            if code in {20003, 20004, 20024, 20049, 20065}:
                raise AppError(ErrorCode.FEISHU_AUTH_FAILED)
            raise AppError(ErrorCode.UPSTREAM_ERROR)
        return data

    async def exchange_code(self, code: str, verifier: str) -> Principal:
        self.check_config()
        # Feishu authorize documentation currently pairs PKCE with the v2 token endpoint.
        # v3 rejected the live challenge (20049); do not drop PKCE to work around it.
        response = await self.client.post("https://open.feishu.cn/open-apis/authen/v2/oauth/token", json={
            "grant_type": "authorization_code", "client_id": self.settings.feishu_app_id,
            "client_secret": self.settings.feishu_app_secret.get_secret_value(),
            "code": code, "redirect_uri": self.settings.feishu_redirect_uri,
            "code_verifier": verifier,
        })
        access = self.payload(response).get("access_token")
        if not isinstance(access, str) or not access:
            raise AppError(ErrorCode.UPSTREAM_ERROR)
        response = await self.client.get("https://open.feishu.cn/open-apis/authen/v1/user_info",
                                         headers={"Authorization": f"Bearer {access}"})
        info = self.payload(response).get("data")
        if not isinstance(info, dict) or not all(isinstance(info.get(k), str) and info[k]
                                                for k in ("open_id", "tenant_key", "name")):
            raise AppError(ErrorCode.UPSTREAM_ERROR)
        allowed = self.settings.feishu_allowed_tenants
        if allowed and info["tenant_key"] not in allowed:
            raise AppError(ErrorCode.FORBIDDEN)
        email = self.text(info.get("enterprise_email")) or self.text(info.get("email"))
        departments, profile_status = [], "partial" if email else "unavailable"
        if self.settings.feishu_profile_enabled:
            email, departments, profile_status = await self.profile(access, info)
        # Feishu access tokens never reach the browser/store.
        # Custom-app availability is enforced by Feishu during token exchange.
        return Principal(user_id=f"feishu:{self.settings.feishu_app_id}:{info['open_id']}",
                         tenant_id=info["tenant_key"], display_name=info["name"],
                         identity_source="feishu", roles=["member"], email=email,
                         departments=departments, profile_status=profile_status)


    async def optional_data(self, path: str, access: str, params: dict | None = None) -> dict:
        """Profile enrichment is optional; failure must not invalidate verified identity."""
        try:
            response = await self.client.get("https://open.feishu.cn/open-apis/" + path,
                                             headers={"Authorization": f"Bearer {access}"},
                                             params=params)
            data = self.payload(response).get("data")
            return data if isinstance(data, dict) else {}
        except (AppError, httpx.HTTPError):
            logging.getLogger("ows_community").warning("feishu_profile_unavailable")
            return {}

    @staticmethod
    def text(value):
        return value.strip() if isinstance(value, str) and value.strip() else None

    async def profile(self, access: str, identity: dict):
        # Query only the authenticated user's open_id, never a client-supplied identifier.
        data = await self.optional_data("contact/v3/users/" + quote(identity["open_id"], safe=""), access,
                                       {"user_id_type": "open_id", "department_id_type": "open_department_id"})
        user = data.get("user")
        user = user if isinstance(user, dict) else {}
        email = (self.text(identity.get("enterprise_email")) or self.text(user.get("enterprise_email"))
                 or self.text(identity.get("email")) or self.text(user.get("email")))
        ids = user.get("department_ids")
        departments = []
        if isinstance(ids, list):
            for department_id in dict.fromkeys(item for item in ids if isinstance(item, str) and item):
                result = await self.optional_data("contact/v3/departments/" + quote(department_id, safe=""), access,
                                                 {"department_id_type": "open_department_id"})
                department = result.get("department")
                name = self.text(department.get("name")) if isinstance(department, dict) else None
                departments.append(Department(department_id=department_id, name=name))
        complete = bool(email and departments and all(item.name for item in departments))
        status = "complete" if complete else "partial" if email or departments else "unavailable"
        return email, departments, status
