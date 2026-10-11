/**
 * 用真实 MongoDB 跑一遍 hydro_ranking_api 插件。
 *
 * 做法：把插件的 index.ts 用 esbuild 真编译，把 `hydrooj` 指向 mock_hydrooj.cjs，
 * 其余（Schema 校验、Mongo 查询、集合名）全部走真实实现。
 */
const path = require('path');
const fs = require('fs');
const { MongoMemoryServer } = require('mongodb-memory-server');
const { MongoClient } = require('mongodb');
const esbuild = require('esbuild');

const ADDON = path.join(__dirname, '..', 'index.ts');
const MOCK = path.join(__dirname, 'mock_hydrooj.cjs');

let pass = 0, fail = 0;
const failures = [];
function check(label, cond, extra = '') {
    if (cond) { pass++; console.log(`  [PASS] ${label}`); }
    else { fail++; failures.push(label); console.log(`  [FAIL] ${label}${extra ? '  -> ' + extra : ''}`); }
}
function eq(label, actual, expected) {
    check(label, JSON.stringify(actual) === JSON.stringify(expected), `实际=${JSON.stringify(actual)} 期望=${JSON.stringify(expected)}`);
}

async function main() {
    // ---------- 1. 启动真实 MongoDB ----------
    const mongo = await MongoMemoryServer.create();
    const client = new MongoClient(mongo.getUri());
    await client.connect();
    const db = client.db('hydro');

    // ---------- 2. 建索引 + 灌数据（模拟真实 Hydro 的 domain.user / user 集合）----------
    const dudocColl = db.collection('domain.user');
    const userColl = db.collection('user');
    await dudocColl.createIndex({ domainId: 1, rp: -1 });
    await userColl.createIndex({ unameLower: 1 });

    const seedUsers = [
        { _id: 0, uname: 'system', unameLower: 'system' },
        { _id: 1, uname: 'Guest', unameLower: 'guest' },
        { _id: 2, uname: 'alice', unameLower: 'alice' },
        { _id: 3, uname: 'bob', unameLower: 'bob' },
        { _id: 4, uname: 'carol', unameLower: 'carol' },
        { _id: 5, uname: 'dave', unameLower: 'dave' },
        { _id: 6, uname: 'eve', unameLower: 'eve' },
        { _id: 7, uname: 'frank', unameLower: 'frank' },
        { _id: 8, uname: 'liujiameng', unameLower: 'liujiameng' },
        { _id: 9, uname: 'xiao-liu', unameLower: 'xiao-liu' },
    ];
    await userColl.insertMany(seedUsers);

    const seedDudocs = [
        // uid 0/1 必须被 uid:{$gt:1} 过滤掉
        { domainId: 'system', uid: 0, rp: 99999, join: true, rank: 1, level: 10 },
        { domainId: 'system', uid: 1, rp: 88888, join: true, rank: 1, level: 10 },
        // 正式榜单（join=true, rp>0）
        { domainId: 'system', uid: 2, rp: 500, join: true, rank: 1, level: 10, nSubmit: 300, nAccept: 200, rpInfo: { problem: 400, contest: 100 }, displayName: '爱丽丝' },
        { domainId: 'system', uid: 3, rp: 300, join: true, rank: 2, level: 8, nSubmit: 200, nAccept: 150, rpInfo: { problem: 250, contest: 50 } },
        { domainId: 'system', uid: 4, rp: 200, join: true, rank: 3, level: 6, nSubmit: 120, nAccept: 90, rpInfo: { problem: 200, contest: 0 }, displayName: '卡罗尔' },
        { domainId: 'system', uid: 8, rp: 150, join: true, rank: 4, level: 5, nSubmit: 80, nAccept: 60, rpInfo: { problem: 100, contest: 50 } },
        { domainId: 'system', uid: 5, rp: 100, join: true, rank: 5, level: 4, nSubmit: 40, nAccept: 30, rpInfo: { problem: 100, contest: 0 } },
        // 必须被过滤掉
        { domainId: 'system', uid: 6, rp: 0, join: true, rank: null, level: 0 },
        { domainId: 'system', uid: 7, rp: 250, join: false, rank: null, level: 0 },
        { domainId: 'system', uid: 9, rp: 50, join: true, rank: 6, level: 3, rpInfo: { problem: 50, contest: 0 } },
        // 另一个域，必须不被串到
        { domainId: 'other', uid: 2, rp: 7777, join: true, rank: 1, level: 10 },
    ];
    await dudocColl.insertMany(seedDudocs);

    // ---------- 3. 断言集合名真的是 domain.user ----------
    const colls = (await db.listCollections().toArray()).map((c) => c.name).sort();
    console.log('\n[1] 数据库位置');
    check('集合名恰为 "domain.user"', colls.includes('domain.user'), `实际集合: ${colls.join(', ')}`);
    check('domain.user 里有种子数据', (await dudocColl.countDocuments({})) === seedDudocs.length);

    // ---------- 4. 编译并加载真实插件 ----------
    const built = await esbuild.build({
        entryPoints: [ADDON],
        bundle: true, platform: 'node', format: 'cjs', write: false,
        external: ['schemastery', 'mongodb'],
        plugins: [{
            name: 'alias-hydrooj',
            setup(b) { b.onResolve({ filter: /^hydrooj$/ }, () => ({ path: MOCK, external: true })); },
        }],
    });
    const tmp = path.join(__dirname, `.built_addon_${Date.now()}.cjs`);
    fs.writeFileSync(tmp, built.outputFiles[0].text);
    const addon = require(tmp);

    check('插件导出 name', addon.name === 'ranking-api', addon.name);
    check('插件导出 Config', !!addon.Config);
    check('插件导出 apply', typeof addon.apply === 'function');

    // ---------- 5. 接线：注入真实 collection，调用 apply ----------
    const mock = require(MOCK);
    mock.DomainModel.collUser = dudocColl;
    mock.UserModel._userColl = userColl;
    mock.UserModel._dudocColl = dudocColl;

    const provided = {};
    const fakeCtx = {
        inject(services, cb) { cb({ api: { provide: (calls) => Object.assign(provided, calls) } }); },
    };
    addon.apply(fakeCtx, { enable: true });
    check('apply 注册了 ranking 操作', !!provided.ranking, Object.keys(provided).join(','));
    check('注册的操作类型是 Query', provided.ranking?.type === 'Query', provided.ranking?.type);

    // ---------- 6. 模拟 ApiService.execute：先 Schema 校验，再调 func ----------
    const userWithPerm = { _id: 2, uname: 'alice', hasPerm: (p) => (mock.PERM.PERM_DEFAULT & p) === p };
    const userNoPerm = { _id: 3, uname: 'bob', hasPerm: () => false };

    async function call(rawArgs, { user = userWithPerm, domain = { _id: 'system' } } = {}) {
        const args = provided.ranking.input(rawArgs);       // 真实 schemastery 校验
        return await provided.ranking.func({ user, domain }, args);
    }

    // ---------- 7. 断言 ----------
    console.log('\n[2] 分页与排序（limit=2）');
    const p1 = await call({ domainId: 'system', page: 1, limit: 2 });
    eq('page1 返回 2 行', p1.rows.length, 2);
    eq('page1 名次 = 1,2', p1.rows.map((r) => r.rank), [1, 2]);
    eq('page1 按 rp 降序', p1.rows.map((r) => r.rp), [500, 300]);
    eq('page1 用户名', p1.rows.map((r) => r.uname), ['alice', 'bob']);
    eq('upcount = 3 页', p1.upcount, 3);
    eq('ucount = 6 人（uid0/1/rp0/未加入 被排除）', p1.ucount, 6);
    eq('字段齐全', Object.keys(p1.rows[0]).sort(),
        ['displayName', 'level', 'nAccept', 'nSubmit', 'rank', 'rankStored', 'rp', 'rpInfo', 'uid', 'uname']);
    eq('rpInfo 原样透出', p1.rows[0].rpInfo, { problem: 400, contest: 100 });
    eq('displayName 透出', p1.rows[0].displayName, '爱丽丝');
    eq('nSubmit/nAccept', [p1.rows[0].nSubmit, p1.rows[0].nAccept], [300, 200]);

    const p2 = await call({ domainId: 'system', page: 2, limit: 2 });
    eq('page2 名次 = 3,4', p2.rows.map((r) => r.rank), [3, 4]);
    eq('page2 用户名', p2.rows.map((r) => r.uname), ['carol', 'liujiameng']);
    eq('page2 rp', p2.rows.map((r) => r.rp), [200, 150]);

    const p9 = await call({ domainId: 'system', page: 9, limit: 2 });
    eq('越界页 rows = []', p9.rows, []);

    console.log('\n[3] 域隔离');
    const other = await call({ domainId: 'other', page: 1, limit: 10 });
    eq('other 域只有 1 人', other.ucount, 1);
    eq('other 域 rp = 7777', other.rows[0].rp, 7777);

    console.log('\n[4] 不传 domainId 时回退到 ctx.domain');
    const viaCtx = await call({ page: 1, limit: 1 });
    eq('domainId 取自 ctx.domain', viaCtx.domainId, 'system');

    console.log('\n[5] search 查找');
    const s1 = await call({ domainId: 'system', search: 'liu' });
    eq('search=liu 命中 1 人', s1.rows.length, 1);
    eq('命中 liujiameng', s1.rows[0]?.uname, 'liujiameng');
    console.log(`      >>> search 返回的 rank = ${s1.rows[0]?.rank}，rankStored = ${s1.rows[0]?.rankStored}`);
    check('search 时 rank 应为真实名次 4（而非过滤后序号 1）', s1.rows[0]?.rank === 4,
        `实际 rank=${s1.rows[0]?.rank}`);

    const s2 = await call({ domainId: 'system', search: 'zzz-nobody' });
    eq('search 无命中 -> rows = []', s2.rows, []);
    eq('search 无命中 -> ucount = 0', s2.ucount, 0);

    console.log('\n[6] 权限');
    let threw = null;
    try { await call({ domainId: 'system' }, { user: userNoPerm }); } catch (e) { threw = e; }
    check('无 PERM_VIEW_RANKING 时抛 PermissionError', threw?.name === 'PermissionError', String(threw));
    let threw2 = null;
    try { await call({ domainId: 'system' }, { user: null }); } catch (e) { threw2 = e; }
    check('ctx.user 缺失时也抛 PermissionError（不会崩）', threw2?.name === 'PermissionError', String(threw2));

    console.log('\n[7] 参数校验（真实 schemastery）');
    let bad = null;
    try { await call({ domainId: 'system', page: '2' }); } catch (e) { bad = e; }
    check('page 传字符串会被拒（客户端必须发 number）', !!bad, String(bad));
    const noSearch = await call({ domainId: 'system' });
    check('不传 search 不会报错', Array.isArray(noSearch.rows));

    await client.close();
    await mongo.stop();
    fs.unlinkSync(tmp);

    console.log(`\n===== 结果：${pass} 通过 / ${fail} 失败 =====`);
    if (failures.length) console.log('失败项:\n  - ' + failures.join('\n  - '));
    process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.error('harness 异常:', e); process.exit(2); });
