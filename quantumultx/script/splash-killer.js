/**
 * ============================================================================
 * 通用开屏广告拦截脚本（Splash Ad Killer）
 * ----------------------------------------------------------------------------
 * 适用：Quantumult X（圈X）/ Loon / Surge / Stash
 * 类型：script-response-body（响应体改写）
 *
 * 工作原理
 * ----------------------------------------------------------------------------
 * 开屏广告的接口调用失败时，多数 App 会等待 SDK 超时（通常 1.5~5 秒）才进入主页，
 * 表现为"卡在启动页"。本脚本不拦截请求，而是让请求正常返回，但把响应里的
 * 广告内容清空并伪装成「本次无广告可投放」，App 解析后立即跳过开屏。
 *
 * 这是相比单纯 REJECT 更优雅的方案：既去掉了广告，又消除了等待白屏。
 *
 * 处理策略（双模式）
 * ----------------------------------------------------------------------------
 *   1. 激进模式：URL 命中开屏特征（splash / launch / startup / start_ad 等）时，
 *      清空响应中所有承载广告的容器字段（data / list / result / ad_list …）
 *   2. 精确模式：URL 无明显特征时，递归遍历 JSON，仅清空键名命中广告特征的字段
 *
 * 安全兜底
 * ----------------------------------------------------------------------------
 * 任何异常都会原样放行响应，绝不会因为脚本出错导致 App 打不开。
 * ============================================================================
 */

// ---------------------------------------------------------------------------
// 配置区
// ---------------------------------------------------------------------------

const CONFIG = {
    // 开屏广告 URL 特征：命中则进入激进模式
    splashUrl: /splash|launch|startup|start[_-]?ad|boot[_-]?ad|open[_-]?screen|开屏/i,

    // 广告字段特征：递归清理时的键名匹配规则
    adKey: /^(ad|ads|advert|advertise|advertising|advertisement|adInfo|adList|adData|adConfig|adPos|adPosition|adSlot|adSource|splashAd|launchAd|startAd|startupAd|openAd|bootAd|banner|bannerList|popup|popUp|popups|dialogAd|feedAd|recommendAd|material|creative)(_|s)?$/i,

    // 广告容器字段：激进模式下会被清空
    containerKey: /^(data|list|result|results|items|content|body|rows|records|entities|ad_list|adList|showList|show_list)$/i,

    // 需要保留原值的字段（避免 App 判定为接口异常）
    preserveKey: /^(code|status|success|errmsg|errMsg|message|msg|errno|error|errorCode|error_code|resultCode|result_code)$/i,

    // 展示时长字段：命中广告时归零
    durationKey: /^(duration|showTime|show_time|displayTime|display_time|countdown|countDown|stayTime|stay_time|delay|adDuration|ad_duration|skipTime|skip_time)$/i,

    // 调试开关：开启后会弹通知，便于排查规则是否生效
    debug: false,
};

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function isPlainObject(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v);
}

function notify(title, msg) {
    if (CONFIG.debug && typeof $notify === "function") {
        $notify("开屏广告拦截", title, msg);
    }
}

/**
 * 递归净化：删除所有键名命中广告特征且值为数组/对象的字段。
 * 返回 true 表示本次发生了修改。
 */
function purify(node, depth) {
    if (depth > 12 || !isPlainObject(node)) return false;

    let changed = false;
    for (const key of Object.keys(node)) {
        const value = node[key];

        // 跳过需要保留的状态字段
        if (CONFIG.preserveKey.test(key)) continue;

        if (CONFIG.adKey.test(key)) {
            if (Array.isArray(value)) {
                if (value.length > 0) {
                    node[key] = [];
                    changed = true;
                }
            } else if (isPlainObject(value)) {
                // 广告对象：清空内部但保留对象壳，避免 App 解析崩溃
                node[key] = {};
                changed = true;
            }
            continue;
        }

        // 广告容器里若只有广告内容，激进模式下清空
        if (CONFIG.containerKey.test(key) && Array.isArray(value)) {
            // 只有当数组元素看起来像广告时才清空
            if (value.length > 0 && looksLikeAdArray(value)) {
                node[key] = [];
                changed = true;
                continue;
            }
        }

        // 时长字段归零（命中广告上下文时）
        if (CONFIG.durationKey.test(key) && typeof value === "number" && value > 0) {
            node[key] = 0;
            changed = true;
            continue;
        }

        if (Array.isArray(value)) {
            for (const item of value) {
                if (purify(item, depth + 1)) changed = true;
            }
        } else if (isPlainObject(value)) {
            if (purify(value, depth + 1)) changed = true;
        }
    }
    return changed;
}

/**
 * 判断数组元素是否为广告对象。
 * 取前若干个元素，看键名是否命中广告特征，或元素本身带 ad/type 等广告标识。
 */
function looksLikeAdArray(arr) {
    const sample = arr.slice(0, 5);
    let hits = 0;
    for (const item of sample) {
        if (!isPlainObject(item)) return false;
        const keys = Object.keys(item).join(" ");
        if (/(\bad|advert|creative|material|impression|click|landing|slot)/i.test(keys)) {
            hits++;
        }
    }
    return hits > 0;
}

/**
 * 激进模式：清空顶层常见的广告承载容器。
 */
function aggressiveClean(obj) {
    let changed = false;
    if (Array.isArray(obj)) return { obj: [], changed: true };

    if (isPlainObject(obj)) {
        // 情况一：响应本身就是广告列表的容器
        for (const key of Object.keys(obj)) {
            if (CONFIG.preserveKey.test(key)) continue;
            const value = obj[key];
            if (CONFIG.containerKey.test(key)) {
                if (Array.isArray(value) && value.length > 0) {
                    obj[key] = [];
                    changed = true;
                } else if (isPlainObject(value)) {
                    const inner = aggressiveClean(value);
                    if (inner.changed) {
                        obj[key] = inner.obj;
                        changed = true;
                    }
                }
            }
        }
        // 情况二：整个响应就是一个广告对象（键名高度广告化）
        const allKeys = Object.keys(obj).join(" ");
        if (!changed && /(\bad|advert|creative|material|impression)/i.test(allKeys)
            && Object.keys(obj).length <= 12) {
            for (const key of Object.keys(obj)) {
                if (CONFIG.preserveKey.test(key)) continue;
                const value = obj[key];
                if (Array.isArray(value)) {
                    obj[key] = [];
                    changed = true;
                } else if (isPlainObject(value)) {
                    obj[key] = {};
                    changed = true;
                }
            }
        }
    }
    return { obj, changed };
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------

function main() {
    const url = ($request && $request.url) || ($response && $response.url) || "";
    const body = $response.body;

    // 非文本响应（图片 / protobuf / 已压缩）无法安全改写，原样放行
    if (!body || typeof body !== "string") {
        $done({});
        return;
    }

    let obj;
    try {
        obj = JSON.parse(body);
    } catch (e) {
        // 非 JSON（HTML / 加密串）不做处理
        $done({});
        return;
    }

    let changed = purify(obj, 0);

    if (CONFIG.splashUrl.test(url)) {
        const agg = aggressiveClean(obj);
        if (agg.changed) {
            obj = agg.obj;
            changed = true;
        }
    }

    if (changed) {
        notify("已拦截", url.split("?")[0].slice(0, 60));
        $done({ body: JSON.stringify(obj) });
    } else {
        $done({});
    }
}

try {
    main();
} catch (e) {
    // 兜底：任何异常都保持原始响应不变
    if (typeof $notify === "function" && CONFIG.debug) {
        $notify("开屏广告拦截", "脚本异常已放行", String(e));
    }
    $done({});
}
