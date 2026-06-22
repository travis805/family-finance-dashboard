"""
insights.py — Quarterly financial insights helpers
Family Finance Dashboard

Generating insights no longer requires an Anthropic API key. Use
build_insights_data() to produce the formatted data block, paste it into
Cowork, and then save the result back with save_insight().

The generate_quarterly_insights() function is retained for backwards-
compatibility but is no longer called from the dashboard UI.
"""
import os
import pandas as pd
from datetime import datetime

from db import (
    get_quarterly_spending,
    get_quarterly_income,
    get_budget_categories,
    get_net_worth_summary,
    get_net_worth_history,
    get_quarter_dates,
)


SYSTEM_PROMPT = """You are a thoughtful personal finance advisor reviewing a family's quarterly finances.
Your job is to write a clear, honest, and encouraging quarterly summary — not a scolding report.
Tone: warm, direct, plain English. Targets are guidelines, not hard limits.
Format: use markdown headers and bullet points. Keep it concise and actionable."""


def build_insights_prompt(
    quarter_label: str,
    spending_df: pd.DataFrame,
    budget_df: pd.DataFrame,
    income: float,
    nw_start: float,
    nw_end: float,
    top_transactions: pd.DataFrame,
) -> str:

    # Merge spending with budget targets
    if not spending_df.empty and not budget_df.empty:
        merged = spending_df.merge(
            budget_df[["category", "quarterly_target"]],
            on="category",
            how="left",
        )
    else:
        merged = spending_df.copy() if not spending_df.empty else pd.DataFrame()

    spending_lines = []
    total_spending = 0.0
    if not merged.empty:
        for _, row in merged.iterrows():
            actual = row.get("actual", 0) or 0
            target = row.get("quarterly_target")
            total_spending += actual
            if target and target > 0:
                pct = (actual / target) * 100
                status = "✅" if pct <= 100 else "⚠️"
                spending_lines.append(
                    f"  - {row['category']}: ${actual:,.0f} actual vs ${target:,.0f} target ({pct:.0f}%) {status}"
                )
            else:
                spending_lines.append(f"  - {row['category']}: ${actual:,.0f} (no target set)")

    nw_change = nw_end - nw_start
    savings_rate = ((income - total_spending) / income * 100) if income > 0 else 0

    notable = ""
    if not top_transactions.empty:
        top5 = top_transactions.head(5)
        notable = "\n".join(
            f"  - {row['date']} | {row['description']} | ${abs(row['amount']):,.2f} | {row['category']}"
            for _, row in top5.iterrows()
        )

    prompt = f"""Please generate a quarterly financial summary for {quarter_label}.

## Financial Data

**Income this quarter:** ${income:,.2f}
**Total spending this quarter:** ${total_spending:,.2f}
**Estimated savings rate:** {savings_rate:.1f}%

**Net worth change:**
- Start of quarter: ${nw_start:,.2f}
- End of quarter: ${nw_end:,.2f}
- Change: ${nw_change:+,.2f}

**Spending by category vs budget:**
{chr(10).join(spending_lines) if spending_lines else '  (No spending data available)'}

**Largest individual transactions this quarter:**
{notable if notable else '  (No transaction data available)'}

---
Please write a quarterly summary covering:
1. **Overall financial health** — one-paragraph take on how the quarter went
2. **Spending highlights** — top categories, any notable spikes or wins
3. **Budget performance** — where you were over/under and what it means
4. **Savings rate** — brief comment on the rate and what drove it
5. **Net worth** — what moved it and why
6. **2–3 flags or suggestions** — specific, actionable observations for next quarter

Keep the tone encouraging. Avoid moralizing. Focus on facts and patterns."""

    return prompt


def generate_quarterly_insights(
    conn,
    quarter_label: str,
    api_key: str | None = None,
) -> tuple[str, str]:
    """
    Generate a Claude-powered quarterly insights narrative.
    Returns (insight_text, prompt_used).
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("No Anthropic API key provided.")

    start_date, end_date = get_quarter_dates(quarter_label)

    # Gather data
    spending_df  = get_quarterly_spending(conn, start_date, end_date)
    budget_df    = get_budget_categories(conn)
    income       = get_quarterly_income(conn, start_date, end_date)
    nw_history   = get_net_worth_history(conn)

    # Net worth at start and end of quarter
    if not nw_history.empty:
        before = nw_history[nw_history["snapshot_date"] < start_date]
        during = nw_history[nw_history["snapshot_date"] <= end_date]
        nw_start = float(before["net_worth"].iloc[-1]) if not before.empty else 0.0
        nw_end   = float(during["net_worth"].iloc[-1]) if not during.empty else 0.0
    else:
        nw_start = nw_end = 0.0

    # Top transactions by size
    from db import get_transactions
    txns = get_transactions(conn, start_date=start_date, end_date=end_date,
                            exclude_categories=["Transfers", "Salary/Wages"])
    if not txns.empty:
        top_txns = txns[txns["amount"] < 0].nlargest(5, "amount", keep="last").copy()
        top_txns["amount"] = top_txns["amount"].abs()
        top_txns = top_txns[["date", "description", "amount", "category"]]
    else:
        top_txns = pd.DataFrame()

    prompt = build_insights_prompt(
        quarter_label, spending_df, budget_df, income, nw_start, nw_end, top_txns
    )

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    insight_text = message.content[0].text
    return insight_text, prompt


def build_insights_data(conn, quarter_label: str) -> str:
    """
    Build a plain-text data block for a given quarter that can be pasted
    into a Cowork chat for Claude to analyze — no API key required.
    """
    start_date, end_date = get_quarter_dates(quarter_label)

    spending_df  = get_quarterly_spending(conn, start_date, end_date)
    budget_df    = get_budget_categories(conn)
    income       = get_quarterly_income(conn, start_date, end_date)
    nw_history   = get_net_worth_history(conn)

    # Net worth at start and end of quarter
    if not nw_history.empty:
        before   = nw_history[nw_history["snapshot_date"] < start_date]
        during   = nw_history[nw_history["snapshot_date"] <= end_date]
        nw_start = float(before["net_worth"].iloc[-1]) if not before.empty else 0.0
        nw_end   = float(during["net_worth"].iloc[-1]) if not during.empty else 0.0
    else:
        nw_start = nw_end = 0.0

    # Top transactions by size
    from db import get_transactions
    txns = get_transactions(conn, start_date=start_date, end_date=end_date,
                            exclude_categories=["Transfers", "Salary/Wages"])
    if not txns.empty:
        top_txns = txns[txns["amount"] < 0].nlargest(10, "amount", keep="last").copy()
        top_txns["amount"] = top_txns["amount"].abs()
    else:
        top_txns = pd.DataFrame()

    # Build spending section with budget comparison
    if not spending_df.empty and not budget_df.empty:
        merged = spending_df.merge(
            budget_df[["category", "quarterly_target"]], on="category", how="left"
        )
    else:
        merged = spending_df.copy() if not spending_df.empty else pd.DataFrame()

    total_spending = 0.0
    spending_lines = []
    if not merged.empty:
        for _, row in merged.iterrows():
            actual = row.get("actual", 0) or 0
            target = row.get("quarterly_target")
            total_spending += actual
            if target and target > 0:
                pct    = actual / target * 100
                status = "✅" if pct <= 100 else "⚠️"
                spending_lines.append(
                    f"  {row['category']}: ${actual:,.0f} actual vs ${target:,.0f} target ({pct:.0f}%) {status}"
                )
            else:
                spending_lines.append(f"  {row['category']}: ${actual:,.0f} (no target set)")

    nw_change    = nw_end - nw_start
    savings_rate = (income - total_spending) / income * 100 if income > 0 else 0.0

    top_txn_lines = ""
    if not top_txns.empty:
        top_txn_lines = "\n".join(
            f"  {r['date']} | {r['description']} | ${abs(r['amount']):,.2f} | {r['category']}"
            for _, r in top_txns.iterrows()
        )

    lines = [
        f"FAMILY FINANCE — QUARTERLY DATA FOR ANALYSIS",
        f"Quarter: {quarter_label}",
        f"",
        f"INCOME & CASH FLOW",
        f"  Income this quarter:     ${income:,.2f}",
        f"  Total spending:          ${total_spending:,.2f}",
        f"  Net cash flow:           ${income - total_spending:+,.2f}",
        f"  Estimated savings rate:  {savings_rate:.1f}%",
        f"",
        f"NET WORTH",
        f"  Start of quarter: ${nw_start:,.2f}",
        f"  End of quarter:   ${nw_end:,.2f}",
        f"  Change:           ${nw_change:+,.2f}",
        f"",
        f"SPENDING BY CATEGORY vs BUDGET",
    ] + (spending_lines if spending_lines else ["  (no spending data)"]) + [
        f"",
        f"LARGEST TRANSACTIONS THIS QUARTER",
    ] + ([top_txn_lines] if top_txn_lines else ["  (no transaction data)"]) + [
        f"",
        f"---",
        f"Please generate a quarterly financial summary covering:",
        f"1. Overall financial health (one-paragraph take on how the quarter went)",
        f"2. Spending highlights (top categories, notable spikes or wins)",
        f"3. Budget performance (where over/under and what it means)",
        f"4. Savings rate (brief comment and what drove it)",
        f"5. Net worth (what moved it and why)",
        f"6. 2–3 flags or suggestions (specific, actionable observations for next quarter)",
        f"",
        f"Tone: warm, direct, plain English. Targets are guidelines, not hard limits.",
    ]

    return "\n".join(lines)


def save_insight(conn, quarter_label: str, prompt: str, insight_text: str):
    conn.execute("""
        INSERT INTO insights_log (quarter, prompt_used, insight_text)
        VALUES (?, ?, ?)
    """, (quarter_label, prompt, insight_text))
    conn.commit()


def get_insights_log(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT * FROM insights_log ORDER BY generated_at DESC", conn
    )
