"""金额工具。

库内金额一律保存为**规范十进制文本**（``str(int)``），所有加减与合计在 Python
任意精度整数上完成。这样单笔仍受 15 位上限约束，而批次合计、首页总额、归档
净额与三角累计都不会受 SQLite 有符号 64 位整数限制。
"""
import re

MAX_AMOUNT_CENTS = 999_999_999_999_999  # 单笔上限：15 位整数分
AMOUNT_RE = re.compile(r"^\d{1,15}$")


def parse_amount_cents(text):
    """解析单笔金额（非负整数分，最多 15 位）；非法或超限返回 None。"""
    value = (text or "").strip()
    if not AMOUNT_RE.match(value):
        return None
    cents = int(value)
    if cents > MAX_AMOUNT_CENTS:
        return None
    return cents


def to_text(value):
    """Python 整数 -> 库内规范十进制文本（无前导零、无正号）。"""
    return str(int(value))


def to_int(value):
    """库内文本（或整数） -> Python 整数；None 视为 0。"""
    if value is None:
        return 0
    return int(value)


def format_cents(value):
    """千分位格式，如 1234567 -> '1,234,567'。"""
    return f"{to_int(value):,}"


def format_cents_signed(value):
    """带符号千分位格式，如 -1234567 -> '-1,234,567'。"""
    return f"{to_int(value):+,}"
