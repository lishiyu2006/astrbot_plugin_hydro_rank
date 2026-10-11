/**
 * ranking-api —— 给 Hydro 增加一个官方风格的排行榜 API 操作。
 *
 * 为什么需要它：
 *   Hydro 核心的 HTTP API（/api/:op）只注册了 8 个操作
 *   （domain / domain.current / groups / domain.group / problem / problems / user / users），
 *   没有排行榜；而 /d/<域>/ranking 的 JSON 模式返回的是原始用户文档，
 *   rp / rank / nAccept 这些字段被 User.serialize() 刻意丢弃。
 *
 *   但 api.provide() 是框架公开的扩展点，所以本插件在服务端补上这个操作，
 *   让外部程序（如 AstrBot 机器人）能用干净的 JSON 拿排行榜，不必解析 HTML。
 *
 * 调用：
 *   POST /api/ranking
 *   { "domainId": "system", "page": 1, "limit": 100 }
 *   { "domainId": "system", "search": "liujiameng" }   // 按用户名前缀查
 *
 * 返回行里的 rank 语义：
 *   - 分页浏览（无 search）：与排名页一致的下标名次 (page-1)*limit + i + 1
 *   - 查找（有 search）：该用户在本域榜单上的真实名次（rp 高于他的人数 + 1）
 *   两者都不受“当前结果被过滤”影响；rankStored 始终是 Hydro 自己算好并落库的 rank。
 */
import {
    Context, DomainModel, PERM, PermissionError, Query, Schema, UserModel,
} from 'hydrooj';

const DEFAULT_LIMIT = 100;
const MAX_LIMIT = 100;

/** 与核心 DomainRankHandler 保持一致的筛选条件。 */
const baseFilter = () => ({ uid: { $gt: 1 }, rp: { $gt: 0 }, join: true });

/** 统计“排在我前面的人数”用的筛选条件（名次 = 该人数 + 1）。 */
const rankFilter = (rp: number) => ({ uid: { $gt: 1 }, join: true, rp: { $gt: rp } });

const RankingApi = {
    ranking: Query(Schema.object({
        domainId: Schema.string(),
        page: Schema.number().default(1),
        limit: Schema.number().default(DEFAULT_LIMIT),
        search: Schema.string(),
    }), async (ctx: any, args: any) => {
        const domainId = args.domainId || ctx?.domain?._id || 'system';
        const page = Math.max(1, Math.floor(args.page || 1));
        const limit = Math.min(MAX_LIMIT, Math.max(1, Math.floor(args.limit || DEFAULT_LIMIT)));

        if (!ctx?.user?.hasPerm?.(PERM.PERM_VIEW_RANKING)) {
            throw new PermissionError(PERM.PERM_VIEW_RANKING);
        }

        const filter: any = baseFilter();

        if (args.search) {
            const matched = await UserModel.getPrefixList(domainId, args.search, MAX_LIMIT);
            const uids = matched.filter(Boolean).map((u: any) => u._id);
            if (!uids.length) {
                return { domainId, page, limit, upcount: 0, ucount: 0, rows: [] };
            }
            filter.uid = { $in: uids };
        }

        const total = await DomainModel.collUser.countDocuments({ domainId, ...filter });
        const dudocs = await DomainModel
            .getMultiUserInDomain(domainId, filter)
            .sort({ rp: -1 })
            .skip((page - 1) * limit)
            .limit(limit)
            .toArray();

        const udict = await UserModel.getList(domainId, dudocs.map((d: any) => d.uid));

        // search 模式下结果是“被过滤的子集”，按数组下标算名次会失真（每个人都是第 1 名），
        // 因此改为反查真实名次：名次 = 榜单里 rp 严格高于本行的人数 + 1。
        // 非 search（分页浏览）时沿用与排名页一致的下标算法，零额外开销。
        let ranks: number[] | null = null;
        if (args.search) {
            const distinctRp: number[] = [...new Set(dudocs.map((d: any) => d.rp))];
            const above = new Map<number, number>();
            await Promise.all(distinctRp.map(async (rp: number) => {
                above.set(rp, await DomainModel.collUser.countDocuments({ domainId, ...rankFilter(rp) }));
            }));
            ranks = dudocs.map((d: any) => (above.get(d.rp) || 0) + 1);
        }

        return {
            domainId,
            page,
            limit,
            upcount: Math.ceil(total / limit),
            ucount: total,
            rows: dudocs.map((d: any, i: number) => {
                const u: any = udict[d.uid] || {};
                return {
                    rank: ranks ? ranks[i] : (page - 1) * limit + i + 1,
                    uid: d.uid,
                    uname: u.uname || '',
                    displayName: d.displayName || u.displayName || '',
                    rp: Math.round(d.rp || 0),
                    rankStored: d.rank ?? null,
                    level: d.level ?? 0,
                    nSubmit: d.nSubmit || 0,
                    nAccept: d.nAccept || 0,
                    rpInfo: d.rpInfo || {},
                };
            }),
        };
    }),
};

declare module '@hydrooj/framework' {
    interface Apis {
        ranking: typeof RankingApi;
    }
}

export const name = 'ranking-api';

export const Config = Schema.object({
    enable: Schema.boolean().default(true).description('启用排行榜 API'),
}).description('Ranking API Config');

export function apply(ctx: Context, config: ReturnType<typeof Config>) {
    if (config && config.enable === false) return;
    ctx.inject(['api'], ({ api }: any) => {
        api.provide(RankingApi);
    });
}
