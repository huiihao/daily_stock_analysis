"""BaoStock 可用性体检（在 GitHub Actions runner 上运行）。

目的：确认 BaoStock 在境外 IP 上是否可用，以及能否承担以下职责 ——
    1. 个股日K主源（免费、协议级、免积分）
    2. 全市场股票列表（query_all_stock）
    3. 全市场行业分类（query_stock_industry）—— 若可用，可替代当前需要
       爬 335 个申万行业、耗时约 2 小时的映射生成流程

Runs on GitHub Actions. Always exits 0 so the report is visible even if
individual checks fail.
"""

from __future__ import annotations

import io
import sys
import time
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 测试标的：贵州茅台 + 用户自选股中的两只
TEST_CODES = ["sh.600519", "sz.300750", "sz.002594"]


def section(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}", flush=True)


def timed(label: str, fn):
    started = time.monotonic()
    try:
        result = fn()
        elapsed = time.monotonic() - started
        print(f"✅ {label}  耗时 {elapsed:.1f}s", flush=True)
        return result, elapsed
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        print(f"❌ {label}  失败({type(exc).__name__}) 耗时 {elapsed:.1f}s", flush=True)
        print(f"   {exc}", flush=True)
        traceback.print_exc(limit=2)
        return None, elapsed


def main() -> int:
    try:
        import baostock as bs
    except ImportError:
        print("❌ baostock 未安装")
        return 0

    section("1. 登录")
    lg = None
    try:
        lg = bs.login()
        print(f"   登录结果: error_code={lg.error_code} msg={lg.error_msg}", flush=True)
        if lg.error_code != "0":
            print("❌ 登录失败，后续测试跳过")
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 登录异常: {type(exc).__name__}: {exc}")
        return 0

    try:
        # ---- 2. 个股日K ----
        section("2. 个股日K（判断能否作为日K主源）")

        def query_kline():
            total = 0
            for code in TEST_CODES:
                rs = bs.query_history_k_data_plus(
                    code,
                    "date,code,open,high,low,close,volume,amount,pctChg,turn",
                    start_date="2026-09-01", end_date="2026-11-05",
                    frequency="d", adjustflag="3",
                )
                if rs.error_code != "0":
                    raise RuntimeError(f"{code}: {rs.error_msg}")
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                print(f"   {code}: {len(rows)} 行，最新 {rows[-1][0] if rows else '—'}")
                total += len(rows)
            return total

        timed("3 只股票日K批量查询", query_kline)

        # ---- 3. 全市场股票列表 ----
        section("3. 全市场股票列表 query_all_stock（判断能否替代快照）")

        def query_all():
            rs = bs.query_all_stock(day="2026-11-04")
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg)
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            print(f"   字段: {rs.fields}")
            print(f"   返回 {len(rows)} 条")
            print(f"   样例: {rows[:2]}")
            return rows

        timed("query_all_stock(2026-11-04)", query_all)

        # ---- 4. 全市场行业分类 ----
        section("4. 全市场行业分类 query_stock_industry（关键：能否替代申万爬取）")

        def query_industry():
            rs = bs.query_stock_industry()
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg)
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            print(f"   字段: {rs.fields}")
            print(f"   返回 {len(rows)} 条")
            for r in rows[:3]:
                print(f"   {r}")
            industries = {r[3] for r in rows if len(r) > 3}
            print(f"   去重行业数: {len(industries)}")
            return rows

        timed("query_stock_industry()", query_industry)

        # ---- 5. 分红/财务（可选能力探测）----
        section("5. 其他能力抽查")

        def query_profit():
            rs = bs.query_profit_data(code="sh.600519", year=2025, quarter=4)
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg)
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            print(f"   财务(利润)字段数: {len(rs.fields)}，返回 {len(rows)} 行")
            return rows

        timed("query_profit_data(600519, 2025Q4)", query_profit)

    finally:
        try:
            bs.logout()
            print("\n已登出", flush=True)
        except Exception:  # noqa: BLE001
            pass

    section("体检结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
