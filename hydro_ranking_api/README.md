# ranking-api —— Hydro 排行榜 API 插件

给 Hydro 补上一个**官方风格的排行榜接口**，让 AstrBot 等外部程序用干净的 JSON 取域排行榜，不必解析 HTML。

## 为什么需要它

Hydro 核心自带的 HTTP API 是 `/api/:op`（`packages/hydrooj/src/service/server.ts:111`），
但全核心只注册了 8 个操作：

```
domain · domain.current · groups · domain.group · problem · problems · user · users
```

**没有任何排行榜操作。** 而排行榜页面 `/d/<域>/ranking`：

- 加 `Accept: application/json` 确实会返回 JSON，但内容是**原始用户文档**
  （`_id` / `uname` / `mail` / `perm` / `priv` / `role` / `regat` / `loginat` / `avatar`），
  **不含 RP、排名、题数、通过数**，还顺带泄漏了 `mail`；
- 原因在 `model/setting.ts:288-291`：`rp` / `rank` / `nAccept` / `nSubmit` / `rpInfo` / `level` / `join`
  被定义为 `DomainUserSetting` 的 `setting_storage` 族，**既没有 `FLAG_PUBLIC` 也没有 `FLAG_PRIVATE`**；
- `model/user.ts` 的 `User` 构造器把它们加载成自有属性（所以模板里 `udoc.rp` 读得到、页面上显示得出来），
  但 `User.serialize()` 只输出 `pick(this, getFields('public'))`，这些字段不在名单里 → **永远被 JSON 序列化丢弃**；
- 而 `framework/framework/api.ts` 的 `projection()` 第一件事就是调用 `input.serialize()`，
  所以即使你在 API 请求里写 `projection: "rp"` 也拿不到。

结论：**排行榜数据在服务端是存在的，只是没有对外出口。** 本插件用框架公开的扩展点
`api.provide()` 把它补上。

## 安装

### 方式一：作为 addon 安装（推荐，不改动 Hydro 源码）

```bash
# 1. 创建 addon 目录（Hydro 会自动 yarn init 并建好子目录）
cd /path/to/Hydro
yarn hydrooj addon create ranking-api

# 2. 把本目录的 index.ts 与 package.json 拷进去
cp index.ts package.json ~/.hydro/addons/ranking-api/

# 3. 确认 package.json 里 main 指向 index.ts
#    { "name": "@hydrooj/ranking-api", "main": "index.ts" }

# 4. 注册 addon
yarn hydrooj addon add ranking-api

# 5. 重启 Hydro
pm2 restart hydrooj     # 或 systemctl restart hydrooj / docker restart <容器>
```

### 方式二：放进 packages/ 目录

```bash
cp -r hydro_ranking_api /path/to/Hydro/packages/ranking-api
cd /path/to/Hydro
# 在根 package.json 的 dependencies 里加一行：
#   "@hydrooj/ranking-api": "file:packages/ranking-api"
yarn install
pm2 restart hydrooj
```

> Hydro 直接用 `@hydrooj/register` 运行 TypeScript，不需要预编译。

## 调用

```bash
# 取第 1 页（默认每页 100）
curl -X POST https://your-oj.example.com/api/ranking \
  -H 'Content-Type: application/json' \
  -d '{"domainId":"system","page":1,"limit":100}'

# 查某个用户
curl -X POST https://your-oj.example.com/api/ranking \
  -H 'Content-Type: application/json' \
  -d '{"domainId":"system","search":"liujiameng"}'
```

### 返回结构

```json
{
  "domainId": "system",
  "page": 1,
  "limit": 100,
  "upcount": 108,
  "ucount": 10784,
  "rows": [
    {
      "rank": 1,
      "uid": 3309,
      "uname": "liujiameng",
      "displayName": "",
      "rp": 475,
      "rankStored": 1,
      "level": 10,
      "nSubmit": 294,
      "nAccept": 856,
      "rpInfo": { "problem": 320, "contest": 155 }
    }
  ]
}
```

| 字段 | 含义 |
|---|---|
| `rank` | 名次。**分页浏览**时等于本页位置 `(page-1)*limit + i + 1`（与排名页显示一致）；**带 `search`** 时是该用户在本域榜单上的**真实名次**（rp 严格高于他的人数 + 1），因为此时结果是过滤后的子集，按下标算会失真 |
| `rankStored` | 数据库里 `domain.user.rank` 存的名次（Hydro 的 `calcLevel()` 写入，同分同名，可能为 `null`） |
| `level` | 等级，是 `LEVELS = [100,90,70,55,40,30,20,10,5,2,1]` 的**下标**（越大越好），0 为最低档 |
| `rp` | 总 RP |
| `rpInfo` | RP 来源拆分（`problem` 做题 / `contest` 比赛 / `delta` 手动调整） |
| `nSubmit` / `nAccept` | 提交数 / 通过数 |
| `displayName` | 域内昵称（`DomainUserSetting`，默认空串） |

### 参数类型提示

入参用 `Schema.number()` 声明，**schemastery 不做字符串转换**：`page` 必须传 JSON number，
传 `"2"` 会被拒（HTTP 400 `$.page expected number but got 2`）。
`domainId` / `search` 是可选字符串，不传不会报错。

## 权限

接口沿用核心排行榜页的权限位 `PERM.PERM_VIEW_RANKING`，
没有该权限的请求会抛 `PermissionError`（HTTP 403）。
未登录访客能否访问取决于该域的 `perm` 配置，与网页版排行榜一致。

## 数据从哪来

排行数据存在 MongoDB 的 **`domain.user`** 集合，每条文档就是一行：

```
domainId · uid · rp · rpInfo · rpdelta · rank · level · join · role · displayName · nSubmit · nAccept
```

`packages/hydrooj/src/script/rating.ts` 里的 `calcLevel()` 就是把它按 `rp:-1` 排序后写回 `rank` 和 `level`。
所以**如果你只是想导出一次**，不改代码也能做：

```bash
mongosh hydro --eval 'db.getCollection("domain.user")
  .find({domainId:"system", rp:{$gt:0}, join:true})
  .sort({rp:-1}).limit(100).toArray()'
```

本插件的价值在于把它变成一个**稳定的 HTTP 接口**，供远程程序长期调用。

## 已验证

针对 `hydrooj 5.0.7` 源码逐行核对 + 真实 MongoDB 运行验证：

- **集合位置**：`model/domain.ts:14` 是 `db.collection('domain.user')`；`service/db.ts:68-71` 只在
  配置了 `db.prefix` 时才加前缀（默认无）。已在真实 MongoDB 上断言实际集合名就是 `domain.user`。
- **筛选条件**：与核心 `DomainRankHandler`（`handler/domain.ts:28-32`）逐字一致 ——
  `{ uid: { $gt: 1 }, rp: { $gt: 0 }, join: true }` + `.sort({ rp: -1 })`。
- **分页数学**：`upcount` / `ucount` 与 `service/db.ts:156-165` 的 `paginate()` 等价。
- **字段名**：`rp` / `rpInfo` / `rank` / `level` / `nSubmit` / `nAccept` / `join` / `displayName`
  全部来自 `model/setting.ts:284-294` 的 `DomainUserSetting`。
- **`rpInfo` 的键**：`script/rating.ts:30,57` 的 `RpTypes` 只有 `problem` 与 `contest`。
- **注册方式**：`api.provide()` 与 `declare module '@hydrooj/framework'` 的写法与核心
  `handler/domain.ts:473-492` 完全相同。
- **请求体**：`framework/framework/base.ts:50-52` 把 `ctx.request.body` 并进 `args`，
  `koa-body` 默认解析 `application/json`，所以 JSON POST 可用。
- **权限**：`PERM_VIEW_RANKING` 在 `PERM_BASIC`（guest）与 `PERM_DEFAULT` 里都有，
  匿名请求也能过（`packages/common/permission.ts:98-153`）。
- **限流**：`limitRate('global', 5, 100)` 是 **5 秒 100 次**（参数序为 period,max），不会挡住分页抓取。

运行验证脚本 `verify_ranking_api.cjs`（真实 MongoDB + 真实 schemastery + esbuild 编译真实 `index.ts`）：
**33/33 通过**，覆盖分页/排序/域隔离/越界页/权限/参数校验/search 真实名次。

## License

MIT
