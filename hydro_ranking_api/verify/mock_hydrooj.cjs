/**
 * 忠实还原 Hydro 的 `hydrooj` 包中本插件用到的那部分。
 *
 * 每一段都对照真实源码逐行复制/等价实现，改动点都写了注释：
 *   - Query/_get      <- framework/framework/api.ts:23-31
 *   - PERM            <- packages/common/permission.ts（真实 bigint 位）
 *   - PermissionError <- packages/hydrooj/src/error.ts:22（Err() 生成的可抛类）
 *   - DomainModel     <- packages/hydrooj/src/model/domain.ts:14,256-258
 *   - UserModel       <- packages/hydrooj/src/model/user.ts:241-247,476-483
 *   - Schema          <- packages/hydrooj/src/libs.ts（即 schemastery 本体）
 */
const Schema = require('schemastery');

// ---- framework/framework/api.ts:23-31 原样复制 ----
const _get = (type) => (schema, func, hooks = []) => ({ input: schema, func, hooks, type });
const Query = _get('Query');
const Mutation = _get('Mutation');

// ---- packages/common/permission.ts 真实位 ----
const PERM = {
    PERM_VIEW: 1n << 0n,
    PERM_VIEW_USER_PRIVATE_INFO: 1n << 1n,
    PERM_EDIT_DOMAIN: 1n << 2n,
    PERM_VIEW_RANKING: 1n << 59n,
    PERM_ALL: -1n,
};
// packages/common/permission.ts:98-153 —— guest/default 都含 PERM_VIEW_RANKING
PERM.PERM_BASIC = PERM.PERM_VIEW | PERM.PERM_VIEW_RANKING;
PERM.PERM_DEFAULT = PERM.PERM_VIEW | PERM.PERM_VIEW_USER_PRIVATE_INFO | PERM.PERM_VIEW_RANKING;

// ---- packages/hydrooj/src/error.ts:22 等价 ----
class PermissionError extends Error {
    constructor(perm) {
        super(`PERMISSION_DENIED: ${perm}`);
        this.name = 'PermissionError';
        this.perm = perm;
    }
}

// ---- 由 harness 注入真实的 mongo collection ----
const DomainModel = {
    collUser: null, // 真实实现：db.collection('domain.user')
    // packages/hydrooj/src/model/domain.ts:256-258 原样复制
    getMultiUserInDomain(domainId, query = {}) {
        return DomainModel.collUser.find({ domainId, ...query });
    },
};

const UserModel = {
    _userColl: null,
    _dudocColl: null,
    _domainId: 'system',
    async _makeUser(uid) {
        const udoc = await UserModel._userColl.findOne({ _id: uid });
        if (!udoc) return null;
        const dudoc = await UserModel._dudocColl.findOne({ domainId: UserModel._domainId, uid });
        const scope = PERM.PERM_DEFAULT;
        return {
            _id: uid,
            uname: udoc.uname,
            displayName: dudoc?.displayName || '',
            perm: PERM.PERM_DEFAULT,
            scope,
            hasPerm(...perms) { return perms.some((p) => (this.perm & this.scope & p) === p); },
            hasPriv() { return false; },
        };
    },
    // packages/hydrooj/src/model/user.ts:241-247 等价
    async getList(domainId, uids) {
        const r = {};
        await Promise.all([...new Set(uids)].map(async (uid) => {
            r[uid] = (await UserModel._makeUser(uid)) || { _id: uid, uname: '', displayName: '' };
        }));
        return r;
    },
    // packages/hydrooj/src/model/user.ts:476-483 等价（含 ^ 前缀锚定）
    async getPrefixList(domainId, prefix, limit = 50) {
        const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const $regex = `^${escapeRegExp(prefix.toLowerCase())}`;
        const udocs = await UserModel._userColl
            .find({ unameLower: { $regex } }).limit(limit).project({ _id: 1 }).toArray();
        const dudocs = await DomainModel
            .getMultiUserInDomain(domainId, { displayName: { $regex } })
            .limit(limit).project({ uid: 1 }).toArray();
        const uids = [...new Set([...udocs.map(({ _id }) => _id), ...dudocs.map(({ uid }) => uid)])];
        return await Promise.all(uids.map((_id) => UserModel._makeUser(_id)));
    },
};

class Context {}

module.exports = { Query, Mutation, Schema, PERM, PermissionError, DomainModel, UserModel, Context };
