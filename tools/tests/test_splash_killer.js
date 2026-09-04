// 开屏广告脚本功能测试（本地模拟 Quantumult X 运行时环境）
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '..', 'quantumultx', 'script', 'splash-killer.js');
const src = fs.readFileSync(SRC, 'utf8');

/** 模拟 QX 执行环境 */
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

const cases = [
    ['B站开屏广告列表', 'https://app.bilibili.com/x/v2/splash/list',
     JSON.stringify({ code: 0, data: { list: [{ id: 1, type: 'ad' }, { id: 2, type: 'ad' }] } }), 'clear'],

    ['首页夹带广告字段', 'https://api.example.com/v3/home',
     JSON.stringify({ code: 0, success: true, data: { feed: [{ t: 'news' }] }, adList: [{ ad_id: 1 }], banner: [{ img: 'x' }] }), 'clear'],

    ['含展示时长字段', 'https://api.example.com/launch/ad',
     JSON.stringify({ code: 0, result: { ad: { id: 1 }, duration: 5, showTime: 5 } }), 'clear'],

    ['无广告的正常响应', 'https://api.example.com/v3/home',
     JSON.stringify({ code: 0, data: { user: { name: 'x' } } }), 'keep'],

    ['非JSON内容', 'https://api.example.com/x', '<html>hi</html>', 'keep'],

    ['空响应体', 'https://api.example.com/x', '', 'keep'],
];

let pass = 0;
let fail = 0;

console.log('=== splash-killer.js 功能测试 ===\n');

for (const [desc, url, body, expect] of cases) {
    const r = run(src, url, body);
    const changed = !!(r && r.body && r.body !== body);

    console.log('【' + desc + '】期望: ' + (expect === 'clear' ? '拦截改写' : '原样放行'));
    console.log('  原始: ' + (body.slice(0, 88) || '(空)'));
    console.log('  结果: ' + (changed ? r.body.slice(0, 88) : '(未改动)'));

    if (expect === 'keep') {
        if (changed) { fail++; console.log('  ✗ 不该被修改却修改了'); }
        else { pass++; console.log('  ✓ 正确放行'); }
    } else {
        if (changed) { pass++; console.log('  ✓ 已拦截'); }
        else { fail++; console.log('  ✗ 未拦截'); }
    }
    console.log('');
}

// 关键校验：状态字段必须保留
const r = run(src, 'https://api.example.com/launch/ad',
    JSON.stringify({ code: 0, success: true, data: { ad: [1] } }));
if (r && r.body) {
    const o = JSON.parse(r.body);
    if (o.code === 0 && o.success === true) {
        pass++; console.log('✓ 状态字段保留正常（code/success 未被破坏）');
    } else {
        fail++; console.log('✗ 状态字段被破坏，App 可能判定为接口异常');
    }
}

console.log('\n通过 ' + pass + ' / 失败 ' + fail);
process.exit(fail ? 1 : 0);
