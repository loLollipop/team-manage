"""
ChatGPT API 服务
用于调用 ChatGPT 后端 API,实现 Team 成员管理功能
"""
import asyncio
import logging
from typing import Optional, Dict, Any, List
from curl_cffi.requests import AsyncSession
from app.services.settings import settings_service
from sqlalchemy.ext.asyncio import AsyncSession as DBAsyncSession

logger = logging.getLogger(__name__)


class ChatGPTService:
    """ChatGPT API 服务类"""

    BASE_URL = "https://chatgpt.com/backend-api"

    # 重试配置
    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 2, 4]  # 指数退避: 1s, 2s, 4s

    def __init__(self):
        """初始化 ChatGPT API 服务"""
        # 不再使用全局 session 以防止 Cookie 污染
        self.proxy: Optional[str] = None

    async def _get_proxy_config(self, db_session: DBAsyncSession) -> Optional[str]:
        """
        获取代理配置
        """
        proxy_config = await settings_service.get_proxy_config(db_session)
        if proxy_config["enabled"] and proxy_config["proxy"]:
            return proxy_config["proxy"]
        return None

    async def _create_session(self, db_session: DBAsyncSession) -> AsyncSession:
        """
        创建 HTTP 会话
        """
        proxy = await self._get_proxy_config(db_session)
        session = AsyncSession(
            impersonate="chrome",
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=30
        )
        return session

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        json_data: Optional[Dict[str, Any]] = None,
        db_session: Optional[DBAsyncSession] = None
    ) -> Dict[str, Any]:
        """
        发送 HTTP 请求 (使用独立的临时会话防止 Cookie 污染)
        """
        async with await self._create_session(db_session) as session:
            for attempt in range(self.MAX_RETRIES):
                try:
                    logger.info(f"发送请求: {method} {url} (尝试 {attempt + 1}/{self.MAX_RETRIES})")

                    if method == "GET":
                        response = await session.get(url, headers=headers)
                    elif method == "POST":
                        response = await session.post(url, headers=headers, json=json_data)
                    elif method == "DELETE":
                        response = await session.delete(url, headers=headers, json=json_data)
                    else:
                        raise ValueError(f"不支持的 HTTP 方法: {method}")

                    status_code = response.status_code
                    logger.info(f"响应状态码: {status_code}")

                    # 2xx 成功
                    if 200 <= status_code < 300:
                        try:
                            data = response.json()
                        except Exception:
                            data = {}

                        return {
                            "success": True,
                            "status_code": status_code,
                            "data": data,
                            "error": None
                        }

                    # 4xx 客户端错误 (不重试)
                    if 400 <= status_code < 500:
                        error_code = None
                        try:
                            error_data = response.json()
                            error_msg = error_data.get("detail", response.text)
                            if isinstance(error_data, dict):
                                error_info = error_data.get("error")
                                if isinstance(error_info, dict):
                                    error_code = error_info.get("code")
                                else:
                                    error_code = error_data.get("code")
                        except Exception:
                            error_msg = response.text

                        logger.warning(f"客户端错误 {status_code}: {error_msg} (code: {error_code})")
                        return {
                            "success": False,
                            "status_code": status_code,
                            "data": None,
                            "error": error_msg,
                            "error_code": error_code
                        }

                    # 5xx 服务器错误 (需要重试)
                    if status_code >= 500:
                        logger.warning(f"服务器错误 {status_code}, 准备重试")
                        if attempt < self.MAX_RETRIES - 1:
                            delay = self.RETRY_DELAYS[attempt]
                            await asyncio.sleep(delay)
                            continue
                        return {
                            "success": False,
                            "status_code": status_code,
                            "data": None,
                            "error": f"服务器错误 {status_code}, 已重试 {self.MAX_RETRIES} 次"
                        }

                except asyncio.TimeoutError:
                    logger.warning(f"请求超时 (尝试 {attempt + 1}/{self.MAX_RETRIES})")
                    if attempt < self.MAX_RETRIES - 1:
                        delay = self.RETRY_DELAYS[attempt]
                        await asyncio.sleep(delay)
                        continue
                    return {"success": False, "status_code": 0, "data": None, "error": f"请求超时, 已重试 {self.MAX_RETRIES} 次"}

                except Exception as e:
                    logger.error(f"请求异常: {e}")
                    if attempt < self.MAX_RETRIES - 1:
                        delay = self.RETRY_DELAYS[attempt]
                        await asyncio.sleep(delay)
                        continue
                    return {"success": False, "status_code": 0, "data": None, "error": str(e)}

            return {"success": False, "status_code": 0, "data": None, "error": "未知错误"}

    async def send_invite(
        self,
        access_token: str,
        account_id: str,
        email: str,
        db_session: DBAsyncSession
    ) -> Dict[str, Any]:
        """
        发送 Team 邀请
        """
        url = f"{self.BASE_URL}/accounts/{account_id}/invites"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id
        }
        json_data = {
            "email_addresses": [email],
            "role": "standard-user",
            "resend_emails": True
        }
        logger.info(f"发送邀请: {email} -> Team {account_id}")
        result = await self._make_request("POST", url, headers, json_data, db_session)
        if result["status_code"] == 409:
            result["error"] = "用户已是该 Team 的成员"
        if result["status_code"] == 422:
            result["error"] = "Team 已满或邮箱格式错误"
        return result

    async def get_members(
        self,
        access_token: str,
        account_id: str,
        db_session: DBAsyncSession
    ) -> Dict[str, Any]:
        """
        获取 Team 成员列表
        """
        all_members = []
        offset = 0
        limit = 50
        while True:
            url = f"{self.BASE_URL}/accounts/{account_id}/users?limit={limit}&offset={offset}"
            headers = {"Authorization": f"Bearer {access_token}"}
            result = await self._make_request("GET", url, headers, db_session=db_session)
            if not result["success"]:
                return {"success": False, "members": [], "total": 0, "error": result["error"]}
            data = result["data"]
            items = data.get("items", [])
            total = data.get("total", 0)
            all_members.extend(items)
            if len(all_members) >= total:
                break
            offset += limit
        return {"success": True, "members": all_members, "total": len(all_members), "error": None}

    async def get_invites(
        self,
        access_token: str,
        account_id: str,
        db_session: DBAsyncSession
    ) -> Dict[str, Any]:
        """
        获取 Team 邀请列表
        """
        url = f"{self.BASE_URL}/accounts/{account_id}/invites"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id
        }
        result = await self._make_request("GET", url, headers, db_session=db_session)
        if not result["success"]:
            return {"success": False, "items": [], "total": 0, "error": result["error"]}
        data = result["data"]
        items = data.get("items", [])
        return {"success": True, "items": items, "total": len(items), "error": None}

    async def delete_invite(
        self,
        access_token: str,
        account_id: str,
        email: str,
        db_session: DBAsyncSession
    ) -> Dict[str, Any]:
        """
        撤回 Team 邀请
        """
        url = f"{self.BASE_URL}/accounts/{account_id}/invites"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id
        }
        json_data = {"email_address": email}
        return await self._make_request("DELETE", url, headers, json_data, db_session)

    async def delete_member(
        self,
        access_token: str,
        account_id: str,
        user_id: str,
        db_session: DBAsyncSession
    ) -> Dict[str, Any]:
        """
        删除 Team 成员
        """
        url = f"{self.BASE_URL}/accounts/{account_id}/users/{user_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id
        }
        result = await self._make_request("DELETE", url, headers, db_session=db_session)
        if result["status_code"] == 403:
            result["error"] = "无权限删除该成员 (可能是 owner)"
        if result["status_code"] == 404:
            result["error"] = "用户不存在"
        return result

    async def get_account_info(
        self,
        access_token: str,
        db_session: DBAsyncSession
    ) -> Dict[str, Any]:
        """
        获取 account-id 和订阅信息
        """
        url = f"{self.BASE_URL}/accounts/check/v4-2023-04-27"
        headers = {"Authorization": f"Bearer {access_token}"}
        result = await self._make_request("GET", url, headers, db_session=db_session)
        if not result["success"]:
            return {"success": False, "accounts": [], "error": result["error"]}
        data = result["data"]
        accounts_data = data.get("accounts", {})
        team_accounts = []
        for account_id, account_info in accounts_data.items():
            account = account_info.get("account", {})
            entitlement = account_info.get("entitlement", {})
            if account.get("plan_type") == "team":
                team_accounts.append({
                    "account_id": account_id,
                    "name": account.get("name", ""),
                    "plan_type": account.get("plan_type", ""),
                    "account_user_role": account.get("account_user_role", ""),
                    "subscription_plan": entitlement.get("subscription_plan", ""),
                    "expires_at": entitlement.get("expires_at", ""),
                    "has_active_subscription": entitlement.get("has_active_subscription", False)
                })
        return {"success": True, "accounts": team_accounts, "error": None}

    async def refresh_access_token_with_session_token(
        self,
        session_token: str,
        db_session: DBAsyncSession,
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        使用 session_token 刷新 access_token (强制使用独立会话以防止 identity leakage)
        """
        url = "https://chatgpt.com/api/auth/session"
        if account_id:
            params = {
                "exchange_workspace_token": "true",
                "workspace_id": account_id,
                "reason": "setCurrentAccount"
            }
            query_string = "&".join([f"{k}={v}" for k, v in params.items()])
            url = f"{url}?{query_string}"
            
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Cookie": f"__Secure-next-auth.session-token={session_token}"
        }
        
        async with await self._create_session(db_session) as session:
            try:
                response = await session.get(url, headers=headers)
                status_code = response.status_code
                if status_code == 200:
                    data = response.json()
                    access_token = data.get("accessToken")
                    new_session_token = data.get("sessionToken")
                    if access_token:
                        return {"success": True, "access_token": access_token, "session_token": new_session_token}
                    return {"success": False, "error": "响应中未包含 accessToken"}
                else:
                    error_msg = response.text
                    error_code = None
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("detail", error_msg)
                        if isinstance(error_data, dict):
                            error_info = error_data.get("error")
                            error_code = error_info.get("code") if isinstance(error_info, dict) else error_data.get("code")
                    except Exception:
                        pass
                    return {"success": False, "status_code": status_code, "error": error_msg, "error_code": error_code}
            except Exception as e:
                return {"success": False, "error": str(e)}

    async def refresh_access_token_with_refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        db_session: DBAsyncSession
    ) -> Dict[str, Any]:
        """
        使用 refresh_token 刷新 access_token (强制使用独立会话以防止 identity leakage)
        """
        url = "https://auth.openai.com/oauth/token"
        json_data = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "redirect_uri": "com.openai.sora://auth.openai.com/android/com.openai.sora/callback",
            "refresh_token": refresh_token
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with await self._create_session(db_session) as session:
            try:
                response = await session.post(url, headers=headers, json=json_data)
                status_code = response.status_code
                if status_code == 200:
                    data = response.json()
                    return {"success": True, "access_token": data.get("access_token"), "refresh_token": data.get("refresh_token")}
                else:
                    error_msg = response.text
                    error_code = None
                    try:
                        error_data = response.json()
                        if isinstance(error_data, dict):
                            error_code = error_data.get("error")
                            error_msg = error_data.get("error_description", error_msg)
                    except Exception:
                        pass
                    return {"success": False, "status_code": status_code, "error": error_msg, "error_code": error_code}
            except Exception as e:
                return {"success": False, "error": str(e)}

    async def close(self):
        """关闭 HTTP 会话 (目前使用临时会话,此方法留空)"""
        pass

    async def clear_session(self):
        """清理当前会话 (目前使用临时会话,此方法留空)"""
        pass


# 创建全局实例
chatgpt_service = ChatGPTService()
