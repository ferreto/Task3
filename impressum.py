import json
import logging
import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from company_status import CompanyStatus

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "8000"))
TOKEN_DIVISOR = 4
HEAD_MARGIN = 200
TAIL_MARGIN = 400
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "20"))
USER_AGENT = os.environ.get("USER_AGENT", "").strip() or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MAX_ATTEMPTS = 3
DEEPSEEK_BACKOFF = (1, 2)

SYSTEM_PROMPT = """You extract legally required company information ("Impressum" / "Imprint") from
German-language website text.

Return ONLY a valid JSON object (no markdown fences, no commentary) that parses
with json.loads. The JSON must have exactly these keys:

{
  "company_name": "<string or null>",
  "postal_address": {
    "street": "<string or null>",
    "house_no": "<string or null>",
    "postcode": "<string or null>",
    "city": "<string or null>",
    "country": "<string or null>"
  },
  "handelsregister": "<string or null>",
  "vat_number": "<string or null>",
  "phone": "<string or null>",
  "email": "<string or null>",
  "responsible_person": "<string or null>"
}

Rules:
- Extract ONLY information actually present in the text; use null when absent.
- Do not invent or guess values.
- phone: keep the international format when possible (e.g. +49 ...).
- vat_number: German VAT ids look like DE followed by 9 digits.
- email: lowercase.
- responsible_person: the named person or role legally responsible for the
  content (e.g. Geschaeftsfuehrer, Vorstand, "inhaltlich verantwortlich").
- Output valid JSON only."""

REMOVE_TAGS = ("script", "style", "noscript", "svg", "nav", "header", "footer")
# <form> is removed too, unless it holds more than this share of the page text.
FORM_MAX_TEXT_SHARE = 0.5
ADDRESS_KEYS = ("street", "house_no", "postcode", "city", "country")
FIELD_KEYS = (
    "company_name",
    "handelsregister",
    "vat_number",
    "phone",
    "email",
    "responsible_person",
)

HEAD_ANCHOR_RE = re.compile(
    r"(Impressum|Angaben gemäß §\s*5\s*(?:TMG|DDG)|Anbieter|Diensteanbieter)",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"(?:\+49|0049)[\d\s\-/().]{6,}")
VAT_RE = re.compile(r"\bDE\s?\d{9}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
REGISTRY_RE = re.compile(r"(?:(?:HRB|HRA)\s?\d+|Registergericht|Amtsgericht)", re.IGNORECASE)
RESPONSIBLE_RE = re.compile(
    r"(Verantwortlich|Vertreten durch|Geschäftsführer(?:in)?|Inhaltlich verantwortlich)",
    re.IGNORECASE,
)
TAIL_PATTERNS = (PHONE_RE, VAT_RE, EMAIL_RE, REGISTRY_RE, RESPONSIBLE_RE)

EXACT_BOILERPLATE = {
    "datenschutz",
    "datenschutzerklärung",
    "datenschutzhinweise",
    "agb",
    "allgemeine geschäftsbedingungen",
    "menü",
    "menu",
    "navigation",
    "zum inhalt springen",
    "startseite",
    "home",
    "kontakt",
    "contact",
    "newsletter",
    "login",
    "anmelden",
    "suche",
    "search",
    "deutsch",
    "english",
    "german",
}
LANGUAGE_PREFIX_RE = re.compile(r"^(deutsch|english|german|français|español)\b", re.IGNORECASE)
COOKIE_RE = re.compile(r"cookie", re.IGNORECASE)
COPYRIGHT_RE = re.compile(r"(^©|©\s*\d{4})")


class DeepSeekError(RuntimeError):
    pass


class DeepSeekParseError(DeepSeekError):
    pass


def normalize_url(web):
    if web is None:
        return None
    value = str(web).strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value.lstrip("/")
    parsed = urlparse(value)
    if not parsed.netloc:
        return None
    return value


def _make_soup(html):
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def fetch_html(url, timeout=None):
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        },
        timeout=timeout or REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.url, response.content


def _is_unusable_href(href):
    if not href:
        return True
    lowered = href.strip().lower()
    return (
        lowered.startswith("#")
        or lowered.startswith("mailto:")
        or lowered.startswith("javascript:")
        or lowered.startswith("tel:")
    )


def find_impressum_url(html, base_url):
    soup = _make_soup(html)
    candidates = []
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if _is_unusable_href(href):
            continue
        text = anchor.get_text(" ", strip=True).lower()
        candidates.append((href, text))

    for href, text in candidates:
        if text in ("impressum", "imprint"):
            return urljoin(base_url, href)

    for href, _text in candidates:
        lowered = href.lower()
        if "impressum" in lowered or "imprint" in lowered:
            return urljoin(base_url, href)

    return None


def _remove_forms(soup):
    """Drop <form> blocks (contact / newsletter forms) from the soup.

    Some sites (ASP.NET WebForms, for example) wrap the whole page in a single
    <form>. Removing it would leave no text at all, so a form that holds most of
    the page text is kept.
    """
    total = len(soup.get_text(" ", strip=True))
    for form in soup.find_all("form"):
        if form.decomposed:
            continue
        if len(form.get_text(" ", strip=True)) <= total * FORM_MAX_TEXT_SHARE:
            form.decompose()


def extract_visible_text(html):
    soup = _make_soup(html)
    for tag in soup.find_all(REMOVE_TAGS):
        tag.decompose()
    _remove_forms(soup)
    text = soup.get_text("\n")
    lines = []
    for line in text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def strip_boilerplate_lines(text):
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered in EXACT_BOILERPLATE:
            continue
        if COOKIE_RE.search(stripped):
            continue
        if COPYRIGHT_RE.search(stripped):
            continue
        if LANGUAGE_PREFIX_RE.match(stripped) and len(stripped) < 30:
            continue
        lines.append(stripped)
    return "\n".join(lines)


def estimate_tokens(text):
    return len(text) // TOKEN_DIVISOR


def _strong_signal_present(text):
    return any(pattern.search(text) for pattern in TAIL_PATTERNS)


def _stats(method, original, optimized):
    original_tokens = estimate_tokens(original)
    optimized_tokens = estimate_tokens(optimized)
    return {
        "method": method,
        "original_chars": len(original),
        "optimized_chars": len(optimized),
        "original_tokens": original_tokens,
        "optimized_tokens": optimized_tokens,
        "saved_tokens": max(0, original_tokens - optimized_tokens),
    }


def _head_tail_window(text, max_chars):
    head_len = max_chars * 2 // 3
    tail_len = max(0, max_chars - head_len)
    separator = "\n...\n"
    return text[:head_len] + separator + (text[-tail_len:] if tail_len else "")


def optimize_text(text, max_chars=None):
    if max_chars is None:
        max_chars = MAX_TEXT_CHARS

    stripped = (text or "").strip()
    if len(stripped) <= max_chars:
        stats = _stats("unchanged", stripped, stripped)
        logger.info(
            "Text optimization: method=%s chars=%d tokens~%d",
            stats["method"],
            stats["original_chars"],
            stats["original_tokens"],
        )
        return stripped, stats

    head_match = HEAD_ANCHOR_RE.search(stripped)
    start = max(0, head_match.start() - HEAD_MARGIN) if head_match else 0

    last_tail_end = None
    for pattern in TAIL_PATTERNS:
        for match in pattern.finditer(stripped):
            if last_tail_end is None or match.end() > last_tail_end:
                last_tail_end = match.end()

    if last_tail_end is not None:
        end = min(len(stripped), last_tail_end + TAIL_MARGIN)
        candidate = stripped[start:end]
        method = "sandwich"
        anchor_ok = head_match is None or bool(HEAD_ANCHOR_RE.search(candidate))
        if not (anchor_ok and _strong_signal_present(candidate)):
            candidate = stripped
            method = "full_text_fallback"
    else:
        candidate = stripped
        method = "full_text_fallback"

    if len(candidate) > max_chars:
        if method == "sandwich" or len(candidate) > max_chars * 2:
            candidate = _head_tail_window(candidate, max_chars)
            method = "window_fallback"

    stats = _stats(method, stripped, candidate)
    logger.info(
        "Text optimization: method=%s chars %d->%d tokens~%d->%d saved~%d",
        stats["method"],
        stats["original_chars"],
        stats["optimized_chars"],
        stats["original_tokens"],
        stats["optimized_tokens"],
        stats["saved_tokens"],
    )
    return candidate, stats


def _clean(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def has_any_field(data):
    if not isinstance(data, dict):
        return False
    if any(_clean(data.get(key)) for key in FIELD_KEYS):
        return True
    address = data.get("postal_address")
    if isinstance(address, dict):
        return any(_clean(address.get(key)) for key in ADDRESS_KEYS)
    return False


def is_address_complete(data):
    if not isinstance(data, dict):
        return False
    address = data.get("postal_address")
    if not isinstance(address, dict):
        return False
    required = ("street", "house_no", "postcode", "city")
    return all(_clean(address.get(key)) for key in required)


def flatten_address(data):
    if not isinstance(data, dict):
        return ""
    address = data.get("postal_address")
    if not isinstance(address, dict):
        return ""
    street_line = " ".join(
        part
        for part in (_clean(address.get("street")), _clean(address.get("house_no")))
        if part
    )
    city_line = " ".join(
        part
        for part in (_clean(address.get("postcode")), _clean(address.get("city")))
        if part
    )
    country = _clean(address.get("country"))
    return ", ".join(part for part in (street_line, city_line, country) if part)


def decide_status(data):
    if not has_any_field(data):
        return CompanyStatus.PARSING_FAILED
    if not is_address_complete(data):
        return CompanyStatus.ADDRESS_INCOMPLETE
    return CompanyStatus.OK


def _strip_json_fences(content):
    text = content.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json_object(content):
    """Return the JSON object contained in the model output, exactly as parsed."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Empty DeepSeek response")

    text = _strip_json_fences(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])

    if not isinstance(data, dict):
        raise ValueError("DeepSeek response is not a JSON object")
    return data


def normalize_extraction(data):
    address = data.get("postal_address")
    if not isinstance(address, dict):
        address = {}

    normalized = {
        "company_name": _clean(data.get("company_name")) or None,
        "postal_address": {
            key: (_clean(address.get(key)) or None) for key in ADDRESS_KEYS
        },
        "handelsregister": _clean(data.get("handelsregister")) or None,
        "vat_number": _clean(data.get("vat_number")) or None,
        "phone": _clean(data.get("phone")) or None,
        "email": _clean(data.get("email")) or None,
        "responsible_person": _clean(data.get("responsible_person")) or None,
    }
    if normalized["email"]:
        normalized["email"] = normalized["email"].lower()
    return normalized


def parse_extraction(content):
    return normalize_extraction(parse_json_object(content))


def _backoff(attempt):
    time.sleep(DEEPSEEK_BACKOFF[min(attempt - 1, len(DEEPSEEK_BACKOFF) - 1)])


def call_deepseek(text, api_key, model="deepseek-chat", timeout=60, with_raw=False):
    """Extract Impressum data with DeepSeek.

    Returns the normalized dict, or, with ``with_raw=True``, a tuple
    ``(normalized, raw)`` where ``raw`` is the JSON object exactly as the model
    returned it.
    """
    if not api_key:
        raise DeepSeekError("DEEPSEEK_API_KEY is not set")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Extract the Impressum data from the text below and return JSON:\n\n" + text,
            },
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 1024,
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(1, DEEPSEEK_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            last_error = DeepSeekError(f"DeepSeek request failed: {exc}")
            if attempt < DEEPSEEK_MAX_ATTEMPTS:
                _backoff(attempt)
                continue
            raise last_error from exc

        if response.status_code in (401, 403):
            raise DeepSeekError(
                f"DeepSeek authorization failed (HTTP {response.status_code})"
            )
        if response.status_code == 429 or response.status_code >= 500:
            last_error = DeepSeekError(f"DeepSeek HTTP {response.status_code}")
            if attempt < DEEPSEEK_MAX_ATTEMPTS:
                _backoff(attempt)
                continue
            raise last_error
        if response.status_code >= 400:
            raise DeepSeekError(
                f"DeepSeek HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            last_error = DeepSeekError(f"Malformed DeepSeek response: {exc}")
            if attempt < DEEPSEEK_MAX_ATTEMPTS:
                _backoff(attempt)
                continue
            raise last_error from exc

        try:
            raw = parse_json_object(content)
            normalized = normalize_extraction(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = DeepSeekParseError(f"Could not parse DeepSeek JSON: {exc}")
            if attempt < DEEPSEEK_MAX_ATTEMPTS:
                _backoff(attempt)
                continue
            raise last_error from exc
        return (normalized, raw) if with_raw else normalized

    raise last_error or DeepSeekError("DeepSeek request failed")
