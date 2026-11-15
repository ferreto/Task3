import csv
import io
import os
import time
import psycopg2
import psycopg2.extras
from pathlib import Path
from urllib.parse import quote
from flask import Flask, request, jsonify, g, send_from_directory, render_template_string

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
SCHEMA_PATH = BASE_DIR / "schema.sql"

# Создаем папку uploads при старте, если ее нет
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Забираем настройки БД из переменных окружения (Docker передаст их)
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "videos")
DB_USER = os.environ.get("DB_USER", "video_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "video_pass")

COMPANY_FIELD_MAP = {
    "company name": "company_name",
    "language": "language",
    "country": "country",
    "postcode": "postcode",
    "city": "city",
    "street": "street",
    "houseno": "house_no",
    "web": "web",
}

UPLOAD_FORM = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Upload companies CSV</title>
</head>
<body>
  <h1>Upload companies CSV</h1>
  <form method="post" action="/upload_companies" enctype="multipart/form-data">
    <input type="file" name="file" accept=".csv,text/csv" required>
    <button type="submit">Upload</button>
  </form>
  <p>CSV format: <code>URL;Company Name;Language;Country;Postcode;City;Street;HouseNo;Web</code></p>
  <p><a href="/companies">View companies</a></p>
</body>
</html>
"""


def get_db():
    """Открывает соединение с PostgreSQL для текущего контекста запроса."""
    db = getattr(g, '_database', None)
    if db is None:
        # psycopg2 не является потокобезопасным, поэтому открываем
        # новое соединение на каждый запрос (через flask.g)
        db = g._database = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        )
        # Используем DictCursor, чтобы обращаться к колонкам по имени (row['name'])
        db.cursor_factory = psycopg2.extras.DictCursor
    return db


@app.teardown_appcontext
def close_db(exception):
    """Закрывает соединение с БД после завершения запроса."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    """Применяет idempotent schema.sql. Ждет, пока база данных полностью загрузится."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    retries = 10
    for attempt in range(retries):
        try:
            # Открываем соединение напрямую, без контекста Flask (так как это запуск)
            with psycopg2.connect(
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(schema)
                conn.commit()
            print("[web_service] Database initialized successfully.")
            return
        except psycopg2.OperationalError as e:
            print(f"[web_service] Database not ready (attempt {attempt + 1}/{retries}). Waiting 2 seconds...")
            time.sleep(2)

    print("[web_service] ERROR: Could not connect to the database.")
    raise RuntimeError("Database initialization failed")


def _sanitize_filename(name):
    """Очищает имя файла от недопустимых символов."""
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip() or "video"


def _parse_companies_csv(raw_bytes):
    """Разбирает semicolon-separated CSV с UTF-8 BOM и пробелами в заголовках."""
    text = raw_bytes.decode("utf-8-sig")
    reader = csv.DictReader(
        io.StringIO(text),
        delimiter=";",
        quotechar='"',
        skipinitialspace=True,
    )

    # Without this check a file with the wrong delimiter or without the
    # "Company Name" column parses "successfully" and every row is skipped.
    fieldnames = reader.fieldnames
    if not fieldnames:
        raise csv.Error("file is empty")
    known = {COMPANY_FIELD_MAP.get((name or "").strip().lower()) for name in fieldnames}
    if "company_name" not in known:
        header = ";".join(name or "" for name in fieldnames)
        if len(header) > 100:
            header = header[:100] + "..."
        raise csv.Error(
            "header must contain a 'Company Name' column and use ';' as the "
            f"delimiter, got: {header!r}"
        )

    rows = []
    for raw in reader:
        row = {}
        for key, value in raw.items():
            if key is None:
                continue
            canonical = COMPANY_FIELD_MAP.get(key.strip().lower())
            if canonical is None:
                continue
            cleaned = (value or "").strip()
            row[canonical] = cleaned or None
        rows.append(row)
    return rows


def _upsert_company(cur, row):
    """Upsert по company_name. Обновляет только CSV-колонки, не трогая enrichment."""
    cur.execute(
        "SELECT 1 FROM companies WHERE company_name = %s",
        (row["company_name"],),
    )
    existed = cur.fetchone() is not None
    cur.execute(
        """
        INSERT INTO companies
            (company_name, language, country, postcode, city, street, house_no, web)
        VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (company_name) DO UPDATE SET
            language = EXCLUDED.language,
            country = EXCLUDED.country,
            postcode = EXCLUDED.postcode,
            city = EXCLUDED.city,
            street = EXCLUDED.street,
            house_no = EXCLUDED.house_no,
            web = EXCLUDED.web
        """,
        (
            row["company_name"],
            row.get("language"),
            row.get("country"),
            row.get("postcode"),
            row.get("city"),
            row.get("street"),
            row.get("house_no"),
            row.get("web"),
        ),
    )
    return "updated" if existed else "imported"


@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    company_name = request.form.get('company_name', '').strip()

    if not file or file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if not company_name:
        return jsonify({"error": "No company_name provided"}), 400

    safe_name = _sanitize_filename(company_name)
    ext = os.path.splitext(file.filename)[1] or ".webm"
    filename = f"{safe_name}{ext}"
    filepath = UPLOAD_DIR / filename

    file.save(filepath)

    # The file name keeps spaces and non-ASCII letters from the company name,
    # so it has to be percent-encoded to form a valid URL.
    video_url = f"{request.host_url.rstrip('/')}/media/{quote(filename)}"

    db = get_db()
    try:
        with db.cursor() as cur:
            # Синтаксис PostgreSQL: плейсхолдеры %s и EXCLUDED для Upsert
            cur.execute("""
                INSERT INTO videos (company_name, file_path, video_url)
                VALUES (%s, %s, %s)
                ON CONFLICT (company_name) DO UPDATE SET
                    file_path = EXCLUDED.file_path,
                    video_url = EXCLUDED.video_url,
                    updated_at = NOW()
            """, (company_name, str(filepath), video_url))
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "company_name": company_name,
        "video_url": video_url
    }), 200


@app.route('/get_video_url', methods=['GET'])
def get_video_url():
    company_name = request.args.get('company_name', '').strip()
    if not company_name:
        return jsonify({"error": "company_name is required"}), 400

    db = get_db()
    with db.cursor() as cur:
        # Синтаксис PostgreSQL: %s вместо ?
        cur.execute("SELECT video_url FROM videos WHERE company_name = %s", (company_name,))
        row = cur.fetchone()

    if row:
        return jsonify({
            "company_name": company_name,
            "video_url": row["video_url"]
        }), 200
    else:
        return jsonify({"error": "Not found"}), 404


@app.route('/media/<path:filename>', methods=['GET'])
def serve_media(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route('/videos', methods=['GET'])
def list_videos():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM videos ORDER BY updated_at DESC")
        rows = cur.fetchall()
    return jsonify([dict(r) for r in rows]), 200


@app.route('/upload_companies', methods=['GET'])
def upload_companies_form():
    return render_template_string(UPLOAD_FORM), 200


@app.route('/upload_companies', methods=['POST'])
def upload_companies():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    try:
        rows = _parse_companies_csv(file.read())
    except UnicodeDecodeError:
        return jsonify({"error": "File must be UTF-8 encoded"}), 400
    except csv.Error as e:
        return jsonify({"error": f"Invalid CSV: {e}"}), 400

    imported = 0
    updated = 0
    skipped = 0
    db = get_db()
    try:
        with db.cursor() as cur:
            for row in rows:
                if not row.get("company_name"):
                    skipped += 1
                    app.logger.warning("Skipping CSV row without Company Name")
                    continue
                result = _upsert_company(cur, row)
                if result == "imported":
                    imported += 1
                else:
                    updated += 1
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "imported": imported,
        "updated": updated,
        "total_rows": len(rows),
    }), 200


@app.route('/companies', methods=['GET'])
def list_companies():
    status_raw = request.args.get('status')
    params = []
    query = "SELECT * FROM companies"
    if status_raw is not None and status_raw.strip() != "":
        try:
            status_value = int(status_raw)
        except ValueError:
            return jsonify({"error": "status must be an integer"}), 400
        query += " WHERE status = %s"
        params.append(status_value)
    query += " ORDER BY id"

    db = get_db()
    with db.cursor() as cur:
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
    return jsonify([dict(r) for r in rows]), 200


@app.route('/companies/<path:company_name>', methods=['GET'])
def get_company(company_name):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM companies WHERE company_name = %s", (company_name,))
        row = cur.fetchone()
    if row is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row)), 200


# Вызываем инициализацию при старте WSGI-приложения (Apache загрузит этот файл)
init_db()

# app.run() отсюда удален — Apache + mod_wsgi сами запустят сервер
