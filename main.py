"""AstrBot 插件：查询自定义 Hydro(OJ) 服务器的排行榜。

Hydro 侧依赖的接口：
  * ``GET /ranking?page=N``            —— 服务端渲染的排行榜 HTML 表格
  * ``GET /d/<domain>/ranking``        —— 指定「域(domain)」的排行榜
  * 同地址 + ``Accept: application/json`` —— 返回 ``{udocs, upcount, ucount, page}``

实现见 ``packages/hydrooj/src/handler/domain.ts`` 的 ``DomainRankHandler``。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.api import AstrBotConfig
except ImportError:  # 兼容旧版本
    AstrBotConfig = dict  # type: ignore[assignment,misc]

# 同目录模块：正常以 data.plugins.<目录>.main 方式加载时走相对导入，
# 某些加载方式下相对导入会失败，此时退回把插件目录加进 sys.path。
try:
    from .hydro_client import HydroClient, HydroError, RankPage, RankRow
    from .renderer import cleanup_cache, render_image, render_text
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from hydro_client import HydroClient, HydroError, RankPage, RankRow
    from renderer import cleanup_cache, render_image, render_text

PLUGIN_NAME = "astrbot_plugin_hydro_rank"

# 指令名与别名（别名由 CommandFilter 的 alias 参数注册）
COMMAND_NAMES = {"hydro_rank", "hydrorank", "排名", "hydro排名"}

SUB_FIND = {"find", "查", "查询", "搜索", "查找"}
SUB_USER = {"user", "uid", "用户", "资料"}
SUB_SITES = {"sites", "site", "服务器", "站点", "列表"}
SUB_HELP = {"help", "帮助", "-h", "--help", "?", "？"}

HELP_TEXT = """📖 Hydro 排行榜插件 · 使用说明

/hydro_rank [数量]                查看排行榜前 N 名（默认 10，最大 100）
/hydro_rank page <页码>           查看第 k 页（每页 100 人）
/hydro_rank <数量> page <页码>     组合使用，如：/hydro_rank 20 page 2
/hydro_rank find <用户名|UID>     查找某人的排名（跨页搜索）
/hydro_rank user <UID>            按 UID 查看用户排名
/hydro_rank sites                 列出已配置的 Hydro 站点
/hydro_rank help                  显示本帮助

多站点：在指令中带上站点别名即可，例如
/hydro_rank 主站 20
/hydro_rank 别名 find Macesuted"""


@dataclass
class ServerConfig:
    """一个 Hydro 站点的配置。"""

    alias: str
    base_url: str
    domain: str = ""
    token: str = ""


def _split_extra_line(line: str) -> ServerConfig | None:
    """解析 extra_servers 中的一行：``别名|站点地址|域ID|Token``。

    只有站点地址是必填的，其余可省略；也支持逗号分隔。
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in re.split(r"[|｜,，\t]", line)]
    parts = [p for p in parts if p]
    if not parts:
        return None

    url = next((p for p in parts if re.match(r"^https?://", p, re.I) or "." in p), "")
    if not url:
        return None
    alias = next((p for p in parts if p != url and not re.match(r"^https?://", p, re.I)), "")

    idx = parts.index(url)
    rest = parts[idx + 1 :]
    domain = rest[0] if len(rest) >= 1 else ""
    token = rest[1] if len(rest) >= 2 else ""

    if not alias:
        alias = re.sub(r"^https?://", "", url).split("/")[0]

    return ServerConfig(alias=alias, base_url=url, domain=domain, token=token)


@register(
    PLUGIN_NAME,
    "WorkBuddy",
    "查询自定义 Hydro(OJ) 服务器的排行榜：优先走排行榜 API，不可用时自动回退解析网页；支持多站点、分页、按用户名/UID 查找，并可渲染为图片。",
    "1.1.0",
)
class HydroRankPlugin(Star):
    """Hydro 排行榜查询插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._clients: dict[str, HydroClient] = {}
        self._servers_sig = ""
        self._cache_dir = self._resolve_cache_dir()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_cache_dir() -> str:
        try:
            base = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:  # noqa: BLE001 - 旧版本 / 特殊加载方式兜底
            base = os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_NAME)
        path = os.path.join(str(base), "cache")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            path = str(base)
        return path

    async def terminate(self):
        """插件卸载/停用时关闭所有 HTTP 会话。"""
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #

    def _cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return value

    def _load_servers(self) -> dict[str, ServerConfig]:
        """从插件配置里解析出所有站点。"""
        servers: dict[str, ServerConfig] = {}

        base_url = str(self._cfg("base_url", "") or "").strip()
        if base_url:
            alias = str(self._cfg("default_server_alias", "main") or "main").strip() or "main"
            servers[alias] = ServerConfig(
                alias=alias,
                base_url=base_url,
                domain=str(self._cfg("domain", "") or "").strip(),
                token=str(self._cfg("token", "") or "").strip(),
            )

        extra = str(self._cfg("extra_servers", "") or "")
        for line in extra.splitlines():
            parsed = _split_extra_line(line)
            if parsed is None:
                continue
            if parsed.alias in servers:
                logger.warning(f"[{PLUGIN_NAME}] 站点别名重复，已跳过：{parsed.alias}")
                continue
            servers[parsed.alias] = parsed

        return servers

    def _get_client(self, server: ServerConfig) -> HydroClient:
        key = f"{server.alias}|{server.base_url}|{server.domain}|{server.token}"
        client = self._clients.get(key)
        if client is None:
            client = HydroClient(
                alias=server.alias,
                base_url=server.base_url,
                domain=server.domain,
                token=server.token,
                timeout=int(self._cfg("timeout", 15)),
                proxy=str(self._cfg("proxy", "") or ""),
                user_agent=str(self._cfg("user_agent", "") or ""),
                prefer_api=bool(self._cfg("prefer_api", True)),
            )
            # 配置变更后旧的会话不再需要，顺手清理
            for old_key, old in list(self._clients.items()):
                if old_key.split("|")[0] == server.alias and old_key != key:
                    self._clients.pop(old_key, None)
                    asyncio.ensure_future(old.close())
            self._clients[key] = client
        return client

    # ------------------------------------------------------------------ #
    # 指令解析
    # ------------------------------------------------------------------ #

    @staticmethod
    def _message_str(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:  # noqa: BLE001
                pass
        return str(getattr(event, "message_str", "") or "")

    def _extract_args(self, event: AstrMessageEvent) -> list[str]:
        """取出指令后面的参数（去掉指令名，并兼容可能残留的唤醒前缀）。"""
        tokens = self._message_str(event).strip().split()
        while tokens:
            head = tokens[0].lstrip("/!！／.。").lower()
            if head in COMMAND_NAMES:
                tokens.pop(0)
                continue
            break
        return tokens

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        for name in ("is_admin", "is_admin_user"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    return bool(fn())
                except Exception:  # noqa: BLE001
                    return False
        return False

    # ------------------------------------------------------------------ #
    # 指令入口
    # ------------------------------------------------------------------ #

    @filter.command("hydro_rank", alias={"排名", "hydrorank", "hydro排名"})
    async def hydro_rank(self, event: AstrMessageEvent):
        """查询 Hydro 排行榜。用法：/hydro_rank [数量] | page <页码> | find <用户名> | sites"""
        async for result in self._handle(event):
            yield result

    async def _handle(self, event: AstrMessageEvent):
        servers = self._load_servers()
        if not servers:
            yield event.plain_result(
                "⚠️ 还没有配置 Hydro 服务器。\n"
                "请打开 AstrBot WebUI → 插件 → Hydro 排名查询 → 配置，"
                "填写「站点地址」（如 https://hydro.ac），保存后再试。"
            )
            return

        args = self._extract_args(event)

        # 站点别名与子命令，顺序不限："校赛 find x" 和 "find 校赛 x" 都支持
        server: ServerConfig | None = None
        sub = ""
        while args:
            head = args[0].lower()
            if not sub and head in SUB_FIND:
                sub = "find"
                args = args[1:]
                continue
            if not sub and head in SUB_USER:
                sub = "user"
                args = args[1:]
                continue
            if server is None:
                matched = next((c for a, c in servers.items() if a.lower() == head), None)
                if matched is not None:
                    server = matched
                    args = args[1:]
                    continue
            break

        if args and args[0].lower() in SUB_HELP:
            yield event.plain_result(HELP_TEXT)
            return
        if args and args[0].lower() in SUB_SITES:
            yield event.plain_result(self._format_sites(servers))
            return

        if server is None:
            default_alias = str(self._cfg("default_server_alias", "main") or "main").strip()
            server = servers.get(default_alias) or next(iter(servers.values()))

        # 剩余参数
        page = 1
        count: int | None = None
        query_parts: list[str] = []
        i = 0
        while i < len(args):
            token = args[i]
            low = token.lower()
            if low == "page" and i + 1 < len(args) and args[i + 1].isdigit():
                page = max(1, int(args[i + 1]))
                i += 2
                continue
            m = re.fullmatch(r"(\d+)\s*页", token)
            if m:
                page = max(1, int(m.group(1)))
                i += 1
                continue
            if token.isdigit():
                count = int(token)
                i += 1
                continue
            query_parts.append(token)
            i += 1

        if sub in ("find", "user") or query_parts:
            query = " ".join(query_parts).strip()
            if not query:
                yield event.plain_result(
                    "请给出要查找的用户名或 UID。例如：/hydro_rank find Macesuted"
                )
                return
            if self._cfg("admin_only_find", False) and not self._is_admin(event):
                yield event.plain_result("⚠️ 查找用户排名功能当前仅管理员可用。")
                return
            async for result in self._handle_find(event, server, query):
                yield result
            return

        async for result in self._handle_rank(event, server, page, count):
            yield result

    # ------------------------------------------------------------------ #
    # 排行榜
    # ------------------------------------------------------------------ #

    async def _handle_rank(self, event, server: ServerConfig, page: int, count: int | None):
        default_rows = int(self._cfg("rows_per_page", 10))
        show_rows = max(1, min(int(count or default_rows), 100))
        ttl = int(self._cfg("cache_ttl", 60))
        client = self._get_client(server)

        try:
            rank_page: RankPage = await client.fetch_ranking(page, ttl=ttl)
        except HydroError as exc:
            yield event.plain_result(f"❌ 获取 {server.alias} 的排行榜失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 排行榜请求异常")
            yield event.plain_result(f"❌ 请求异常：{type(exc).__name__}: {exc}")
            return

        if rank_page.page > rank_page.total_pages:
            yield event.plain_result(
                f"⚠️ 页码超出范围：{server.alias} 共 {rank_page.total_pages} 页。"
            )
            return

        rows = rank_page.rows[:show_rows]
        if not rows:
            yield event.plain_result(f"⚠️ {server.alias} 第 {page} 页没有数据。")
            return

        if self._cfg("enable_image", True):
            path = await asyncio.to_thread(
                render_image,
                rank_page,
                rows=rows,
                show_bio=bool(self._cfg("show_bio", False)),
                font_path=str(self._cfg("font_path", "") or ""),
                out_dir=self._cache_dir,
            )
            if path:
                await asyncio.to_thread(cleanup_cache, self._cache_dir)
                yield event.image_result(path)
                return

        yield event.plain_result(
            render_text(rank_page, rows=rows, show_bio=bool(self._cfg("show_bio", False)))
        )

    # ------------------------------------------------------------------ #
    # 查找用户
    # ------------------------------------------------------------------ #

    async def _handle_find(self, event, server: ServerConfig, query: str):
        max_pages = max(1, int(self._cfg("max_scan_pages", 20)))
        ttl = int(self._cfg("cache_ttl", 60))
        client = self._get_client(server)

        yield event.plain_result(
            f"🔍 正在 {server.alias} 中查找「{query}」，最多扫描 {max_pages} 页…"
        )

        try:
            hits: list[RankRow]
            hits, scanned, total_pages = await client.find_user(query, max_pages=max_pages, ttl=ttl)
        except HydroError as exc:
            yield event.plain_result(f"❌ 查找失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 查找用户异常")
            yield event.plain_result(f"❌ 查找异常：{type(exc).__name__}: {exc}")
            return

        if not hits:
            yield event.plain_result(
                f"😕 在前 {scanned} 页（约 {scanned * 100} 名）中没有找到「{query}」。\n"
                f"该站点共 {total_pages} 页，可调大插件配置中的「查找最大扫描页数」后重试。"
            )
            return

        first = hits[0]
        # 用命中所在的页重新取一次，拿到完整的行上下文（并列展示名次附近的用户）
        page_of_hit = max(1, (first.rank - 1) // 100 + 1) if first.rank else 1
        try:
            rank_page = await client.fetch_ranking(page_of_hit, ttl=ttl)
        except HydroError:
            rank_page = None

        highlight = {first.username, str(first.uid or "")}
        header = (
            f"🎯 找到 {len(hits)} 个匹配（已扫描 {scanned}/{min(total_pages, max_pages)} 页）\n"
            f"🏆 {first.username}"
            + (f"（UID {first.uid}）" if first.uid else "")
            + (f" 第 {first.rank} 名" if first.rank else "")
            + f"\n🌐 {server.base_url}"
        )

        if rank_page is not None:
            rows = [r for r in rank_page.rows if r.uid in {h.uid for h in hits}] or hits
            if self._cfg("enable_image", True):
                path = await asyncio.to_thread(
                    render_image,
                    rank_page,
                    rows=rows,
                    show_bio=bool(self._cfg("show_bio", False)),
                    highlight=highlight,
                    font_path=str(self._cfg("font_path", "") or ""),
                    out_dir=self._cache_dir,
                )
                if path:
                    yield event.plain_result(header)
                    await asyncio.to_thread(cleanup_cache, self._cache_dir)
                    yield event.image_result(path)
                    return

        lines = [header, "─" * 26]
        keys = [k for k in ("rp", "problem", "contest", "accept") if k in first.values]
        for row in hits:
            detail = " · ".join(f"{k.upper()} {row.value(k)}" for k in keys if row.value(k) != "-")
            lines.append(f"#{row.rank}  {row.username}" + (f"  [{row.level}]" if row.level else ""))
            if detail:
                lines.append(f"      {detail}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 站点列表
    # ------------------------------------------------------------------ #

    def _format_sites(self, servers: dict[str, ServerConfig]) -> str:
        default_alias = str(self._cfg("default_server_alias", "main") or "main").strip()
        lines = ["🌐 已配置的 Hydro 站点：", ""]
        for alias, cfg in servers.items():
            tag = "（默认）" if alias == default_alias else ""
            lines.append(f"· {alias}{tag}")
            lines.append(f"    {cfg.base_url}" + (f"  ·  域 {cfg.domain}" if cfg.domain else ""))
        lines.append("")
        lines.append("用法：/hydro_rank " + next(iter(servers)) + " 20")
        return "\n".join(lines)
