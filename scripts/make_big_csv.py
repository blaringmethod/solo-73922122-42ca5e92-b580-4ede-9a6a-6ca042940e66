#!/usr/bin/env python3
"""生成「单笔合法、整批合计超出有符号 64 位」的演示 CSV。

用法：
    python scripts/make_big_csv.py [输出路径] [行数]

默认 9225 行、每行 999999999999999 分（15 位，单笔合法），合计
9,224,999,999,999,990,775 分 > 2^63-1；旧版会在批次合计/首页 SUM/三角处溢出。
"""
import sys

DEFAULT_ROWS = 9225            # INT64_MAX // 999999999999999 + 2
BIG_CENTS = 999_999_999_999_999


def main(argv):
    out = argv[1] if len(argv) > 1 else "big_batch.csv"
    rows = int(argv[2]) if len(argv) > 2 else DEFAULT_ROWS
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write("claim_no,accident_month,payment_month,amount_cents\n")
        for i in range(rows):
            f.write(f"BIG-{i:05d},2025-12,2026-01,{BIG_CENTS}\n")
    print(f"已生成 {out}：{rows} 行，合计 {rows * BIG_CENTS:,} 分")


if __name__ == "__main__":
    main(sys.argv)
