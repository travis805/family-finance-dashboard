# Family Finance Dashboard

Private Streamlit dashboard for our household finances. Data lives in **Turso**
(hosted libSQL), so every device opens one live database over the network. Nothing
syncs a local `.db` file.

## Run locally

Requires **Python 3.12** (the `libsql` wheel does not build on 3.14).

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Add `.streamlit/secrets.toml` (gitignored) from the template:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# fill in TURSO_DATABASE_URL and TURSO_AUTH_TOKEN
```

Then:

```bash
streamlit run app.py
```

With the two secrets set, `db.get_conn()` connects to Turso. Without them it falls
back to a local `family_finance.db` for offline work.

## Deploy (Streamlit Community Cloud)

1. New app, point at this repo's `app.py`.
2. Advanced settings -> Secrets: paste the same `TURSO_DATABASE_URL` and
   `TURSO_AUTH_TOKEN`.
3. Sharing -> restrict to specific viewers.
