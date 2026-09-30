"""生成 A 股「个股 → 行业」静态映射文件（申万分类，支持断点续爬）。

数据源选择原因：
    原方案用东方财富行业板块（akshare.stock_board_industry_*_em），但东财的
    clist/get 端点已对高频访问做持久封禁 —— 本地 IP 与 GitHub runner 的 IP
    都被拒（实测 4 次重试全 ConnectionError）。因此改用申万行业分类，走
    akshare 的 sw_index_third_* 接口，数据源不同、不受该封禁影响。

    申万还有额外好处：sw_index_third_cons 每行同时带回「申万3级」行业名与
    市盈率/市净率/ROE/股息率/市值/涨幅等因子，一次抓取既建映射又拿到因子，
    且总数 335 个行业少于东财的近 500 个。

为什么需要静态文件 + 断点续爬：
    335 个行业需 335 次请求。GitHub Actions 的 runner 每次运行都是全新容器、
    不保留缓存，每天重拉既慢又没必要（行业归属月级才变）。脚本每爬完一个
    行业即落盘，被中断后重跑自动跳过已完成的行业。

用法：
    python scripts/build_industry_map.py
    python scripts/build_industry_map.py --sleep 1 --reset
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 申万数据源实测约 0.8s/行业。默认留 1 秒间隔，既快又不易触发限流。
DEFAULT_SLEEP_SECONDS = 1.0
DEFAULT_JITTER_SECONDS = 0.4
DEFAULT_RETRIES = 3
COOLDOWN_SECONDS = 30


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def _normalize_code(value: Any) -> str | None:
    """把 600313.SH 这类代码规整成 6 位数字字符串。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "." in text:
        text = text.split(".")[0]
    text = text.strip()
    if not text.isdigit():
        return None
    return text.zfill(6) if len(text) <= 6 else None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        result = float(value)
        return None if result != result else result  # 过滤 NaN
    except (TypeError, ValueError):
        return None


def _load_partial(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"industries": {}, "failed": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"进度文件损坏，忽略并重来: {exc}")
        return {"industries": {}, "failed": []}


def _save_partial(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 A 股申万行业映射静态文件")
    parser.add_argument("--out", default="data/industry_map.json")
    parser.add_argument("--partial", default="data/industry_map.partial.json")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER_SECONDS)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--reset", action="store_true", help="丢弃已有进度重新开始")
    args = parser.parse_args()

    out_path = Path(args.out)
    partial_path = Path(args.partial)
    if args.reset:
        for p in (out_path, partial_path):
            if p.exists():
                p.unlink()
        log("已清空进度")

    state = _load_partial(partial_path)
    done: dict[str, Any] = state.get("industries", {})

    import akshare as ak

    # ---- 1. 行业清单与层级 ----
    # 申万数据源限制较严：连续约 30 次调用后会被拦截，返回拦截页而不是数据
    # （表现为 akshare 抛 AttributeError: 'NoneType' object has no attribute 'find_all'）。
    # 因此这里必须耐心重试，不能一失败就退出。
    log("拉取申万行业分类 ...")
    third_info = None
    for attempt in range(1, 41):
        try:
            third_info = ak.sw_index_third_info()   # 335 个三级行业
            break
        except Exception as exc:  # noqa: BLE001
            log(f"  行业清单第 {attempt} 次失败 ({type(exc).__name__})，"
                f"{COOLDOWN_SECONDS * 6}s 后重试")
            time.sleep(COOLDOWN_SECONDS * 6)
    if third_info is None or third_info.empty:
        log("行业清单持续不可用，退出（进度已保留，稍后重跑即可续爬）")
        return 1
    log(f"申万三级行业 {len(third_info)} 个")

    # 二级/一级名称，用于向上汇总
    level2_parent: dict[str, str] = {}
    try:
        second_info = ak.sw_index_second_info()
        name_col = "行业名称" if "行业名称" in second_info.columns else second_info.columns[1]
        parent_col = "上级行业" if "上级行业" in second_info.columns else None
        for _, row in second_info.iterrows():
            level2_parent[str(row[name_col]).strip()] = (
                str(row[parent_col]).strip() if parent_col else ""
            )
        log(f"申万二级行业 {len(second_info)} 个，已建立 二级->一级 映射")
    except Exception as exc:  # noqa: BLE001
        log(f"二级行业清单获取失败（不影响主流程）: {type(exc).__name__}")

    # ---- 2. 逐行业抓成分股 ----
    rows: list[dict[str, Any]] = []
    for _, row in third_info.iterrows():
        code = str(row["行业代码"]).strip()
        name = str(row["行业名称"]).strip()
        parent = str(row.get("上级行业", "")).strip()
        rows.append({"code": code, "name": name, "parent": parent})

    pending = [r for r in rows if r["code"] not in done]
    log(f"待爬取 {len(pending)} 个行业（已有进度 {len(done)} 个）")

    for index, item in enumerate(pending, start=1):
        code, name = item["code"], item["name"]
        df = None
        last_error: Exception | None = None
        for attempt in range(1, args.retries + 2):
            try:
                df = ak.sw_index_third_cons(symbol=code)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(args.sleep * attempt * 3)

        if df is None or df.empty or "股票代码" not in getattr(df, "columns", []):
            state.setdefault("failed", [])
            if name not in state["failed"]:
                state["failed"].append(name)
            _save_partial(partial_path, state)
            log(f"  [{index}/{len(pending)}] {name} 失败: {type(last_error).__name__}")
            time.sleep(args.sleep + random.uniform(0, args.jitter))
            continue

        # 涨幅列名带日期后缀（如「近1日涨幅(2026-09-29)」），用前缀匹配定位
        def _col(prefix: str) -> str | None:
            for c in df.columns:
                if str(c).startswith(prefix):
                    return c
            return None

        col_1d, col_ytd = _col("近1日涨幅"), _col("今年以来涨幅")

        members: list[dict[str, Any]] = []
        for _, m in df.iterrows():
            stock_code = _normalize_code(m.get("股票代码"))
            if not stock_code or stock_code == "000000":
                continue
            members.append({
                "code": stock_code,
                "name": str(m.get("股票简称", "")).strip(),
                "pe": _safe_float(m.get("市盈率")),
                "pe_ttm": _safe_float(m.get("市盈率ttm")),
                "pb": _safe_float(m.get("市净率")),
                "roe": _safe_float(m.get("ROE(%)")),
                "dividend": _safe_float(m.get("股息率")),
                "market_cap": _safe_float(m.get("市值")),
                "chg_1d": _safe_float(m.get(col_1d)) if col_1d else None,
                "chg_ytd": _safe_float(m.get(col_ytd)) if col_ytd else None,
            })

        done[code] = {
            "name": name,
            "parent": parent,
            "parent_l1": level2_parent.get(parent, ""),
            "count": len(members),
            "members": members,
        }
        state["industries"] = done
        _save_partial(partial_path, state)
        log(f"  [{index}/{len(pending)}] {name}({parent}): {len(members)} 只"
            f"（累计 {len(done)}/{len(rows)}）")
        time.sleep(args.sleep + random.uniform(0, args.jitter))

    # ---- 3. 汇总成 个股 -> 行业 索引 ----
    stocks: dict[str, dict[str, Any]] = {}
    for ind in done.values():
        for m in ind["members"]:
            # 一只股票只归属第一个命中的行业（申万三级互斥，正常不会重复）
            stocks.setdefault(m["code"], {
                "name": m["name"],
                "sw3": ind["name"],
                "sw2": ind["parent"],
                "sw1": ind["parent_l1"],
            })

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "akshare sw_index_third_info / sw_index_third_cons（申万行业分类）",
        "taxonomy": "shenwan",
        "industry_count": len(done),
        "stock_count": len(stocks),
        "failed_industries": state.get("failed", []),
        "industries": done,
        "stocks": stocks,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    log(f"完成：{len(done)} 个行业 / {len(stocks)} 只股票 / "
        f"失败 {len(payload['failed_industries'])} 个")
    log(f"输出 {out_path}（{size_kb:.0f} KB）")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
