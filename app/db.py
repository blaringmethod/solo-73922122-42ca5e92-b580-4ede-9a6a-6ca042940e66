"""SQLite 连接与表结构。

表设计：
- meta            : 全局参数，当前仅有 pending_month（待结月）。
- batches         : 导入批次；period_month 记录导入时的待结月，决定该批计入哪一期封账。
- claims          : 赔款当前状态（同号重报整笔覆盖后的最新值）。
- claim_archived  : 每笔赔案在最近一次封账时的归档快照，用于计算下期净变动。
- closes          : 已封账月份。
- archive         : 每期归档汇总，只保存净变动非零的 (事故月, 支付月) 组合；回查只读此表。

金额列（batches.total_cents / claims.amount_cents / claim_archived.amount_cents /
archive.net_cents）均以 TEXT 保存规范十进制整数字符串，计算在 Python 任意精度整数
上完成：单笔仍限 15 位，但批合计、首页总额、归档净额与三角累计不受 int64 限制。
旧库若金额列为 INTEGER，启动时自动重建为 TEXT（值原样保留）。
"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  period_month  TEXT NOT NULL,
  row_count     INTEGER NOT NULL,
  total_cents   TEXT NOT NULL,
  overwritten   INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS claims (
  claim_no       TEXT PRIMARY KEY,
  accident_month TEXT NOT NULL,
  payment_month  TEXT NOT NULL,
  amount_cents   TEXT NOT NULL,
  batch_id       INTEGER NOT NULL REFERENCES batches(id)
);

CREATE TABLE IF NOT EXISTS claim_archived (
  claim_no       TEXT PRIMARY KEY,
  accident_month TEXT NOT NULL,
  payment_month  TEXT NOT NULL,
  amount_cents   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS closes (
  close_month TEXT PRIMARY KEY,
  batch_count INTEGER NOT NULL,
  combo_count INTEGER NOT NULL,
  closed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS archive (
  close_month    TEXT NOT NULL,
  accident_month TEXT NOT NULL,
  payment_month  TEXT NOT NULL,
  net_cents      TEXT NOT NULL,
  PRIMARY KEY (close_month, accident_month, payment_month)
);

CREATE INDEX IF NOT EXISTS idx_claims_batch   ON claims(batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_period ON batches(period_month);
CREATE INDEX IF NOT EXISTS idx_archive_close  ON archive(close_month);
"""

# 旧版把金额存成 INTEGER（受 int64 限制）。重建为 TEXT 时保持列顺序与列名不变，
# 用 CAST(col AS TEXT) 原样搬运已有值。
MONEY_TABLE_REBUILDS = {
    "batches": (
        "CREATE TABLE batches_migrate ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " period_month TEXT NOT NULL,"
        " row_count INTEGER NOT NULL,"
        " total_cents TEXT NOT NULL,"
        " overwritten INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')))"
    ),
    "claims": (
        "CREATE TABLE claims_migrate ("
        " claim_no TEXT PRIMARY KEY,"
        " accident_month TEXT NOT NULL,"
        " payment_month TEXT NOT NULL,"
        " amount_cents TEXT NOT NULL,"
        " batch_id INTEGER NOT NULL REFERENCES batches(id))"
    ),
    "claim_archived": (
        "CREATE TABLE claim_archived_migrate ("
        " claim_no TEXT PRIMARY KEY,"
        " accident_month TEXT NOT NULL,"
        " payment_month TEXT NOT NULL,"
        " amount_cents TEXT NOT NULL)"
    ),
    "archive": (
        "CREATE TABLE archive_migrate ("
        " close_month TEXT NOT NULL,"
        " accident_month TEXT NOT NULL,"
        " payment_month TEXT NOT NULL,"
        " net_cents TEXT NOT NULL,"
        " PRIMARY KEY (close_month, accident_month, payment_month))"
    ),
}


def _column_types(conn, table):
    return {row[1]: (row[2] or "").upper() for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_money_columns(conn):
    """把仍为 INTEGER 的金额列重建为 TEXT，保留已有数据。幂等。

    各金额表在同一事务内重建：任一表失败则整体回滚，旧库保持原样。
    """
    pending = []
    for table in MONEY_TABLE_REBUILDS:
        cols = _column_types(conn, table)
        if not cols:
            continue
        money_col = "total_cents" if table == "batches" else (
            "net_cents" if table == "archive" else "amount_cents")
        if cols.get(money_col) != "TEXT":
            pending.append((table, money_col))
    if not pending:
        return
    # 重建期间外键检查须关闭，否则 DROP TABLE 会因引用而失败。
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, money_col in pending:
            tmp = f"{table}_migrate"
            col_names = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            select_exprs = ", ".join(
                f"CAST({c} AS TEXT)" if c == money_col else c for c in col_names)
            conn.execute(f"DROP TABLE IF EXISTS {tmp}")
            conn.execute(MONEY_TABLE_REBUILDS[table])
            conn.execute(f"INSERT INTO {tmp}({', '.join(col_names)})"
                         f" SELECT {select_exprs} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DB_PATH"], isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(app):
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate_money_columns(conn)
        conn.executescript(
            "CREATE INDEX IF NOT EXISTS idx_claims_batch   ON claims(batch_id);"
            "CREATE INDEX IF NOT EXISTS idx_batches_period ON batches(period_month);"
            "CREATE INDEX IF NOT EXISTS idx_archive_close  ON archive(close_month);"
        )
        conn.commit()
    finally:
        conn.close()
