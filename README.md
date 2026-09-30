# Asset Library

A small Flask CRUD app for item- and supplier-level assets. It reads from Cloud SQL MySQL, supports item/supplier search, creates and edits asset records, and previews Google Drive PDFs/images/Google Docs when the configured identity has access.

## Run locally

1. Copy `.env.example` to `.env` and fill in the database values and local service-account path.
2. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Start the app:

   ```powershell
   python app.py
   ```

Open <http://localhost:8080>.

The local service account needs Cloud SQL access and Viewer access to the relevant Google Drive files/folders.

## Cloud Run deployment

Cloud Run uses its attached runtime service account through Application Default Credentials. No service-account JSON file should be committed or uploaded. Grant the runtime service account:

- `Cloud SQL Client` on the project
- Viewer access to the Drive files or folders used for previews

Create a Secret Manager secret for the database password:

```powershell
gcloud secrets create asset-db-password --replication-policy=automatic
gcloud secrets versions add asset-db-password --data-file=-
```

Paste the database password, then press `Ctrl+Z` followed by Enter in Windows PowerShell.

Deploy from this repository directory:

```powershell
gcloud run deploy asset-library `
  --source . `
  --region YOUR_REGION `
  --service-account YOUR_RUNTIME_SERVICE_ACCOUNT `
  --set-env-vars CLOUDSQL_INSTANCE_CONNECTION_NAME=PROJECT:REGION:INSTANCE,DB_NAME=ep,DB_USER=DATABASE_USER,DB_PORT=3306 `
  --set-secrets DB_PASSWORD=asset-db-password:latest
```

The service account running the deployment must be able to deploy Cloud Run services and build from source. If the service should be publicly reachable, add `--allow-unauthenticated`; otherwise use `--no-allow-unauthenticated` and grant Cloud Run Invoker to the intended users. The app also supports optional `APP_USERNAME`, `APP_PASSWORD`, and `APP_SECRET_KEY` environment variables for basic protection.

## Database expectations

The app expects these tables and columns to exist:

- `new_item_catalog(item_number)`
- `new_supplier(supplier_code, company_name)`
- `item_asset_requirement`
- `new_supplier_asset`

The app does not expose delete actions. Asset type is restricted to `PDF` and `Google Doc`; location is optional and can be populated by a later automation.
