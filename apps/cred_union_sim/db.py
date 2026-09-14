import json
import os
import secrets
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("CRED_UNION_SIM_DB", ":memory:")
SEED_SQL_PATH = Path(__file__).parent / "seed.sql"
SEED_DATA_MARKER = "-- ==== SEED DATA ===="

_connection: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        _connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA foreign_keys = ON")
    return _connection


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _read_seed_sql() -> tuple[str, str]:
    sql = SEED_SQL_PATH.read_text()
    schema_sql, _, seed_sql = sql.partition(SEED_DATA_MARKER)
    return schema_sql, seed_sql


def init_db() -> None:
    schema_sql, _ = _read_seed_sql()
    conn = get_connection()
    conn.executescript(schema_sql)
    conn.commit()


def seed_db() -> None:
    _, seed_sql = _read_seed_sql()
    conn = get_connection()
    conn.executescript(seed_sql)
    conn.commit()


def get_member(member_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM members WHERE member_id = ?", (member_id,)
    ).fetchone()
    return _row_to_dict(row)


def get_accounts_by_member(member_id: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM accounts WHERE member_id = ?", (member_id,)
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_account(account_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    return _row_to_dict(row)


def create_subaccount_app(
    member_id: str,
    account_type: str,
    opening_amount: float,
    funding_account_id: str,
) -> dict:
    conn = get_connection()
    app_id = "app_" + secrets.token_hex(6)
    conn.execute(
        """
        INSERT INTO sub_account_apps
            (app_id, account_id, member_id, requested_type, opening_amount,
             funding_account_id, disclosure_accepted, review_status)
        VALUES (?, ?, ?, ?, ?, ?, 0, 'pending')
        """,
        (
            app_id,
            funding_account_id,
            member_id,
            account_type,
            opening_amount,
            funding_account_id,
        ),
    )
    conn.commit()
    return get_subaccount_app(app_id)


def get_subaccount_app(app_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM sub_account_apps WHERE app_id = ?", (app_id,)
    ).fetchone()
    return _row_to_dict(row)


def log_audit(member_id: str | None, action: str, details: dict | None = None) -> None:
    conn = get_connection()
    event_id = "evt_" + secrets.token_hex(6)
    conn.execute(
        "INSERT INTO audit_log (event_id, member_id, action, details) VALUES (?, ?, ?, ?)",
        (event_id, member_id, action, json.dumps(details) if details is not None else None),
    )
    conn.commit()
