"""生成 A 股「个股 → 东财行业板块」静态映射文件（支持断点续爬）。

为什么需要它：
    东方财富的行业板块约 500 个，成分股需「板块列表 + 每板块一次成分股查询」，
    合计约 500 次请求。GitHub Actions 的 runner 每次运行都是全新容器、不保留
    缓存，每天重拉既慢又必然触发东财限流与熔断。

    行业归属变化很慢（月级），所以把映射固化成仓库里的静态文件：生成一次，
    每天只读文件，零外部请求。

为什么需要断点续爬：
    东财对高频请求限流很凶（实测连续十几次即被 RemoteDisconnected）。本脚本
    每拉完一个板块就把进度追加写入 --partial 文件，被限流中断后重跑会自动
    跳过已完成的板块，不会白费前面的工作。

用法：
    python scripts/build_industry_map.py                  # 首次或续爬
    python scripts/build_industry_map.py --sleep 4        # 更保守的节流
    python scripts/build_industry_map.py --reset          # 丢弃进度重来
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

# 每板块之间的基础间隔。东财敏感，建议 >=3 秒。
DEFAULT_SLEEP_SECONDS = 3.0
# 间隔上的随机抖动，避免固定节奏被识别
DEFAULT_JITTER_SECONDS = 1.0
# 单板块失败后的重试次数
DEFAULT_RETRIES = 3
# 触发疑似限流后的冷却时间（秒）
COOLDOWN_SECONDS = 90


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _normalize_code(value: Any) -> str | None:
    """把成分股代码规整成 6 位数字字符串，无法识别时返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "." in text:
        text = text.split(".")[0]
    for prefix in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.strip()
    if not text.isdigit():
        return None
    return text.zfill(6) if len(text) <= 6 else None


def _pick_column(df, candidates: list[str]) -> str | None:
    for name in candidates:
        if name in df.columns:
            return name
    return None


def _is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        token in text
        for token in ("remote disconnected", "connection aborted", "too many requests",
                      "429", "502", "503", "timeout", "timed out")
    )


def _load_partial(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"boards": {}, "failed": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"进度文件损坏，忽略并重来: {exc}")
        return {"boards": {}, "failed": []}


def _save_partial(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 A 股行业映射静态文件")
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
    done_boards: dict[str, list[str]] = state.get("boards", {})

    import akshare as ak

    log("拉取东财行业板块列表 ...")
    for attempt in range(1, args.retries + 2):
        try:
            boards_df = ak.stock_board_industry_name_em()
            break
        except Exception as exc:  # noqa: BLE001
            log(f"板块列表获取失败 (第 {attempt} 次): {type(exc).__name__}")
            if attempt > args.retries:
                log("板块列表最终失败，退出（进度已保留，稍后重跑即可续爬）")
                return 1
            time.sleep(COOLDOWN_SECONDS)

    name_col = _pick_column(boards_df, ["板块名称", "板块", "名称"])
    if name_col is None:
        log(f"无法识别板块名称列，实际列: {list(boards_df.columns)}")
        return 1
    board_names = [str(v).strip() for v in boards_df[name_col].tolist() if str(v).strip()]
    log(f"共 {len(board_names)} 个板块；已有进度 {len(done_boards)} 个")

    pending = [b for b in board_names if b not in done_boards]
    log(f"待爬取 {len(pending)} 个")

    # 先把板块名单落盘，便于分析
    state["board_list"] = board_names
    _save_partial(partial_path, state)

    for index, board in enumerate(pending, start=1):
        members_df = None
        last_error: Exception | None = None
        for attempt in range(1, args.retries + 2):
            try:
                members_df = ak.stock_board_industry_cons_em(symbol=board)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if _is_rate_limit(exc):
                    log(f"  {board}: 疑似限流，冷却 {COOLDOWN_SECONDS}s 后重试")
                    time.sleep(COOLDOWN_SECONDS)
                else:
                    time.sleep(args.sleep * attempt * 2)

        if members_df is None or members_df.empty:
            state.setdefault("failed", [])
            if board not in state["failed"]:
                state["failed"].append(board)
            _save_partial(partial_path, state)
            log(f"  [{index}/{len(pending)}] {board} 失败: {type(last_error).__name__}")
            time.sleep(args.sleep + random.uniform(0, args.jitter))
            continue

        code_col = _pick_column(members_df, ["代码", "股票代码", "code"])
        if code_col is None:
            state.setdefault("failed", [])
            state["failed"].append(board)
            _save_partial(partial_path, state)
            log(f"  [{index}/{len(pending)}] {board} 无代码列，跳过")
            continue

        codes = sorted({
            code for code in (_normalize_code(v) for v in members_df[code_col].tolist())
            if code
        })
        done_boards[board] = codes
        state["boards"] = done_boards
        # 每个板块完成即落盘 —— 这是断点续爬的关键
        _save_partial(partial_path, state)
        log(f"  [{index}/{len(pending)}] {board}: {len(codes)} 只"
            f"（累计 {len(done_boards)}/{len(board_names)}）")

        time.sleep(args.sleep + random.uniform(0, args.jitter))

    # 汇总：一只股票只归属第一个命中的行业
    stocks: dict[str, str] = {}
    for board, codes in done_boards.items():
        for code in codes:
            stocks.setdefault(code, board)

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "akshare.stock_board_industry_name_em / stock_board_industry_cons_em",
        "board_count": len(done_boards),
        "stock_count": len(stocks),
        "failed_boards": state.get("failed", []),
        "boards": done_boards,
        "stocks": stocks,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    log(f"完成：{len(done_boards)} 个板块 / {len(stocks)} 只股票 / "
        f"失败 {len(payload['failed_boards'])} 个")
    log(f"输出 {out_path}（{size_kb:.0f} KB）")
    return 0 if done_boards else 1


if __name__ == "__main__":
    sys.exit(main())
