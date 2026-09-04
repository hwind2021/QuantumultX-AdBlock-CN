/**
 * ============================================================================
 * 信息流与弹窗广告清理脚本（Feed Ad Killer）
 * ----------------------------------------------------------------------------
 * 适用：Quantumult X（圈X）/ Loon / Surge / Stash
 * 类型：script-response-body（响应体改写）
 *
 * 与 splash-killer.js 的区别
 * ----------------------------------------------------------------------------
 * 信息流广告混在真实内容里（如首页推荐列表、评论区插入、搜索结果），
 * 不能像开屏那样整个清空容器，必须逐条甄别后剔除广告条目，
 * 保留正常内容。本脚本做的是「数组元素级」的精确摘除。
 *
 * 识别方式
 * ----------------------------------------------------------------------------
 * 1. 元素带明确的广告类型字段（type/cardType/itemType 命中 ad / advert / promote）
 * 2. 元素键名高度广告化（adId / creativeId / impressionUrl / clickUrl / landingUrl）
 * 3. 元素带 isAd / is_ad 之类的布尔标记
 *
 * 安全兜底：任何异常都会原样放行响应。
 * ============================================================================
 */

const CONFIG = {
    // 广告类型字段：值为这些关键词时判定为广告
    typeKey: /^(type|cardType|card_type|itemType|item_type|cellType|cell_type|style|styleType|contentType|content_type|feedType|feed_type|moduleType|module_type|showType|show_type)$/i,
    typeValue: /^(ad|ads|advert|advertise|advertising|advertisement|promote|promotion|sponsor|sponsored|commercial|recommend_ad|goods_ad|banner_ad|adCard|ad_card|adCell|ad_cell|ad_?item|ad_?feed|adBanner|ad_banner)$/i,

    // 广告标识字段：出现即判定为广告条目
    markerKey: /^(isAd|is_ad|isAdvert|is_advert|isPromote|is_promote|adFlag|ad_flag|isSponsor|adType|ad_type|adId|ad_id|creativeId|creative_id|impressionUrl|impression_url|clickUrl|click_url|landingUrl|landing_url|adSource|ad_source|adLabel|ad_label|adMark|ad_mark|advertId|advert_id|promotionId|promotion_id)$/i,

    // 承载信息流的数组容器
    containerKey: /^(data|list|items|result|results|content|rows|records|feeds|feedList|feed_list|cards|cardList|card_list|entities|moduleList|module_list|itemList|item_list|array|infos|infoList|info_list)$/i,

    // 保留字段
    preserveKey: /^(code|status|success|errmsg|errMsg|message|msg|errno|error|errorCode|error_code|hasMore|has_more|cursor|nextCursor|offset|page|total|count)$/i,

    // 单容器内广告占比上限：超过则怀疑是广告专用接口，整体清空
    aggressiveRatio: 0.9,

    debug: false,
};

function isPlainObject(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v);
}

function notify(title, msg) {
    if (CONFIG.debug && typeof $notify === "function") {
        $notify("信息流广告清理", title, msg);
    }
}

/**
 * 判定单个元素是否为广告条目。
 */
function isAdItem(item) {
    if (!isPlainObject(item)) return false;

    // 规则一：布尔型广告标记
    for (const k of Object.keys(item)) {
        if (/^(isAd|is_ad|isAdvert|is_advert|isPromote|is_promote)$/i.test(k)) {
            if (item[k] === true || item[k] === 1 || item[k] === "1" || item[k] === "true") {
                return true;
            }
        }
    }

    // 规则二：类型字段命中广告值
    for (const k of Object.keys(item)) {
        if (CONFIG.typeKey.test(k) && typeof item[k] === "string") {
            if (CONFIG.typeValue.test(item[k].trim())) return true;
        }
    }

    // 规则三：存在广告专有字段
    let adSignals = 0;
    for (const k of Object.keys(item)) {
        if (CONFIG.markerKey.test(k)) adSignals++;
    }
    if (adSignals >= 2) return true;

    // 规则四：键名整体高度广告化
    const joined = Object.keys(item).join(" ");
    if (/\bad(id|vert|vertise)?(_|-)?(id|url|type|source|label|mark|slot|pos)\b/i.test(joined)
        && Object.keys(item).length <= 10) {
        return true;
    }

    return false;
}

/**
 * 递归清理容器数组中的广告元素。
 */
function clean(node, depth) {
    if (depth > 12) return false;
    if (!isPlainObject(node)) return false;

    let changed = false;

    for (const key of Object.keys(node)) {
        if (CONFIG.preserveKey.test(key)) continue;
        const value = node[key];
        if (value === null || value === undefined) continue;

        if (Array.isArray(value)) {
            // 只在「看起来是内容容器」的数组里摘广告
            if (value.length > 0 && value.every((x) => isPlainObject(x))) {
                const adCount = value.filter(isAdItem).length;
                if (adCount === 0) {
                    // 无广告命中，继续向下递归
                    for (const item of value) if (clean(item, depth + 1)) changed = true;
                    continue;
                }
                const ratio = adCount / value.length;
                if (ratio >= CONFIG.aggressiveRatio) {
                    // 几乎全是广告 → 这是广告专用接口，整体清空
                    node[key] = [];
                } else {
                    node[key] = value.filter((x) => !isAdItem(x));
                }
                changed = true;
            } else {
                for (const item of value) {
                    if (isPlainObject(item) && clean(item, depth + 1)) changed = true;
                }
            }
        } else if (isPlainObject(value)) {
            if (clean(value, depth + 1)) changed = true;
        }
    }
    return changed;
}

function main() {
    const url = ($request && $request.url) || ($response && $response.url) || "";
    const body = $response.body;

    if (!body || typeof body !== "string") {
        $done({});
        return;
    }

    let obj;
    try {
        obj = JSON.parse(body);
    } catch (e) {
        $done({});
        return;
    }

    let removed = 0;
    const original = JSON.stringify(obj);

    if (clean(obj, 0)) {
        const before = (original.match(/\{/g) || []).length;
        const after = (JSON.stringify(obj).match(/\{/g) || []).length;
        removed = before - after;
        notify("已清理", `${url.split("?")[0].slice(0, 50)} · 移除 ${removed} 项`);
        $done({ body: JSON.stringify(obj) });
    } else {
        $done({});
    }
}

try {
    main();
} catch (e) {
    if (typeof $notify === "function" && CONFIG.debug) {
        $notify("信息流广告清理", "脚本异常已放行", String(e));
    }
    $done({});
}
