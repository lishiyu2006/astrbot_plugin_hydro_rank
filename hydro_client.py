"""Hydro(OJ) 排行榜数据抓取与解析。

本模块支持**三种**取数方式，按可靠性从高到低自动选择：

1. **排行榜 API 插件**（首选，需目标站点安装 ``hydro_ranking_api``）

   ``POST {站点根}/api/ranking``  body::

       {"domainId": "system", "page": 1, "limit": 100}

   返回结构化 JSON，含 ``rank / uid / uname / rp / level / nSubmit / nAccept / rpInfo``，
   无需解析 HTML，也不会因主题改动而失效。

   Hydro 核心自带的 HTTP API（``/api/:op``）**只有** 8 个操作
   （``domain / domain.current / groups / domain.group / problem / problems / user / users``），
   没有排行榜；本模块使用的 ``ranking`` 操作由插件补上。
   探测失败（404 / 非 JSON / 缺字段 / 无权限）会自动降级到方式 2。

2. **HTML 页面**（通用兜底）

   ``Accept: text/html`` 请求 ``/ranking``，服务端渲染出完整的 ``<table>``，
   每一行包含排名 / 用户名 / RP / 题目 / 比赛 / 签到 / 贡献 / 通过 / 简介 等列。
   字段最全，任何 Hydro 站点都能用，但依赖模板结构。

3. **``Accept: application/json``**（仅用于取总数）

   Hydro 在请求头接受 JSON 时会把 ``this.response.body`` 直接序列化，形如::

       {"udocs": [...], "upcount": 108, "ucount": 10784, "page": 1}

   ``upcount`` 是**总页数**、``ucount`` 是**总人数**（每页 100 条）。

   .. warning::
      ``udocs`` 里是**原始用户文档**（``_id/uname/mail/perm/priv/role/regat/loginat/avatar``），
      **不含 RP、排名、题数、通过数**，而且带 ``mail``。
      原因是 ``model/user.ts`` 的 ``User.serialize()`` 只输出公开字段，
      而 ``rp`` / ``rank`` / ``nAccept`` 在 ``model/setting.ts`` 里既没标
      ``FLAG_PUBLIC`` 也没标 ``FLAG_PRIVATE``，因此永远被丢弃。
      所以这个接口**只能拿总数**，不能拿来当排行榜数据源。

对应源码：``packages/hydrooj/src/handler/domain.ts`` 的 ``DomainRankHandler``，
路由 ``ctx.Route('ranking', '/ranking', DomainRankHandler, PERM.PERM_VIEW_RANKING)``，
仅支持一个 ``page`` 查询参数。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote

try:  # AstrBot 自带 aiohttp；这里做一次兜底以免导入期直接崩溃
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

__all__ = [
    "HydroError",
    "HydroClient",
    "RankRow",
    "RankPage",
    "KEY_LABELS",
    "DEFAULT_UA",
    "API_PATH",
]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 AstrBot-HydroRank/1.0"
)

# 每个站点每页最多返回的条数（Hydro 服务端 limit.ranking 默认为 100）
HYDRO_PAGE_SIZE = 100

# 排行榜 API 插件的路径（挂在站点根上，不是 /d/<domain> 下面）
API_PATH = "/api/ranking"


class HydroError(Exception):
    """Hydro 请求或解析失败。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

# 表头 -> 标准字段名。使用「包含匹配」，兼容中英文与不同主题
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "rank": ("排名", "rank", "#"),
    "username": ("用户名", "用户", "选手", "username", "user", "name"),
    "rp": ("rp", "rating", "积分", "rating分"),
    "problem": ("题目", "题数", "problem"),
    "contest": ("比赛", "contest"),
    "checkin": ("签到", "checkin", "check-in"),
    "contribution": ("贡献", "contribution"),
    "accept": ("通过", "accept", "ac数"),
    "bio": ("简介", "bio", "签名"),
}

# 字段 -> 展示用的中文列名
KEY_LABELS: dict[str, str] = {
    "rank": "排名",
    "username": "用户",
    "rp": "RP",
    "problem": "题目",
    "contest": "比赛",
    "checkin": "签到",
    "contribution": "贡献",
    "accept": "通过",
    "bio": "简介",
}

# 除了排名/用户名以外，默认参与展示的字段（按优先级）
DETAIL_KEYS = ("rp", "problem", "contest", "checkin", "contribution", "accept")


@dataclass
class RankRow:
    """排行榜中的一行。"""

    rank: int | None = None
    uid: int | None = None
    username: str = ""
    level: str = ""  # 例如 "LV 10" / "MOD"
    bio: str = ""
    values: dict[str, str] = field(default_factory=dict)

    def value(self, key: str, default: str = "-") -> str:
        text = (self.values.get(key) or "").strip()
        return text or default


@dataclass
class RankPage:
    """一页排行榜。"""

    alias: str
    base_url: str
    page: int
    total_pages: int
    total_users: int
    rows: list[RankRow]
    columns: list[str]
    fetched_at: float = field(default_factory=time.time)
    source: str = "html"  # "api" 或 "html"，便于排查

    @property
    def start_rank(self) -> int:
        for row in self.rows:
            if row.rank is not None:
                return row.rank
        return (self.page - 1) * HYDRO_PAGE_SIZE + 1

    @property
    def end_rank(self) -> int:
        for row in reversed(self.rows):
            if row.rank is not None:
                return row.rank
        return self.start_rank + max(0, len(self.rows) - 1)


# --------------------------------------------------------------------------- #
# HTML 解析
# --------------------------------------------------------------------------- #


def _normalize_header(text: str) -> str:
    """把表头文字映射为标准字段名，无法识别返回空串。"""
    t = re.sub(r"\s+", "", (text or "").strip().lower())
    if not t:
        return ""
    # 先做精确匹配，避免 "ac" 命中 "accept" 之外的词
    for key, names in COLUMN_ALIASES.items():
        if t in names:
            return key
    for key, names in COLUMN_ALIASES.items():
        if any(n and n in t for n in names):
            return key
    return ""


class _RankTableParser(HTMLParser):
    """从排名页中提取所有 ``<table>`` 的表头与数据行。

    只关心结构，不依赖具体 class 名（class 仅用于识别用户名列里的
    ``<a class="user-profile-name">`` 与等级徽章），因此对主题改动更耐受。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._in_table = False
        self._section = ""
        self._cell: dict | None = None
        self._row: list[dict] = []
        self._rows: list[list[dict]] = []
        self._headers: list[str] = []
        self._in_uname = False
        self._in_badge = False

    # -- HTMLParser 回调 ---------------------------------------------------- #

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "table":
            if not self._in_table:
                self._in_table = True
                self._section = ""
                self._cell = None
                self._row = []
                self._rows = []
                self._headers = []
            return
        if not self._in_table:
            return

        if tag in ("thead", "tbody", "tfoot"):
            self._section = tag
            return
        if tag == "tr":
            self._row = []
            return
        if tag in ("td", "th"):
            attr = dict(attrs)
            self._cell = {
                "class": attr.get("class", ""),
                "text": [],
                "uname": [],
                "level": [],
                "uid": None,
            }
            return
        if tag == "a" and self._cell is not None:
            cls = dict(attrs).get("class", "")
            if "user-profile-name" in cls:
                self._in_uname = True
                m = re.search(r"/user/(\d+)", dict(attrs).get("href", ""))
                if m:
                    self._cell["uid"] = int(m.group(1))
            elif "user-profile-badge" in cls:
                self._in_badge = True

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._cell is None:
            return
        text = data.strip()
        if not text:
            return
        self._cell["text"].append(text)
        if self._in_uname:
            self._cell["uname"].append(text)
        elif self._in_badge:
            self._cell["level"].append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._in_uname = False
            self._in_badge = False
            return
        if not self._in_table:
            return
        if tag in ("td", "th"):
            if self._cell is not None:
                self._row.append(self._cell)
                self._cell = None
            return
        if tag == "tr":
            if self._row:
                if self._section == "thead":
                    self._headers = ["".join(c["text"]) for c in self._row]
                elif self._section != "tfoot":
                    self._rows.append(self._row)
            self._row = []
            return
        if tag in ("thead", "tbody", "tfoot"):
            self._section = ""
            return
        if tag == "table":
            # 只保留表头或数据行非空的表格；空表格（超出页码范围）也会被保留
            if self._rows or self._headers:
                self.tables.append({"headers": self._headers, "rows": self._rows})
            self._in_table = False
            self._cell = None
            self._row = []
            self._rows = []
            self._headers = []


def _score_table(table: dict) -> int:
    """给表格打分，用于在页面里挑出真正的排名表。"""
    keys = [_normalize_header(h) for h in table.get("headers", [])]
    score = 0
    if "rank" in keys:
        score += 4
    if "username" in keys:
        score += 4
    if "rp" in keys:
        score += 2
    score += min(len(table.get("rows", [])), 20)
    return score


def _looks_like_ranking_page(html: str) -> bool:
    """判断页面是否是一个「排行榜页」（即便当前页没有任何数据行）。

    Hydro 在页码超出范围时仍返回 200 和正常的排行榜页骨架，只是没有表格，
    因此不能仅凭「没有 table」就断定对方不是 Hydro 站点。
    """
    if re.search(r'data-page="[^"]*ranking', html, re.I):
        return True
    if re.search(r'class="[^"]*\bpager\b', html):
        return True
    return bool(re.search(r"<th[^>]*>[^<]*(排名|rank)", html, re.I))


def parse_ranking_html(html: str, *, alias: str, base_url: str, page: int) -> RankPage:
    """解析排名页 HTML，返回 :class:`RankPage`。

    注意：页码超出范围时会返回 ``rows`` 为空的 :class:`RankPage`（而不是抛异常），
    由调用方结合 ``total_pages`` 给出更友好的提示。
    """
    parser = _RankTableParser()
    parser.feed(html)
    parser.close()

    if not parser.tables and not _looks_like_ranking_page(html):
        raise HydroError("页面中没有找到排行榜，请确认该站点确实是 Hydro 且已开放排名页")

    table = max(parser.tables, key=_score_table) if parser.tables else {"headers": [], "rows": []}
    headers = table["headers"]
    raw_rows = table["rows"]

    keys = [_normalize_header(h) for h in headers]
    if headers and not any(keys):
        # 表头无法识别时按 Hydro 默认列顺序兜底
        keys = [
            "rank",
            "username",
            "rp",
            "problem",
            "contest",
            "checkin",
            "contribution",
            "accept",
            "bio",
        ][: len(headers)]

    rows: list[RankRow] = []
    for cells in raw_rows:
        row = RankRow()
        for idx, cell in enumerate(cells):
            key = keys[idx] if idx < len(keys) else ""
            text = " ".join(cell["text"]).strip()
            if key == "rank":
                m = re.search(r"\d+", text)
                if m:
                    row.rank = int(m.group())
            elif key == "username":
                row.username = "".join(cell["uname"]).strip() or text
                row.uid = cell["uid"]
                row.level = "".join(cell["level"]).strip()
                if not cell["uname"] and row.level and row.username.endswith(row.level):
                    row.username = row.username[: -len(row.level)].strip()
            elif key == "bio":
                row.bio = text
            elif key:
                row.values[key] = text
        if not row.username and row.rank is None:
            continue  # 空行 / 占位行
        rows.append(row)

    # 分页控件的最大页码即总页数（Hydro 的 pager 会包含「末页」链接）；
    # 空页时 pager 不可信，交给 JSON 接口的 upcount 兜底
    page_numbers = [int(n) for n in re.findall(r"[?&]page=(\d+)", html)]
    pager_max = max(page_numbers) if page_numbers else 0
    total_pages = max(pager_max, page) if rows else page

    columns = [k for k in keys if k]
    return RankPage(
        alias=alias,
        base_url=base_url,
        page=page,
        total_pages=total_pages,
        total_users=0,
        rows=rows,
        columns=columns,
        source="html",
    )


def _match_rows(rows: list[RankRow], query: str) -> list[RankRow]:
    """在若干行中查找目标用户（支持 UID、精确用户名、模糊用户名）。"""
    q = (query or "").strip()
    if not q:
        return []
    if q.isdigit():
        uid = int(q)
        return [r for r in rows if r.uid == uid]
    ql = q.lower()
    exact = [r for r in rows if r.username.lower() == ql]
    if exact:
        return exact
    return [r for r in rows if ql in r.username.lower()]


# --------------------------------------------------------------------------- #
# API 响应 -> RankPage
# --------------------------------------------------------------------------- #


def _api_detail_keys(rp_keys: list[str]) -> list[str]:
    """把 ``rpInfo`` 里的键整理成展示列顺序：已知的优先，未知的排后面。"""
    ordered = [k for k in DETAIL_KEYS if k != "rp" and k in rp_keys]
    ordered += [k for k in rp_keys if k not in ordered]
    ordered += [k for k in DETAIL_KEYS if k == "rp" and k in rp_keys]
    return ordered


def _api_row_to_rank_row(item: dict) -> RankRow:
    """把插件返回的一行 JSON 转成 :class:`RankRow`。"""
    row = RankRow()
    rank = item.get("rank")
    row.rank = int(rank) if isinstance(rank, (int, float)) else None

    uid = item.get("uid")
    row.uid = int(uid) if isinstance(uid, (int, float)) else None

    row.username = str(item.get("uname") or item.get("displayName") or "").strip()

    # 等级：Hydro 里 level 是 LEVELS 数组的下标，徽章显示 "LV <level>"，
    # 且 level 为 0 时不显示徽章（与模板 components/user.html 一致）
    level = item.get("level")
    if isinstance(level, (int, float)) and int(level) > 0:
        row.level = f"LV {int(level)}"

    rp = item.get("rp")
    if isinstance(rp, (int, float)):
        row.values["rp"] = str(int(round(rp)))

    rpinfo = item.get("rpInfo") or {}
    if isinstance(rpinfo, dict):
        for key, val in rpinfo.items():
            if isinstance(val, (int, float)) and val:
                row.values[str(key)] = str(int(round(val)))

    naccept = item.get("nAccept")
    if isinstance(naccept, (int, float)):
        row.values["accept"] = str(int(naccept))

    bio = item.get("bio")
    if isinstance(bio, str):
        row.bio = bio.strip()

    return row


def parse_ranking_api(data: dict, *, alias: str, base_url: str, page: int) -> RankPage:
    """把 ``/api/ranking`` 的响应转成 :class:`RankPage`。

    响应结构（由 ``hydro_ranking_api`` 插件定义）::

        {"domainId":"system","page":1,"limit":100,
         "upcount":108,"ucount":10784,
         "rows":[{"rank":1,"uid":3309,"uname":"liujiameng","rp":475,
                  "rankStored":1,"level":10,"nSubmit":294,"nAccept":856,
                  "rpInfo":{"problem":320,"contest":155}}]}

    数据不合法时抛 :class:`HydroError`，调用方据此降级到 HTML。
    """
    items = data.get("rows")
    if not isinstance(items, list):
        raise HydroError("排行榜 API 响应缺少 rows 字段")

    rows = [_api_row_to_rank_row(i) for i in items if isinstance(i, dict)]
    rows = [r for r in rows if r.username or r.rank is not None]

    rp_keys: list[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("rpInfo"), dict):
            for k in item["rpInfo"]:
                if str(k) not in rp_keys:
                    rp_keys.append(str(k))

    columns = ["rank", "username"]
    if any("rp" in r.values for r in rows):
        columns.append("rp")
    columns += _api_detail_keys(rp_keys)
    if any("accept" in r.values for r in rows):
        columns.append("accept")

    def _num(key: str) -> int:
        val = data.get(key)
        return int(val) if isinstance(val, (int, float)) else 0

    total_pages = _num("upcount") or page
    return RankPage(
        alias=alias,
        base_url=base_url,
        page=_num("page") or page,
        total_pages=max(total_pages, page),
        total_users=_num("ucount"),
        rows=rows,
        columns=columns,
        source="api",
    )


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


class HydroClient:
    """单个 Hydro 站点的排行榜客户端（带内存缓存）。

    ``prefer_api=True``（默认）时先尝试排行榜 API 插件，失败自动降级到 HTML 解析。
    """

    def __init__(
        self,
        *,
        alias: str,
        base_url: str,
        domain: str = "",
        token: str = "",
        timeout: int = 15,
        proxy: str = "",
        user_agent: str = DEFAULT_UA,
        prefer_api: bool = True,
    ) -> None:
        self.alias = alias
        self.base_url = self._normalize_base(base_url)
        self.domain = (domain or "").strip().strip("/")
        self.token = (token or "").strip()
        self.timeout = max(3, int(timeout or 15))
        self.proxy = (proxy or "").strip()
        self.user_agent = (user_agent or DEFAULT_UA).strip()
        self.prefer_api = bool(prefer_api)

        self._session = None
        self._page_cache: dict[int, tuple[float, RankPage]] = {}
        self._totals: tuple[float, int, int] | None = None
        self._lock = asyncio.Lock()
        # None=尚未探测；True=可用；False=不可用（本实例内不再重试）
        self._api_ok: bool | None = None
        self._domain_id: str = ""

    # -- 基础 -------------------------------------------------------------- #

    @staticmethod
    def _normalize_base(base_url: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise HydroError("站点地址为空")
        if not re.match(r"^https?://", base, re.I):
            base = "https://" + base
        # 允许用户直接粘贴 .../ranking 或 .../d/xxx/ranking
        base = re.sub(r"/ranking/?$", "", base, flags=re.I)
        return base

    @property
    def ranking_url(self) -> str:
        """排名页地址，自动补上 /d/<domain> 前缀。"""
        base = self.base_url
        if self.domain and not re.search(r"/d/[^/]+$", base):
            base = f"{base}/d/{quote(self.domain)}"
        return f"{base}/ranking"

    @property
    def site_root(self) -> str:
        """站点根地址（去掉 ``/d/<domain>`` 后缀）。API 挂在根上，不在域路径下。"""
        return re.sub(r"/d/[^/]+/?$", "", self.base_url) or self.base_url

    @property
    def api_url(self) -> str:
        return f"{self.site_root}{API_PATH}"

    @property
    def site_url(self) -> str:
        return self.base_url

    @property
    def api_available(self) -> bool:
        """排行榜 API 是否可用（未探测时为 False）。"""
        return self._api_ok is True

    async def _get_session(self):
        if aiohttp is None:  # pragma: no cover
            raise HydroError("缺少 aiohttp 依赖，请先安装：pip install aiohttp")
        if self._session is None or self._session.closed:
            headers = {
                "User-Agent": self.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers=headers,
                trust_env=True,
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request_text(self, url: str, accept: str) -> str:
        session = await self._get_session()
        kwargs: dict = {"headers": {"Accept": accept}}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            async with session.get(url, **kwargs) as resp:
                if resp.status != 200:
                    raise HydroError(f"HTTP {resp.status}")
                return await resp.text(errors="replace")
        except HydroError:
            raise
        except asyncio.TimeoutError as exc:
            raise HydroError(f"请求超时（>{self.timeout}s）") from exc
        except Exception as exc:  # noqa: BLE001 - 网络异常统一包装
            raise HydroError(f"请求失败：{type(exc).__name__}: {exc}") from exc

    async def _request_json(self, url: str, payload: dict) -> dict:
        """POST 一个 JSON 请求并解析响应。非 2xx 或非 JSON 一律抛 HydroError。"""
        session = await self._get_session()
        kwargs: dict = {"json": payload}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            async with session.post(url, **kwargs) as resp:
                body = await resp.text(errors="replace")
                if resp.status != 200:
                    raise HydroError(f"HTTP {resp.status}")
        except HydroError:
            raise
        except asyncio.TimeoutError as exc:
            raise HydroError(f"请求超时（>{self.timeout}s）") from exc
        except Exception as exc:  # noqa: BLE001
            raise HydroError(f"请求失败：{type(exc).__name__}: {exc}") from exc

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise HydroError("响应不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise HydroError("响应不是 JSON 对象")
        return data

    # -- API 通道 ---------------------------------------------------------- #

    async def _resolve_domain_id(self) -> str:
        """确定 API 调用要用的 domainId。

        用户显式配置了 ``domain`` 就用它；否则问一次 ``domain.current``，
        由服务端按请求 Host 解析出当前域（Hydro 支持按域名绑定不同域）。
        """
        if self.domain:
            return self.domain
        if self._domain_id:
            return self._domain_id
        try:
            data = await self._request_json(f"{self.site_root}/api/domain.current", {})
            did = ((data.get("domain") or {}) if isinstance(data.get("domain"), dict) else {}).get(
                "_id"
            )
        except Exception:  # noqa: BLE001 - 拿不到就退回 system
            did = None
        self._domain_id = str(did or "system")
        return self._domain_id

    async def _fetch_ranking_api(
        self, page: int, limit: int = HYDRO_PAGE_SIZE, search: str = ""
    ) -> RankPage:
        """走排行榜 API 取一页；任何异常都抛 HydroError，由调用方降级。"""
        domain_id = await self._resolve_domain_id()
        payload: dict = {"domainId": domain_id, "page": page, "limit": limit}
        if search:
            payload["search"] = search
        data = await self._request_json(self.api_url, payload)
        return parse_ranking_api(data, alias=self.alias, base_url=self.base_url, page=page)

    async def probe_api(self) -> bool:
        """探测排行榜 API 是否可用（结果缓存在实例上）。"""
        if not self.prefer_api:
            self._api_ok = False
            return False
        if self._api_ok is not None:
            return self._api_ok
        try:
            await self._fetch_ranking_api(1)
            self._api_ok = True
        except Exception:  # noqa: BLE001 - 探测失败即视为不支持
            self._api_ok = False
        return self._api_ok

    # -- 总数 -------------------------------------------------------------- #

    async def _fetch_totals(self, ttl: int) -> tuple[int, int]:
        """通过 JSON 接口拿 (总页数 upcount, 总人数 ucount)，失败返回 (0, 0)。

        注意：这里的 JSON 只含用户公开字段，**没有 RP / 排名**，
        仅用于取总数；排行榜数据走 API 或 HTML。
        """
        now = time.time()
        if self._totals and now - self._totals[0] < ttl:
            return self._totals[1], self._totals[2]

        up = uc = 0
        try:
            text = await self._request_text(f"{self.ranking_url}?page=1", "application/json")
            data = json.loads(text)
            up = int(data.get("upcount") or 0)
            uc = int(data.get("ucount") or 0)
        except Exception:  # noqa: BLE001 - 总数只是锦上添花，失败不影响主流程
            up = uc = 0

        self._totals = (now, up, uc)
        return up, uc

    # -- 对外接口 ---------------------------------------------------------- #

    async def fetch_ranking(self, page: int = 1, ttl: int = 60) -> RankPage:
        """获取排行榜第 ``page`` 页（Hydro 每页 100 人）。

        优先走排行榜 API（若可用），否则解析 HTML 页面。
        """
        page = max(1, int(page or 1))
        now = time.time()

        if ttl > 0:
            cached = self._page_cache.get(page)
            if cached and now - cached[0] < ttl:
                return cached[1]

        async with self._lock:
            if ttl > 0:
                cached = self._page_cache.get(page)
                if cached and time.time() - cached[0] < ttl:
                    return cached[1]

            result = await self._load_page(page, ttl)

            if ttl > 0:
                self._page_cache[page] = (time.time(), result)
            return result

    async def _load_page(self, page: int, ttl: int) -> RankPage:
        """实际取数：API 优先，失败降级 HTML。"""
        if self._api_ok is None and self.prefer_api:
            try:
                result = await self._fetch_ranking_api(page)
                self._api_ok = True
                return result
            except Exception:  # noqa: BLE001 - 降级到 HTML
                self._api_ok = False
        elif self._api_ok:
            with contextlib.suppress(Exception):
                return await self._fetch_ranking_api(page)

        # ---- HTML 兜底 ----
        html = await self._request_text(f"{self.ranking_url}?page={page}", "text/html")
        result = parse_ranking_html(html, alias=self.alias, base_url=self.base_url, page=page)

        up, uc = await self._fetch_totals(max(ttl, 30))
        if up > 0:
            result.total_pages = up
        result.total_users = uc
        return result

    async def find_user(
        self,
        query: str,
        *,
        max_pages: int = 20,
        concurrency: int = 4,
        ttl: int = 60,
        on_progress=None,
    ) -> tuple[list[RankRow], int, int]:
        """查找用户，返回 ``(命中行, 已扫描页数, 总页数)``。

        走 API 时服务端支持 ``search``，一次请求即可命中；
        走 HTML 时按页并发扫描，命中即提前停止。
        """
        query = (query or "").strip()
        if not query:
            return [], 0, 1

        # ---- API 通道：一次请求 ----
        if self._api_ok is None and self.prefer_api:
            with contextlib.suppress(Exception):
                page = await self._fetch_ranking_api(1, search=query)
                self._api_ok = True
                hits = _match_rows(page.rows, query)
                return hits, 1, page.total_pages or 1
            self._api_ok = False
        elif self._api_ok:
            with contextlib.suppress(Exception):
                page = await self._fetch_ranking_api(1, search=query)
                hits = _match_rows(page.rows, query)
                return hits, 1, page.total_pages or 1

        # ---- HTML 通道：跨页扫描 ----
        first = await self.fetch_ranking(1, ttl=ttl)
        total_pages = max(1, min(first.total_pages or 1, max(1, int(max_pages))))
        scanned = 1

        hits = _match_rows(first.rows, query)
        if hits:
            return hits, scanned, first.total_pages or 1

        sem = asyncio.Semaphore(max(1, int(concurrency)))
        lock = asyncio.Lock()
        found: list[RankRow] = []
        done = 1

        async def worker(p: int) -> None:
            nonlocal done
            async with sem:
                if found:
                    return
                try:
                    rp = await self.fetch_ranking(p, ttl=ttl)
                except HydroError:
                    return
                async with lock:
                    done += 1
                    if on_progress is not None:
                        with contextlib.suppress(Exception):
                            await on_progress(done, total_pages)
                    if not found:
                        found.extend(_match_rows(rp.rows, query))

        for start in range(2, total_pages + 1, max(1, int(concurrency))):
            if found:
                break
            await asyncio.gather(
                *(worker(p) for p in range(start, min(start + concurrency, total_pages + 1)))
            )

        return found, done, first.total_pages or 1
