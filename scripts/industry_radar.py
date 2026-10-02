"""行业冷暖榜：按行业计算个股多因子评分，输出最热/最冷行业排行榜。

数据来源与分工：
    行情（每日变化）—— 全市场快照，只用价量字段（涨跌幅/成交额/振幅）。
        东财的 stock_zh_a_spot_em 字段最全（含量比/换手率/PE/PB），但该端点已
        对本地与 GitHub 的 IP 持久封禁，因此实际走新浪源 stock_zh_a_spot
        （约 22 秒、5571 只），代价是没有估值字段。
    估值与质量（月级变化）—— 取 data/industry_map.json 里随行业映射一起抓到的
        PE/PE_TTM/市净率/ROE/股息率/市值。映射每月刷新时顺带更新，
        因此这部分数据的陈旧度不超过一个月，对估值类因子可接受。

用法：
    python scripts/industry_radar.py --dry-run          # 只算不推送
    python scripts/industry_radar.py --top-n 10
    python scripts/industry_radar.py --level sw1        # 按一级行业汇总
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 以 `python scripts/industry_radar.py` 运行时，sys.path[0] 是 scripts/ 而非仓库根，
# 会导致 `from src.notification_sender import ...` 失败（表现为推送被静默跳过）。
# 显式把仓库根加进搜索路径。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---- 多因子权重（总和应为 1.0，脚本会归一化）----
FACTOR_WEIGHTS: dict[str, float] = {
    "momentum": 0.30,   # 当日涨跌幅
    "liquidity": 0.25,  # 成交额（资金关注度）
    "valuation": 0.20,  # PE/PB 越低越好
    "quality": 0.15,    # ROE 越高越好
    "size": 0.10,       # 市值
}

# 行业汇总级别 -> 个股行里对应的字段名（注意与行业字典的 parent/parent_l1 不同名）
ROW_KEY = {"sw1": "sw1", "sw2": "sw2", "sw3": "sw3"}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        f = float(value)
        return None if f != f else f  # NaN
    except (TypeError, ValueError):
        return None


def _pct_rank(values: list[float | None], *, higher_is_better: bool) -> dict[int, float]:
    """返回 下标 -> 百分位(0~1) 的映射。None 不参与排名，返回 None 表示缺失。"""
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    if not present:
        return {}
    present.sort(key=lambda x: x[1], reverse=higher_is_better)
    n = len(present)
    ranks: dict[int, float] = {}
    for pos, (idx, _) in enumerate(present):
        ranks[idx] = 1.0 - (pos / max(n - 1, 1))  # 最好的 = 1.0，最差 = 0.0
    return ranks


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #

def load_industry_map(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"找不到行业映射 {path}，请先运行 scripts/build_industry_map.py")
    data = json.loads(path.read_text(encoding="utf-8"))
    log(f"行业映射：{data.get('industry_count')} 个行业 / "
        f"{data.get('stock_count')} 只股票（生成于 {data.get('generated_at')}）")
    return data


def load_valuation(path: Path) -> dict[str, dict[str, Any]]:
    """从映射里抽出 个股 -> 估值/质量因子。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for ind in data.get("industries", {}).values():
        for m in ind.get("members", []):
            out[m["code"]] = m
    log(f"估值因子：{len(out)} 只股票")
    return out


def fetch_snapshot() -> dict[str, dict[str, Any]]:
    """全市场快照。回退链：东财 -> 新浪。"""
    import akshare as ak

    for source, fn in (("东财 stock_zh_a_spot_em", ak.stock_zh_a_spot_em),
                       ("新浪 stock_zh_a_spot", ak.stock_zh_a_spot)):
        try:
            log(f"拉取全市场快照：{source} ...")
            df = fn()
            if df is None or df.empty:
                raise RuntimeError("返回为空")
            log(f"  ✅ {source} 返回 {len(df)} 只")
            return _normalize_snapshot(df)
        except Exception as exc:  # noqa: BLE001
            log(f"  ❌ {source} 失败: {type(exc).__name__}")
    raise RuntimeError("所有快照源均失败")


def _normalize_snapshot(df) -> dict[str, dict[str, Any]]:
    """把不同源的快照统一成 {6位代码: {price, chg_pct, amount, amplitude}}。"""
    cols = {str(c): c for c in df.columns}

    def pick(*candidates):
        for name in candidates:
            if name in cols:
                return cols[name]
        return None

    c_code = pick("代码", "code")
    c_name = pick("名称", "name")
    c_chg = pick("涨跌幅")
    c_amount = pick("成交额")
    c_high, c_low, c_prev = pick("最高"), pick("最低"), pick("昨收")
    c_price = pick("最新价")

    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        raw = str(row.get(c_code, "")).strip()
        # 新浪返回 bj920000 / sh600519 这类带前缀的代码
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) != 6:
            continue
        high, low, prev = _num(row.get(c_high)), _num(row.get(c_low)), _num(row.get(c_prev))
        amplitude = None
        if high is not None and low is not None and prev not in (None, 0):
            amplitude = (high - low) / prev * 100.0
        out[digits] = {
            "name": _clean_name(row.get(c_name)) if c_name else "",
            "price": _num(row.get(c_price)),
            "chg_pct": _num(row.get(c_chg)),
            "amount": _num(row.get(c_amount)),
            "amplitude": amplitude,
        }
    return out


def _clean_name(value: Any) -> str:
    """清洗股票名：把 pandas 的 nan/None 归一成空串。"""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "") else text


def _is_new_listing(name: str) -> bool:
    """识别新股/次新股：A 股命名约定 —— 上市首日冠 N，次日起冠 C。"""
    return len(name) > 0 and name[0] in ("N", "C")


# --------------------------------------------------------------------------- #
# 评分
# --------------------------------------------------------------------------- #

def score_stocks(
    industry_map: dict[str, Any],
    snapshot: dict[str, dict[str, Any]],
    valuation: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """给每只股票算多因子综合分（0~100）。"""
    stocks = industry_map.get("stocks", {})
    rows: list[dict[str, Any]] = []
    skipped_new = 0
    for code, meta in stocks.items():
        snap = snapshot.get(code)
        if not snap:
            continue  # 停牌/退市，快照里没有
        val = valuation.get(code, {})
        name = (_clean_name(meta.get("name")) or snap.get("name")
                or _clean_name(val.get("name")))
        # 新股/次新股涨跌幅无 10% 限制（实测有 +206% 的），会污染动量与资金因子
        if _is_new_listing(name):
            skipped_new += 1
            continue
        rows.append({
            "code": code,
            "name": name,
            "sw1": meta.get("sw1", ""),
            "sw2": meta.get("sw2", ""),
            "sw3": meta.get("sw3", ""),
            "chg_pct": snap.get("chg_pct"),
            "amount": snap.get("amount"),
            "amplitude": snap.get("amplitude"),
            # 估值因子取自映射文件（月级陈旧度）
            "pe": val.get("pe_ttm") if val.get("pe_ttm") is not None else val.get("pe"),
            "pb": val.get("pb"),
            "roe": val.get("roe"),
            "market_cap": val.get("market_cap"),
            "dividend": val.get("dividend"),
        })

    if skipped_new:
        log(f"  已剔除 {skipped_new} 只新股/次新股（涨跌幅无限制，会污染动量因子）")
    if not rows:
        return []

    # 各因子转百分位（0~1）。估值类"越低越好"，故 higher_is_better=False。
    # PE/PB 为负数（亏损）时视为最差，避免 -50 被当成"极便宜"。
    def pe_key(r):
        v = r["pe"]
        return None if v is None or v <= 0 else v

    def pb_key(r):
        v = r["pb"]
        return None if v is None or v <= 0 else v

    ranks = {
        "momentum": _pct_rank([r["chg_pct"] for r in rows], higher_is_better=True),
        "liquidity": _pct_rank([r["amount"] for r in rows], higher_is_better=True),
        "valuation": _pct_rank(
            [(_combine(pe_key(r), pb_key(r))) for r in rows], higher_is_better=False),
        "quality": _pct_rank([r["roe"] for r in rows], higher_is_better=True),
        "size": _pct_rank([r["market_cap"] for r in rows], higher_is_better=True),
    }

    total_w = sum(FACTOR_WEIGHTS.values())
    for i, r in enumerate(rows):
        score, used, used_w = 0.0, 0, 0.0
        for factor, weight in FACTOR_WEIGHTS.items():
            rank = ranks[factor].get(i)
            if rank is None:
                continue
            score += rank * weight
            used += 1
            used_w += weight
        # 缺失因子不拉低分数：按已参与因子的权重归一化
        r["score"] = round(score / used_w * 100, 1) if used_w else None
        r["_factors_used"] = used
        r["_total_factors"] = len(FACTOR_WEIGHTS)

    return rows


def _combine(pe: float | None, pb: float | None) -> float | None:
    """把 PE 与 PB 合成一个"估值水平"标量（越小越便宜）。"""
    parts = [v for v in (pe, pb) if v is not None]
    if not parts:
        return None
    return sum(parts) / len(parts)


# --------------------------------------------------------------------------- #
# 汇总与输出
# --------------------------------------------------------------------------- #

def aggregate(rows: list[dict[str, Any]], level: str) -> list[dict[str, Any]]:
    key = ROW_KEY[level]
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        name = r.get(key) or "未分类"
        buckets.setdefault(name, []).append(r)

    out: list[dict[str, Any]] = []
    for name, members in buckets.items():
        scored = [m for m in members if m["score"] is not None]
        if not scored:
            continue
        scored.sort(key=lambda m: m["score"], reverse=True)
        chgs = [m["chg_pct"] for m in scored if m["chg_pct"] is not None]
        out.append({
            "name": name,
            "count": len(members),
            "avg_score": round(sum(m["score"] for m in scored) / len(scored), 1),
            "avg_chg": round(sum(chgs) / len(chgs), 2) if chgs else None,
            "leader": scored[0],
            "laggard": scored[-1],
        })
    # 行业冷暖按「平均涨跌幅」排序 —— 这才是"冷热"的直觉含义。
    # 综合评分里含估值/质量/规模等月级静态因子，用它排行业会变成
    # "估值最便宜的行业"，而非"今天最强的行业"。评分只用于行业内个股排序。
    out.sort(key=lambda b: (b["avg_chg"] is None, -(b["avg_chg"] or 0)))
    return out


def format_digest(boards: list[dict[str, Any]], level: str, top_n: int,
                  generated_at: str) -> str:
    level_label = {"sw1": "申万一级", "sw2": "申万二级", "sw3": "申万三级"}[level]
    hot, cold = boards[:top_n], boards[-top_n:][::-1]

    def block(title: str, items: list[dict[str, Any]]) -> list[str]:
        lines = [f"### {title}", ""]
        for i, b in enumerate(items, 1):
            chg = f"{b['avg_chg']:+.2f}%" if b["avg_chg"] is not None else "—"
            lead, lag = b["leader"], b["laggard"]
            lines.append(
                f"**{i}. {b['name']}**  {chg} · {b['count']}只 · 均分 {b['avg_score']}"
            )
            lines.append(
                f"    🟢 {lead['name']}({lead['code']}) "
                f"{lead['chg_pct']:+.2f}% 分{lead['score']}"
            )
            if lag["code"] != lead["code"]:
                lines.append(
                    f"    🔴 {lag['name']}({lag['code']}) "
                    f"{lag['chg_pct']:+.2f}% 分{lag['score']}"
                )
            lines.append("")
        return lines

    lines = [
        f"# 🌡️ 行业冷暖榜 · {generated_at}",
        "",
        f"> 口径：{level_label}（共 {len(boards)} 个行业）  "
        f"|  多因子：动量/资金/估值/质量/规模",
        "",
    ]
    lines += block(f"🔥 最热 {top_n} 个行业", hot)
    lines += block(f"❄️ 最冷 {top_n} 个行业", cold)
    lines += [
        "---",
        f"完整 {len(boards)} 个行业明细见运行产物 artifact。",
        "本榜单为量化排序结果，不构成投资建议。",
    ]
    return "\n".join(lines)


def format_full_html(boards: list[dict[str, Any]], level: str) -> str:
    body = []
    for i, b in enumerate(boards, 1):
        chg = f"{b['avg_chg']:+.2f}%" if b["avg_chg"] is not None else "—"
        body.append(
            f"<tr><td>{i}</td><td>{b['name']}</td><td>{chg}</td>"
            f"<td>{b['count']}</td><td>{b['avg_score']}</td>"
            f"<td>{b['leader']['name']}({b['leader']['code']})</td>"
            f"<td>{b['laggard']['name']}({b['laggard']['code']})</td></tr>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>行业冷暖榜明细</title><style>"
        "body{font-family:system-ui,sans-serif;margin:24px}"
        "table{border-collapse:collapse;width:100%;font-size:13px}"
        "th,td{border:1px solid #ddd;padding:4px 8px;text-align:left}"
        "th{background:#f5f5f5;position:sticky;top:0}"
        "tr:nth-child(even){background:#fafafa}</style></head><body>"
        f"<h1>行业冷暖榜完整明细（{level}）</h1>"
        "<table><thead><tr><th>#</th><th>行业</th><th>平均涨跌</th><th>成分数</th>"
        "<th>平均分</th><th>龙头</th><th>垫底</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table></body></html>"
    )


# --------------------------------------------------------------------------- #
# 推送
# --------------------------------------------------------------------------- #

def push(content: str, dry_run: bool) -> bool:
    """推送到已配置的渠道。返回是否全部成功。

    配置加载放在 dry_run 判断之前 —— 这样 --dry-run 也能验证导入链路是否正常，
    否则 sys.path 之类的导入问题会在 dry-run 里被掩盖，只在真实运行时才暴露。
    """
    try:
        from src.config import setup_env, get_config
        setup_env()
        config = get_config()
    except Exception as exc:  # noqa: BLE001
        log(f"❌ 加载配置失败: {type(exc).__name__}: {exc}")
        return False

    if dry_run:
        log("--dry-run：配置加载正常，跳过实际推送")
        return True

    from src.notification_sender import WechatSender, FeishuSender

    all_ok = True
    for label, sender in (("企业微信", WechatSender), ("飞书", FeishuSender)):
        try:
            inst = sender(config)
            ok = (inst.send_to_wechat(content) if sender is WechatSender
                  else inst.send_to_feishu(content))
            log(f"  {label}: {'✅ 发送成功' if ok else '⚪ 跳过（未配置 webhook）'}")
            # 未配置不算失败，真正抛异常才算
        except Exception as exc:  # noqa: BLE001
            log(f"  {label}: ❌ {type(exc).__name__}: {exc}")
            all_ok = False
    return all_ok


def main() -> int:
    p = argparse.ArgumentParser(description="行业冷暖榜")
    p.add_argument("--map", default="data/industry_map.json")
    p.add_argument("--level", default="sw2", choices=list(ROW_KEY))
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--select-count", type=int, default=50,
                   help="输出给 LLM 深度分析的股票数")
    p.add_argument("--outdir", default="reports")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    map_path = Path(args.map)
    industry_map = load_industry_map(map_path)
    valuation = load_valuation(map_path)
    snapshot = fetch_snapshot()

    log("计算多因子评分 ...")
    rows = score_stocks(industry_map, snapshot, valuation)
    log(f"  有效评分 {sum(1 for r in rows if r['score'] is not None)} / {len(rows)} 只")

    boards = aggregate(rows, args.level)
    log(f"按 {args.level} 汇总出 {len(boards)} 个行业")

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    digest = format_digest(boards, args.level, args.top_n, generated_at)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"industry_radar_{generated_at}.md").write_text(digest, encoding="utf-8")
    (outdir / f"industry_radar_full_{generated_at}.html").write_text(
        format_full_html(boards, args.level), encoding="utf-8")

    # 精选：全局评分最高的 N 只（每个行业最多 3 只，保证分散）
    ranked = sorted((r for r in rows if r["score"] is not None),
                    key=lambda r: r["score"], reverse=True)
    picked: list[str] = []
    per_industry: dict[str, int] = {}
    for r in ranked:
        key = r.get(ROW_KEY[args.level], "")
        if per_industry.get(key, 0) >= 3:
            continue
        per_industry[key] = per_industry.get(key, 0) + 1
        picked.append(r["code"])
        if len(picked) >= args.select_count:
            break
    (outdir / "selected_stocks.txt").write_text(",".join(picked), encoding="utf-8")
    log(f"精选 {len(picked)} 只 -> {outdir}/selected_stocks.txt")

    log("推送 ...")
    push_ok = push(digest, args.dry_run)
    if not push_ok:
        log("⚠️ 推送未全部成功 —— 榜单文件仍已写入 reports/，会随 artifact 上传")

    print("\n" + "=" * 62)
    print(digest[:1600])
    print("=" * 62)
    return 0 if push_ok else 1


if __name__ == "__main__":
    sys.exit(main())
