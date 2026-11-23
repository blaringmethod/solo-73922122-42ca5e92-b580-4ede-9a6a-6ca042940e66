#!/usr/bin/env python3
"""关键操作自测（不依赖 pytest）：设置待结月、CSV 校验、封账推进、同号覆盖、
补报净变动归档、两期回查一致性、零额显示、失败原子性、导入/封账并发，
以及超出 int64 的大额合计、正负抵消回查、旧库迁移。

运行：python3 tests/selftest.py
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.money import MAX_AMOUNT_CENTS  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_CSV = os.path.join(REPO, "demo", "claims_2026-01_base.csv")
SUPP_CSV = os.path.join(REPO, "demo", "claims_2026-02_supplement.csv")

_passed, _failed = [], []


def check(name, cond, detail=""):
    (_passed if cond else _failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  -> {detail}"))


def make_client():
    tmp = tempfile.mkdtemp(prefix="lt-test-")
    app = create_app({"DB_PATH": os.path.join(tmp, "t.db"), "TESTING": True})
    return app, app.test_client(), app.config["DB_PATH"]


def q(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def setup_pending(client, month="2026-01"):
    return client.post("/setup", data={"pending_month": month}, follow_redirects=True)


def import_text(client, text):
    return client.post("/import", data={"csv_text": text})


def close_month(client, month):
    return client.post("/close", data={"close_month": month}, follow_redirects=True)


HEADER = "claim_no,accident_month,payment_month,amount_cents\n"
BIG = MAX_AMOUNT_CENTS                    # 999,999,999,999,999 分
INT64_MAX = 2 ** 63 - 1

# 单笔 15 位、但整批合计要超过有符号 64 位，最少需要 9224 笔：
# INT64_MAX / BIG ≈ 9223.4，取 9225 笔保证严格超出。
OVERFLOW_ROWS = (INT64_MAX // BIG) + 2    # 9225 笔，合计 > 2^63-1


def big_batch(prefix, count, amount=BIG, acc="2025-12", pay="2026-01"):
    lines = [HEADER.rstrip("\n")]
    for i in range(count):
        lines.append(f"{prefix}{i},{acc},{pay},{amount}")
    return "\n".join(lines) + "\n"


def fmt(n):
    return f"{n:,}"


# ---------------------------------------------------------------- 1. 全流程 + 两期回查
def test_full_flow_two_periods():
    print("\n[1] 完整流程：基础批 -> 封账 -> 补报/更正 -> 封账 -> 两期回查")
    app, client, db = make_client()
    r = setup_pending(client)
    check("设置首个待结月 2026-01", "首个待结月已设为 2026-01" in r.get_data(as_text=True))

    r = import_text(client, open(BASE_CSV, encoding="utf-8").read())
    check("基础批导入成功", r.status_code == 200 and "批次 #1" in r.get_data(as_text=True),
          f"status={r.status_code}")
    check("基础批计入 2026-01 期", "计入 2026-01 期" in r.get_data(as_text=True))

    r = close_month(client, "2026-02")
    check("封非待结月被拒绝", "只能封当前待结月 2026-01" in r.get_data(as_text=True))
    check("拒绝后待结月仍为 2026-01",
          q(db, "SELECT value FROM meta WHERE key='pending_month'")[0][0] == "2026-01")

    r = close_month(client, "2026-01")
    check("封 2026-01 成功并推进", "已封账 2026-01" in r.get_data(as_text=True)
          and "2026-02" in r.get_data(as_text=True))
    rows = q(db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-01'")
    check("2026-01 期归档 7 个组合", len(rows) == 7, f"got {len(rows)}")

    r = import_text(client, open(SUPP_CSV, encoding="utf-8").read())
    body = r.get_data(as_text=True)
    check("补报批计入 2026-02 期", "计入 2026-02 期" in body)
    check("补报批覆盖重报 3 笔", "覆盖重报 3 笔" in body)

    r = close_month(client, "2026-02")
    check("封 2026-02 成功", "已封账 2026-02" in r.get_data(as_text=True))
    nets = dict(((a, p), int(n)) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-02'"))
    expected = {
        ("2025-10", "2025-12"): 15000,    # C-0002 更正金额 80000 -> 95000
        ("2025-11", "2026-01"): -60000,   # C-0004 支付月 2026-01 -> 2026-02
        ("2025-11", "2026-02"): 105000,   # C-0004 移入 60000 + C-0008 补报 45000
        ("2025-12", "2026-02"): 70000,    # C-0009 补报
        ("2026-01", "2026-02"): 110000,   # C-0010 补报
        ("2026-02", "2026-02"): 180000,   # C-0011 新事故月
    }
    check("2026-02 期仅归档 6 个净变动组合（同数重报 C-0007 不归档）", nets == expected,
          f"got {nets}")

    t1 = client.get("/triangle?start=2025-10&close=2026-01").get_data(as_text=True)
    check("一期三角含旧值 200,000 / 210,000 / 290,000",
          all(s in t1 for s in ("200,000", "210,000", "290,000")))
    check("一期三角不含补报后的 215,000", "215,000" not in t1)
    check("一期三角封账月之后留空（4 个空白格）",
          t1.count('<td class="blank"></td>') == 4, f"got {t1.count('blank')}")

    t2 = client.get("/triangle?start=2025-10&close=2026-02").get_data(as_text=True)
    check("二期三角含更正后 215,000 / 255,000 / 360,000 / 180,000",
          all(s in t2 for s in ("215,000", "255,000", "360,000", "180,000")))
    check("二期三角无空白格（已结到最新月）", '<td class="blank"></td>' not in t2)

    t1b = client.get("/triangle?start=2025-10&close=2026-01").get_data(as_text=True)
    check("两期回查：一期三角在二期封账后保持原样", t1b == t1)

    a2 = client.get("/archive/2026-02").get_data(as_text=True)
    check("归档明细页展示 2026-02 期净变动", "2025-11" in a2 and "-60,000" in a2)


# ---------------------------------------------------------------- 2. CSV 校验
def test_import_validation():
    print("\n[2] CSV 校验：非法行整批拒绝")
    app, client, db = make_client()
    setup_pending(client)
    cases = {
        "负数金额": "C1,2025-12,2026-01,-100",
        "小数金额": "C1,2025-12,2026-01,10.5",
        "非法月份": "C1,2025-13,2026-01,100",
        "支付月早于事故月": "C1,2026-01,2025-12,100",
        "支付月晚于待结月": "C1,2025-12,2026-02,100",
        "批内赔款号重复": "C1,2025-12,2026-01,100\nC1,2025-12,2026-01,200",
        "赔款号含逗号": '"C,1",2025-12,2026-01,100',
    }
    for name, line in cases.items():
        r = import_text(client, HEADER + line + "\n")
        check(f"拒绝：{name}", r.status_code == 400, f"status={r.status_code}")
    r = import_text(client, "claim_no,accident_month,payment_month,amount_cents,extra\nC1,2025-12,2026-01,100,x\n")
    check("拒绝：无法识别的表头列", r.status_code == 400)
    r = import_text(client, HEADER + "C1,2025-12,2026-01\n")
    check("拒绝：列数不一致", r.status_code == 400)
    r = import_text(client, HEADER)
    check("拒绝：无数据行", r.status_code == 400)
    check("全部拒绝后 claims/batches 仍为空（原子性）",
          q(db, "SELECT COUNT(*) FROM claims")[0][0] == 0
          and q(db, "SELECT COUNT(*) FROM batches")[0][0] == 0)


# ---------------------------------------------------------------- 3. 覆盖语义 + 零额显示
def test_overwrite_and_zero():
    print("\n[3] 同号覆盖（改事故月/支付月）与零额显示")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, HEADER + "A1,2025-12,2026-01,10000\nA2,2026-01,2026-01,5000\n")
    # 同期内重报 A1：改事故月 2025-12 -> 2026-01，金额 10000 -> 30000
    import_text(client, HEADER + "A1,2026-01,2026-01,30000\n")
    close_month(client, "2026-01")
    rows = q(db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-01'")
    check("封前同期覆盖：只按最新值归档 (2026-01,2026-01)=35000",
          [(a, p, int(n)) for a, p, n in rows] == [("2026-01", "2026-01", 35000)], f"got {rows}")

    # 下期更正 A1 支付月 2026-01 -> 2026-02；A3 正常补报
    import_text(client, HEADER + "A1,2026-01,2026-02,30000\nA3,2026-01,2026-02,4000\n")
    close_month(client, "2026-02")
    nets = dict(((a, p), int(n)) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-02'"))
    check("跨期更正支付月：旧组合 -30000，新组合 +34000",
          nets == {("2026-01", "2026-01"): -30000, ("2026-01", "2026-02"): 34000},
          f"got {nets}")

    # A5 事故 2026-02、支付 2026-03：事故当月无赔付 -> 零额显示 0
    import_text(client, HEADER + "A5,2026-02,2026-03,9000\n")
    close_month(client, "2026-03")
    t = client.get("/triangle?start=2026-01&close=2026-03").get_data(as_text=True)
    check("已到月份零额显示 0", '<td class="num">0</td>' in t)
    check("累计值 5,000 / 39,000 / 9,000 正确",
          all(s in t for s in ("5,000", "39,000", "9,000")))


# ---------------------------------------------------------------- 4. 封账约束
def test_close_constraints():
    print("\n[4] 封账约束：只能封当前待结月，成功后进入次月")
    app, client, db = make_client()
    r = client.get("/close", follow_redirects=True)
    check("未设待结月时封账页引导设置", "请先设置首个待结月" in r.get_data(as_text=True))
    setup_pending(client)
    r = close_month(client, "2025-12")
    check("封过去月份被拒绝", "只能封当前待结月 2026-01" in r.get_data(as_text=True))
    r = close_month(client, "2026-01")
    check("空期间也可封账（0 组合）", "已封账 2026-01" in r.get_data(as_text=True))
    r = close_month(client, "2026-01")
    check("重复封同一月被拒绝", "只能封当前待结月 2026-02" in r.get_data(as_text=True))
    check("closes 表仅一条记录", len(q(db, "SELECT * FROM closes")) == 1)
    r = client.get("/triangle")
    check("空归档期三角页可访问", r.status_code == 200)


# ---------------------------------------------------------------- 5. 失败原子性
def test_batch_atomicity():
    print("\n[5] 整批原子性：部分行非法则整批不留痕")
    app, client, db = make_client()
    setup_pending(client)
    csv_text = HEADER + "G1,2025-12,2026-01,100\nG2,2025-12,2026-01,200\nG3,2026-02,2026-01,300\n"
    r = import_text(client, csv_text)
    check("含非法行的批次被拒绝", r.status_code == 400)
    check("前两行有效也未写入",
          q(db, "SELECT COUNT(*) FROM claims")[0][0] == 0
          and q(db, "SELECT COUNT(*) FROM batches")[0][0] == 0)


# ---------------------------------------------------------------- 6. 待结月锁定
def test_setup_locked_after_data():
    print("\n[6] 有数据后禁止改待结月")
    app, client, db = make_client()
    setup_pending(client, "2026-01")
    import_text(client, HEADER + "L1,2025-12,2026-01,100\n")
    r = setup_pending(client, "2026-03")
    check("修改被拒绝", "不能再修改待结月" in r.get_data(as_text=True))
    check("待结月保持 2026-01",
          q(db, "SELECT value FROM meta WHERE key='pending_month'")[0][0] == "2026-01")


# ---------------------------------------------------------------- 7. 导入/封账并发
def test_concurrent_import_and_close():
    print("\n[7] 并发：导入与封账重叠时整批计入本期或下期")
    app, client, db = make_client()
    setup_pending(client)
    barrier = threading.Barrier(4)
    results, errors = [], []

    def worker(i):
        c = app.test_client()
        csv_text = HEADER + f"T{i},2025-12,2026-01,{1000 * (i + 1)}\n"
        try:
            barrier.wait(timeout=10)
            time.sleep(0.01 * i)
            r = c.post("/import", data={"csv_text": csv_text})
            results.append((i, r.status_code))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"worker{i}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    barrier.wait(timeout=10)
    time.sleep(0.02)  # 让部分导入先提交，制造重叠
    r1 = close_month(client, "2026-01")
    for t in threads:
        t.join()
    r2 = close_month(client, "2026-02")

    check("并发期间无异常", not errors, "; ".join(errors))
    check("3 个批次全部成功", sorted(s for _, s in results) == [200, 200, 200],
          f"got {results}")
    check("两次封账均成功", "已封账 2026-01" in r1.get_data(as_text=True)
          and "已封账 2026-02" in r2.get_data(as_text=True))
    periods = [row[0] for row in q(db, "SELECT period_month FROM batches")]
    check("每批计入 2026-01 或 2026-02", set(periods) <= {"2026-01", "2026-02"}
          and len(periods) == 3, f"got {periods}")
    total_claims = q(db, "SELECT SUM(amount_cents) FROM claims")[0][0]
    total_archived = q(db, "SELECT SUM(net_cents) FROM archive")[0][0]
    check("两期归档净额合计 == 赔款合计（无丢失无重复）",
          total_archived == total_claims == 6000,
          f"archived={total_archived} claims={total_claims}")


# ---------------------------------------------------------------- 8. 大额合计超出 int64
def test_overflow_batch_home_close_triangle():
    print("\n[8] 批次合计 > int64：导入、首页、封账、归档、三角仍精确到分")
    app, client, db = make_client()
    setup_pending(client)
    expected_total = BIG * OVERFLOW_ROWS
    check("构造的批次合计确实超过有符号 64 位", expected_total > INT64_MAX,
          f"total={expected_total}")

    r = import_text(client, big_batch("B", OVERFLOW_ROWS))
    body = r.get_data(as_text=True)
    check("大额批次导入成功（此前会 OverflowError）", r.status_code == 200, f"status={r.status_code}")
    check("导入结果页显示精确合计", fmt(expected_total) in body)
    check("库内批次合计保存为精确十进制文本",
          int(q(db, "SELECT total_cents FROM batches WHERE id=1")[0][0]) == expected_total)

    home = client.get("/").get_data(as_text=True)
    check("首页总额精确显示（不溢出、不丢分）", fmt(expected_total) in home)

    r = close_month(client, "2026-01")
    check("大额期间封账成功", "已封账 2026-01" in r.get_data(as_text=True))
    arch = q(db, "SELECT net_cents FROM archive WHERE close_month='2026-01'")
    check("归档净额精确等于批次合计",
          [int(x[0]) for x in arch] == [expected_total], f"got {arch}")

    a = client.get("/archive/2026-01").get_data(as_text=True)
    check("归档明细合计精确显示", f"+{fmt(expected_total)}" in a)

    t = client.get("/triangle?start=2025-10&close=2026-01").get_data(as_text=True)
    check("三角累计精确显示", fmt(expected_total) in t)


# ---------------------------------------------------------------- 9. 跨期正负抵消回查
def test_cross_period_offset_triangle():
    print("\n[9] 补报/更正正负抵消：三角回查给出抵消后的准确结果")
    app, client, db = make_client()
    setup_pending(client)
    # 期 1：两个组合各一大笔
    import_text(client, HEADER + f"O1,2025-11,2025-11,{BIG}\nO2,2025-12,2026-01,{BIG}\n")
    close_month(client, "2026-01")

    # 期 2：O1 全额冲回（同号改金额 0，移到同月），O2 部分更正，新增一大笔
    import_text(client, HEADER + "O1,2025-11,2025-11,0\n"
                                 f"O2,2025-12,2026-01,{BIG - 7}\n"
                                 f"O3,2025-11,2026-02,{BIG}\n")
    close_month(client, "2026-02")

    # 期 3：O3 冲回，制造跨期正负抵消
    import_text(client, HEADER + "O3,2025-11,2026-02,0\n")
    close_month(client, "2026-03")

    # 2026-03 期归档应只留非零净变动：O3 -BIG；O1/O2 无净变动
    net3 = {tuple(r[:2]): int(r[2]) for r in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-03'")}
    check("期 3 仅归档非零净变动（O1 完全抵消不归档）", net3 == {("2025-11", "2026-02"): -BIG},
          f"got {net3}")

    t3 = client.get("/triangle?start=2025-11&close=2026-03").get_data(as_text=True)
    # 2025-11 行：2025-11 列 0；2026-02 列 +BIG 再 -BIG = 0；累计 0
    check("抵消后 2025-11 事故行累计为 0", '<td class="num">0</td>' in t3)
    check("O3 补报再冲回后无残留大额", fmt(BIG) not in t3,
          "仍有未抵消的 BIG 累计")
    # 2025-12 行应保留 BIG-7
    check("部分更正后的 2025-12 行精确为 BIG-7", fmt(BIG - 7) in t3)
    # 一期回查不受后续抵消影响
    t1 = client.get("/triangle?start=2025-11&close=2026-01").get_data(as_text=True)
    check("一期回查仍为当时口径（含两笔 BIG 各自累计）",
          t1.count(f'<td class="num">{fmt(BIG)}</td>') >= 2, "一期累计缺失")


# ---------------------------------------------------------------- 10. 失败原子性（大额封账/导入）
def test_overflow_failure_atomicity():
    print("\n[10] 大额失败原子性：导入/封账失败保留操作前数据与待结月")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, HEADER + f"F1,2025-12,2026-01,{BIG}\n")
    close_month(client, "2026-01")
    pending_before = q(db, "SELECT value FROM meta WHERE key='pending_month'")[0][0]
    claims_before = q(db, "SELECT claim_no, amount_cents FROM claims ORDER BY claim_no")
    archive_before = q(db, "SELECT close_month, accident_month, payment_month, net_cents "
                           "FROM archive ORDER BY close_month, accident_month, payment_month")

    # 导入：含一超限行（16 位）整批拒绝
    bad = HEADER + f"F2,2025-12,2026-02,{BIG}\nF3,2025-12,2026-02,{'9' * 16}\n"
    r = import_text(client, bad)
    check("超 15 位金额整批拒绝", r.status_code == 400, f"status={r.status_code}")
    check("拒绝后 claims 不变（无部分结果）",
          q(db, "SELECT claim_no, amount_cents FROM claims ORDER BY claim_no") == claims_before)
    check("拒绝后待结月不变",
          q(db, "SELECT value FROM meta WHERE key='pending_month'")[0][0] == pending_before)

    # 封账：封非待结月被拒绝，数据与待结月不变
    r = close_month(client, "2026-01")
    check("拒绝封非待结月", "只能封当前待结月" in r.get_data(as_text=True))
    check("拒绝后 archive 不变",
          q(db, "SELECT close_month, accident_month, payment_month, net_cents "
                "FROM archive ORDER BY close_month, accident_month, payment_month") == archive_before)
    check("拒绝后待结月仍为 2026-02",
          q(db, "SELECT value FROM meta WHERE key='pending_month'")[0][0] == "2026-02")

    # 成功封账后再把待结月推进；总额仍精确
    import_text(client, HEADER + f"F4,2025-12,2026-02,{BIG}\n")
    close_month(client, "2026-02")
    total = sum(int(r[0]) for r in q(db, "SELECT net_cents FROM archive"))
    check("两期归档净额合计精确等于两笔 BIG", total == 2 * BIG, f"got {total}")


# ---------------------------------------------------------------- 11. 旧 INTEGER 库迁移
OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, period_month TEXT NOT NULL,
  row_count INTEGER NOT NULL, total_cents INTEGER NOT NULL,
  overwritten INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT '2026-01-01 00:00:00');
CREATE TABLE claims (
  claim_no TEXT PRIMARY KEY, accident_month TEXT NOT NULL, payment_month TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  batch_id INTEGER NOT NULL REFERENCES batches(id));
CREATE TABLE claim_archived (
  claim_no TEXT PRIMARY KEY, accident_month TEXT NOT NULL, payment_month TEXT NOT NULL,
  amount_cents INTEGER NOT NULL);
CREATE TABLE closes (
  close_month TEXT PRIMARY KEY, batch_count INTEGER NOT NULL,
  combo_count INTEGER NOT NULL, closed_at TEXT NOT NULL DEFAULT '2026-01-01 00:00:00');
CREATE TABLE archive (
  close_month TEXT NOT NULL, accident_month TEXT NOT NULL, payment_month TEXT NOT NULL,
  net_cents INTEGER NOT NULL,
  PRIMARY KEY (close_month, accident_month, payment_month));
"""


def test_legacy_db_migration():
    print("\n[11] 旧库（INTEGER 金额）可直接续用并支持大额")
    tmp = tempfile.mkdtemp(prefix="lt-legacy-")
    db = os.path.join(tmp, "old.db")
    conn = sqlite3.connect(db)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO meta VALUES('pending_month','2026-02')")
    conn.execute("INSERT INTO batches(id,period_month,row_count,total_cents) VALUES(1,'2026-01',1,80000)")
    conn.execute("INSERT INTO claims VALUES('C-0002','2025-10','2025-12',80000,1)")
    conn.execute("INSERT INTO claim_archived VALUES('C-0002','2025-10','2025-12',80000)")
    conn.execute("INSERT INTO archive VALUES('2026-01','2025-10','2025-12',80000)")
    conn.execute("INSERT INTO closes VALUES('2026-01',1,1,'2026-01-01 00:00:00')")
    conn.commit()
    conn.close()

    app = create_app({"DB_PATH": db, "TESTING": True})
    client = app.test_client()
    home = client.get("/").get_data(as_text=True)
    check("旧库启动不报错，历史数据可读", "80,000" in home, home[:200])

    # 旧库续用：追加一大额批次并使合计（含历史）超 int64
    import_text(client, big_batch("N", OVERFLOW_ROWS, acc="2025-10", pay="2026-01"))
    close_month(client, "2026-02")
    rows = q(db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-02'")
    expected = {("2025-10", "2026-01"): BIG * OVERFLOW_ROWS}
    check("旧库续用：本期净变动精确",
          {tuple(r[:2]): int(r[2]) for r in rows} == expected, f"got {rows}")

    t = client.get("/triangle?start=2025-10&close=2026-02").get_data(as_text=True)
    check("旧库+新数据三角累计超 int64 仍精确",
          fmt(BIG * OVERFLOW_ROWS + 80000) in t and "80,000" in t)


def main():
    for fn in (test_full_flow_two_periods, test_import_validation,
               test_overwrite_and_zero, test_close_constraints,
               test_batch_atomicity, test_setup_locked_after_data,
               test_concurrent_import_and_close,
               test_overflow_batch_home_close_triangle,
               test_cross_period_offset_triangle,
               test_overflow_failure_atomicity,
               test_legacy_db_migration):
        fn()
    print(f"\n==== 结果：{len(_passed)} 通过，{len(_failed)} 失败 ====")
    if _failed:
        print("失败项：")
        for name in _failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
