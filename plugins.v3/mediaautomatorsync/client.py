"""Media Automator 远程日志客户端。

Media Automator 没有公开的日志 API 文档，但 Web UI 的「日志手动管理 → 下载主日志文件」
对应一个真实端点（``GET /api/system/logs/download``，返回 ``text/plain`` 的 app.log
全文）。本模块封装：

* 登录换取 Token（路径可配置，未配置时自动探测）；
* 带 Token 拉取日志全文，401 时自动重新登录并重试一次；
* Token 缓存，避免每次轮询都登录。

使用 ``app.sdk.network.AsyncRequestUtils``，失败默认返回 ``None`` 而不是抛异常，
因此这里显式检查返回值与状态码。

注意：下载端点每次返回**整个文件**，不是增量。增量语义由调用方按行去重实现
（见 ``MediaAutomatorSync`` 里的行指纹逻辑）。
"""

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from app.sdk import network as network_sdk
from app.sdk.logging import logger

# 未配置登录路径时依次探测的候选路径。
# Media Automator 是 FastAPI 应用，先看 openapi.json，再试常见登录端点。
LOGIN_PATH_CANDIDATES = (
    "/api/login",
    "/api/system/login",
    "/api/auth/login",
    "/api/user/login",
    "/api/token",
    "/api/system/token",
)

# 已确认存在的日志下载端点。
LOG_DOWNLOAD_PATH = "/api/system/logs/download"

# 登录请求里可能使用的用户名字段名，按顺序尝试。
USERNAME_FIELDS = ("username", "user", "account")
# 登录请求里可能使用的密码字段名。
PASSWORD_FIELDS = ("password", "passwd", "pwd")


class MediaAutomatorClient:
    """Media Automator 的远程日志客户端。"""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        login_path: str = "",
        token: str = "",
        timeout: int = 30,
        verify_ssl: bool = True,
    ) -> None:
        """
        :param base_url: 服务地址，如 http://192.168.6.2:8010
        :param username: 登录用户名
        :param password: 登录密码
        :param login_path: 登录接口路径；留空时自动探测
        :param token: 直接可用的 Token；非空时优先于账号密码
        :param timeout: 请求超时秒数
        :param verify_ssl: 是否校验 TLS 证书（内网自签名场景可关闭）
        """
        self.base_url = (base_url or "").strip().rstrip("/")
        self.username = (username or "").strip()
        self.password = password or ""
        self.login_path = (login_path or "").strip()
        self.token = (token or "").strip()
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        # 自动探测到的登录路径会缓存在实例上，避免每次轮询重复探测。
        self._resolved_login_path: str = ""
        # 记录最近一次错误，便于在插件详情页与 API 中展示。
        self.last_error: str = ""

    # ------------------------------------------------------------------
    # 对外能力
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """判断是否具备拉取远程日志所需的最小配置。"""
        if not self.base_url:
            return False
        # 有 Token 或 有账号密码 都可以。
        return bool(self.token) or bool(self.username and self.password)

    async def fetch_log(self) -> Optional[str]:
        """
        拉取日志全文。

        :return: 日志文本；失败返回 None 并设置 ``last_error``
        """
        if not self.available():
            self.last_error = "未配置服务地址，或既未提供 Token 也未提供账号密码"
            return None

        url = urljoin(f"{self.base_url}/", LOG_DOWNLOAD_PATH.lstrip("/"))
        # 先确保有 Token；直接配置了 Token 时就跳过登录。
        if not self.token and not await self._ensure_token():
            return None

        text = await self._request_text(url, with_token=True)
        if text is not None:
            self.last_error = ""
            return text

        # 401/403 时 Token 可能已过期：清掉并重新登录一次再试。
        if self._last_status in (401, 403):
            logger.info("Media Automator 远程日志：Token 可能已过期，尝试重新登录")
            self.token = ""
            self._resolved_login_path = ""
            if await self._ensure_token():
                text = await self._request_text(url, with_token=True)
                if text is not None:
                    self.last_error = ""
                    return text

        if not self.last_error:
            self.last_error = f"拉取日志失败（HTTP {self._last_status or '未知'}）"
        return None

    async def login_only(self) -> Tuple[bool, str]:
        """
        仅执行一次登录，用于配置页的「测试连接」。

        :return: (是否成功, 说明)
        """
        if not self.base_url:
            return False, "未填写服务地址"
        if not (self.username and self.password):
            return False, "未填写用户名或密码"
        ok = await self._ensure_token(force=True)
        if ok:
            # 登录成功后再验证日志端点是否真的可读。
            url = urljoin(f"{self.base_url}/", LOG_DOWNLOAD_PATH.lstrip("/"))
            text = await self._request_text(url, with_token=True)
            if text is None:
                return False, f"登录成功，但读取日志失败：{self.last_error}"
            return True, f"连接成功，日志约 {len(text)} 字符"
        return False, self.last_error or "登录失败"

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------

    async def _ensure_token(self, force: bool = False) -> bool:
        """确保持有可用 Token；已缓存且非强制时直接返回。"""
        if self.token and not force:
            return True
        if not (self.username and self.password):
            self.last_error = "未提供账号密码，且没有可用的 Token"
            return False

        path = self.login_path or self._resolved_login_path
        if path:
            token = await self._try_login(path)
            if token:
                self._resolved_login_path = path
                self.token = token
                self.last_error = ""
                return True
            if self.login_path:
                # 用户显式配置的路径失败时不再探测，直接报错，避免掩盖配置问题。
                return False

        # 自动探测：优先读 openapi.json 找登录端点，再退回常见路径。
        discovered = await self._discover_login_paths()
        for candidate in discovered:
            token = await self._try_login(candidate)
            if token:
                self._resolved_login_path = candidate
                self.token = token
                self.last_error = ""
                logger.info(f"Media Automator 远程日志：登录接口探测成功 {candidate}")
                return True

        tried = ", ".join(discovered) if discovered else "（无候选）"
        self.last_error = (
            f"登录失败，已尝试：{tried}。"
            "请在插件设置里手动填写「登录接口路径」（浏览器 F12 抓登录请求的 URL 路径）"
        )
        return False

    async def _discover_login_paths(self) -> List[str]:
        """从 openapi.json 找出登录端点，并与内置候选合并。"""
        candidates: List[str] = []
        openapi = await self._request_json(urljoin(f"{self.base_url}/", "openapi.json"))
        if isinstance(openapi, dict):
            for path in (openapi.get("paths") or {}):
                lowered = str(path).lower()
                # 只认同时含 login/token/signin 语义的路径。
                if any(word in lowered for word in ("login", "signin", "sign_in", "token")):
                    candidates.append(str(path))

        # 内置候选兜底，并按「更可能命中」排序。
        for fallback in LOGIN_PATH_CANDIDATES:
            if fallback not in candidates:
                candidates.append(fallback)
        return candidates

    async def _try_login(self, path: str) -> Optional[str]:
        """对给定路径尝试登录，成功时返回 Token。"""
        url = urljoin(f"{self.base_url}/", path.lstrip("/"))
        payload = {}
        # 字段名可能不同，逐组尝试；Media Automator 常见的是 username/password。
        for user_field, pass_field in zip(USERNAME_FIELDS, PASSWORD_FIELDS):
            payload = {user_field: self.username, pass_field: self.password}
            data = await self._request_json(url, method="POST", json_body=payload)
            token = self._extract_token(data)
            if token:
                return token
        # 部分实现用表单而非 JSON。
        payload = {"username": self.username, "password": self.password}
        data = await self._request_json(url, method="POST", form_body=payload)
        return self._extract_token(data)

    @staticmethod
    def _extract_token(data: Any) -> Optional[str]:
        """
        从登录响应里提取 Token。

        兼容常见结构：``{"access_token": ...}``、``{"token": ...}``、
        ``{"data": {"token": ...}}``、``{"access": ...}`` 等。
        """
        if not isinstance(data, dict):
            return None

        def pick(mapping: Dict[str, Any]) -> Optional[str]:
            for key in ("access_token", "access", "token", "jwt", "jwt_token", "auth_token", "id_token"):
                value = mapping.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            # 有的实现返回 {"token_type": "Bearer", "token": "..."} 之外的嵌套结构。
            for nested_key in ("data", "result", "payload"):
                nested = mapping.get(nested_key)
                if isinstance(nested, dict):
                    found = pick(nested)
                    if found:
                        return found
            return None

        return pick(data)

    # ------------------------------------------------------------------
    # HTTP 封装
    # ------------------------------------------------------------------

    _last_status: int = 0

    def _headers(self, with_token: bool) -> Dict[str, str]:
        """构造请求头。"""
        headers = {"Accept": "*/*"}
        if with_token and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _http() -> Any:
        """
        取用宿主 HTTP 客户端。

        通过 ``app.sdk.network`` 模块属性在**调用时**解析，而不是在导入时把类绑定到
        本模块：这样宿主替换实现后仍然生效，测试也能注入替身。
        """
        return network_sdk.AsyncRequestUtils()

    async def _request_text(self, url: str, with_token: bool = False) -> Optional[str]:
        """发起 GET 并返回文本响应体。"""
        try:
            response = await self._http().get_res(
                url=url,
                headers=self._headers(with_token),
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except Exception as error:  # 网络异常统一收敛为 None + last_error
            self._last_status = 0
            self.last_error = f"请求异常：{error}"
            logger.warning(f"Media Automator 远程日志：{self.last_error}")
            return None

        if response is None:
            self._last_status = 0
            self.last_error = "请求失败（无响应，可能是地址错误、超时或无法连通）"
            return None

        self._last_status = response.status_code
        if response.status_code != 200:
            self.last_error = f"HTTP {response.status_code}"
            return None
        return response.text

    async def _request_json(
        self,
        url: str,
        method: str = "GET",
        json_body: Optional[dict] = None,
        form_body: Optional[dict] = None,
    ) -> Optional[Any]:
        """发起 GET/POST 并返回解析后的 JSON。"""
        try:
            if method == "POST":
                response = await self._http().post_res(
                    url=url,
                    json=json_body,
                    data=form_body,
                    headers={"Accept": "application/json"},
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
            else:
                response = await self._http().get_res(
                    url=url,
                    headers={"Accept": "application/json"},
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
        except Exception as error:
            self._last_status = 0
            self.last_error = f"请求异常：{error}"
            return None

        if response is None:
            self._last_status = 0
            return None
        self._last_status = response.status_code
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except Exception:
            return None
