import argparse
import logging
import os
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

from company_status import CompanyStatus, label
from impressum import (
    MAX_TEXT_CHARS,
    DeepSeekError,
    DeepSeekParseError,
    call_deepseek,
    decide_status,
    extract_visible_text,
    fetch_html,
    find_impressum_url,
    flatten_address,
    normalize_url,
    optimize_text,
    strip_boilerplate_lines,
)

BASE_DIR = Path(__file__).resolve().parent
REQUEST_DELAY = 1.0


def load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Enrich imported companies with Impressum data via DeepSeek."
    )
    parser.add_argument("--company", help="process only this company_name")
    parser.add_argument("--limit", type=positive_int, help="process at most N rows")
    parser.add_argument("--status", type=int, help="process only rows with this status")
    return parser.parse_args(argv)


def db_config():
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": os.environ.get("DB_PORT", "5432"),
        "dbname": os.environ.get("DB_NAME", "videos"),
        "user": os.environ.get("DB_USER", "video_user"),
        "password": os.environ.get("DB_PASSWORD", "video_pass"),
    }


def select_companies(conn, args):
    conditions = []
    params = []
    if args.company:
        conditions.append("company_name = %s")
        params.append(args.company)
    if args.status is not None:
        conditions.append("status = %s")
        params.append(args.status)
    if not args.company and args.status is None:
        conditions.append("(status IS NULL OR status <> 0)")

    query = "SELECT * FROM companies"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id"
    if args.limit:
        query += " LIMIT %s"
        params.append(args.limit)

    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def update_status(conn, company_id, status, impressum_url=None):
    with conn.cursor() as cur:
        if impressum_url is None:
            cur.execute(
                "UPDATE companies SET status = %s, updated_at = NOW() WHERE id = %s",
                (int(status), company_id),
            )
        else:
            cur.execute(
                """
                UPDATE companies
                SET status = %s, impressum_url = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (int(status), impressum_url, company_id),
            )
    conn.commit()


def update_enriched(conn, company_id, status, impressum_url, legal_name, postal_address, data):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE companies SET
                status = %s,
                impressum_url = %s,
                legal_name = %s,
                postal_address = %s,
                handelsregister = %s,
                vat_number = %s,
                phone = %s,
                email = %s,
                responsible_person = %s,
                raw_json = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (
                int(status),
                impressum_url,
                legal_name,
                postal_address,
                data.get("handelsregister"),
                data.get("vat_number"),
                data.get("phone"),
                data.get("email"),
                data.get("responsible_person"),
                psycopg2.extras.Json(data),
                company_id,
            ),
        )
    conn.commit()


def process_company(conn, row, api_key, model, logger):
    company_name = row["company_name"]

    url = normalize_url(row["web"])
    if not url:
        logger.warning("%s: empty website -> status 5 (NO_WEBSITE)", company_name)
        update_status(conn, row["id"], CompanyStatus.NO_WEBSITE)
        return CompanyStatus.NO_WEBSITE

    try:
        final_url, home_html = fetch_html(url)
    except Exception as exc:
        logger.error("%s: home page fetch failed for %s: %s -> status 4", company_name, url, exc)
        update_status(conn, row["id"], CompanyStatus.ERROR)
        return CompanyStatus.ERROR

    impressum_url = find_impressum_url(home_html, final_url)
    if not impressum_url:
        logger.warning(
            "%s: no Impressum link found on %s -> status 1",
            company_name,
            final_url,
        )
        update_status(conn, row["id"], CompanyStatus.NO_IMPRESSUM_LINK_FOUND)
        return CompanyStatus.NO_IMPRESSUM_LINK_FOUND

    logger.info("%s: Impressum URL %s", company_name, impressum_url)

    time.sleep(REQUEST_DELAY)
    try:
        _, impressum_html = fetch_html(impressum_url)
    except Exception as exc:
        logger.error(
            "%s: Impressum fetch failed for %s: %s -> status 4",
            company_name,
            impressum_url,
            exc,
        )
        update_status(conn, row["id"], CompanyStatus.ERROR, impressum_url=impressum_url)
        return CompanyStatus.ERROR

    visible_text = extract_visible_text(impressum_html)
    cleaned_text = strip_boilerplate_lines(visible_text)
    optimized_text, stats = optimize_text(cleaned_text, MAX_TEXT_CHARS)
    logger.info(
        "%s: text optimization method=%s chars %d->%d tokens ~%d->~%d saved ~%d",
        company_name,
        stats["method"],
        stats["original_chars"],
        stats["optimized_chars"],
        stats["original_tokens"],
        stats["optimized_tokens"],
        stats["saved_tokens"],
    )

    try:
        data = call_deepseek(optimized_text, api_key, model)
    except DeepSeekParseError as exc:
        logger.error(
            "%s: DeepSeek response could not be parsed after retries: %s -> status 2",
            company_name,
            exc,
        )
        update_status(
            conn,
            row["id"],
            CompanyStatus.PARSING_FAILED,
            impressum_url=impressum_url,
        )
        return CompanyStatus.PARSING_FAILED
    except DeepSeekError as exc:
        logger.error("%s: DeepSeek extraction failed: %s -> status 4", company_name, exc)
        update_status(conn, row["id"], CompanyStatus.ERROR, impressum_url=impressum_url)
        return CompanyStatus.ERROR

    status = decide_status(data)
    legal_name = data.get("company_name") or company_name
    postal_address = flatten_address(data)
    update_enriched(
        conn,
        row["id"],
        status,
        impressum_url,
        legal_name,
        postal_address,
        data,
    )
    logger.info(
        "%s: status %s (%s), legal_name=%r, address=%r",
        company_name,
        int(status),
        label(status),
        legal_name,
        postal_address,
    )
    return status


def main(argv=None):
    load_dotenv(BASE_DIR / ".env")
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("enrich_companies")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        logger.error("DEEPSEEK_API_KEY is not set. Provide it via environment or .env")
        return 2
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    try:
        conn = psycopg2.connect(**db_config())
    except psycopg2.Error as exc:
        logger.error("Could not connect to PostgreSQL: %s", exc)
        return 2

    conn.cursor_factory = psycopg2.extras.DictCursor
    counts = {}
    try:
        rows = select_companies(conn, args)
        logger.info("Selected %d companies for enrichment", len(rows))
        if not rows:
            logger.info("Nothing to process")
            return 0

        for index, row in enumerate(rows, 1):
            logger.info("[%d/%d] Processing %s", index, len(rows), row["company_name"])
            try:
                status = process_company(conn, row, api_key, model, logger)
            except Exception as exc:
                logger.exception("%s: unexpected error: %s", row["company_name"], exc)
                try:
                    conn.rollback()
                    update_status(conn, row["id"], CompanyStatus.ERROR)
                except Exception:
                    conn.rollback()
                status = CompanyStatus.ERROR
            counts[status] = counts.get(status, 0) + 1
            time.sleep(REQUEST_DELAY)

        logger.info(
            "Summary: processed=%d ok=%d address_incomplete=%d parsing_failed=%d "
            "no_impressum=%d no_website=%d error=%d",
            len(rows),
            counts.get(CompanyStatus.OK, 0),
            counts.get(CompanyStatus.ADDRESS_INCOMPLETE, 0),
            counts.get(CompanyStatus.PARSING_FAILED, 0),
            counts.get(CompanyStatus.NO_IMPRESSUM_LINK_FOUND, 0),
            counts.get(CompanyStatus.NO_WEBSITE, 0),
            counts.get(CompanyStatus.ERROR, 0),
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
