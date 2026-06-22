"""
importer.py — File parsers and import orchestration
Family Finance Dashboard

Supported formats:
  Chase checking CSV    — Details, Posting Date, Description, Amount, Type, Balance, Check or Slip #
  Chase credit card CSV — Transaction Date, Post Date, Description, Category, Type, Amount, Memo
  CoastHills checking   — 3-line header block, then: Transaction Number, Date, Description, Memo,
                          Amount Debit, Amount Credit, Balance, Check Number
  CoastHills HELOC      — Same header block, extra columns: Fees, Principal, Interest
  Vanguard PDFs         — IRA (Roth/Trad), Cash Plus, 529 (Clark + Corey)
  Fidelity PDF          — 401(k) Statement Details
  Rocket Mortgage PDFs  — Monthly mortgage statements
"""
import re
import os
import csv
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd
import pdfplumber

import db
from db import get_account_id

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ── Filename → account name mapping ─────────────────────────────────────────

CHASE_ACCT_MAP = {
    "4949": "Chase Joint Checking",
    "6908": "Chase Secondary",
    "1396": "Chase CC Travis (1396)",
    "1892": "Chase CC Travis (1892)",
    "3440": "Chase CC Travis (3440)",
    "5912": "Chase CC Travis (5912)",
    "8692": "Chase CC Travis (8692)",
    "2246": "Chase CC Alison (2246)",
    "8861": "Chase CC Alison (8861)",
}

# ── Auto-categorizer ─────────────────────────────────────────────────────────

# Chase built-in category → our category (used as fallback when no keyword matches)
CHASE_CATEGORY_MAP = {
    "food & drink":           "Dining Out",
    "restaurants":            "Dining Out",
    "groceries":              "Groceries",
    "shopping":               "Shopping & Personal",
    "bills & utilities":      "Utilities",
    "utilities":              "Utilities",
    "health & wellness":      "Health & Medical",
    "travel":                 "Travel",
    "transportation":         "Transportation",
    "gas":                    "Transportation",
    "entertainment":          "Entertainment & Subscriptions",
    "education":              "Kids & Education",
    "personal":               "Shopping & Personal",
    "professional services":  "Miscellaneous",
    "home":                   "Housing",
    "fees & adjustments":     "Miscellaneous",
    "insurance":              "Insurance",
    "gifts & donations":      "Miscellaneous",
    "automotive":             "Transportation",
}

# Description keyword → our category (case-insensitive, checked in order).
# NOTE: keyword rules are checked BEFORE the Chase category map so our
# specific rules always win over Chase's broad buckets.
KEYWORD_RULES = [
    # ── Specific person / vendor overrides (must precede broader rules) ───────
    # Zelle to specific payees — placed above generic "zelle" → Transfers rule
    (["alicia"],                                                "Housing"),       # house cleaner
    (["chris campbell"],                                        "Loan Payment — Dad"),
    # Vendors that Chase miscategorises
    (["home depot"],                                            "Housing"),
    (["mistobox"],                                              "Groceries"),     # specialty coffee sub
    (["disneyland", "disney springs", "disney world",
      "disney store", "dlr "],                                  "Travel"),        # parks (not Disney+)
    (["ally paymt", "ally financial", "ally auto"],             "Transportation"),  # auto lease
    # "City of SLO" alone → Utilities, but with "pkg" it's a parking meter
    (["city of slo pkg", "slo pkg"],                           "Transportation"),

    # ── Internal transfers (catch-all — broad patterns come first) ────────────
    # CoastHills / checking internal movements
    (["home banking", "remote online deposit", "cohicu",
      "loan advance", "deposit home banking", "withdrawal home banking",
      "deposit jpmorgan", "payment to chase card",
      "payment - thank", "online transfer", "acct_xfer", "autopay",
      "automatic payment", "venmo", "zelle", "paypal",
      "vmc cash mgmt"],                                          "Transfers"),

    # ── Income ───────────────────────────────────────────────────────────────
    (["payroll", "cohesity", "direct dep", "salary"],           "Salary/Wages"),

    # ── Housing ──────────────────────────────────────────────────────────────
    (["rocket mortgage", "mortgage", "hoa", "assessments",
      "kilbern", "fs pay", "nsmdbamr", "nsm dbamr",
      "plumbing", "roofing", "landscap"],                        "Housing"),

    # ── Utilities ────────────────────────────────────────────────────────────
    (["att*", "att bill", "verizon", "comcast", "xfinity",
      "pacific gas", "pg&e", "pgande", "socalgas", "so cal gas",
      "electric", "garbage", "trash", "recycle", "water service",
      "internet", "phone bill", "sloco", "city of slo",
      "city of san luis obispo"],                                "Utilities"),

    # ── Groceries ────────────────────────────────────────────────────────────
    (["trader joe", "whole foods", "wholefds", "safeway", "vons",
      "albertsons", "sprouts", "costco", "grocery", "grocer", "market",
      "smart & final", "smart and final", "righetti ranch",
      "avila valley barn", "magic spoon", "penzeys"],           "Groceries"),

    # ── Dining Out ───────────────────────────────────────────────────────────
    (["doordash", "uber eats", "grubhub", "postmates", "instacart",
      "restaurant", "pizza", "burger", "taco", "chipotle", "mcdonald",
      "starbucks", "coffee", "cafe", "diner", "grill", "sushi",
      "thai", "chinese", "mexican", "boba", "bakery", "brew",
      "salt & straw", "tst*", "negranti", "finney",
      "yumy gurt", "creamery", "old san luis bbq",
      "peppermill", "biscotti", "espresso", "wendy",
      "brown butter", "sees candy", "candy",
      "black sheep bar", "bear and the wren"],                   "Dining Out"),

    # ── Transportation ───────────────────────────────────────────────────────
    (["shell", "chevron", "arco", "76 gas", "exxon", "mobil", "bp",
      "circle k", "fuel", "gas station", "uber", "lyft", "parking",
      "park meter", "parkwhiz", "dmv", "state of calif dmv", "ca dmv",
      "aaa ", "car wash", "auto repair", "jiffy lube", "valvoline",
      "napa auto", "tesla supercharger", "chargepoint", "ev charg",
      "city of slo pkg"],                                        "Transportation"),

    # ── Health & Medical ─────────────────────────────────────────────────────
    (["pharmacy", "walgreens", "cvs", "rite aid", "medical", "doctor",
      "dentist", "dental", "vision", "optometry", "clinic", "urgent care",
      "hospital", "therapy", "mental health", "massage", "wellness",
      "sloco massage", "businessolver", "healthequity", "health equity",
      "wex health", "henry meds", "gymnazo", "solspa", "elev8",
      "empower movement", "pilates"],                             "Health & Medical"),

    # ── Kids & Education ─────────────────────────────────────────────────────
    (["school", "tutor", "education", "book", "learning", "childcare",
      "daycare", "camp", "activity", "sport", "lesson", "homeschool",
      "bright life play", "conservation ambassador",
      "hiya health", "sinsheimer", "baseball alliance",
      "pottery barn kids", "potterybarnkids",
      "99pledg", "lingokids"],                                   "Kids & Education"),

    # ── Entertainment & Subscriptions ────────────────────────────────────────
    # Note: "disney+" is intentionally kept here (streaming, not parks)
    (["netflix", "hulu", "disney+", "spotify", "apple.com/bill", "apple tv",
      "amazon prime", "hbo", "paramount", "peacock", "youtube", "twitch",
      "steam", "playstation", "xbox", "nintendo", "membership",
      "subscription", "directv", "linkedin", "prime video",
      "google *", "google one", "perplexity", "pokedata",
      "sloeats", "siriusxm", "sirius xm"],                     "Entertainment & Subscriptions"),

    # ── Shopping & Personal ───────────────────────────────────────────────────
    (["amazon", "target", "walmart", "best buy", "ebay",
      "etsy", "gap", "old navy", "h&m", "zara", "nordstrom",
      "macy", "tj maxx", "marshalls", "ross",
      "michaels", "staples", "bath and body", "dollar tree",
      "grove collaborative", "prettylitter", "ups store",
      "evereden", "cut it out", "chewy", "pottery barn",
      "frosted pearl", "blueair", "ah louis", "pampered chef",
      "coconuts hair", "hair salon", "gloss*"],                  "Shopping & Personal"),

    # ── Travel ───────────────────────────────────────────────────────────────
    (["airline", "hotel", "airbnb", "marriott", "hilton", "hyatt",
      "united air", "delta air", "southwest", "american air", "jetblue",
      "hertz", "enterprise", "budget rent", "vrbo", "expedia", "kayak",
      "trip", "resort"],                                         "Travel"),

    # ── Insurance ────────────────────────────────────────────────────────────
    (["insurance", "geico", "state farm", "allstate", "progressive",
      "usaa", "farmers", "mercury ins"],                         "Insurance"),

    # ── Savings & Investments ─────────────────────────────────────────────────
    (["vanguard", "fidelity", "schwab", "coinbase", "savings transfer",
      "investment", "vgi 529"],                                   "Savings & Investments"),

    # ── Miscellaneous ─────────────────────────────────────────────────────────
    (["purchase interest charge", "interest charge",
      "experian", "annual fee", "foreign transaction",
      "atm fee", "non-chase atm", "service charge",
      "zenledger", "cryptotaxcalculator"],                       "Miscellaneous"),
]


# (keywords, category, subcategory) — checked after category is known
SUBCATEGORY_RULES = [
    # ── Housing ──────────────────────────────────────────────────────────────
    (["rocket mortgage", "nsmdbamr", "nsm dbamr"],              "Housing",          "Mortgage"),
    (["hoa", "assessments", "fs pay", "kilbern"],               "Housing",          "HOA Fees"),
    (["alicia"],                                                 "Housing",          "House Cleaning"),
    (["home depot", "plumbing", "roofing", "landscap"],         "Housing",          "Home Maintenance & Repairs"),

    # ── Utilities ────────────────────────────────────────────────────────────
    (["pg&e", "pgande", "socalgas", "so cal gas",
      "pacific gas", "sloco"],                                   "Utilities",        "Electric & Gas"),
    (["comcast", "xfinity", "att", "internet"],                 "Utilities",        "Internet & Cable"),
    (["verizon", "phone bill"],                                  "Utilities",        "Phone"),
    (["garbage", "trash", "wci", "water", "city of slo"],       "Utilities",        "Trash & Water"),

    # ── Groceries ────────────────────────────────────────────────────────────
    (["whole foods", "wholefds", "sprouts"],                     "Groceries",        "Specialty & Organic"),
    (["mistobox"],                                               "Groceries",        "Coffee Subscription"),
    (["trader joe", "safeway", "vons", "albertsons", "costco",
      "smart & final", "smart and final", "righetti ranch",
      "avila valley barn", "penzeys", "magic spoon",
      "grocery", "grocer", "market"],                           "Groceries",        "Grocery Stores"),

    # ── Dining Out ───────────────────────────────────────────────────────────
    (["doordash", "uber eats", "grubhub", "postmates",
      "instacart"],                                              "Dining Out",       "Food Delivery"),
    (["mcdonald", "wendy", "chipotle", "burger", "pizza",
      "in-n-out", "jack in", "taco bell", "taco "],             "Dining Out",       "Fast Food"),
    (["starbucks", "coffee", "cafe", "espresso",
      "blackhorse", "boba"],                                     "Dining Out",       "Coffee & Cafes"),
    (["salt & straw", "negranti", "creamery", "sees candy",
      "candy", "brown butter", "yumy gurt", "bakery",
      "dessert"],                                                "Dining Out",       "Desserts & Treats"),
    # Restaurants catch-all — must come AFTER the more specific rules above
    (["restaurant", "grill", "sushi", "thai", "chinese",
      "mexican", "finney", "old san luis bbq", "tst*",
      "peppermill", "biscotti", "black sheep bar",
      "bear and the wren", "diner", "brew"],                    "Dining Out",       "Restaurants"),

    # ── Transportation ───────────────────────────────────────────────────────
    (["ally paymt", "ally financial", "ally auto"],              "Transportation",   "Auto Lease"),
    (["shell", "chevron", "arco", "76 gas", "exxon", "mobil",
      "bp", "circle k", "fuel", "gas station",
      "tesla supercharger", "chargepoint", "ev charg"],         "Transportation",   "Gas & EV Charging"),
    (["parking", "park meter", "parkwhiz",
      "city of slo pkg", "slo pkg"],                            "Transportation",   "Parking"),
    (["car wash", "auto repair", "jiffy lube", "valvoline",
      "napa auto", "dmv", "state of calif dmv", "ca dmv",
      "aaa "],                                                   "Transportation",   "Car Maintenance"),

    # ── Health & Medical ─────────────────────────────────────────────────────
    (["gymnazo", "pilates", "empower movement", "elev8"],        "Health & Medical", "Gym & Fitness"),
    (["pharmacy", "walgreens", "cvs", "rite aid"],               "Health & Medical", "Pharmacy"),
    (["solspa", "massage", "wellness", "sloco massage"],         "Health & Medical", "Spa & Wellness"),
    (["henry meds", "pescience"],                                "Health & Medical", "Supplements & Rx"),
    (["medical", "doctor", "dentist", "dental", "vision",
      "optometry", "clinic", "urgent care", "hospital",
      "therapy"],                                                "Health & Medical", "Medical & Dental"),

    # ── Kids & Education ─────────────────────────────────────────────────────
    (["sinsheimer", "baseball alliance", "99pledg",
      "conservation ambassador"],                                "Kids & Education", "School Activities & Fundraisers"),
    (["bright life play", "pottery barn kids",
      "potterybarnkids"],                                        "Kids & Education", "Toys & Games"),
    (["hiya health"],                                            "Kids & Education", "Children's Health"),
    (["lingokids"],                                              "Kids & Education", "Learning Apps"),

    # ── Entertainment & Subscriptions ────────────────────────────────────────
    (["netflix", "hulu", "disney+", "hbo", "paramount",
      "peacock", "youtube", "prime video", "directv",
      "spi*directv", "apple tv"],                               "Entertainment & Subscriptions", "Streaming Video"),
    (["spotify", "siriusxm", "sirius xm", "apple.com/bill"],    "Entertainment & Subscriptions", "Music & Podcasts"),
    (["steam", "playstation", "xbox", "nintendo", "twitch",
      "pokedata"],                                              "Entertainment & Subscriptions", "Gaming"),
    (["google *", "google one", "perplexity", "linkedin",
      "sloeats"],                                               "Entertainment & Subscriptions", "Apps & Software"),

    # ── Shopping & Personal ──────────────────────────────────────────────────
    (["gap", "old navy", "h&m", "zara", "nordstrom", "macy",
      "tj maxx", "marshalls", "ross"],                          "Shopping & Personal", "Clothing & Apparel"),
    (["coconuts hair", "hair", "gloss*", "evereden",
      "bath and body", "frosted pearl", "bnsc eminence",
      "brightoncutters"],                                        "Shopping & Personal", "Personal Care & Beauty"),
    (["pottery barn", "blueair"],                               "Shopping & Personal", "Home & Decor"),
    (["chewy", "prettylitter"],                                  "Shopping & Personal", "Pet Supplies"),
    (["michaels", "cut it out"],                                 "Shopping & Personal", "Crafts & Hobbies"),
    # General Online catch-all — must come last in Shopping
    (["amazon", "etsy", "walmart", "best buy", "staples",
      "dollar tree", "ups store", "pampered chef", "ah louis",
      "grove collaborative", "target"],                          "Shopping & Personal", "General Online"),

    # ── Travel ───────────────────────────────────────────────────────────────
    (["airline", "united air", "delta air", "southwest",
      "american air", "jetblue", "hertz", "enterprise",
      "budget rent"],                                            "Travel",           "Flights & Transportation"),
    (["hotel", "airbnb", "marriott", "hilton", "hyatt",
      "vrbo", "expedia", "kayak", "resort"],                    "Travel",           "Hotels & Lodging"),
    (["disneyland", "disney springs", "disney world",
      "disney store", "dlr "],                                  "Travel",           "Vacation Activities"),

    # ── Insurance ────────────────────────────────────────────────────────────
    (["mercury ins", "geico", "progressive"],                   "Insurance",        "Auto"),
    (["state farm", "allstate", "usaa", "farmers"],             "Insurance",        "Home"),
]


def auto_subcategorize(description: str, category: str) -> str | None:
    """Return the subcategory for a transaction given its description and already-resolved category."""
    desc_lower = " ".join((description or "").replace("&amp;", "&").lower().split())
    for keywords, cat, subcat in SUBCATEGORY_RULES:
        if cat == category and any(k in desc_lower for k in keywords):
            return subcat
    return None


def auto_categorize(description: str, amount: float,
                    chase_category: str | None = None) -> str:
    # Normalize: collapse whitespace, decode HTML entities like &amp; → &
    raw = (description or "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    desc_lower = " ".join(raw.lower().split())  # collapses all runs of whitespace

    # ── Credit-side payment cleanup ───────────────────────────────────────────
    # Catch CC autopay credits on checking side before anything else
    if amount > 0 and any(k in desc_lower for k in
                          ["automatic payment", "payment - thank", "autopay"]):
        return "Transfers"

    # ── Amount-dependent income overrides ─────────────────────────────────────
    # eBay deposits are selling income; eBay purchases fall through to Shopping
    if amount > 0 and any(k in desc_lower for k in ["ebay", "e bay"]):
        return "Other Income"
    # Tax refunds / government deposits / interest
    if amount > 0 and any(k in desc_lower for k in
                          ["irs treas", "franchise tax bd", "ftb refund",
                           "tax refund", "state refund", "wex health",
                           "deposit dividend", "interest paid"]):
        return "Other Income"

    # ── Keyword scan (runs BEFORE Chase category for higher specificity) ───────
    for keywords, category in KEYWORD_RULES:
        if any(k in desc_lower for k in keywords):
            return category

    # ── Chase's own category as a fallback ────────────────────────────────────
    if chase_category:
        mapped = CHASE_CATEGORY_MAP.get(chase_category.lower())
        if mapped:
            return mapped

    return "Uncategorized"


# ── Bulk recategorize ────────────────────────────────────────────────────────

def recategorize_all(conn) -> int:
    """
    Re-apply auto_categorize() to every transaction that has NOT been manually
    categorized by the user. Returns the number of rows updated.

    Rows where manually_categorized = 1 (set when the user edits a category
    in the Transactions tab) are intentionally skipped so hand-picked categories
    survive repeated runs of this function.
    """
    rows = db._rows_as_dicts(conn.execute(
        "SELECT id, description, amount FROM transactions WHERE manually_categorized = 0"
    ))
    updated = 0
    for row in rows:
        desc = row["description"] or ""
        new_cat = auto_categorize(desc, float(row["amount"]))
        new_subcat = auto_subcategorize(desc, new_cat)
        conn.execute(
            "UPDATE transactions SET category = ?, subcategory = ? WHERE id = ?",
            (new_cat, new_subcat, row["id"]),
        )
        updated += 1
    conn.commit()
    return updated


# ── DB insertion helpers ─────────────────────────────────────────────────────

def insert_transaction(conn, account_id: int, date: str, description: str,
                       amount: float, category: str, source_file: str,
                       subcategory: str | None = None) -> bool:
    """Insert a transaction; return True if new, False if duplicate."""
    try:
        conn.execute("""
            INSERT INTO transactions
              (account_id, date, description, amount, category, subcategory, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (account_id, date, description, round(amount, 2), category, subcategory, source_file))
        conn.commit()
        return True
    except Exception:
        return False


def insert_snapshot(conn, account_id: int, snapshot_date: str,
                    balance: float, source_file: str) -> bool:
    """Insert (or update) a balance snapshot; return True if new/updated."""
    try:
        conn.execute("""
            INSERT OR REPLACE INTO account_snapshots
              (account_id, snapshot_date, balance, source_file)
            VALUES (?, ?, ?, ?)
        """, (account_id, snapshot_date, round(balance, 2), source_file))
        conn.commit()
        return True
    except Exception:
        return False


# ── Date helpers ─────────────────────────────────────────────────────────────

def _to_iso(date_str: str) -> str:
    """Convert MM/DD/YYYY or MM/DD/YY to YYYY-MM-DD."""
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str.strip()


def _parse_dollar(s: str) -> float:
    return float(str(s).replace(",", "").replace("$", "").strip())


def _vanguard_filename_to_date(filename: str) -> str:
    """
    'Vanguard - Travis - Roth IRA - YE 2025.pdf' → '2025-12-31'
    'Vanguard - Travis - Roth IRA - Q1 2026.pdf' → '2026-03-31'
    """
    name = Path(filename).stem
    ye_match = re.search(r'YE\s+(\d{4})', name)
    if ye_match:
        return f"{ye_match.group(1)}-12-31"
    q_match = re.search(r'Q(\d)\s+(\d{4})', name)
    if q_match:
        q, year = int(q_match.group(1)), q_match.group(2)
        ends = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
        return f"{year}-{ends[q]}"
    return datetime.today().strftime("%Y-%m-%d")


# ── Chase parsers ────────────────────────────────────────────────────────────

def _detect_chase_format(filepath) -> str:
    """Return 'checking' or 'credit_card' based on header row."""
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, [])
    if header and header[0].strip().lower() == "details":
        return "checking"
    return "credit_card"


def parse_chase_checking(filepath, account_id: int, conn) -> dict:
    """
    Chase checking format:
      Details, Posting Date, Description, Amount, Type, Balance, Check or Slip #
    """
    fname = Path(filepath).name
    inserted = skipped = 0
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                date = _to_iso(row["Posting Date"])
                desc = row["Description"].strip()
                amount = _parse_dollar(row["Amount"])
                txn_type = row.get("Type", "").strip()

                # Skip internal account transfers that are just CC autopay
                # (the credit card side has the real transaction detail)
                if txn_type == "ACH_DEBIT" and "CHASE CREDIT CRD" in desc.upper():
                    category = "Transfers"
                    subcategory = None
                else:
                    category = auto_categorize(desc, amount)
                    subcategory = auto_subcategorize(desc, category)

                ok = insert_transaction(conn, account_id, date, desc, amount, category, fname, subcategory)
                if ok:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                log.warning(f"Chase checking parse error in {fname}: {e}")
    return {"file": fname, "inserted": inserted, "skipped": skipped}


def parse_chase_credit_card(filepath, account_id: int, conn) -> dict:
    """
    Chase CC format:
      Transaction Date, Post Date, Description, Category, Type, Amount, Memo
    """
    fname = Path(filepath).name
    inserted = skipped = 0
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                txn_type = row.get("Type", "").strip()
                # Skip payment rows — they're just payoff transfers from checking
                if txn_type.lower() == "payment":
                    continue

                date = _to_iso(row["Transaction Date"])
                desc = row["Description"].strip()
                amount = _parse_dollar(row["Amount"])
                chase_cat = row.get("Category", "").strip()
                category = auto_categorize(desc, amount, chase_cat)
                subcategory = auto_subcategorize(desc, category)

                ok = insert_transaction(conn, account_id, date, desc, amount, category, fname, subcategory)
                if ok:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                log.warning(f"Chase CC parse error in {fname}: {e}")
    return {"file": fname, "inserted": inserted, "skipped": skipped}


def import_chase_file(filepath, conn) -> dict:
    """Route a Chase file to the right parser based on header + account number."""
    fname = Path(filepath).name
    # Extract 4-digit account suffix from filename  e.g. Chase4949_Activity...
    m = re.search(r'Chase(\d{4})', fname)
    if not m:
        log.warning(f"Cannot determine Chase account number from {fname}")
        return {"file": fname, "inserted": 0, "skipped": 0, "error": "unknown account"}

    suffix = m.group(1)
    account_name = CHASE_ACCT_MAP.get(suffix)
    if not account_name:
        log.warning(f"No account mapping for Chase suffix {suffix}")
        return {"file": fname, "inserted": 0, "skipped": 0, "error": "unmapped account"}

    account_id = get_account_id(conn, account_name)
    if not account_id:
        log.warning(f"Account not found in DB: {account_name}")
        return {"file": fname, "inserted": 0, "skipped": 0, "error": "no DB account"}

    fmt = _detect_chase_format(filepath)
    if fmt == "checking":
        return parse_chase_checking(filepath, account_id, conn)
    else:
        return parse_chase_credit_card(filepath, account_id, conn)


# ── CoastHills parsers ───────────────────────────────────────────────────────

def _read_coasthills_csv(filepath) -> pd.DataFrame:
    """Skip the 3-line header block CoastHills puts before the real CSV data."""
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        lines = f.readlines()
    # Find the header row (contains "Transaction Number" or "Date")
    header_idx = 0
    for i, line in enumerate(lines):
        if "Transaction Number" in line or ("Date" in line and "," in line):
            header_idx = i
            break
    import io
    content = "".join(lines[header_idx:])
    return pd.read_csv(io.StringIO(content), dtype=str)


def parse_coasthills_checking(filepath, account_id: int, conn) -> dict:
    fname = Path(filepath).name
    inserted = skipped = 0
    df = _read_coasthills_csv(filepath)

    for _, row in df.iterrows():
        try:
            desc = str(row.get("Description", "")).strip()
            # Skip informational comment rows
            if desc.upper() in ("COMMENT", "") or "COMMENT" in str(row.get("Transaction Number", "")):
                continue

            date = _to_iso(str(row["Date"]))
            debit  = _parse_dollar(row["Amount Debit"])  if pd.notna(row.get("Amount Debit"))  and str(row.get("Amount Debit", "")).strip() != "" else 0.0
            credit = _parse_dollar(row["Amount Credit"]) if pd.notna(row.get("Amount Credit")) and str(row.get("Amount Credit", "")).strip() != "" else 0.0
            amount = credit - debit  # positive = deposit, negative = withdrawal
            if amount == 0:
                continue

            category = auto_categorize(desc, amount)
            subcategory = auto_subcategorize(desc, category)
            ok = insert_transaction(conn, account_id, date, desc, amount, category, fname, subcategory)
            if ok:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            log.warning(f"CoastHills checking parse error in {fname}: {e}")

    return {"file": fname, "inserted": inserted, "skipped": skipped}


def parse_coasthills_heloc(filepath, account_id: int, conn) -> dict:
    """
    HELOC: record transactions AND balance snapshots.
    Amount Debit = draw (increases balance owed → negative for us)
    Amount Credit = payment (decreases balance owed → positive for us)
    Balance column = running outstanding balance (store as negative snapshot)
    """
    fname = Path(filepath).name
    inserted = skipped = 0
    snap_inserted = snap_skipped = 0
    df = _read_coasthills_csv(filepath)

    for _, row in df.iterrows():
        try:
            desc = str(row.get("Description", "")).strip()
            if desc.upper() in ("COMMENT", "") or "COMMENT" in str(row.get("Transaction Number", "")):
                continue

            date = _to_iso(str(row["Date"]))
            debit  = _parse_dollar(row["Amount Debit"])  if pd.notna(row.get("Amount Debit"))  and str(row.get("Amount Debit", "")).strip() != "" else 0.0
            credit = _parse_dollar(row["Amount Credit"]) if pd.notna(row.get("Amount Credit")) and str(row.get("Amount Credit", "")).strip() != "" else 0.0
            amount = credit - debit

            # Balance snapshot (negate because it's a liability)
            bal_str = str(row.get("Balance", "")).strip()
            if bal_str and bal_str not in ("", "nan"):
                balance = -abs(_parse_dollar(bal_str))
                ok = insert_snapshot(conn, account_id, date, balance, fname)
                if ok:
                    snap_inserted += 1
                else:
                    snap_skipped += 1

            if amount == 0:
                continue
            category = "Transfers" if credit > 0 else "Uncategorized"
            subcategory = None
            ok = insert_transaction(conn, account_id, date, desc, amount, category, fname, subcategory)
            if ok:
                inserted += 1
            else:
                skipped += 1

        except Exception as e:
            log.warning(f"CoastHills HELOC parse error in {fname}: {e}")

    return {
        "file": fname,
        "inserted": inserted, "skipped": skipped,
        "snapshots_inserted": snap_inserted, "snapshots_skipped": snap_skipped,
    }


# ── Vanguard PDF parsers ─────────────────────────────────────────────────────

def _extract_pdf_text(filepath) -> str:
    text = ""
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text += (page.extract_text() or "") + "\n"
    return text


def _find_second_dollar(text_segment: str) -> float | None:
    """
    Vanguard tables show two values: prior period, current period.
    We want the second (current) dollar amount on the relevant line.
    """
    amounts = re.findall(r'\$([\d,]+\.\d{2})', text_segment)
    if len(amounts) >= 2:
        return _parse_dollar(amounts[1])
    if len(amounts) == 1:
        return _parse_dollar(amounts[0])
    return None


def parse_vanguard_ira_pdf(filepath, conn) -> dict:
    """
    Parse a Vanguard IRA or Cash Plus statement.
    Determines account from filename.
    Extracts end-of-period balance snapshot.
    """
    fname = Path(filepath).name
    snapshot_date = _vanguard_filename_to_date(fname)
    text = _extract_pdf_text(filepath)
    results = []

    # Determine which account this file maps to
    fname_lower = fname.lower()
    if "travis" in fname_lower and "roth" in fname_lower:
        account_name = "Vanguard Travis Roth IRA"
        pattern = r'Roth IRA brokerage account\s+(\$[\d,]+\.\d{2}\s+\$[\d,]+\.\d{2})'
    elif "alison" in fname_lower and "roth" in fname_lower:
        account_name = "Vanguard Alison Roth IRA"
        pattern = r'Roth IRA brokerage account\s+(\$[\d,]+\.\d{2}\s+\$[\d,]+\.\d{2})'
    elif "alison" in fname_lower and "trad" in fname_lower:
        account_name = "Vanguard Alison Trad IRA"
        pattern = r'(?:Traditional|IRA) brokerage account\s+(\$[\d,]+\.\d{2}\s+\$[\d,]+\.\d{2})'
    elif "cash plus" in fname_lower:
        account_name = "Vanguard Cash Plus (Joint)"
        pattern = r'Cash Plus account\s+(\$[\d,]+\.\d{2}\s+\$[\d,]+\.\d{2})'
    else:
        log.warning(f"Unknown Vanguard IRA file: {fname}")
        return {"file": fname, "snapshots_inserted": 0, "error": "unknown file"}

    account_id = get_account_id(conn, account_name)
    if not account_id:
        return {"file": fname, "snapshots_inserted": 0, "error": f"no DB account: {account_name}"}

    m = re.search(pattern, text)
    if not m:
        # Fallback: look for "Statement overview $X.XX" (single-account statements)
        m2 = re.search(r'Statement overview\s+\$([\d,]+\.\d{2})', text)
        if m2:
            balance = _parse_dollar(m2.group(1))
        else:
            log.warning(f"Could not extract balance from {fname}")
            return {"file": fname, "snapshots_inserted": 0, "error": "balance not found"}
    else:
        balance = _find_second_dollar(m.group(1)) or 0.0

    ok = insert_snapshot(conn, account_id, snapshot_date, balance, fname)
    return {"file": fname, "snapshots_inserted": 1 if ok else 0, "snapshots_skipped": 0 if ok else 1}


def parse_vanguard_529_pdf(filepath, conn) -> dict:
    """
    The 529 statement covers both Clark and Corey.
    Extract each beneficiary's balance separately.
    """
    fname = Path(filepath).name
    snapshot_date = _vanguard_filename_to_date(fname)
    text = _extract_pdf_text(filepath)
    snap_inserted = snap_skipped = 0

    beneficiaries = [
        ("Clark",  "Vanguard 529 — Clark"),
        ("Corey",  "Vanguard 529 — Corey"),
    ]

    for name, account_name in beneficiaries:
        account_id = get_account_id(conn, account_name)
        if not account_id:
            log.warning(f"No DB account for {account_name}")
            continue

        # Pattern: "Beneficiary-<Name>..." then "529 Plan  $prior  $current"
        pattern = rf'Beneficiary-{name}.*?529 Plan\s+((?:\$[\d,]+\.\d{{2}}\s*){{1,2}})'
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if not m:
            log.warning(f"Could not find balance for {name} in {fname}")
            continue

        balance = _find_second_dollar(m.group(1)) or 0.0
        ok = insert_snapshot(conn, account_id, snapshot_date, balance, fname)
        if ok:
            snap_inserted += 1
        else:
            snap_skipped += 1

    return {"file": fname, "snapshots_inserted": snap_inserted, "snapshots_skipped": snap_skipped}


# ── Fidelity PDF parser ──────────────────────────────────────────────────────

def parse_fidelity_pdf(filepath, conn) -> dict:
    """
    Fidelity NetBenefits statement.
    Extracts: Ending Balance and statement end date.
    """
    fname = Path(filepath).name
    text = _extract_pdf_text(filepath)

    # Extract ending balance
    m_balance = re.search(r'Ending Balance\s+\$([\d,]+\.\d{2})', text)
    if not m_balance:
        log.warning(f"Could not extract balance from {fname}")
        return {"file": fname, "snapshots_inserted": 0, "error": "balance not found"}
    balance = _parse_dollar(m_balance.group(1))

    # Extract statement end date: "Statement Period: MM/DD/YYYY to MM/DD/YYYY"
    m_date = re.search(r'Statement Period:\s+[\d/]+\s+to\s+(\d{2}/\d{2}/\d{4})', text)
    snapshot_date = _to_iso(m_date.group(1)) if m_date else datetime.today().strftime("%Y-%m-%d")

    account_id = get_account_id(conn, "Fidelity 401(k) — Travis")
    if not account_id:
        return {"file": fname, "snapshots_inserted": 0, "error": "no DB account"}

    ok = insert_snapshot(conn, account_id, snapshot_date, balance, fname)
    return {"file": fname, "snapshots_inserted": 1 if ok else 0, "snapshots_skipped": 0 if ok else 1}


# ── Rocket Mortgage PDF parser ───────────────────────────────────────────────

def parse_rocket_mortgage_pdf(filepath, conn) -> dict:
    """
    Rocket Mortgage monthly statement.
    Handles two statement templates:
      - Older (Mr. Cooper style): "STATEMENT DATE ... \n MM/DD/YYYY" + "PRINCIPAL BALANCE  X%\n$NNN"
      - Newer template: "Statement date\nMM/DD/YYYY" + "Interest bearing principal balance: $NNN"
    Balance is stored as negative (liability).
    """
    fname = Path(filepath).name
    text = _extract_pdf_text(filepath)

    # ── Date extraction (two formats) ────────────────────────────────────
    # Older: "STATEMENT DATE  PAYMENT DUE DATE\n05/12/2025 ..."
    m_date = re.search(r'STATEMENT DATE\s+\w[^0-9]*(\d{2}/\d{2}/\d{4})', text)
    if not m_date:
        # Newer: "Statement date\n04/02/2026"
        m_date = re.search(r'Statement date\s+(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
    if not m_date:
        m_date = re.search(r'(\d{2}/\d{2}/\d{4})', text)
    snapshot_date = _to_iso(m_date.group(1)) if m_date else datetime.today().strftime("%Y-%m-%d")

    # ── Balance extraction (two formats) ─────────────────────────────────
    balance = None

    # Newer template: "Interest bearing principal balance: $616,397.24"
    m_new = re.search(r'Interest bearing principal balance:\s+\$([\d,]+\.\d{2})', text)
    if m_new:
        balance = _parse_dollar(m_new.group(1))

    if balance is None:
        # Older template: "PRINCIPAL BALANCE  X.XXX%\n$NNN,NNN.NN"
        m_bal = re.search(r'PRINCIPAL BALANCE\s+[\d.]+%\s*\n\s*\$([\d,]+\.\d{2})', text)
        if m_bal:
            balance = _parse_dollar(m_bal.group(1))

    if balance is None:
        # Last-resort fallback: largest dollar amount anywhere near "principal"
        idx = text.lower().find("principal balance")
        if idx >= 0:
            segment = text[idx:idx+300]
            parsed = [_parse_dollar(a) for a in re.findall(r'\$([\d,]+\.\d{2})', segment)]
            parsed = [p for p in parsed if p > 50_000]
            balance = max(parsed) if parsed else None

    if balance is None:
        log.warning(f"Could not extract mortgage balance from {fname}")
        return {"file": fname, "snapshots_inserted": 0, "error": "balance not found"}

    account_id = get_account_id(conn, "Rocket Mortgage")
    if not account_id:
        return {"file": fname, "snapshots_inserted": 0, "error": "no DB account"}

    ok = insert_snapshot(conn, account_id, snapshot_date, -abs(balance), fname)
    return {"file": fname, "snapshots_inserted": 1 if ok else 0, "snapshots_skipped": 0 if ok else 1}


# ── Main orchestrator ────────────────────────────────────────────────────────

def import_all(import_dir: str | Path, conn) -> list[dict]:
    """
    Scan all subdirectories of import_dir and process every recognised file.
    Returns a list of result dicts (one per file).
    """
    import_dir = Path(import_dir)
    results = []

    for folder in sorted(import_dir.iterdir()):
        if not folder.is_dir():
            continue
        folder_name = folder.name.lower()

        for filepath in sorted(folder.iterdir()):
            if not filepath.is_file():
                continue
            suffix = filepath.suffix.lower()
            fname  = filepath.name

            try:
                if folder_name == "chase" and suffix == ".csv":
                    results.append(import_chase_file(filepath, conn))

                elif folder_name == "coasthills" and suffix == ".csv":
                    if "heloc" in fname.lower():
                        account_id = get_account_id(conn, "CoastHills HELOC")
                        results.append(parse_coasthills_heloc(filepath, account_id, conn))
                    else:
                        account_id = get_account_id(conn, "CoastHills Checking")
                        results.append(parse_coasthills_checking(filepath, account_id, conn))

                elif folder_name == "vanguard" and suffix == ".pdf":
                    if "529" in fname:
                        results.append(parse_vanguard_529_pdf(filepath, conn))
                    else:
                        results.append(parse_vanguard_ira_pdf(filepath, conn))

                elif folder_name == "fidelity" and suffix == ".pdf":
                    results.append(parse_fidelity_pdf(filepath, conn))

                elif folder_name == "rocket mortgage" and suffix == ".pdf":
                    results.append(parse_rocket_mortgage_pdf(filepath, conn))

                elif folder_name == "amex" and suffix == ".csv":
                    # Stub — Alison's files pending
                    results.append({"file": fname, "inserted": 0, "skipped": 0,
                                    "note": "Amex parser pending — files not yet available"})
                else:
                    log.info(f"Skipping unrecognised file: {filepath}")

            except Exception as e:
                log.error(f"Unexpected error processing {filepath}: {e}")
                results.append({"file": fname, "error": str(e)})

    return results
