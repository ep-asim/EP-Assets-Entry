import io
import os
import re
from datetime import date
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for
import google.auth
from google.cloud.sql.connector import Connector, IPTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import pymysql


def load_local_env():
    """Small .env loader so the container does not require python-dotenv."""
    env_file = Path(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "local-development-secret")
connector = None


def db_connection():
    global connector
    if connector is None:
        credential_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        if credential_file:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", credential_file)
        connector = Connector()
    return connector.connect(
        os.environ["CLOUDSQL_INSTANCE_CONNECTION_NAME"],
        "pymysql",
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        db=os.environ["DB_NAME"],
        ip_type=IPTypes.PUBLIC,
        connect_timeout=20,
        read_timeout=20,
        write_timeout=20,
    )


def query(sql, params=(), one=False):
    conn = db_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(sql, params)
            result = cursor.fetchone() if one else cursor.fetchall()
        return result
    finally:
        conn.close()


def execute(sql, params=()):
    conn = db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def require_basic_auth(view):
    """Optional lightweight protection for Cloud Run deployments."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        username = os.environ.get("APP_USERNAME")
        password = os.environ.get("APP_PASSWORD")
        if username and password:
            auth = request.authorization
            if not auth or auth.username != username or auth.password != password:
                return ("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Asset Library"'})
        return view(*args, **kwargs)
    return wrapped


def parse_date(value, required=False):
    if not value:
        if required:
            raise ValueError("Start date is required.")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Dates must use YYYY-MM-DD format.") from exc


def asset_form(kind, form):
    try:
        data = {
            "certification_type": form.get("certification_type", "").strip(),
            "audience": form.get("audience", "").strip(),
            "start_date": parse_date(form.get("start_date"), required=True),
            "expiry_date": parse_date(form.get("expiry_date")),
            "asset_type": form.get("asset_type", "").strip(),
            "location": form.get("location", "").strip() or None,
            "link": form.get("link", "").strip() or None,
        }
    except ValueError as exc:
        raise ValueError(str(exc))
    if not data["certification_type"] or not data["asset_type"] or not data["audience"]:
        raise ValueError("Certification type, audience, and asset type are required.")
    if data["asset_type"] not in {"Google Doc", "PDF"}:
        raise ValueError("Asset type must be Google Doc or PDF.")
    if data["expiry_date"] and data["expiry_date"] < data["start_date"]:
        raise ValueError("Expiry date cannot be earlier than the start date.")
    if data["audience"] not in {"internal", "customer-facing", "both"}:
        raise ValueError("Audience must be internal, customer-facing, or both.")
    return data


@app.context_processor
def inject_globals():
    return {"today": date.today().isoformat()}


@app.get("/")
@require_basic_auth
def index():
    return render_template("index.html")


@app.get("/items")
@require_basic_auth
def items():
    search = request.args.get("q", "").strip()
    if search:
        rows = query("SELECT item_number FROM new_item_catalog WHERE item_number LIKE %s ORDER BY item_number", (f"%{search}%",))
    else:
        rows = query("SELECT item_number FROM new_item_catalog ORDER BY item_number")
    assets = query("SELECT * FROM item_asset_requirement ORDER BY item_number, start_date, id")
    grouped = {row["item_number"]: [] for row in rows}
    for asset in assets:
        grouped.setdefault(asset["item_number"], []).append(asset)
    return render_template("items.html", items=rows, assets=grouped, search=search)


@app.get("/suppliers")
@require_basic_auth
def suppliers():
    search = request.args.get("q", "").strip()
    if search:
        rows = query("""SELECT supplier_code, company_name FROM new_supplier
            WHERE supplier_code LIKE %s OR company_name LIKE %s
            ORDER BY company_name, supplier_code""", (f"%{search}%", f"%{search}%"))
    else:
        rows = query("SELECT supplier_code, company_name FROM new_supplier ORDER BY company_name, supplier_code")
    assets = query("SELECT * FROM new_supplier_asset ORDER BY supplier_code, start_date, id")
    grouped = {row["supplier_code"]: [] for row in rows}
    for asset in assets:
        grouped.setdefault(asset["supplier_code"], []).append(asset)
    return render_template("suppliers.html", suppliers=rows, assets=grouped, search=search)


@app.get("/item-assets/new")
@require_basic_auth
def new_item_asset():
    item_number = request.args.get("item_number", "")
    choices = query("SELECT item_number FROM new_item_catalog ORDER BY item_number")
    return render_template("asset_form.html", kind="item", mode="create", asset=None, choices=choices, selected=item_number)


@app.post("/item-assets")
@require_basic_auth
def create_item_asset():
    try:
        item_number = request.form.get("item_number", "").strip()
        if not item_number:
            raise ValueError("Choose an item.")
        data = asset_form("item", request.form)
        execute("""INSERT INTO item_asset_requirement
            (item_number, asset_type, certification_type, audience, start_date, expiry_date, link, location)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (item_number, data["asset_type"], data["certification_type"], data["audience"], data["start_date"], data["expiry_date"], data["link"], data["location"]))
        flash("Item asset created.", "success")
        return redirect(url_for("items"))
    except (ValueError, pymysql.MySQLError) as exc:
        flash(str(exc), "error")
        choices = query("SELECT item_number FROM new_item_catalog ORDER BY item_number")
        return render_template("asset_form.html", kind="item", mode="create", asset=request.form, choices=choices, selected=request.form.get("item_number", "")), 400


@app.get("/supplier-assets/new")
@require_basic_auth
def new_supplier_asset():
    supplier_code = request.args.get("supplier_code", "")
    choices = query("SELECT supplier_code, company_name FROM new_supplier ORDER BY company_name, supplier_code")
    return render_template("asset_form.html", kind="supplier", mode="create", asset=None, choices=choices, selected=supplier_code)


@app.post("/supplier-assets")
@require_basic_auth
def create_supplier_asset():
    try:
        supplier_code = request.form.get("supplier_code", "").strip()
        if not supplier_code:
            raise ValueError("Choose a supplier.")
        data = asset_form("supplier", request.form)
        execute("""INSERT INTO new_supplier_asset
            (supplier_code, certification_type, audience, start_date, expiry_date, asset_type, location, link)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (supplier_code, data["certification_type"], data["audience"], data["start_date"], data["expiry_date"], data["asset_type"], data["location"], data["link"]))
        flash("Supplier asset created.", "success")
        return redirect(url_for("suppliers"))
    except (ValueError, pymysql.MySQLError) as exc:
        flash(str(exc), "error")
        choices = query("SELECT supplier_code, company_name FROM new_supplier ORDER BY company_name, supplier_code")
        return render_template("asset_form.html", kind="supplier", mode="create", asset=request.form, choices=choices, selected=request.form.get("supplier_code", "")), 400


def get_asset(kind, asset_id):
    table = "item_asset_requirement" if kind == "item" else "new_supplier_asset"
    return query(f"SELECT * FROM {table} WHERE id=%s", (asset_id,), one=True)


@app.route("/<kind>-assets/<int:asset_id>/edit", methods=["GET", "POST"])
@require_basic_auth
def edit_asset(kind, asset_id):
    if kind not in {"item", "supplier"}:
        abort(404)
    asset = get_asset(kind, asset_id)
    if not asset:
        abort(404)
    if request.method == "POST":
        try:
            data = asset_form(kind, request.form)
            table = "item_asset_requirement" if kind == "item" else "new_supplier_asset"
            execute(f"""UPDATE {table} SET certification_type=%s, audience=%s, start_date=%s,
                expiry_date=%s, asset_type=%s, location=%s, link=%s WHERE id=%s""", (data["certification_type"], data["audience"], data["start_date"], data["expiry_date"], data["asset_type"], data["location"], data["link"], asset_id))
            flash("Asset updated.", "success")
            return redirect(url_for("items" if kind == "item" else "suppliers"))
        except (ValueError, pymysql.MySQLError) as exc:
            flash(str(exc), "error")
            asset = {**asset, **request.form}
            return render_template("asset_form.html", kind=kind, mode="edit", asset=asset, choices=[], selected=""), 400
    return render_template("asset_form.html", kind=kind, mode="edit", asset=asset, choices=[], selected="")


def drive_file_id(link):
    if not link:
        return None
    match = re.search(r"(?:/d/|id=)([-\w]{10,})", link)
    return match.group(1) if match else None


def drive_service():
    credential_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if credential_file:
        credentials = service_account.Credentials.from_service_account_file(
            credential_file, scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
    else:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


@app.get("/<kind>-assets/<int:asset_id>/preview")
@require_basic_auth
def preview_asset(kind, asset_id):
    if kind not in {"item", "supplier"}:
        abort(404)
    asset = get_asset(kind, asset_id)
    if not asset or not asset.get("link"):
        abort(404)
    file_id = drive_file_id(asset["link"])
    if not file_id:
        return redirect(asset["link"])
    service = drive_service()
    if not service:
        return redirect(asset["link"])
    metadata = service.files().get(fileId=file_id, fields="id,name,mimeType,webViewLink").execute()
    mime = metadata.get("mimeType", "application/octet-stream")
    if mime == "application/vnd.google-apps.document":
        content = service.files().export(fileId=file_id, mimeType="text/html").execute()
        return content.decode("utf-8") if isinstance(content, bytes) else content
    if mime.startswith("image/") or mime == "application/pdf" or mime.startswith("text/"):
        buffer = io.BytesIO()
        request_media = service.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(buffer, request_media)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buffer.seek(0)
        return send_file(buffer, mimetype=mime, download_name=metadata.get("name", "preview"), as_attachment=False)
    return redirect(metadata.get("webViewLink") or asset["link"])


@app.get("/health")
def health():
    query("SELECT 1", one=True)
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
