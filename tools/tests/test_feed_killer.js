// 信息流广告脚本功能测试（本地模拟 Quantumult X 运行时环境）
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '..', 'quantumultx', 'script', 'feed-killer.js');
const src = fs.readFileSync(SRC, 'utf8');

function run(scriptSrc, url, body) {
    let result = null;
    const sandbox = {
        $request: { url, method: 'GET', headers: {}, body: '' },
        $response: { status: 200, headers: {}, body, url },
        $done: (o) => { result = o; },
        $notify: () => {},
        console,
    };
    const keys = Object.keys(sandbox);
    const fn = new Function(...keys, scriptSrc);
    fn(...keys.map((k) => sandbox[k]));
    return result;
}

let pass = 0;
let fail = 0;
console.log('=== feed-killer.js 功能测试 ===\n');

// 用例 1：混合列表 —— 只摘广告，保留新闻
{
    const body = JSON.stringify({
        code: 0, hasMore: true,
        data: [
            { id: 1, type: 'news', title: '正常新闻一' },
            { id: 2, type: 'ad', adId: 'a1', clickUrl: 'http://x' },
            { id: 3, type: 'news', title: '正常新闻二' },
            { id: 4, isAd: true, title: '这是广告' },
        ],
    });
    const r = run(src, 'https://api.example.com/feed', body);
    const o = r && r.body ? JSON.parse(r.body) : null;
    const ok = o && o.data.length === 2 && o.data.every((x) => x.type === 'news')
        && o.hasMore === true && o.code === 0;
    console.log('【混合列表】4 条中 2 条广告');
    console.log('  结果: ' + (o ? JSON.stringify(o.data) : '(未改动)'));
    ok ? (pass++, console.log('  ✓ 正确剔除广告，保留 2 条新闻\n'))
       : (fail++, console.log('  ✗ 处理有误\n'));
}

// 用例 2：纯广告列表 —— 整体清空
{
    const body = JSON.stringify({
        code: 0,
        list: [
            { adId: 1, creativeId: 1, clickUrl: 'x' },
            { adId: 2, creativeId: 2, clickUrl: 'y' },
        ],
    });
    const r = run(src, 'https://api.example.com/ad/list', body);
    const o = r && r.body ? JSON.parse(r.body) : null;
    const ok = o && o.list.length === 0;
    console.log('【纯广告列表】应整体清空');
    console.log('  结果: ' + (o ? JSON.stringify(o.list) : '(未改动)'));
    ok ? (pass++, console.log('  ✓ 已清空\n')) : (fail++, console.log('  ✗ 未清空\n'));
}

// 用例 3：无广告内容 —— 不应改动
{
    const body = JSON.stringify({
        code: 0,
        data: [{ id: 1, type: 'news' }, { id: 2, type: 'video' }],
    });
    const r = run(src, 'https://api.example.com/feed', body);
    const changed = !!(r && r.body && r.body !== body);
    console.log('【纯正常内容】不应改动');
    changed ? (fail++, console.log('  ✗ 不该被修改\n')) : (pass++, console.log('  ✓ 正确放行\n'));
}

// 用例 4：嵌套结构 —— 深层广告也要清除
{
    const body = JSON.stringify({
        code: 0,
        result: {
            modules: [
                { name: 'banner', items: [{ adId: 9, clickUrl: 'z' }] },
                { name: 'news', items: [{ id: 1, title: '新闻' }] },
            ],
        },
    });
    const r = run(src, 'https://api.example.com/home', body);
    const o = r && r.body ? JSON.parse(r.body) : null;
    const ok = o && o.result.modules[0].items.length === 0
        && o.result.modules[1].items.length === 1;
    console.log('【嵌套结构】深层广告清除，新闻保留');
    console.log('  结果: ' + (o ? JSON.stringify(o.result.modules) : '(未改动)'));
    ok ? (pass++, console.log('  ✓ 处理正确\n')) : (fail++, console.log('  ✗ 处理有误\n'));
}

console.log('通过 ' + pass + ' / 失败 ' + fail);
process.exit(fail ? 1 : 0);
