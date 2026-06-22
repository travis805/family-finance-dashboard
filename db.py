"""
db.py — Database connection, schema, seeding, and query helpers
Family Finance Dashboard
"""
import os
import sqlite3
import libsql
import pandas as pd
import streamlit as st
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "family_finance.db"


def _get_turso_creds():
    """Look up Turso creds from Streamlit secrets first, then env vars."""
    url = token = None
    try:
        url = st.secrets.get("TURSO_DATABASE_URL")
        token = st.secrets.get("TURSO_AUTH_TOKEN")
    except Exception:
        pass  # no secrets.toml / not running in a Streamlit context
    url = url or os.environ.get("TURSO_DATABASE_URL")
    token = token or os.environ.get("TURSO_AUTH_TOKEN")
    return url, token


# ── Connection ───────────────────────────────────────────────────────────────

def get_conn():
    """
    Pick the backend at runtime:
    - Turso creds present (Streamlit secrets or env vars) -> remote libSQL,
      used in production / when deployed.
    - Otherwise -> local sqlite3 file, used for dev / offline work.
    """
    url, token = _get_turso_creds()
    if url and token:
        conn = libsql.connect(database=url, auth_token=token)
        # Note: libsql connections don't support row_factory assignment.
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        return conn

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _rows_as_dicts(cursor):
    """Convert a cursor's fetchall() into plain dicts, backend-agnostic
    (libsql cursor rows are plain tuples; sqlite3.Row supports name access
    natively, but this also works for it)."""
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def _row_as_dict(cursor):
    """Same as _rows_as_dicts but for a single-row fetch; returns None if empty."""
    rows = _rows_as_dicts(cursor)
    return rows[0] if rows else None


# ── Schema & seeding ─────────────────────────────────────────────────────────

def init_db():
    """Create all tables and seed reference data. Safe to call on every startup."""
    conn = get_conn()
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            institution   TEXT    NOT NULL,
            account_type  TEXT    NOT NULL,   -- checking, savings, credit_card, ira, 401k, 529, heloc, mortgage
            owner         TEXT    NOT NULL,   -- travis, alison, joint
            is_liability  INTEGER DEFAULT 0,  -- 1 for HELOC, mortgage
            is_active     INTEGER DEFAULT 1,
            notes         TEXT
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id           INTEGER NOT NULL REFERENCES accounts(id),
            date                 TEXT    NOT NULL,         -- ISO: YYYY-MM-DD
            description          TEXT,
            amount               REAL    NOT NULL,          -- negative = expense / debit, positive = income / credit
            category             TEXT,
            subcategory          TEXT,
            notes                TEXT,
            source_file          TEXT,
            manually_categorized INTEGER DEFAULT 0,        -- 1 = user-edited, skip on Recategorize All
            imported_at          TEXT    DEFAULT (datetime('now')),
            UNIQUE(account_id, date, description, amount)
        );

        CREATE TABLE IF NOT EXISTS account_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id    INTEGER NOT NULL REFERENCES accounts(id),
            snapshot_date TEXT    NOT NULL,        -- ISO: YYYY-MM-DD
            balance       REAL    NOT NULL,         -- positive = asset, negative = liability
            source_file   TEXT,
            notes         TEXT,
            imported_at   TEXT    DEFAULT (datetime('now')),
            UNIQUE(account_id, snapshot_date)
        );

        CREATE TABLE IF NOT EXISTS budget (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            category         TEXT    NOT NULL UNIQUE,
            quarterly_target REAL,                 -- NULL until set
            budget_type      TEXT    NOT NULL,     -- income, expense
            notes            TEXT
        );

        CREATE TABLE IF NOT EXISTS net_worth_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date    TEXT    NOT NULL UNIQUE,
            total_assets     REAL,
            total_liabilities REAL,
            net_worth        REAL,
            notes            TEXT,
            created_at       TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS subcategories (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            category   TEXT    NOT NULL,
            name       TEXT    NOT NULL,
            sort_order INTEGER DEFAULT 0,
            UNIQUE(category, name)
        );

        CREATE TABLE IF NOT EXISTS insights_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            quarter      TEXT    NOT NULL,          -- e.g. "Q1 2025"
            generated_at TEXT    DEFAULT (datetime('now')),
            prompt_used  TEXT,
            insight_text TEXT    NOT NULL
        );
    """)

    _run_migrations(c)
    _seed_accounts(c)
    _seed_budget_categories(c)
    _seed_subcategories(c)
    _seed_manual_assets(c)
    conn.commit()
    conn.close()


def _run_migrations(c):
    """Idempotent one-time fixes that run on every startup."""
    # Remove duplicate account rows — keep the lowest id per name.
    # Root cause: original seed used INSERT OR IGNORE without a UNIQUE constraint
    # on name, so every app restart inserted a fresh copy of every account.
    c.execute("""
        DELETE FROM accounts
        WHERE id NOT IN (
            SELECT MIN(id) FROM accounts GROUP BY name
        )
    """)
    # Retire the old generic Alison CC placeholder (replaced by 2246 and 8861)
    c.execute("""
        UPDATE accounts SET is_active = 0, notes = 'Replaced by (2246) and (8861)'
        WHERE name = 'Chase CC Alison'
    """)
    # Amex confirmed no activity — mark inactive
    c.execute("""
        UPDATE accounts SET is_active = 0, notes = 'No activity — excluded'
        WHERE name = 'Amex CC Alison'
    """)
    # Remove categories that don't warrant their own budget line
    c.execute("DELETE FROM budget WHERE category IN ('Auto Lease', 'House Cleaning')")
    # Add manually_categorized column if this is an older database that predates it
    existing_cols = [row[1] for row in c.execute("PRAGMA table_info(transactions)").fetchall()]
    if "manually_categorized" not in existing_cols:
        c.execute("ALTER TABLE transactions ADD COLUMN manually_categorized INTEGER DEFAULT 0")


def _seed_accounts(c):
    accounts = [
        # ── Chase ─────────────────────────────────────────────────────────
        ("Chase Joint Checking",    "Chase",           "checking",    "joint",  0, 1, "Account ending 4949"),
        ("Chase Secondary",         "Chase",           "savings",     "joint",  0, 1, "Account ending 6908 — purpose TBD"),
        # Travis credit cards
        ("Chase CC Travis (1396)",  "Chase",           "credit_card", "travis", 0, 1, None),
        ("Chase CC Travis (1892)",  "Chase",           "credit_card", "travis", 0, 1, None),
        ("Chase CC Travis (3440)",  "Chase",           "credit_card", "travis", 0, 1, None),
        ("Chase CC Travis (5912)",  "Chase",           "credit_card", "travis", 0, 1, None),
        ("Chase CC Travis (8692)",  "Chase",           "credit_card", "travis", 0, 1, None),
        # Alison credit cards
        ("Chase CC Alison (2246)",  "Chase",           "credit_card", "alison", 0, 1, None),
        ("Chase CC Alison (8861)",  "Chase",           "credit_card", "alison", 0, 1, None),
        # ── Amex ──────────────────────────────────────────────────────────
        ("Amex CC Alison",          "Amex",            "credit_card", "alison", 0, 0, "No activity — excluded"),
        # ── CoastHills ────────────────────────────────────────────────────
        ("CoastHills Checking",     "CoastHills CU",   "checking",    "joint",  0, 1, "Account ending S00"),
        ("CoastHills HELOC",        "CoastHills CU",   "heloc",       "joint",  1, 1, "Account ending L51"),
        # ── Vanguard ──────────────────────────────────────────────────────
        ("Vanguard Travis Roth IRA",  "Vanguard",      "ira",         "travis", 0, 1, None),
        ("Vanguard Alison Roth IRA",  "Vanguard",      "ira",         "alison", 0, 1, None),
        ("Vanguard Alison Trad IRA",  "Vanguard",      "ira",         "alison", 0, 1, None),
        ("Vanguard Cash Plus (Joint)","Vanguard",      "savings",     "joint",  0, 1, "Emergency / cash fund"),
        ("Vanguard 529 — Clark",      "Vanguard",      "529",         "joint",  0, 1, "Beneficiary: Clark E Campbell"),
        ("Vanguard 529 — Corey",      "Vanguard",      "529",         "joint",  0, 1, "Beneficiary: Corey Q Campbell"),
        # ── Fidelity ──────────────────────────────────────────────────────
        ("Fidelity 401(k) — Travis",  "Fidelity",      "401k",        "travis", 0, 1, None),
        # ── Rocket Mortgage ───────────────────────────────────────────────
        ("Rocket Mortgage",           "Rocket Mortgage","mortgage",   "joint",  1, 1, "Principal balance — not payoff amount"),
    ]
    for row in accounts:
        # Use WHERE NOT EXISTS so this is safe to call on every startup —
        # no duplicates even without a UNIQUE constraint on name.
        c.execute("""
            INSERT INTO accounts
              (name, institution, account_type, owner, is_liability, is_active, notes)
            SELECT ?,?,?,?,?,?,?
            WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE name = ?)
        """, (*row, row[0]))


def _seed_budget_categories(c):
    categories = [
        # Income
        ("Salary/Wages",                   "income"),
        ("Other Income",                   "income"),
        # Expenses
        ("Room Parent",                    "expense"),
        ("Housing",                        "expense"),
        ("Utilities",                      "expense"),
        ("Groceries",                      "expense"),
        ("Dining Out",                     "expense"),
        ("Transportation",                 "expense"),
        ("Health & Medical",               "expense"),
        ("Kids & Education",               "expense"),
        ("Entertainment & Subscriptions",  "expense"),
        ("Travel",                         "expense"),
        ("Shopping & Personal",            "expense"),
        ("Insurance",                      "expense"),
        ("Loan Payment — Dad",             "expense"),
        ("Savings & Investments",          "expense"),
        ("Transfers",                      "expense"),
        ("Miscellaneous",                  "expense"),
        ("Uncategorized",                  "expense"),
    ]
    for cat, btype in categories:
        c.execute("""
            INSERT INTO budget (category, quarterly_target, budget_type)
            SELECT ?, NULL, ?
            WHERE NOT EXISTS (SELECT 1 FROM budget WHERE category = ?)
        """, (cat, btype, cat))


def _seed_subcategories(c):
    """Seed the canonical subcategory list per category."""
    SUBCATS = {
        "Housing":                        ["Mortgage", "HOA Fees", "Home Maintenance & Repairs", "House Cleaning"],
        "Utilities":                      ["Electric & Gas", "Internet & Cable", "Phone", "Trash & Water"],
        "Groceries":                      ["Grocery Stores", "Specialty & Organic", "Coffee Subscription"],
        "Dining Out":                     ["Restaurants", "Fast Food", "Coffee & Cafes", "Desserts & Treats", "Food Delivery"],
        "Transportation":                 ["Auto Lease", "Gas & EV Charging", "Parking", "Car Maintenance"],
        "Health & Medical":               ["Gym & Fitness", "Pharmacy", "Spa & Wellness", "Supplements & Rx", "Medical & Dental"],
        "Kids & Education":               ["School Activities & Fundraisers", "Toys & Games", "Children's Health", "Learning Apps"],
        "Entertainment & Subscriptions":  ["Streaming Video", "Music & Podcasts", "Gaming", "Apps & Software"],
        "Shopping & Personal":            ["Clothing & Apparel", "Personal Care & Beauty", "Home & Decor",
                                           "Pet Supplies", "Crafts & Hobbies", "Gifts", "General Online"],
        "Travel":                         ["Flights & Transportation", "Hotels & Lodging", "Vacation Activities"],
        "Insurance":                      ["Auto", "Home", "Health"],
    }
    for category, subcats in SUBCATS.items():
        for order, name in enumerate(subcats):
            c.execute("""
                INSERT INTO subcategories (category, name, sort_order)
                SELECT ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM subcategories WHERE category = ? AND name = ?
                )
            """, (category, name, order, category, name))


def _seed_manual_assets(c):
    """Seed the Primary Residence account if it doesn't exist yet."""
    c.execute("""
        INSERT INTO accounts
          (name, institution, account_type, owner, is_liability, is_active, notes)
        SELECT 'Primary Residence', 'Manual', 'real_estate', 'joint', 0, 1,
               'Estimated market value — updated manually each quarter'
        WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE name = 'Primary Residence')
    """)


def upsert_manual_asset_value(conn, account_name: str, value: float, as_of_date: str = None):
    """Save or update the value of a manually-entered asset."""
    if as_of_date is None:
        as_of_date = datetime.today().strftime("%Y-%m-%d")
    account_id = get_account_id(conn, account_name)
    if not account_id:
        return
    conn.execute("""
        INSERT OR REPLACE INTO account_snapshots
          (account_id, snapshot_date, balance, source_file)
        VALUES (?, ?, ?, 'manual')
    """, (account_id, as_of_date, round(value, 2)))
    conn.commit()


def get_manual_asset_value(conn, account_name: str) -> float | None:
    """Return the most recent manually-entered value for an asset, or None."""
    account_id = get_account_id(conn, account_name)
    if not account_id:
        return None
    row = _row_as_dict(conn.execute("""
        SELECT balance FROM account_snapshots
        WHERE account_id = ? AND source_file = 'manual'
        ORDER BY snapshot_date DESC LIMIT 1
    """, (account_id,)))
    return float(row["balance"]) if row else None


# ── Account helpers ──────────────────────────────────────────────────────────

def get_accounts(conn) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM accounts ORDER BY institution, name", conn)


def get_account_id(conn, account_name: str) -> int | None:
    row = _row_as_dict(conn.execute(
        "SELECT id FROM accounts WHERE name = ?", (account_name,)
    ))
    return row["id"] if row else None


# ── Transaction helpers ──────────────────────────────────────────────────────

def get_transactions(
    conn,
    account_ids=None,
    start_date=None,
    end_date=None,
    categories=None,
    exclude_categories=None,
    search=None,
) -> pd.DataFrame:
    q = """
        SELECT t.id, t.date, t.description, t.amount, t.category,
               t.subcategory, t.notes, t.source_file, t.imported_at,
               a.name AS account_name, a.institution, a.account_type, a.owner
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE 1=1
    """
    params = []

    if account_ids:
        q += f" AND t.account_id IN ({','.join('?'*len(account_ids))})"
        params.extend(account_ids)
    if start_date:
        q += " AND t.date >= ?"
        params.append(start_date)
    if end_date:
        q += " AND t.date <= ?"
        params.append(end_date)
    if categories:
        q += f" AND t.category IN ({','.join('?'*len(categories))})"
        params.extend(categories)
    if exclude_categories:
        q += f" AND (t.category NOT IN ({','.join('?'*len(exclude_categories))}) OR t.category IS NULL)"
        params.extend(exclude_categories)
    if search:
        q += " AND LOWER(t.description) LIKE ?"
        params.append(f"%{search.lower()}%")

    q += " ORDER BY t.date DESC, t.id DESC"
    return pd.read_sql(q, conn, params=params)


def update_transaction_category(conn, txn_id: int, category: str, subcategory: str = None):
    """Update category (and optionally subcategory) and mark the row as manually categorized."""
    conn.execute(
        "UPDATE transactions SET category = ?, subcategory = ?, manually_categorized = 1 WHERE id = ?",
        (category, subcategory, txn_id),
    )
    conn.commit()


# ── Snapshot helpers ─────────────────────────────────────────────────────────

def get_latest_snapshots(conn) -> pd.DataFrame:
    """Most recent balance snapshot per account."""
    return pd.read_sql("""
        SELECT s.id, s.account_id, s.snapshot_date, s.balance, s.source_file,
               a.name AS account_name, a.institution, a.account_type, a.owner, a.is_liability
        FROM account_snapshots s
        JOIN accounts a ON a.id = s.account_id
        WHERE s.snapshot_date = (
            SELECT MAX(s2.snapshot_date)
            FROM account_snapshots s2
            WHERE s2.account_id = s.account_id
        )
        ORDER BY a.institution, a.name
    """, conn)


def get_snapshot_history(conn) -> pd.DataFrame:
    return pd.read_sql("""
        SELECT s.*, a.name AS account_name, a.institution, a.account_type,
               a.owner, a.is_liability
        FROM account_snapshots s
        JOIN accounts a ON a.id = s.account_id
        ORDER BY s.snapshot_date, a.name
    """, conn)


# ── Net worth ────────────────────────────────────────────────────────────────

def get_net_worth_summary(conn) -> dict:
    snaps = get_latest_snapshots(conn)
    if snaps.empty:
        return {"assets": 0.0, "liabilities": 0.0, "net_worth": 0.0}
    assets      = snaps[snaps["is_liability"] == 0]["balance"].sum()
    liabilities = abs(snaps[snaps["is_liability"] == 1]["balance"].sum())
    return {"assets": assets, "liabilities": liabilities, "net_worth": assets - liabilities}


def calculate_and_save_net_worth(conn):
    summary = get_net_worth_summary(conn)
    today = datetime.today().strftime("%Y-%m-%d")
    conn.execute("""
        INSERT OR REPLACE INTO net_worth_history
          (snapshot_date, total_assets, total_liabilities, net_worth)
        VALUES (?, ?, ?, ?)
    """, (today, summary["assets"], summary["liabilities"], summary["net_worth"]))
    conn.commit()


def get_net_worth_history(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT * FROM net_worth_history ORDER BY snapshot_date", conn
    )


# ── Budget & spending ────────────────────────────────────────────────────────

def get_budget_categories(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT * FROM budget ORDER BY budget_type DESC, category", conn
    )


def update_budget_target(conn, category: str, target):
    conn.execute("UPDATE budget SET quarterly_target = ? WHERE category = ?", (target, category))
    conn.commit()


def add_budget_category(conn, category: str, budget_type: str):
    conn.execute(
        "INSERT OR IGNORE INTO budget (category, budget_type) VALUES (?, ?)",
        (category, budget_type),
    )
    conn.commit()


def delete_budget_category(conn, category: str):
    conn.execute("DELETE FROM budget WHERE category = ?", (category,))
    conn.commit()


# ── Subcategory management ───────────────────────────────────────────────────

def get_subcategories_by_category(conn) -> dict[str, list[str]]:
    """Return {category: [name, ...]} ordered by sort_order then name."""
    rows = _rows_as_dicts(conn.execute(
        "SELECT category, name FROM subcategories ORDER BY category, sort_order, name"
    ))
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(row["category"], []).append(row["name"])
    return result


def add_subcategory(conn, category: str, name: str) -> bool:
    """
    Add a subcategory under the given category.
    Returns True on success, False if the (category, name) pair already exists.
    """
    try:
        conn.execute(
            "INSERT INTO subcategories (category, name) VALUES (?, ?)",
            (category, name.strip()),
        )
        conn.commit()
        return True
    except Exception:
        return False


def delete_subcategory(conn, category: str, name: str):
    """Remove a subcategory. Transactions that used it keep their text value."""
    conn.execute(
        "DELETE FROM subcategories WHERE category = ? AND name = ?",
        (category, name),
    )
    conn.commit()


def get_quarterly_spending(conn, start_date: str, end_date: str) -> pd.DataFrame:
    """
    Spending by category for a date range.
    - Only expenses (amount < 0) from non-savings/investment accounts.
    - Excludes Transfers and Salary/Wages categories.
    """
    return pd.read_sql("""
        SELECT
            COALESCE(t.category, 'Uncategorized') AS category,
            SUM(ABS(t.amount)) AS actual
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.date BETWEEN ? AND ?
          AND t.amount < 0
          AND COALESCE(t.category, 'Uncategorized') NOT IN ('Transfers', 'Salary/Wages')
          AND a.account_type NOT IN ('savings', '401k', 'ira', '529')
        GROUP BY category
        ORDER BY actual DESC
    """, conn, params=[start_date, end_date])


def get_quarterly_income(conn, start_date: str, end_date: str) -> float:
    """Sum of positive-amount transactions in any income-type budget category."""
    row = _row_as_dict(conn.execute("""
        SELECT COALESCE(SUM(t.amount), 0) AS total
        FROM transactions t
        JOIN budget b ON b.category = t.category
        WHERE t.date BETWEEN ? AND ?
          AND t.amount > 0
          AND b.budget_type = 'income'
    """, (start_date, end_date)))
    return float(row["total"]) if row else 0.0


# ── Data freshness ───────────────────────────────────────────────────────────

def get_data_freshness(conn) -> pd.DataFrame:
    return pd.read_sql("""
        SELECT
            a.name         AS account_name,
            a.institution,
            a.account_type,
            a.owner,
            MAX(t.date)           AS last_transaction,
            COUNT(t.id)           AS transaction_count,
            MAX(s.snapshot_date)  AS last_snapshot
        FROM accounts a
        LEFT JOIN transactions    t ON t.account_id = a.id
        LEFT JOIN account_snapshots s ON s.account_id = a.id
        WHERE a.is_active = 1
        GROUP BY a.id
        ORDER BY a.institution, a.name
    """, conn)


def get_monthly_activity(conn, start_date: str = "2025-01-01") -> pd.DataFrame:
    """
    Monthly income and spending by category — income statement view.

    Returns columns: month (YYYY-MM), category, budget_type, total
    - Income rows: total is the sum of positive amounts (as-is)
    - Expense rows: total is the sum of absolute values of negative amounts
    Transfers and Uncategorized are excluded.
    """
    end_date = datetime.today().strftime("%Y-%m-%d")
    return pd.read_sql("""
        SELECT
            strftime('%Y-%m', t.date) AS month,
            COALESCE(t.category, 'Uncategorized') AS category,
            COALESCE(b.budget_type, 'expense') AS budget_type,
            SUM(
                CASE WHEN COALESCE(b.budget_type, 'expense') = 'income'
                     THEN t.amount
                     ELSE ABS(t.amount)
                END
            ) AS total
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN budget b ON b.category = COALESCE(t.category, 'Uncategorized')
        WHERE t.date BETWEEN ? AND ?
          AND COALESCE(t.category, 'Uncategorized') != 'Transfers'
          AND a.account_type NOT IN ('savings', '401k', 'ira', '529')
          AND (
              (COALESCE(b.budget_type, 'expense') = 'income' AND t.amount > 0)
              OR
              (COALESCE(b.budget_type, 'expense') = 'expense' AND t.amount < 0)
          )
        GROUP BY strftime('%Y-%m', t.date), COALESCE(t.category, 'Uncategorized')
        ORDER BY strftime('%Y-%m', t.date), COALESCE(t.category, 'Uncategorized')
    """, conn, params=[start_date, end_date])


def get_monthly_activity_detail(conn, start_date: str = "2025-01-01") -> pd.DataFrame:
    """
    Monthly income and spending broken down by category AND subcategory.

    Returns columns: month (YYYY-MM), category, budget_type, subcategory, total
    - Includes Uncategorized (subcategory = 'Other' when NULL)
    - Excludes only Transfers
    """
    end_date = datetime.today().strftime("%Y-%m-%d")
    return pd.read_sql("""
        SELECT
            strftime('%Y-%m', t.date)              AS month,
            COALESCE(t.category, 'Uncategorized')  AS category,
            COALESCE(b.budget_type, 'expense')      AS budget_type,
            COALESCE(t.subcategory, 'Other')        AS subcategory,
            SUM(
                CASE WHEN COALESCE(b.budget_type, 'expense') = 'income'
                     THEN t.amount
                     ELSE ABS(t.amount)
                END
            ) AS total
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN budget b ON b.category = COALESCE(t.category, 'Uncategorized')
        WHERE t.date BETWEEN ? AND ?
          AND COALESCE(t.category, 'Uncategorized') != 'Transfers'
          AND a.account_type NOT IN ('savings', '401k', 'ira', '529')
          AND (
              (COALESCE(b.budget_type, 'expense') = 'income' AND t.amount > 0)
              OR
              (COALESCE(b.budget_type, 'expense') = 'expense' AND t.amount < 0)
          )
        GROUP BY
            strftime('%Y-%m', t.date),
            COALESCE(t.category, 'Uncategorized'),
            COALESCE(t.subcategory, 'Other')
        ORDER BY
            strftime('%Y-%m', t.date),
            COALESCE(t.category, 'Uncategorized'),
            COALESCE(t.subcategory, 'Other')
    """, conn, params=[start_date, end_date])


# ── Quarter utilities ────────────────────────────────────────────────────────

def get_quarter_dates(quarter_str: str) -> tuple[str, str]:
    """'Q2 2025' → ('2025-04-01', '2025-06-30')"""
    q_label, year = quarter_str.split()
    year = int(year)
    q = int(q_label[1])
    starts = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}
    ends   = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
    return f"{year}-{starts[q]}", f"{year}-{ends[q]}"


def get_current_quarter() -> str:
    now = datetime.now()
    q = (now.month - 1) // 3 + 1
    return f"Q{q} {now.year}"


def get_quarter_list(start_year: int = 2025) -> list[str]:
    """All quarters from Q1 start_year through the current quarter, newest first."""
    now = datetime.now()
    current_q = (now.month - 1) // 3 + 1
    quarters = []
    y, q = start_year, 1
    while True:
        quarters.append(f"Q{q} {y}")
        if y == now.year and q == current_q:
            break
        q += 1
        if q > 4:
            q, y = 1, y + 1
    return list(reversed(quarters))
