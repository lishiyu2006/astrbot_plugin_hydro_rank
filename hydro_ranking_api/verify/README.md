# 验证脚本

用**真实 MongoDB + 真实 schemastery** 把 `../index.ts` 真编译、真跑一遍，
证明插件确实连到正确的集合、并吐出正确形状的 JSON。

不是"读代码觉得没问题"，而是实际执行。

## 跑法

```bash
cd verify
npm install            # 装 mongodb-memory-server / mongodb / schemastery / esbuild
node verify_ranking_api.cjs
```

首次运行会下载 MongoDB 二进制（约 100 MB），之后走缓存。

期望输出结尾：

```
===== 结果：33 通过 / 0 失败 =====
```

## 它是怎么做到"真实"的

| 环节 | 真实度 |
|---|---|
| `index.ts` | **真实**，用 esbuild 原样编译，不是复制粘贴 |
| Schema 校验 | **真实**，直接用 schemastery 3.x（`hydrooj` 的 `Schema` 就是它） |
| `Query()` 装配 | 逐行复制自 `framework/framework/api.ts:23-31` |
| MongoDB | **真实** MongoDB（mongodb-memory-server），真建集合真查询 |
| 集合名 | 与源码同一表达式 `db.collection('domain.user')` |
| `PERM` 位 | 取自 `packages/common/permission.ts` 的真实 bigint |
| `DomainModel` / `UserModel` | 按 `model/domain.ts:256-258`、`model/user.ts:241-247,476-483` 等价实现 |

`mock_hydrooj.cjs` 里每一段都标了对应的源码位置，方便对照。

## 覆盖的断言

- 集合名恰为 `domain.user`
- 分页 / 按 `rp` 降序 / 名次编号 / `upcount`、`ucount` 数学
- 过滤规则：`uid > 1`、`rp > 0`、`join = true` 各自生效
- 域隔离（不同 `domainId` 不串数据）
- 越界页返回空 `rows` 而不是报错
- `search` 命中 / 无命中 / **返回真实名次而非过滤后序号**
- 权限：无 `PERM_VIEW_RANKING` 抛 `PermissionError`；`ctx.user` 缺失不崩
- 参数类型：`page` 传字符串被拒（schemastery 不做类型转换）
- 不传 `search` 不报错（`Schema.string()` 是可选的）
