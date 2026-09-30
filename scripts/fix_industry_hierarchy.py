"""修补行业映射文件里的「三级 -> 二级 -> 一级」层级字段。

背景：
    build_industry_map.py 在爬取开始时拉取 sw_index_third_info() 取层级。
    若那一刻数据源正处于限流状态，页面会返回加载不全的内容 —— 表现为「上级行业」
    列变成同一个常量（实测全部变成「医疗美容」），而行业名与成分股本身是对的。
    这属于静默的脏数据，不会报错，因此需要事后校验与修补。

    修补不需要重爬：重新拉一次两个小清单（335 + 131 行），按行业名 join 回去即可。

用法：
    python scripts/fix_industry_hierarchy.py
    python scripts/fix_industry_hierarchy.py --file data/industry_map.json
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="修补行业映射的层级字段")
    parser.add_argument("--file", default="data/industry_map.json")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"找不到 {path}")
        return 1

    import akshare as ak

    print("重新拉取申万层级清单 ...")
    third = ak.sw_index_third_info()   # 335 行，带 上级行业(=二级名)
    second = ak.sw_index_second_info()  # 131 行，带 上级行业(=一级名)
    print(f"  三级 {len(third)} 行 / 二级 {len(second)} 行")

    # 三级行业名 -> 二级行业名
    lv3_to_lv2 = {
        str(r["行业名称"]).strip(): str(r["上级行业"]).strip()
        for _, r in third.iterrows()
    }
    # 二级行业名 -> 一级行业名
    lv2_to_lv1 = {
        str(r["行业名称"]).strip(): str(r["上级行业"]).strip()
        for _, r in second.iterrows()
    }

    # 健全性检查：二级名不该是单一常量
    if len(set(lv3_to_lv2.values())) < 10:
        print(f"❌ 层级清单仍异常（二级名唯一值只有 "
              f"{len(set(lv3_to_lv2.values()))} 个），数据源可能仍在限流，请稍后重试")
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    industries = data.get("industries", {})
    stocks = data.get("stocks", {})

    fixed, missing = 0, []
    for code, item in industries.items():
        name = item.get("name", "")
        parent = lv3_to_lv2.get(name)
        if not parent:
            missing.append(name)
            continue
        parent_l1 = lv2_to_lv1.get(parent, "")
        if item.get("parent") != parent or item.get("parent_l1") != parent_l1:
            fixed += 1
        item["parent"] = parent
        item["parent_l1"] = parent_l1

    # 重建个股的 sw2 / sw1（先建 三级名 -> 行业项 索引，避免逐个线性扫描）
    by_name = {item.get("name"): item for item in industries.values()}
    stock_fixed = 0
    for s in stocks.values():
        ind = by_name.get(s.get("sw3"))
        if ind is None:
            continue
        new2, new1 = ind.get("parent", ""), ind.get("parent_l1", "")
        if s.get("sw2") != new2 or s.get("sw1") != new1:
            stock_fixed += 1
            s["sw2"], s["sw1"] = new2, new1

    data["hierarchy_repaired_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")

    print(f"✅ 修补完成：行业 {fixed} 个、个股 {stock_fixed} 条被修正")
    if missing:
        print(f"⚠️ {len(missing)} 个行业在层级清单里找不到对应：{missing[:8]}")
    print("\n样本验证：")
    for code in ("300750", "600519", "000001"):
        if code in stocks:
            s = stocks[code]
            print(f"  {code} {s.get('name')}: {s.get('sw1')} > {s.get('sw2')} > {s.get('sw3')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
