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
 */
import {
    Context, DomainModel, PERM, PermissionError, Query, Schema, UserModel,
} from 'hydrooj';

const DEFAULT_LIMIT = 100;
const MAX_LIMIT = 100;

/** 与核心 DomainRankHandler 保持一致的筛选条件。 */
const baseFilter = () => ({ uid: { $gt: 1 }, rp: { $gt: 0 }, join: true });

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

        return {
            domainId,
            page,
            limit,
            upcount: Math.ceil(total / limit),
            ucount: total,
            rows: dudocs.map((d: any, i: number) => {
                const u: any = udict[d.uid] || {};
                return {
                    rank: (page - 1) * limit + i + 1,
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
