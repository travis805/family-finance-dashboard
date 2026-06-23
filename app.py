"""
app.py — Family Finance Dashboard
6-tab Streamlit app: Overview · Monthly Activity · Transactions · Budget · Net Worth · Insights
"""
import os
from pathlib import Path
from datetime import datetime

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

import db
import importer
import insights as ins

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Family Finance",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Minimal custom CSS ───────────────────────────────────────────────────────

st.markdown("""
<style>
  .metric-card {
    background: #f8f9fa;
    border-radius: 8px;
    padding: 1rem 1.25rem;
    text-align: center;
  }
  .metric-card .label { color: #6c757d; font-size: 0.85rem; margin-bottom: 0.25rem; }
  .metric-card .value { font-size: 1.6rem; font-weight: 700; }
  .metric-card .value.positive { color: #198754; }
  .metric-card .value.negative { color: #dc3545; }
  .metric-card .value.neutral  { color: #0d6efd; }
  .stDataFrame thead tr th { background-color: #f1f3f5 !important; }
</style>
""", unsafe_allow_html=True)

IMPORT_DIR = Path(__file__).parent / "Import Files"

# ── Access gate ──────────────────────────────────────────────────────────────

def check_password() -> bool:
    """Gate the app behind a passphrase stored as a server-side secret.

    APP_PASSWORD comes from Streamlit secrets or the environment. If it is not
    set (local/offline dev), the app is open. On the public Cloud deployment the
    secret is set, so nothing renders and no query runs until it matches.
    """
    import hmac

    expected = None
    try:
        expected = st.secrets.get("APP_PASSWORD")
    except Exception:
        pass
    expected = expected or os.environ.get("APP_PASSWORD")

    if not expected:
        return True  # no passphrase configured -> open (dev convenience)
    if st.session_state.get("authed"):
        return True

    st.title("💰 Family Finance")
    with st.form("login"):
        pw = st.text_input("Passphrase", type="password")
        if st.form_submit_button("Enter"):
            if hmac.compare_digest(pw, expected):
                st.session_state["authed"] = True
                st.rerun()
            else:
                st.error("Incorrect passphrase")
    return False


if not check_password():
    st.stop()


# ── DB init ──────────────────────────────────────────────────────────────────

@st.cache_resource
def init():
    db.init_db()

init()


def get_conn():
    return db.get_conn()


# ── Helper: currency format ──────────────────────────────────────────────────

def fmt(value: float, show_sign: bool = False) -> str:
    if value is None:
        return "—"
    prefix = "+" if show_sign and value > 0 else ""
    return f"{prefix}${value:,.2f}"


# ── Sidebar ──────────────────────────────────────────────────────────────────

def render_sidebar():
    st.sidebar.title("💰 Family Finance")
    st.sidebar.caption("Private · Local · Quarterly")
    st.sidebar.divider()

    st.sidebar.subheader("Import Data")
    if st.sidebar.button("▶ Run Import", use_container_width=True, type="primary"):
        with st.spinner("Importing files…"):
            conn = get_conn()
            results = importer.import_all(IMPORT_DIR, conn)
            db.calculate_and_save_net_worth(conn)
            conn.close()
        # Show results
        total_new  = sum(r.get("inserted", 0) + r.get("snapshots_inserted", 0) for r in results)
        total_skip = sum(r.get("skipped",  0) + r.get("snapshots_skipped", 0) for r in results)
        st.sidebar.success(f"Done — {total_new} new records, {total_skip} duplicates skipped")
        with st.sidebar.expander("Import details"):
            for r in results:
                fname = r.get("file", "?")
                new   = r.get("inserted", 0) + r.get("snapshots_inserted", 0)
                skip  = r.get("skipped",  0) + r.get("snapshots_skipped",  0)
                err   = r.get("error", "")
                if err:
                    st.write(f"⚠️ `{fname}` — {err}")
                else:
                    st.write(f"✅ `{fname}` — {new} new, {skip} skipped")
        st.cache_data.clear()
        st.rerun()

    st.sidebar.subheader("Categorization")
    if st.sidebar.button("🔁 Recategorize All", use_container_width=True):
        with st.spinner("Re-applying categorization rules to all transactions…"):
            conn = get_conn()
            updated = importer.recategorize_all(conn)
            conn.close()
        st.sidebar.success(f"Done — {updated:,} transactions updated")
        st.cache_data.clear()
        st.rerun()

    st.sidebar.divider()

    # DB stats
    conn = get_conn()
    try:
        txn_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        snap_count = conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]
        last_import = conn.execute(
            "SELECT MAX(imported_at) FROM transactions"
        ).fetchone()[0]
    finally:
        conn.close()

    st.sidebar.caption(f"**{txn_count:,}** transactions · **{snap_count}** snapshots")
    if last_import:
        st.sidebar.caption(f"Last import: {last_import[:10]}")


# ── Tab 1: Overview ──────────────────────────────────────────────────────────

def render_overview():
    st.header("Overview")

    conn = get_conn()
    current_quarter = db.get_current_quarter()
    start_date, end_date = db.get_quarter_dates(current_quarter)

    # ── Net Worth snapshot ────────────────────────────────────────────────
    st.subheader(f"Net Worth — {datetime.today().strftime('%B %d, %Y')}")
    nw = db.get_net_worth_summary(conn)

    c1, c2, c3 = st.columns(3)
    c1.markdown(f"""<div class="metric-card">
        <div class="label">Total Assets</div>
        <div class="value positive">{fmt(nw['assets'])}</div>
    </div>""", unsafe_allow_html=True)
    c2.markdown(f"""<div class="metric-card">
        <div class="label">Total Liabilities</div>
        <div class="value negative">{fmt(nw['liabilities'])}</div>
    </div>""", unsafe_allow_html=True)
    c3.markdown(f"""<div class="metric-card">
        <div class="label">Net Worth</div>
        <div class="value {'positive' if nw['net_worth'] >= 0 else 'negative'}">{fmt(nw['net_worth'])}</div>
    </div>""", unsafe_allow_html=True)

    st.write("")

    # ── Quarterly Cash Flow ───────────────────────────────────────────────
    st.subheader(f"Cash Flow — {current_quarter}")
    income   = db.get_quarterly_income(conn, start_date, end_date)
    spending_df = db.get_quarterly_spending(conn, start_date, end_date)
    total_spend = spending_df["actual"].sum() if not spending_df.empty else 0.0
    net_flow = income - total_spend

    cf1, cf2, cf3 = st.columns(3)
    cf1.markdown(f"""<div class="metric-card">
        <div class="label">Income</div>
        <div class="value positive">{fmt(income)}</div>
    </div>""", unsafe_allow_html=True)
    cf2.markdown(f"""<div class="metric-card">
        <div class="label">Spending</div>
        <div class="value negative">{fmt(total_spend)}</div>
    </div>""", unsafe_allow_html=True)
    cf3.markdown(f"""<div class="metric-card">
        <div class="label">Net</div>
        <div class="value {'positive' if net_flow >= 0 else 'negative'}">{fmt(net_flow, show_sign=True)}</div>
    </div>""", unsafe_allow_html=True)

    st.write("")

    # ── Budget Health ─────────────────────────────────────────────────────
    st.subheader(f"Budget Health — {current_quarter}")
    budget_df = db.get_budget_categories(conn)
    expense_budget = budget_df[budget_df["budget_type"] == "expense"]

    if not spending_df.empty and not expense_budget.empty:
        merged = expense_budget.merge(spending_df, on="category", how="left")
        merged["actual"] = merged["actual"].fillna(0.0)

        def traffic_light(row):
            if not row["quarterly_target"] or row["quarterly_target"] == 0:
                return "⬜"
            pct = row["actual"] / row["quarterly_target"]
            if pct <= 0.8:
                return "🟢"
            elif pct <= 1.0:
                return "🟡"
            else:
                return "🔴"

        merged["status"] = merged.apply(traffic_light, axis=1)
        merged["pct_used"] = merged.apply(
            lambda r: f"{r['actual'] / r['quarterly_target'] * 100:.0f}%"
            if r["quarterly_target"] and r["quarterly_target"] > 0 else "—",
            axis=1,
        )
        merged["Target"]  = merged["quarterly_target"].apply(lambda v: fmt(v) if v else "Not set")
        merged["Actual"]  = merged["actual"].apply(fmt)

        display = merged[merged["actual"] > 0][["status", "category", "Target", "Actual", "pct_used"]]
        display.columns = ["", "Category", "Target", "Actual", "% Used"]
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        st.info("No spending data for the current quarter yet. Run an import to populate.")

    st.write("")

    # ── Data Freshness ────────────────────────────────────────────────────
    st.subheader("Data Freshness")
    freshness = db.get_data_freshness(conn)
    if not freshness.empty:
        today = datetime.today().date()

        def staleness_flag(row):
            last = row.get("last_transaction") or row.get("last_snapshot")
            if not last:
                return "⬜ No data"
            try:
                last_date = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
                days = (today - last_date).days
                if days <= 30:
                    return f"🟢 {days}d ago"
                elif days <= 90:
                    return f"🟡 {days}d ago"
                else:
                    return f"🔴 {days}d ago"
            except Exception:
                return "⬜ Unknown"

        freshness["Status"] = freshness.apply(staleness_flag, axis=1)
        freshness["Owner"]  = freshness["owner"].str.title()
        display = freshness[["Status", "account_name", "institution", "Owner",
                              "last_transaction", "last_snapshot", "transaction_count"]]
        display.columns = ["Status", "Account", "Institution", "Owner",
                           "Last Transaction", "Last Snapshot", "# Transactions"]
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        st.info("No accounts found — run an import to get started.")

    conn.close()


# ── Tab 2: Transactions ──────────────────────────────────────────────────────

@st.fragment
def render_transactions():
    st.header("Transactions")

    conn = get_conn()
    accounts_df = db.get_accounts(conn)
    budget_df   = db.get_budget_categories(conn)
    all_categories = sorted(budget_df["category"].tolist()) if not budget_df.empty else []

    # All subcategories (flat list for the inline dropdown)
    subcats_df = pd.read_sql(
        "SELECT DISTINCT name FROM subcategories ORDER BY name", conn
    )
    all_subcategories = subcats_df["name"].tolist() if not subcats_df.empty else []

    # ── Filters ───────────────────────────────────────────────────────────
    with st.expander("🔍 Filters", expanded=True):
        fc1, fc2, fc3 = st.columns(3)

        with fc1:
            date_range = st.date_input(
                "Date range",
                value=(datetime(2025, 1, 1).date(), datetime.today().date()),
            )
        with fc2:
            owner_filter = st.multiselect(
                "Owner", ["travis", "alison", "joint"],
                default=["travis", "alison", "joint"],
            )
        with fc3:
            search = st.text_input("Search description", placeholder="e.g. Amazon")

        fc4, fc5 = st.columns(2)
        with fc4:
            account_options = accounts_df["name"].tolist() if not accounts_df.empty else []
            selected_accounts = st.multiselect("Accounts", account_options)
        with fc5:
            selected_categories = st.multiselect("Categories", all_categories)

    start_str = date_range[0].strftime("%Y-%m-%d") if len(date_range) > 0 else "2025-01-01"
    end_str   = date_range[1].strftime("%Y-%m-%d") if len(date_range) > 1 else datetime.today().strftime("%Y-%m-%d")

    # Get matching account IDs
    if selected_accounts:
        acct_ids = accounts_df[accounts_df["name"].isin(selected_accounts)]["id"].tolist()
    elif owner_filter and len(owner_filter) < 3:
        acct_ids = accounts_df[accounts_df["owner"].isin(owner_filter)]["id"].tolist()
    else:
        acct_ids = None

    txns = db.get_transactions(
        conn,
        account_ids=acct_ids,
        start_date=start_str,
        end_date=end_str,
        categories=selected_categories if selected_categories else None,
        search=search if search else None,
    )

    st.caption(f"**{len(txns):,}** transactions matched")

    if txns.empty:
        st.info("No transactions match your filters.")
        conn.close()
        return

    # ── Category editor ───────────────────────────────────────────────────
    st.caption(
        "Click a row's Category or Subcategory dropdown to reassign it. "
        "🔒 = manually pinned (protected from Recategorize All). "
        "Use the **🏷️ Categorize** tab for a filtered subcategory picker."
    )

    # Pull manually_categorized flag alongside the other columns
    display = txns[["id", "date", "account_name", "description",
                     "amount", "category", "subcategory", "owner"]].copy()

    id_list = txns["id"].tolist()
    placeholders = ",".join("?" * len(id_list))
    mc_rows = db._rows_as_dicts(conn.execute(
        f"SELECT id, manually_categorized FROM transactions WHERE id IN ({placeholders})",
        id_list,
    ))
    mc_map = {r["id"]: r["manually_categorized"] for r in mc_rows}
    display["pinned"] = display["id"].map(mc_map).fillna(0).astype(int)

    display["amount_fmt"] = display["amount"].apply(
        lambda v: f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"
    )
    display["owner"] = display["owner"].str.title()
    display["🔒"] = display["pinned"].apply(lambda v: "🔒" if v else "")

    show_cols = ["date", "account_name", "description", "amount_fmt",
                 "category", "subcategory", "owner", "🔒"]
    col_labels = {
        "date": "Date", "account_name": "Account", "description": "Description",
        "amount_fmt": "Amount", "category": "Category",
        "subcategory": "Subcategory", "owner": "Owner",
    }
    styled = display[show_cols].rename(columns=col_labels)

    # Streamlit data editor — both Category and Subcategory editable inline
    edited = st.data_editor(
        styled,
        column_config={
            "Category": st.column_config.SelectboxColumn(
                "Category",
                options=all_categories,
                required=False,
            ),
            "Subcategory": st.column_config.SelectboxColumn(
                "Subcategory",
                options=all_subcategories,
                required=False,
            ),
            "🔒": st.column_config.TextColumn("🔒", width="small", disabled=True),
        },
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        key="txn_editor",
    )

    # Persist category and/or subcategory changes — sets manually_categorized = 1
    if edited is not None:
        cat_changed    = edited["Category"].fillna("") != styled["Category"].fillna("")
        subcat_changed = edited["Subcategory"].fillna("") != styled["Subcategory"].fillna("")
        any_changed    = cat_changed | subcat_changed
        if any_changed.any():
            for i, row in edited[any_changed].iterrows():
                txn_id = int(display.iloc[i]["id"])
                new_cat = row["Category"]
                if not new_cat:
                    continue
                if cat_changed.iloc[i]:
                    # Category changed — auto-derive subcategory from rules
                    desc = display.iloc[i]["description"]
                    new_subcat = importer.auto_subcategorize(desc, new_cat)
                else:
                    # Only subcategory changed — use the user's explicit pick
                    new_subcat = row["Subcategory"] or None
                db.update_transaction_category(conn, txn_id, new_cat, new_subcat)
            count = int(any_changed.sum())
            st.success(f"Updated {count} transaction(s). These rows are now pinned 🔒.")
            st.rerun()  # fragment-scoped rerun — page position is preserved

    # ── Unpin / release manual overrides ─────────────────────────────────
    pinned_count = int(display["pinned"].sum())
    if pinned_count > 0:
        with st.expander(f"🔓 Release pinned categories ({pinned_count} in current view)"):
            st.caption(
                "Releasing a pin lets 'Recategorize All' overwrite it with the auto-rule next time. "
                "Useful if you've improved a keyword rule and want it to apply retroactively."
            )
            if st.button("Release all pins in current view", type="secondary"):
                pinned_ids = display[display["pinned"] == 1]["id"].tolist()
                if pinned_ids:
                    placeholders = ",".join("?" * len(pinned_ids))
                    conn.execute(
                        f"UPDATE transactions SET manually_categorized = 0 WHERE id IN ({placeholders})",
                        pinned_ids,
                    )
                    conn.commit()
                    st.success(f"Released {len(pinned_ids)} pin(s).")
                    st.rerun()

    conn.close()


# ── Tab 3: Budget ────────────────────────────────────────────────────────────

def render_budget():
    st.header("Budget")

    conn = get_conn()
    quarters = db.get_quarter_list()
    selected_quarter = st.selectbox("Quarter", quarters, index=0)
    start_date, end_date = db.get_quarter_dates(selected_quarter)

    budget_df  = db.get_budget_categories(conn)
    spending_df = db.get_quarterly_spending(conn, start_date, end_date)
    expense_budget = budget_df[budget_df["budget_type"] == "expense"].copy()

    if not expense_budget.empty:
        merged = expense_budget.merge(spending_df, on="category", how="left")
        merged["actual"] = merged["actual"].fillna(0.0)
        merged["variance"] = merged.apply(
            lambda r: (r["quarterly_target"] - r["actual"]) if r["quarterly_target"] else None,
            axis=1,
        )
        merged["pct"] = merged.apply(
            lambda r: (r["actual"] / r["quarterly_target"] * 100)
            if r["quarterly_target"] and r["quarterly_target"] > 0 else None,
            axis=1,
        )

        # ── Budget table ──────────────────────────────────────────────────
        st.subheader(f"Spending vs Budget — {selected_quarter}")

        def color_variance(val):
            if val is None or pd.isna(val):
                return ""
            return "color: #198754" if val >= 0 else "color: #dc3545"

        def color_pct(val):
            if val is None or pd.isna(val):
                return ""
            if val <= 80:
                return "color: #198754"
            elif val <= 100:
                return "color: #fd7e14"
            else:
                return "color: #dc3545; font-weight: bold"

        display = merged[["category", "quarterly_target", "actual", "variance", "pct"]].copy()
        display.columns = ["Category", "Target ($)", "Actual ($)", "Variance ($)", "% Used"]

        styled_table = (
            display.style
            .format({
                "Target ($)":   lambda v: fmt(v) if v else "—",
                "Actual ($)":   fmt,
                "Variance ($)": lambda v: fmt(v, show_sign=True) if v is not None else "—",
                "% Used":       lambda v: f"{v:.0f}%" if v is not None else "—",
            })
            .map(color_variance, subset=["Variance ($)"])
            .map(color_pct, subset=["% Used"])
        )
        st.dataframe(styled_table, use_container_width=True, hide_index=True)

        # ── Edit targets ──────────────────────────────────────────────────
        with st.expander("✏️ Edit budget targets"):
            st.caption("Set quarterly spending targets for each category.")
            for _, row in expense_budget.iterrows():
                current = row["quarterly_target"] or 0.0
                new_val = st.number_input(
                    row["category"],
                    min_value=0.0,
                    value=float(current),
                    step=100.0,
                    format="%.2f",
                    key=f"budget_{row['category']}",
                )
                if new_val != current:
                    db.update_budget_target(conn, row["category"], new_val if new_val > 0 else None)

        # ── Add / remove categories ───────────────────────────────────────
        with st.expander("➕ Manage categories"):
            col_a, col_b = st.columns(2)
            with col_a:
                st.caption("**Add category**")
                new_cat = st.text_input("Category name", key="mgr_new_cat")
                new_type = st.radio("Type", ["expense", "income"], horizontal=True, key="mgr_new_type")
                if st.button("Add category", key="mgr_add_cat") and new_cat:
                    db.add_budget_category(conn, new_cat.strip(), new_type)
                    st.success(f"Added '{new_cat}'")
                    st.rerun()
            with col_b:
                st.caption("**Remove category**")
                del_cat = st.selectbox("Category to remove", [""] + expense_budget["category"].tolist(), key="mgr_del_cat")
                if st.button("Remove category", type="secondary", key="mgr_rm_cat") and del_cat:
                    db.delete_budget_category(conn, del_cat)
                    st.success(f"Removed '{del_cat}'")
                    st.rerun()

        # ── Add / remove subcategories ────────────────────────────────────
        with st.expander("🗂️ Manage subcategories"):
            subcats_by_cat = db.get_subcategories_by_category(conn)
            all_categories = sorted(budget_df["category"].tolist())

            # Current subcategory table
            if subcats_by_cat:
                rows = [
                    {"Category": cat, "Subcategory": sub}
                    for cat, subs in sorted(subcats_by_cat.items())
                    for sub in subs
                ]
                st.dataframe(
                    pd.DataFrame(rows),
                    use_container_width=True,
                    hide_index=True,
                    height=min(36 * len(rows) + 38, 320),
                )
            else:
                st.info("No subcategories defined yet.")

            st.divider()
            col_sa, col_sb = st.columns(2)

            with col_sa:
                st.caption("**Add subcategory**")
                add_parent = st.selectbox(
                    "Parent category",
                    all_categories,
                    key="sub_add_parent",
                )
                add_name = st.text_input("Subcategory name", key="sub_add_name")
                if st.button("Add subcategory", key="sub_add_btn") and add_name.strip():
                    ok = db.add_subcategory(conn, add_parent, add_name)
                    if ok:
                        st.success(f"Added '{add_name.strip()}' under {add_parent}.")
                        st.rerun()
                    else:
                        st.warning(f"'{add_name.strip()}' already exists under {add_parent}.")

            with col_sb:
                st.caption("**Remove subcategory**")
                del_parent = st.selectbox(
                    "Parent category",
                    [""] + sorted(subcats_by_cat.keys()),
                    key="sub_del_parent",
                )
                if del_parent:
                    del_sub = st.selectbox(
                        "Subcategory to remove",
                        [""] + subcats_by_cat.get(del_parent, []),
                        key="sub_del_name",
                    )
                    if st.button("Remove subcategory", type="secondary", key="sub_del_btn") and del_sub:
                        db.delete_subcategory(conn, del_parent, del_sub)
                        st.success(f"Removed '{del_sub}' from {del_parent}.")
                        st.rerun()
                else:
                    st.info("Select a parent category first.")
    else:
        st.info("No budget categories found.")

    conn.close()


# ── Tab 4: Net Worth ─────────────────────────────────────────────────────────

def render_net_worth():
    st.header("Net Worth")

    conn = get_conn()

    # ── Home value input ──────────────────────────────────────────────────
    with st.expander("🏠 Primary Residence Value", expanded=False):
        current_home = db.get_manual_asset_value(conn, "Primary Residence")
        st.caption("Enter your estimated home value. This is saved as a snapshot and included in net worth.")
        col_val, col_btn = st.columns([3, 1])
        with col_val:
            home_input = st.number_input(
                "Estimated value ($)",
                min_value=0.0,
                value=float(current_home) if current_home else 0.0,
                step=1000.0,
                format="%.2f",
                label_visibility="collapsed",
            )
        with col_btn:
            if st.button("Save", use_container_width=True):
                db.upsert_manual_asset_value(conn, "Primary Residence", home_input)
                db.calculate_and_save_net_worth(conn)
                st.success(f"Saved {fmt(home_input)}")
                st.cache_data.clear()
                st.rerun()
        if current_home:
            st.caption(f"Current saved value: **{fmt(current_home)}**")

    snaps = db.get_latest_snapshots(conn)
    nw_history = db.get_net_worth_history(conn)
    nw_summary = db.get_net_worth_summary(conn)

    # ── Headline metrics ──────────────────────────────────────────────────
    m1, m2, m3 = st.columns(3)
    m1.markdown(f"""<div class="metric-card">
        <div class="label">Total Assets</div>
        <div class="value positive">{fmt(nw_summary['assets'])}</div>
    </div>""", unsafe_allow_html=True)
    m2.markdown(f"""<div class="metric-card">
        <div class="label">Total Liabilities</div>
        <div class="value negative">{fmt(nw_summary['liabilities'])}</div>
    </div>""", unsafe_allow_html=True)
    m3.markdown(f"""<div class="metric-card">
        <div class="label">Net Worth</div>
        <div class="value {'positive' if nw_summary['net_worth'] >= 0 else 'negative'}">{fmt(nw_summary['net_worth'])}</div>
    </div>""", unsafe_allow_html=True)

    st.write("")

    # ── Asset breakdown ───────────────────────────────────────────────────
    if not snaps.empty:
        st.subheader("Current Breakdown")

        # Bucket mapping
        def bucket(row):
            at = row["account_type"]
            if at == "real_estate":
                return "Real Estate"
            elif at in ("checking", "savings"):
                return "Cash & Savings"
            elif at in ("ira", "401k"):
                return "Retirement"
            elif at == "529":
                return "Education (529)"
            elif at in ("heloc", "mortgage"):
                return "Debt"
            else:
                return "Other"

        snaps["bucket"] = snaps.apply(bucket, axis=1)

        col_left, col_right = st.columns([1, 1])

        with col_left:
            # Account-level table
            display = snaps[["account_name", "institution", "owner", "snapshot_date", "balance"]].copy()
            display["balance_fmt"] = display["balance"].apply(fmt)
            display["owner"] = display["owner"].str.title()
            display["snapshot_date"] = display["snapshot_date"].str[:10]
            display = display.rename(columns={
                "account_name": "Account", "institution": "Institution",
                "owner": "Owner", "snapshot_date": "As of", "balance_fmt": "Balance",
            })
            st.dataframe(display[["Account", "Institution", "Owner", "As of", "Balance"]],
                         use_container_width=True, hide_index=True)

        with col_right:
            # Donut by bucket
            bucket_totals = (
                snaps.groupby("bucket")["balance"]
                .sum()
                .reset_index()
            )
            # Separate assets and liabilities for clarity
            assets_df = bucket_totals[bucket_totals["balance"] > 0].copy()
            if not assets_df.empty:
                fig = px.pie(
                    assets_df,
                    names="bucket",
                    values="balance",
                    hole=0.4,
                    color_discrete_sequence=px.colors.qualitative.Pastel,
                    title="Asset Mix (latest snapshots)",
                )
                fig.update_layout(margin=dict(t=40, b=0, l=0, r=0), height=320)
                st.plotly_chart(fig, use_container_width=True)

        st.write("")

        # ── Net worth trend ───────────────────────────────────────────────
        st.subheader("Net Worth Over Time")
        if not nw_history.empty and len(nw_history) > 1:
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(
                x=nw_history["snapshot_date"],
                y=nw_history["net_worth"],
                mode="lines+markers",
                name="Net Worth",
                line=dict(color="#0d6efd", width=2),
                marker=dict(size=6),
                fill="tozeroy",
                fillcolor="rgba(13,110,253,0.08)",
            ))
            fig2.update_layout(
                xaxis_title=None,
                yaxis_title="Net Worth ($)",
                yaxis_tickformat="$,.0f",
                margin=dict(t=10, b=10, l=10, r=10),
                height=320,
            )
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("Net worth trend will appear as you import data across multiple quarters. Run an import to record today's snapshot.")
    else:
        st.info("No account snapshots yet. Run an import to populate the net worth view.")

    conn.close()


# ── Tab 5: Insights ──────────────────────────────────────────────────────────

def render_insights():
    st.header("Insights")
    st.caption("Quarterly financial narrative — analyzed by Claude in Cowork (no API key needed).")

    conn = get_conn()
    quarters = db.get_quarter_list()
    selected_quarter = st.selectbox("Quarter", quarters, index=0, key="insights_q")

    # ── Step 1: Build data summary ────────────────────────────────────────
    col_btn, col_help = st.columns([1, 2])
    with col_btn:
        build_clicked = st.button("📊 Build data summary", type="primary", use_container_width=True)
    with col_help:
        with st.expander("How this works"):
            st.markdown(
                "1. Click **Build data summary** to pull this quarter's numbers\n"
                "2. Copy the generated block into a Cowork chat\n"
                "3. Ask Claude: *\"Generate a quarterly financial insights report based on this data\"*\n"
                "4. Paste Claude's response into the **Save to dashboard** section below"
            )

    if build_clicked:
        with st.spinner(f"Gathering {selected_quarter} data…"):
            data_text = ins.build_insights_data(conn, selected_quarter)
            st.session_state["insights_data"]    = data_text
            st.session_state["insights_quarter"] = selected_quarter

    if "insights_data" in st.session_state:
        q_label   = st.session_state.get("insights_quarter", selected_quarter)
        data_text = st.session_state["insights_data"]
        st.subheader(f"Data Summary — {q_label}")
        st.caption("Copy everything below into Cowork and ask Claude to generate the quarterly insights report.")
        st.code(data_text, language=None)

        st.divider()

    # ── Step 2: Paste insights back & save ────────────────────────────────
    with st.expander("💾 Save insights to dashboard"):
        st.caption(
            "After Claude generates the insights in Cowork, paste the text here "
            "to save it to the dashboard log."
        )
        paste_q = st.selectbox(
            "Quarter to save for", quarters, index=0, key="save_insights_q"
        )
        pasted = st.text_area("Paste insights here…", height=300, key="insights_paste")
        if st.button("Save to dashboard", type="secondary") and pasted.strip():
            ins.save_insight(conn, paste_q, "(Cowork-generated)", pasted.strip())
            st.success(f"Insights saved for {paste_q}!")
            if "insights_data" in st.session_state:
                del st.session_state["insights_data"]
            st.rerun()

    # ── Display past insights ─────────────────────────────────────────────
    past = ins.get_insights_log(conn)
    if not past.empty:
        st.divider()
        st.subheader("Saved Insights")
        for _, row in past.iterrows():
            with st.expander(f"📊 {row['quarter']} — generated {str(row['generated_at'])[:16]}"):
                st.markdown(row["insight_text"])
    elif "insights_data" not in st.session_state:
        st.info(
            "No insights saved yet. Build a data summary above and analyze it in Cowork, "
            "then paste the result back to save it here."
        )

    conn.close()


# ── Tab: Categorize ─────────────────────────────────────────────────────────

@st.fragment
def render_categorize():
    st.header("Categorize")

    conn = get_conn()
    budget_df      = db.get_budget_categories(conn)
    all_categories = sorted(budget_df["category"].tolist()) if not budget_df.empty else []

    # Subcategories grouped by category for dynamic filtering
    subcats_df = pd.read_sql(
        "SELECT category, name FROM subcategories ORDER BY sort_order, name", conn
    )
    subcats_by_category: dict[str, list[str]] = {}
    for _, row in subcats_df.iterrows():
        subcats_by_category.setdefault(row["category"], []).append(row["name"])

    # ── Mode / filter ─────────────────────────────────────────────────────
    mode = st.radio(
        "Show",
        ["🔍 Uncategorized only", "📂 By category", "📋 All transactions"],
        horizontal=True,
        key="cat_mode",
    )

    cat_filter = None
    if mode == "📂 By category":
        cat_filter = st.multiselect(
            "Category", all_categories, key="cat_cat_filter",
            placeholder="Pick one or more categories…"
        )

    # ── Load transactions ─────────────────────────────────────────────────
    if mode == "🔍 Uncategorized only":
        txns = pd.read_sql("""
            SELECT t.id, t.date, t.description, t.amount,
                   COALESCE(t.category, 'Uncategorized') AS category,
                   t.subcategory, t.manually_categorized,
                   a.name AS account_name, a.owner
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            WHERE t.category IS NULL OR t.category = 'Uncategorized'
            ORDER BY t.date DESC, t.id DESC
        """, conn)
    elif mode == "📂 By category" and cat_filter:
        placeholders = ",".join("?" * len(cat_filter))
        txns = pd.read_sql(f"""
            SELECT t.id, t.date, t.description, t.amount,
                   COALESCE(t.category, 'Uncategorized') AS category,
                   t.subcategory, t.manually_categorized,
                   a.name AS account_name, a.owner
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            WHERE t.category IN ({placeholders})
            ORDER BY t.date DESC, t.id DESC
        """, conn, params=cat_filter)
    elif mode == "📋 All transactions":
        txns = pd.read_sql("""
            SELECT t.id, t.date, t.description, t.amount,
                   COALESCE(t.category, 'Uncategorized') AS category,
                   t.subcategory, t.manually_categorized,
                   a.name AS account_name, a.owner
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            ORDER BY t.date DESC, t.id DESC
            LIMIT 500
        """, conn)
    else:
        txns = pd.DataFrame()

    if txns.empty:
        if mode == "📂 By category" and not cat_filter:
            st.info("Select one or more categories above.")
        else:
            st.success("✅ Nothing to review — all transactions are categorized!")
        conn.close()
        return

    total = len(txns)
    st.caption(f"**{total:,}** transaction(s)" + (" · Showing up to 500" if mode == "📋 All transactions" else ""))

    # ── Split layout: table left, edit panel right ────────────────────────
    col_list, col_edit = st.columns([3, 2])

    with col_list:
        # Build display table with a Select checkbox column
        list_df = txns[["id", "date", "account_name", "description",
                         "amount", "category", "subcategory"]].copy()
        list_df.insert(0, "✓", False)
        list_df["amount"] = list_df["amount"].apply(
            lambda v: f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"
        )

        edited_list = st.data_editor(
            list_df,
            column_config={
                "✓":           st.column_config.CheckboxColumn("✓", width="small"),
                "id":          None,  # hidden
                "date":        st.column_config.TextColumn("Date",        disabled=True, width="small"),
                "account_name":st.column_config.TextColumn("Account",     disabled=True),
                "description": st.column_config.TextColumn("Description", disabled=True),
                "amount":      st.column_config.TextColumn("Amount",      disabled=True, width="small"),
                "category":    st.column_config.TextColumn("Category",    disabled=True),
                "subcategory": st.column_config.TextColumn("Subcategory", disabled=True),
            },
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            key="cat_table",
            height=520,
        )

        selected = edited_list[edited_list["✓"] == True]

    with col_edit:
        st.subheader("Edit")

        if selected.empty:
            st.info("← Check a row to edit it.")
            conn.close()
            return

        if len(selected) > 1:
            # ── Bulk edit ─────────────────────────────────────────────────
            sel_indices = selected.index.tolist()
            sel_ids     = [int(txns.iloc[i]["id"]) for i in sel_indices]
            st.caption(f"**{len(sel_ids)} transactions** selected — bulk assign category.")

            bulk_cat = st.selectbox(
                "Category",
                [""] + all_categories,
                key="bulk_cat",
            )
            filtered_subcats = subcats_by_category.get(bulk_cat, [])
            bulk_sub = st.selectbox(
                "Subcategory",
                [""] + filtered_subcats,
                key=f"bulk_sub_{bulk_cat}",
            ) if filtered_subcats else None

            if st.button("✅ Save all", type="primary", use_container_width=True, key="bulk_save"):
                if not bulk_cat:
                    st.error("Pick a category first.")
                else:
                    for txn_id in sel_ids:
                        sub = bulk_sub or None
                        db.update_transaction_category(conn, txn_id, bulk_cat, sub)
                    st.success(f"Updated {len(sel_ids)} transactions → {bulk_cat}.")
                    st.rerun()

        else:
            # ── Single edit ───────────────────────────────────────────────
            sel_i   = selected.index[0]
            orig    = txns.iloc[sel_i]

            st.caption(f"**{orig['date']}**  ·  {orig['account_name']}")
            st.markdown(f"**{orig['description']}**")
            amount_fmt = f"${orig['amount']:,.2f}" if orig["amount"] >= 0 else f"-${abs(orig['amount']):,.2f}"
            st.caption(amount_fmt)
            st.divider()

            # Category selectbox — stable key per transaction id
            cur_cat   = orig["category"] if orig["category"] != "Uncategorized" else ""
            cat_opts  = [""] + all_categories
            cat_idx   = cat_opts.index(cur_cat) if cur_cat in cat_opts else 0
            new_cat   = st.selectbox(
                "Category",
                cat_opts,
                index=cat_idx,
                key=f"single_cat_{orig['id']}",
            )

            # Subcategory — key includes new_cat so it resets when category changes
            filtered_subcats = subcats_by_category.get(new_cat, [])
            cur_sub  = orig["subcategory"] or ""
            sub_opts = [""] + filtered_subcats
            sub_idx  = sub_opts.index(cur_sub) if cur_sub in sub_opts else 0

            if filtered_subcats:
                new_sub = st.selectbox(
                    "Subcategory",
                    sub_opts,
                    index=sub_idx,
                    key=f"single_sub_{new_cat}_{orig['id']}",
                )
            else:
                if new_cat:
                    st.caption("_No subcategories defined for this category._")
                new_sub = None

            st.write("")
            if st.button("✅ Save", type="primary", use_container_width=True, key="single_save"):
                if not new_cat:
                    st.error("Pick a category first.")
                else:
                    db.update_transaction_category(
                        conn, int(orig["id"]), new_cat, new_sub or None
                    )
                    st.success(f"Saved → {new_cat}" + (f" / {new_sub}" if new_sub else "") + ".")
                    st.rerun()

    conn.close()


# ── Tab: Monthly Activity ────────────────────────────────────────────────────

def render_monthly_activity():
    st.header("Monthly Activity")

    col_cap, col_toggle = st.columns([3, 1])
    with col_cap:
        st.caption("Income statement view · Jan 2025 to present · excludes Transfers & investment accounts")
    with col_toggle:
        view_mode = st.radio(
            "view_mode",
            ["📊 Summary", "🔍 Detail"],
            horizontal=True,
            label_visibility="collapsed",
        )

    conn = get_conn()
    if view_mode == "🔍 Detail":
        df = db.get_monthly_activity_detail(conn)
    else:
        df = db.get_monthly_activity(conn)
    conn.close()

    if df.empty:
        st.info("No transaction data yet. Run an import to populate.")
        return

    # All months sorted oldest → newest
    all_months = sorted(df["month"].unique().tolist())

    def fmt_month(m: str) -> str:
        return datetime.strptime(m, "%Y-%m").strftime("%b '%y")

    month_labels = [fmt_month(m) for m in all_months]

    # ── Row builders ──────────────────────────────────────────────────────
    rows_data = []   # list of dicts {Category: ..., Jan '25: ..., ...}
    row_types = []   # parallel styling hints

    def _add_header(label):
        rows_data.append({"Category": label, **{lbl: "" for lbl in month_labels}})
        row_types.append("header")

    def _add_data(label, values_by_month, rtype, indent=False):
        row = {"Category": ("  ↳ " + label) if indent else label}
        for m, lbl in zip(all_months, month_labels):
            v = values_by_month.get(m, 0.0)
            row[lbl] = v if v != 0 else None
        rows_data.append(row)
        row_types.append(rtype)

    def _add_total(label, totals_dict, rtype):
        row = {"Category": label}
        for m, lbl in zip(all_months, month_labels):
            row[lbl] = totals_dict.get(m, 0.0)
        rows_data.append(row)
        row_types.append(rtype)

    def _add_spacer():
        rows_data.append({"Category": "", **{lbl: None for lbl in month_labels}})
        row_types.append("spacer")

    income_totals  = {m: 0.0 for m in all_months}
    expense_totals = {m: 0.0 for m in all_months}

    # ── Summary view ──────────────────────────────────────────────────────
    if view_mode == "📊 Summary":
        pivot = df.pivot_table(
            index=["category", "budget_type"],
            columns="month",
            values="total",
            aggfunc="sum",
            fill_value=0,
        )
        for m in all_months:
            if m not in pivot.columns:
                pivot[m] = 0.0
        pivot = pivot[all_months]

        def _rows_for_type(btype):
            try:
                return pivot.xs(btype, level="budget_type")
            except KeyError:
                return pd.DataFrame(columns=all_months)

        income_pivot  = _rows_for_type("income")
        expense_pivot = _rows_for_type("expense")

        _add_header("INCOME")
        for cat in sorted(income_pivot.index.tolist()):
            vals = {m: float(income_pivot.loc[cat, m]) for m in all_months}
            _add_data(cat, vals, "income")
            for m in all_months:
                income_totals[m] += vals[m]
        _add_total("Total Income", income_totals, "total_income")

        _add_spacer()

        _add_header("EXPENSES")
        for cat in sorted(expense_pivot.index.tolist()):
            vals = {m: float(expense_pivot.loc[cat, m]) for m in all_months}
            _add_data(cat, vals, "expense")
            for m in all_months:
                expense_totals[m] += vals[m]
        _add_total("Total Expenses", expense_totals, "total_expense")

    # ── Detail view ───────────────────────────────────────────────────────
    else:
        income_df  = df[df["budget_type"] == "income"]
        expense_df = df[df["budget_type"] == "expense"]

        _add_header("INCOME")
        for cat in sorted(income_df["category"].unique().tolist()):
            cat_rows = income_df[income_df["category"] == cat]
            cat_totals = {
                m: float(cat_rows[cat_rows["month"] == m]["total"].sum())
                for m in all_months
            }
            _add_data(cat, cat_totals, "income")
            for m in all_months:
                income_totals[m] += cat_totals[m]
            for subcat in sorted(cat_rows["subcategory"].unique().tolist()):
                sub_rows = cat_rows[cat_rows["subcategory"] == subcat]
                sub_totals = {
                    m: float(sub_rows[sub_rows["month"] == m]["total"].sum())
                    for m in all_months
                }
                _add_data(subcat, sub_totals, "income_subcat", indent=True)
        _add_total("Total Income", income_totals, "total_income")

        _add_spacer()

        _add_header("EXPENSES")
        for cat in sorted(expense_df["category"].unique().tolist()):
            cat_rows = expense_df[expense_df["category"] == cat]
            cat_totals = {
                m: float(cat_rows[cat_rows["month"] == m]["total"].sum())
                for m in all_months
            }
            _add_data(cat, cat_totals, "expense")
            for m in all_months:
                expense_totals[m] += cat_totals[m]
            for subcat in sorted(cat_rows["subcategory"].unique().tolist()):
                sub_rows = cat_rows[cat_rows["subcategory"] == subcat]
                sub_totals = {
                    m: float(sub_rows[sub_rows["month"] == m]["total"].sum())
                    for m in all_months
                }
                _add_data(subcat, sub_totals, "expense_subcat", indent=True)
        _add_total("Total Expenses", expense_totals, "total_expense")

    # ── Net row (both views) ──────────────────────────────────────────────
    _add_spacer()
    net = {m: income_totals[m] - expense_totals[m] for m in all_months}
    _add_total("Net Cash Flow", net, "net")

    display_df = pd.DataFrame(rows_data, columns=["Category"] + month_labels)

    # ── Styling ───────────────────────────────────────────────────────────
    def _style_row(row):
        rt = row_types[row.name]
        if rt == "header":
            return ["font-weight:700; background:#e9ecef; color:#495057"] * len(row)
        if rt == "total_income":
            return ["font-weight:700; background:#d1e7dd; color:#0f5132"] * len(row)
        if rt == "total_expense":
            return ["font-weight:700; background:#f8d7da; color:#842029"] * len(row)
        if rt == "net":
            return ["font-weight:700; background:#cfe2ff; color:#084298"] * len(row)
        if rt in ("income_subcat", "expense_subcat"):
            return ["color:#6c757d; font-size:0.88em"] * len(row)
        return [""] * len(row)

    def _fmt_cell(v):
        if v is None or v == "" or (isinstance(v, float) and v == 0.0):
            return "—"
        if isinstance(v, (int, float)):
            return f"${v:,.0f}"
        return str(v)

    format_dict = {col: _fmt_cell for col in month_labels}

    styled = (
        display_df.style
        .apply(_style_row, axis=1)
        .format(format_dict, na_rep="—")
    )

    st.dataframe(styled, use_container_width=True, hide_index=True)

    # ── Net Cash Flow bar chart ───────────────────────────────────────────
    if len(all_months) > 1:
        st.write("")
        st.subheader("Net Cash Flow by Month")
        net_vals = [net[m] for m in all_months]
        colors   = ["#198754" if v >= 0 else "#dc3545" for v in net_vals]
        fig = go.Figure(go.Bar(
            x=month_labels,
            y=net_vals,
            marker_color=colors,
            text=[f"${v:,.0f}" for v in net_vals],
            textposition="outside",
        ))
        fig.update_layout(
            yaxis_title="Net ($)",
            yaxis_tickformat="$,.0f",
            xaxis_title=None,
            margin=dict(t=10, b=10, l=10, r=10),
            height=320,
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    render_sidebar()

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "📊 Overview",
        "📅 Monthly Activity",
        "📋 Transactions",
        "🏷️ Categorize",
        "🎯 Budget",
        "📈 Net Worth",
        "✨ Insights",
    ])

    with tab1:
        render_overview()
    with tab2:
        render_monthly_activity()
    with tab3:
        render_transactions()
    with tab4:
        render_categorize()
    with tab5:
        render_budget()
    with tab6:
        render_net_worth()
    with tab7:
        render_insights()


if __name__ == "__main__":
    main()
