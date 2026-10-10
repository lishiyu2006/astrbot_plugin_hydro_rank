# astrbot_plugin_hydro_rank

> 让 AstrBot 接入**你自己的 Hydro（HydroOJ）服务器**排行榜。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.5-0B62D0)](https://astrbot.app)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

支持多站点、分页、按用户名/UID 查找排名，输出图片或纯文本。

![logo](logo.png)

## 功能

- 📊 查询排行榜，自动读取「排名 / 用户名 / RP / 题目 / 比赛 / 签到 / 贡献 / 通过 / 简介」等列（列随站点配置自适应，缺列不会报错）
- 🔌 **双通道取数**：目标站点装了排行榜 API 插件就走 `POST /api/ranking` 拿结构化数据，否则自动回退解析网页，兼容所有 Hydro 站点
- 🖼️ 默认渲染成表格图片，观感好；没有可用中文字体时自动回退为纯文本
- 🌐 支持配置多个 Hydro 站点，用别名切换（如 `/hydro_rank 校赛 20`）
- 🔍 按用户名或 UID 查找某人排名，命中后高亮展示（走 API 时一次请求搞定）
- 🏷️ 支持 Hydro 的「域（domain）」，自动请求 `/d/<域ID>/ranking`
- ⚡ 内置缓存，避免频繁请求对方服务器
- 🌍 插件名称与配置项支持中英文（`.astrbot-plugin/i18n/`）

## 目录结构

```
astrbot_plugin_hydro_rank/
├── metadata.yaml            # 插件元数据（AstrBot 靠它识别插件）
├── main.py                  # 插件入口：指令注册与业务编排
├── hydro_client.py          # Hydro 排行榜抓取与 HTML/JSON 解析
├── renderer.py              # 文本 / 图片渲染
├── _conf_schema.json        # 配置 Schema（WebUI 可视化配置）
├── requirements.txt         # 依赖：aiohttp、Pillow
├── logo.png                 # 插件 Logo（256×256，1:1）
├── pyproject.toml           # 仅开发期 ruff 配置
├── .astrbot-plugin/
│   └── i18n/
│       ├── zh-CN.json       # 中文文案
│       └── en-US.json       # 英文文案
├── .gitignore
├── LICENSE
└── README.md
```

## 安装

### 方式一：从插件市场 / 仓库地址安装（推荐）

在 AstrBot WebUI 的 `插件 → 插件市场` 搜索，或直接在插件面板填入仓库地址：

```
https://github.com/lishiyu2006/astrbot_plugin_hydro_rank
```

### 方式二：上传 zip

在 WebUI 的插件页选择上传 `astrbot_plugin_hydro_rank-<版本>.zip`。

### 方式三：手动解压

把整个 `astrbot_plugin_hydro_rank` 目录放到 AstrBot 的 `data/plugins/` 下，然后重启 AstrBot 或在插件页点「重载插件」。

```
AstrBot/data/plugins/astrbot_plugin_hydro_rank/
```

### 方式四：开发模式

```bash
cd AstrBot/data/plugins
git clone https://github.com/lishiyu2006/astrbot_plugin_hydro_rank
```

插件会自动安装 `requirements.txt` 中的依赖（`aiohttp`、`Pillow`）。安装后在 WebUI → 插件 → **Hydro 排行榜查询** → 配置，填写站点地址并保存。

### 可选：给 Hydro 装配套插件（开启 API 通道）

本仓库的 [`hydro_ranking_api/`](./hydro_ranking_api) 目录是一个 **Hydro 插件（addon）**，它在你的 Hydro 实例上注册 `POST /api/ranking`，让本插件能拿到结构化排行榜数据（更快、更稳）。**不装也能用**，会自动回退解析网页。

在你的 Hydro 服务器上执行（假设已部署好 Hydro）：

```bash
# 1. 把 hydro_ranking_api 目录放到 Hydro 的 addons 目录
mkdir -p ~/.hydro/addons/ranking-api
cp hydro_ranking_api/index.ts hydro_ranking_api/package.json ~/.hydro/addons/ranking-api/

# 2. 启用插件
hydrooj addon add ranking-api

# 3. 重启 Hydro（按你的部署方式，例如 systemd / pm2 / docker）
```

验证：

```bash
curl -s -X POST https://你的域名/api/ranking \
  -H 'Content-Type: application/json' \
  -d '{"domainId":"system","page":1,"limit":5}'
```

详见 [`hydro_ranking_api/README.md`](./hydro_ranking_api/README.md)。

## 配置项

| 配置 | 说明 |
| --- | --- |
| 站点地址 `base_url` | **必填**。如 `https://hydro.ac`、`https://oj.example.com`。也可以直接粘 `https://oj.example.com/d/system/ranking` 这种完整地址 |
| 域 ID `domain` | 可选。填写后请求 `/d/<域ID>/ranking`；留空则查询主域。Hydro 默认域 ID 为 `system` |
| 默认站点别名 `default_server_alias` | 默认 `main`，用于多站点时区分 |
| 访问令牌 `token` | 可选。以 `Authorization: Bearer <token>` 发送，用于排行榜需要登录才可见的站点 |
| 额外站点 `extra_servers` | 每行一个，格式 `别名\|站点地址\|域ID\|Token`（后三项可省略）。以 `#` 开头的行会被忽略 |
| 优先使用排行榜 API `prefer_api` | 默认开启。目标站点装了 [`hydro_ranking_api`](https://github.com/lishiyu2006/astrbot_plugin_hydro_rank/tree/main/hydro_ranking_api) 时走 `POST /api/ranking`；接口不可用自动回退解析网页。关闭则始终解析网页 |
| 每次展示条数 `rows_per_page` | 默认 10，最大 100 |
| 渲染为图片 `enable_image` | 默认开启 |
| 显示个人简介列 `show_bio` | 默认关闭 |
| 查找最大扫描页数 `max_scan_pages` | 默认 20（约 2000 名），每页 100 人 |
| 查找功能仅管理员可用 `admin_only_find` | 默认关闭 |
| 请求超时 `timeout` | 默认 15 秒 |
| 缓存时间 `cache_ttl` | 默认 60 秒，设为 0 关闭缓存 |
| 字体路径 `font_path` | 可选。图片中文显示为方框时指定一个 `.ttf`/`.ttc` |
| HTTP 代理 `proxy` | 可选，如 `http://127.0.0.1:7890` |
| User-Agent `user_agent` | 可选，部分站点会校验 UA |

`extra_servers` 示例：

```
主站|https://hydro.ac
校赛|https://oj.school.edu.cn|system
私有站|https://oj.internal.net|system|your-access-token
```

## 指令

指令名 `hydro_rank`，别名 `排名` / `hydrorank` / `hydro排名`。

```
/hydro_rank                      排行榜前 10 名（默认站点）
/hydro_rank 20                   排行榜前 20 名
/hydro_rank page 2               第 2 页（第 101-200 名）的前 10 条
/hydro_rank 20 page 2            第 2 页的前 20 条
/hydro_rank 2页 20               同上，页码也支持 “2页” 写法
/hydro_rank find Macesuted       查找某人的排名（跨页搜索，命中后高亮）
/hydro_rank user 1425            按 UID 查找
/hydro_rank 校赛 20              指定站点（站点别名）
/hydro_rank 校赛 find baoshuo    站点别名 + 子命令，顺序可互换
/hydro_rank sites                列出已配置的站点
/hydro_rank help                 帮助
```

## 实现原理

Hydro 的排名页由 `packages/hydrooj/src/handler/domain.ts` 中的 `DomainRankHandler` 提供：

```ts
ctx.Route('ranking', '/ranking', DomainRankHandler, PERM.PERM_VIEW_RANKING);

class DomainRankHandler extends Handler {
    @query('page', Types.PositiveInt, true)
    async get(domainId: string, page = 1) { /* ... */ }
}
```

本插件按可靠性从高到低自动选择取数方式：

1. **排行榜 API（首选，需目标站点安装 [`hydro_ranking_api`](https://github.com/lishiyu2006/astrbot_plugin_hydro_rank/tree/main/hydro_ranking_api)）**

   `POST {站点根}/api/ranking`，body 为 `{"domainId":"system","page":1,"limit":100}`，
   返回结构化 JSON，含 `rank / uid / uname / rp / level / nSubmit / nAccept / rpInfo`。
   取数不受页面改版影响，且查找用户只需一次请求。

   > Hydro 核心自带的 HTTP API（`/api/:op`）**只有 8 个操作**
   > （`domain` / `domain.current` / `groups` / `domain.group` / `problem` / `problems` / `user` / `users`），
   > **没有排行榜**。上面这个操作由插件补上。
   > 探测失败（404 / 非 JSON / 缺字段 / 无权限）会自动降级到方式 2。

2. **`GET /ranking?page=N`（`Accept: text/html`）** —— 通用兜底。服务端渲染出完整的排行榜 `<table>`，字段最全。插件用标准库 `html.parser` 解析，按 `<th>` 文本识别列，因此不依赖具体主题的 class 名。任何 Hydro 站点都能用。

3. **同地址 + `Accept: application/json`** —— **仅用于取总数**。Hydro 的服务端框架在客户端接受 JSON 时会把 `response.body` 序列化返回：`{"udocs": [...], "upcount": 108, "ucount": 10784, "page": 1}`，其中 `upcount` 是总页数、`ucount` 是总人数。

   > ⚠️ 这里的 `udocs` 是**原始用户文档**（`_id` / `uname` / `mail` / `perm` / `priv` / `role` / `regat` / `loginat` / `avatar`），
   > **不含 RP、排名、题数、通过数**，还带 `mail`。
   > 根因在 `model/setting.ts`：`rp` / `rank` / `nAccept` 等被定义为 `DomainUserSetting` 的 `setting_storage` 族，
   > 既没有 `FLAG_PUBLIC` 也没有 `FLAG_PRIVATE`；而 `model/user.ts` 的 `User.serialize()`
   > 只输出 `pick(this, getFields('public'))`，于是这些字段**永远被 JSON 序列化丢弃**。
   > 所以这个接口拿不到排行榜数据，只能用来取总数。

想关掉 API 通道、始终解析网页，把配置项「优先使用排行榜 API」设为关闭即可。

## 常见问题

**Q：提示「页面中没有找到排行榜」？**
确认地址填的是 Hydro 站点根地址（不是某个题目/比赛页），且站点没有关闭 `PERM_VIEW_RANKING` 权限。若排行榜需要登录才能看，请配置 `token`。

**Q：图片里的中文是方框？**
系统缺少中文字体。在配置的「字体路径」里填一个中文字体文件，例如 Windows 的 `C:\Windows\Fonts\msyh.ttc`，或 Linux 的 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`。找不到可用字体时会自动退回纯文本模式。

**Q：查找用户很慢？**
查找需要逐页扫描排行榜，每页 100 人。默认扫描前 20 页（约 2000 名）。调小 `max_scan_pages` 可更快返回，调大则能覆盖更多用户。

**Q：会不会把对方服务器打挂？**
默认带 60 秒缓存，且查找时并发上限为 4，命中即停止。请勿把 `max_scan_pages` 和调用频率设得过高。

**Q：兼容性？**
需要 AstrBot ≥ 4.5（使用 `metadata.yaml` 元数据与 `filter.command` 的 `alias` 参数）。Hydro 侧适用于保留默认排行榜页面的版本（v4 / v5 均已验证）。

## 数据目录

插件产生的图片缓存位于 `data/plugin_data/astrbot_plugin_hydro_rank/cache/`，超过 1 小时会自动清理。

## 发布到插件市场（维护者）

AstrBot 使用 GitHub 托管插件，发布入口是 [cloud.astrbot.app/publish](https://cloud.astrbot.app/publish)。

```bash
# 0) 先把 metadata.yaml 里的 lishiyu2006 换成你的 GitHub 用户名/组织名
gh auth login                                   # 只需一次
OWNER=$(gh api user --jq .login)
sed -i "s/lishiyu2006/$OWNER/g" metadata.yaml

# 1) 初始化仓库并推送
git init && git add . && git commit -m "feat: v1.0.0 初始版本"
gh repo create "$OWNER/astrbot_plugin_hydro_rank" --public --source=. --remote=origin --push

# 2) 打包（zip 必须小于 16MB）
cd .. && zip -r "astrbot_plugin_hydro_rank-1.0.0.zip" astrbot_plugin_hydro_rank \
  -x "*/__pycache__/*" -x "*/.git/*"
```

发布前请确认：

- `metadata.yaml` 的 `author`、`name`、`version` 与市场记录一致（`plugin_id = author/name`）
- `repo` 是 `https://github.com/{owner}/{repository}` 形式，不能是子目录 / release / issue 链接
- 仓库里不要提交 `.git`、`__pycache__`、`node_modules`、开发用配置
- 仓库根目录直接包含 `metadata.yaml`（即本目录内容作为仓库根）

## 致谢与灵感来源

- 插件骨架、`Star` 生命周期与指令注册写法参考 AstrBot 官方插件模板 [Soulter/helloworld](https://github.com/Soulter/helloworld)。
- 排行榜取数方式（`/ranking` 路由、`page` 参数、`Accept: application/json` 返回 `upcount`/`ucount`）是通过阅读 [hydro-dev/Hydro](https://github.com/hydro-dev/Hydro) 源码（`packages/hydrooj/src/handler/domain.ts` 等）确定的，本插件仅通过其公开的 HTTP 页面/接口读取数据。
- 本插件与 Hydro 官方、AstrBot 官方均无隶属关系。

## 许可

MIT，详见 [LICENSE](LICENSE)。
