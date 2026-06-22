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

Community Cloud's free tier allows only one private app per workspace, so this app
deploys as a **public** app gated by a passphrase (`APP_PASSWORD`). Nothing renders and
no query runs until the passphrase matches; the Turso token stays server-side.

1. New app, point at this repo's `app.py`.
2. Advanced settings -> Secrets: paste `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, and a
   long `APP_PASSWORD`.
3. Deploy. The app is public-URL but locked behind the passphrase.
