import base64
import hashlib
import hmac
import inspect
import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

from workers import WorkerEntrypoint, Response, fetch


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_response(data, status: int = 200) -> Response:
    return Response(
        json.dumps(data, indent=2),
        status=status,
        headers={"content-type": "application/json; charset=utf-8"},
    )


def _env_str(env, key: str, default: str = "") -> str:
    value = getattr(env, key, None)
    if value is None:
        return default
    return str(value)


def _to_positive_int(raw: str, fallback: int) -> int:
    try:
        value = int(raw)
        if value > 0:
            return value
    except Exception:
        pass
    return fallback


def _to_bool(raw: str, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


async def _awaitable(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def _unsubscribe_secret(env) -> str:
    return _env_str(env, "UNSUBSCRIBE_SIGNING_SECRET", "").strip()


def _unsubscribe_token_for_email(env, email: str) -> str:
    secret = _unsubscribe_secret(env)
    if not secret:
        return ""
    digest = hmac.new(secret.encode("utf-8"), _normalize_email(email).encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _is_valid_unsubscribe_token(env, email: str, token: str) -> bool:
    secret = _unsubscribe_secret(env)
    if not secret:
        return True
    expected = _unsubscribe_token_for_email(env, email)
    return bool(token) and hmac.compare_digest(expected, token)


def _build_unsubscribe_url(env, email: str) -> str:
    base = _env_str(env, "PUBLICATIONS_BASE_URL", "").strip().rstrip("/")
    token = _unsubscribe_token_for_email(env, email)
    query_parts = [f"email={quote(_normalize_email(email), safe='')}"]
    if token:
        query_parts.append(f"token={quote(token, safe='')}")
    query_parts.append("source=email-footer")
    path = f"/newsletter/unsubscribe?{'&'.join(query_parts)}"
    if base:
        return f"{base}{path}"
    return path


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or str(uuid.uuid4())


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


async def _d1_all(env, sql: str, params: tuple = ()) -> list[dict]:
    stmt = env.DB.prepare(sql)
    if params:
        stmt = stmt.bind(*params)
    result = await _awaitable(stmt.all())
    if isinstance(result, dict):
        return result.get("results", []) or []
    rows = getattr(result, "results", None)
    return rows or []


async def _d1_run(env, sql: str, params: tuple = ()):
    stmt = env.DB.prepare(sql)
    if params:
        stmt = stmt.bind(*params)
    return await _awaitable(stmt.run())


async def _fetch_json(url: str):
    response = await fetch(url)
    if not response.ok:
        raise Exception(f"fetch failed {url} ({int(response.status)})")
    text = await response.text()
    try:
        return json.loads(text)
    except Exception:
        return {}


def _extract_articles(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("articles", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _article_title(article: dict) -> str:
    return str(article.get("title") or "Untitled Story").strip()


def _article_author(article: dict) -> str:
    if isinstance(article.get("author"), dict):
        return str(article.get("author", {}).get("name") or "Zenos Contributor")
    return str(article.get("author_name") or "Zenos Contributor")


def _article_excerpt(article: dict) -> str:
    for key in ("excerpt", "summary", "subtitle", "description"):
        value = article.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    body = article.get("content") or article.get("body")
    if isinstance(body, str):
        clean = re.sub(r"\s+", " ", body).strip()
        return clean[:420] + ("..." if len(clean) > 420 else "")
    return "A curated editorial pick from the Zenos newsroom."


async def _fetch_top_articles(env, limit: int) -> list[dict]:
    api_base = _env_str(env, "API_BASE_URL", "https://api.zenos.work").rstrip("/")
    latest_url = f"{api_base}/api/articles?status=published&sort=latest&limit={limit}"
    trending_url = f"{api_base}/api/articles?status=published&sort=trending&limit={limit}"

    latest = _extract_articles(await _fetch_json(latest_url))
    trending = _extract_articles(await _fetch_json(trending_url))

    merged = []
    seen = set()
    for article in latest + trending:
        key = str(article.get("id") or article.get("slug") or _article_title(article)).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(article)
        if len(merged) >= limit:
            break

    return merged


async def _newsletter_recipients(env) -> list[str]:
    rows = await _d1_all(
        env,
        """
        SELECT email
        FROM newsletter_subscriptions
        WHERE status = 'subscribed'
        ORDER BY created_at ASC
        """,
    )
    emails = []
    for row in rows:
        email = _normalize_email(str(row.get("email") or ""))
        if _valid_email(email):
            emails.append(email)
    return sorted(list(set(emails)))


async def _all_recipients(env) -> list[str]:
    user_rows = await _d1_all(
        env,
        """
        SELECT email
        FROM users
        WHERE email IS NOT NULL
          AND TRIM(email) <> ''
          AND (is_active IS NULL OR is_active = 1)
        """,
    )
    newsletter_rows = await _d1_all(
        env,
        """
        SELECT email
        FROM newsletter_subscriptions
        WHERE status = 'subscribed'
        """,
    )

    emails = set()
    for row in user_rows + newsletter_rows:
        email = _normalize_email(str(row.get("email") or ""))
        if _valid_email(email):
            emails.add(email)
    return sorted(list(emails))


def _build_newsletter_html(period_label: str, articles: list[dict], unsubscribe_url: str) -> str:
    cards = []
    for idx, article in enumerate(articles, start=1):
        cards.append(
            f"""
            <div style=\"padding:14px;border:1px solid #e4e4e4;border-radius:10px;margin-bottom:12px;\">
              <div style=\"font-size:12px;color:#666;\">Feature {idx}</div>
              <h3 style=\"margin:4px 0 6px;font-size:18px;\">{_article_title(article)}</h3>
              <p style=\"margin:0 0 6px;color:#444;\">{_article_excerpt(article)}</p>
              <div style=\"font-size:12px;color:#777;\">By {_article_author(article)}</div>
            </div>
            """
        )

    return f"""
    <!doctype html>
    <html><body style=\"font-family:Arial,Helvetica,sans-serif;background:#f8f8f8;padding:16px;\">
      <div style=\"max-width:720px;margin:0 auto;background:#fff;border:1px solid #ddd;border-radius:12px;padding:20px;\">
        <h1 style=\"margin-top:0;\">Zenos Weekly Newsletter</h1>
        <p style=\"color:#666;\">{period_label}</p>
        <p>Highlights from the editorial desk: top stories, writer insights, and what to read next.</p>
        {''.join(cards)}
                <hr style=\"border:none;border-top:1px solid #e5e5e5;margin:18px 0;\" />
                <p style=\"margin:0;color:#777;font-size:12px;\">
                    Prefer fewer emails?
                    <a href=\"{unsubscribe_url}\" style=\"color:#0a66c2;\">Unsubscribe</a>
                </p>
      </div>
    </body></html>
    """


def _build_newsletter_text(period_label: str, articles: list[dict], unsubscribe_url: str) -> str:
    lines = [
        "Zenos Weekly Newsletter",
        period_label,
        "",
        "Top Stories:",
    ]
    for idx, article in enumerate(articles, start=1):
        lines.append(f"{idx}. {_article_title(article)}")
        lines.append(f"   By {_article_author(article)}")
        lines.append(f"   {_article_excerpt(article)}")
    lines.append("")
    lines.append(f"Unsubscribe: {unsubscribe_url}")
    return "\n".join(lines)


def _paginate_text(text: str, chars_per_page: int) -> list[str]:
    normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not normalized:
        return ["Zenos Magazine\n\nNo content available."]

    pages = []
    start = 0
    length = len(normalized)
    while start < length:
        end = min(start + chars_per_page, length)
        if end < length:
            split = normalized.rfind("\n", start, end)
            if split <= start + 200:
                split = normalized.rfind(" ", start, end)
            if split > start:
                end = split
        chunk = normalized[start:end].strip()
        if chunk:
            pages.append(chunk)
        start = end
    return pages or [normalized]


def _build_magazine_document(title: str, month_label: str, articles: list[dict], target_pages: int) -> tuple[str, list[str]]:
    preface = (
        f"{title}\n"
        f"Edition: {month_label}\n\n"
        "Editorial Preface\n"
        "This issue brings together the strongest ideas from our publishing network. "
        "We highlight durable thinking, practical depth, and multi-domain perspectives from the community.\n\n"
    )

    toc_lines = ["Index", "-----"]
    for idx, article in enumerate(articles, start=1):
        toc_lines.append(f"{idx:02d}. {_article_title(article)} - {_article_author(article)}")

    body_sections = []
    for idx, article in enumerate(articles, start=1):
        section = (
            f"Section {idx}: {_article_title(article)}\n"
            f"By {_article_author(article)}\n\n"
            f"{_article_excerpt(article)}\n\n"
            f"Extended Editorial Notes:\n"
            f"{_article_excerpt(article)}\n"
            f"{_article_excerpt(article)}\n\n"
        )
        body_sections.append(section)

    full_text = preface + "\n".join(toc_lines) + "\n\n" + "\n".join(body_sections)
    pages = _paginate_text(full_text, 2300)

    filler_seed = (
        "Editorial Digest\n"
        "We continue to synthesize perspectives from technology, product strategy, research methods, "
        "and domain-first writing practices to create practical value for readers and teams.\n\n"
    )
    while len(pages) < target_pages:
        pages.append(filler_seed + f"Supplementary page {len(pages) + 1}.\n")

    if len(pages) > target_pages:
        pages = pages[:target_pages]

    return full_text, pages


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _simple_pdf_from_pages(title: str, pages: list[str]) -> bytes:
    objects = {}
    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>"

    font_obj = 3
    objects[font_obj] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    page_obj_nums = []
    content_obj_nums = []

    for i, page in enumerate(pages):
        content_num = 4 + (i * 2)
        page_num = 5 + (i * 2)
        content_obj_nums.append(content_num)
        page_obj_nums.append(page_num)

        lines = [line[:110] for line in page.split("\n")]
        ops = ["BT", "/F1 10 Tf", "42 770 Td", "13 TL"]
        for line in lines[:58]:
            ops.append(f"({_escape_pdf_text(line)}) Tj")
            ops.append("T*")
        ops.append("ET")
        stream = "\n".join(ops)
        encoded = stream.encode("latin-1", errors="replace")
        objects[content_num] = f"<< /Length {len(encoded)} >>\nstream\n{stream}\nendstream"
        objects[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> /Contents {content_num} 0 R >>"
        )

    kids = " ".join([f"{num} 0 R" for num in page_obj_nums])
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_obj_nums)} >>"

    max_obj = max(objects.keys())
    buf = bytearray()
    buf.extend(b"%PDF-1.4\n")
    offsets = [0] * (max_obj + 1)

    for obj_num in range(1, max_obj + 1):
        content = objects[obj_num]
        offsets[obj_num] = len(buf)
        buf.extend(f"{obj_num} 0 obj\n".encode("latin-1"))
        buf.extend(content.encode("latin-1", errors="replace"))
        buf.extend(b"\nendobj\n")

    xref_start = len(buf)
    buf.extend(f"xref\n0 {max_obj + 1}\n".encode("latin-1"))
    buf.extend(b"0000000000 65535 f \n")
    for obj_num in range(1, max_obj + 1):
        buf.extend(f"{offsets[obj_num]:010d} 00000 n \n".encode("latin-1"))

    trailer = (
        f"trailer\n<< /Size {max_obj + 1} /Root 1 0 R /Info << /Title ({_escape_pdf_text(title)}) >> >>\n"
        f"startxref\n{xref_start}\n%%EOF\n"
    )
    buf.extend(trailer.encode("latin-1", errors="replace"))
    return bytes(buf)


async def _create_issue(env, issue_type: str, title: str, period_start: str, period_end: str, preface: str, toc_json: str, total_pages: int, metadata_json: str, status: str) -> str:
    issue_id = str(uuid.uuid4())
    slug = _slugify(f"{issue_type}-{title}-{period_start[:10]}")
    await _d1_run(
        env,
        """
        INSERT INTO publication_issues (
          id, issue_type, title, slug, period_start, period_end, status,
          editorial_preface, toc_json, total_pages, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            issue_id,
            issue_type,
            title,
            slug,
            period_start,
            period_end,
            status,
            preface,
            toc_json,
            total_pages,
            metadata_json,
            _now_iso(),
            _now_iso(),
        ),
    )
    return issue_id


async def _record_delivery(env, issue_id: str, email: str, status: str, provider: str = "resend", provider_message_id: str = "", error_text: str = ""):
    await _d1_run(
        env,
        """
        INSERT INTO publication_deliveries (
          id, issue_id, email, channel, status, provider, provider_message_id,
          error_text, sent_at, last_attempt_at, attempt_count, created_at, updated_at
        ) VALUES (?, ?, ?, 'email', ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            issue_id,
            email,
            status,
            provider,
            provider_message_id,
            error_text,
            _now_iso() if status == "sent" else None,
            _now_iso(),
            _now_iso(),
            _now_iso(),
        ),
    )


async def _send_resend(env, sender: str, recipient: str, subject: str, html: str, text: str, attachment_name: str | None = None, attachment_bytes: bytes | None = None) -> dict:
    api_key = _env_str(env, "RESEND_API_KEY", "")
    if not api_key:
        raise Exception("RESEND_API_KEY is not configured")

    payload = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if attachment_name and attachment_bytes:
        payload["attachments"] = [
            {
                "filename": attachment_name,
                "content": base64.b64encode(attachment_bytes).decode("ascii"),
            }
        ]

    response = await fetch(
        "https://api.resend.com/emails",
        method="POST",
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        body=json.dumps(payload),
    )
    text_body = await response.text()
    if not response.ok:
        raise Exception(f"email send failed ({int(response.status)}): {text_body}")

    try:
        data = json.loads(text_body)
    except Exception:
        data = {"raw": text_body}
    return data if isinstance(data, dict) else {"raw": data}


async def _run_weekly_newsletter(env, source: str = "scheduled") -> dict:
    started = _now_iso()
    period_label = datetime.now(timezone.utc).strftime("Week of %d %b %Y")
    top_limit = _to_positive_int(_env_str(env, "NEWSLETTER_TOP_ARTICLE_LIMIT", "12"), 12)

    articles = await _fetch_top_articles(env, top_limit)
    recipients = await _newsletter_recipients(env)

    issue_title = f"Zenos Weekly Newsletter - {period_label}"
    toc = [{"position": i + 1, "title": _article_title(a)} for i, a in enumerate(articles)]
    issue_status = "published"

    issue_id = await _create_issue(
        env,
        issue_type="newsletter",
        title=issue_title,
        period_start=started,
        period_end=started,
        preface="Weekly editorial digest for subscribers.",
        toc_json=json.dumps(toc),
        total_pages=0,
        metadata_json=json.dumps({"source": source}),
        status=issue_status,
    )

    sender = _env_str(env, "NEWSLETTER_FROM", _env_str(env, "RESEND_FROM", "newsletter@zenos.work"))

    sent = 0
    failed = 0
    for recipient in recipients:
        try:
            unsubscribe_url = _build_unsubscribe_url(env, recipient)
            html = _build_newsletter_html(period_label, articles, unsubscribe_url)
            text = _build_newsletter_text(period_label, articles, unsubscribe_url)
            data = await _send_resend(env, sender, recipient, issue_title, html, text)
            await _record_delivery(env, issue_id, recipient, "sent", provider_message_id=str(data.get("id", "")))
            sent += 1
        except Exception as error:
            await _record_delivery(env, issue_id, recipient, "failed", error_text=str(error))
            failed += 1

    summary = f"newsletter delivered: sent={sent}, failed={failed}, recipients={len(recipients)}"
    return {
        "ok": failed == 0,
        "job": "weekly-newsletter",
        "startedAt": started,
        "finishedAt": _now_iso(),
        "summary": summary,
        "details": {
            "issueId": issue_id,
            "recipientCount": len(recipients),
            "sent": sent,
            "failed": failed,
            "articles": len(articles),
        },
    }


async def _run_monthly_magazine(env, source: str = "scheduled") -> dict:
    started = _now_iso()
    month_label = datetime.now(timezone.utc).strftime("%B %Y")

    top_limit = _to_positive_int(_env_str(env, "MAGAZINE_TOP_ARTICLE_LIMIT", "60"), 60)
    target_pages = _to_positive_int(_env_str(env, "MAGAZINE_TARGET_PAGES", "80"), 80)
    min_pages = _to_positive_int(_env_str(env, "MAGAZINE_MIN_PAGES", "80"), 80)
    max_pages = _to_positive_int(_env_str(env, "MAGAZINE_MAX_PAGES", "120"), 120)
    target_pages = _clamp(target_pages, min_pages, max_pages)

    articles = await _fetch_top_articles(env, top_limit)
    recipients = await _all_recipients(env)

    magazine_title = f"Zenos Monthly Magazine - {month_label}"
    full_text, pages = _build_magazine_document(magazine_title, month_label, articles, target_pages)
    pdf_bytes = _simple_pdf_from_pages(magazine_title, pages)

    toc = [{"position": i + 1, "title": _article_title(a)} for i, a in enumerate(articles)]
    require_approval = _to_bool(_env_str(env, "PUBLICATION_REQUIRE_APPROVAL", "false"), False)
    issue_status = "pending_review" if require_approval else "published"

    issue_id = await _create_issue(
        env,
        issue_type="magazine",
        title=magazine_title,
        period_start=started,
        period_end=started,
        preface="Monthly editorial preface generated from top content and platform signals.",
        toc_json=json.dumps(toc),
        total_pages=len(pages),
        metadata_json=json.dumps({"source": source, "targetPages": target_pages, "pdfSizeBytes": len(pdf_bytes)}),
        status=issue_status,
    )

    if require_approval:
        return {
            "ok": True,
            "job": "monthly-magazine",
            "startedAt": started,
            "finishedAt": _now_iso(),
            "summary": "issue generated and waiting for approval",
            "details": {
                "issueId": issue_id,
                "status": issue_status,
                "pages": len(pages),
                "recipientCount": len(recipients),
            },
        }

    sender = _env_str(env, "MAGAZINE_FROM", _env_str(env, "RESEND_FROM", "magazine@zenos.work"))
    subject = f"{magazine_title} (PDF)"
    html = (
        f"<h1>{magazine_title}</h1>"
        f"<p>Attached is this month's e-magazine PDF edition ({len(pages)} pages).</p>"
        "<p>This issue includes editorial preface, index, cover-led top stories, and curated features.</p>"
    )
    text = (
        f"{magazine_title}\n"
        f"Attached PDF pages: {len(pages)}\n"
        "Includes preface, index, and top curated stories."
    )

    sent = 0
    failed = 0
    filename = f"zenos-magazine-{datetime.now(timezone.utc).strftime('%Y-%m')}.pdf"
    for recipient in recipients:
        try:
            data = await _send_resend(
                env,
                sender,
                recipient,
                subject,
                html,
                text,
                attachment_name=filename,
                attachment_bytes=pdf_bytes,
            )
            await _record_delivery(env, issue_id, recipient, "sent", provider_message_id=str(data.get("id", "")))
            sent += 1
        except Exception as error:
            await _record_delivery(env, issue_id, recipient, "failed", error_text=str(error))
            failed += 1

    summary = f"magazine delivered: sent={sent}, failed={failed}, recipients={len(recipients)}, pages={len(pages)}"
    return {
        "ok": failed == 0,
        "job": "monthly-magazine",
        "startedAt": started,
        "finishedAt": _now_iso(),
        "summary": summary,
        "details": {
            "issueId": issue_id,
            "recipientCount": len(recipients),
            "sent": sent,
            "failed": failed,
            "pages": len(pages),
            "pdfSizeBytes": len(pdf_bytes),
            "articles": len(articles),
            "documentBytesPreview": full_text[:1800],
        },
    }


async def _trigger_e2e_runner(env) -> dict:
    webhook_url = _env_str(env, "E2E_RUNNER_WEBHOOK_URL", "")
    if not webhook_url:
        return {"ok": False, "error": "E2E_RUNNER_WEBHOOK_URL is not configured", "triggered": False}

    try:
        headers = {"content-type": "application/json"}
        auth_header = _env_str(env, "E2E_RUNNER_AUTH_HEADER", "").strip()
        auth_token = _env_str(env, "E2E_RUNNER_AUTH_TOKEN", "").strip()
        if auth_header and auth_token:
            headers[auth_header] = auth_token

        payload = {
            "source": "zenos-publication-jobs",
            "task": "weekly-e2e",
            "command": "./scripts/run-e2e.sh --ci",
            "requestedAt": _now_iso(),
            "timezone": "Asia/Kolkata",
            "schedule": "Sunday 00:00 IST",
        }

        response = await fetch(webhook_url, method="POST", headers=headers, body=json.dumps(payload))
        text = await response.text()
        return {
            "ok": response.status == 200,
            "status": response.status,
            "triggered": True,
            "response": text[:500] if text else "",
        }
    except Exception as error:
        return {"ok": False, "error": str(error), "triggered": False}


async def _upsert_subscription(env, email: str, status: str, source: str = "web"):
    normalized = _normalize_email(email)
    if not _valid_email(normalized):
        raise Exception("invalid email")

    existing = await _d1_all(env, "SELECT id FROM newsletter_subscriptions WHERE email = ? LIMIT 1", (normalized,))
    now = _now_iso()
    if existing:
        unsubscribed_at = now if status == "unsubscribed" else None
        confirmed_at = now if status == "subscribed" else None
        await _d1_run(
            env,
            """
            UPDATE newsletter_subscriptions
            SET status = ?, source = ?, updated_at = ?,
                unsubscribed_at = COALESCE(?, unsubscribed_at),
                confirmed_at = COALESCE(?, confirmed_at)
            WHERE email = ?
            """,
            (status, source, now, unsubscribed_at, confirmed_at, normalized),
        )
        return

    await _d1_run(
        env,
        """
        INSERT INTO newsletter_subscriptions (id, email, status, source, confirmed_at, unsubscribed_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            normalized,
            status,
            source,
            now if status == "subscribed" else None,
            now if status == "unsubscribed" else None,
            now,
            now,
        ),
    )


async def _run_course_certificate_job(env, source: str = "manual") -> dict:
    started_at = _now_iso()
    limit = _to_positive_int(_env_str(env, "COURSE_CERTIFICATE_JOB_LIMIT", "200"), 200)
    certificate_base = _env_str(env, "COURSE_CERTIFICATE_BASE_URL", "https://zenos.work/certificates").rstrip("/")

    rows = await _d1_all(
        env,
        """
        SELECT ce.id AS enrollment_id, ce.course_id, ce.user_id
        FROM course_enrollments ce
        LEFT JOIN certificates cert ON cert.enrollment_id = ce.id
        WHERE ce.status = 'completed'
          AND cert.id IS NULL
        ORDER BY ce.completed_at DESC, ce.enrolled_at DESC
        LIMIT ?
        """,
        (limit,),
    )

    issued = 0
    failures = []
    for row in rows:
        try:
            cert_id = str(uuid.uuid4())
            cert_url = f"{certificate_base}/{cert_id}"
            await _d1_run(
                env,
                """
                INSERT INTO certificates (id, course_id, user_id, enrollment_id, certificate_url, issued_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    cert_id,
                    str(row.get("course_id") or ""),
                    str(row.get("user_id") or ""),
                    str(row.get("enrollment_id") or ""),
                    cert_url,
                ),
            )
            issued += 1
        except Exception as error:
            failures.append(
                {
                    "enrollment_id": row.get("enrollment_id"),
                    "error": str(error),
                }
            )

    return {
        "ok": len(failures) == 0,
        "job": "course-certificates",
        "startedAt": started_at,
        "finishedAt": _now_iso(),
        "summary": f"Issued {issued} certificates, {len(failures)} failures (source={source}).",
        "details": {
            "source": source,
            "candidates": len(rows),
            "issued": issued,
            "failures": failures,
        },
    }


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        parsed = urlparse(str(request.url))
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            return _json_response(
                {
                    "ok": True,
                    "service": "zenos-publications-jobs",
                    "runtime": "python",
                    "environment": _env_str(self.env, "ENVIRONMENT", "unknown"),
                }
            )

        if path == "/newsletter/subscribe":
            email = (query.get("email", [""])[0] or "").strip()
            source = (query.get("source", ["web"])[0] or "web").strip()[:40]
            try:
                await _upsert_subscription(self.env, email, "subscribed", source)
                return _json_response({"ok": True, "email": _normalize_email(email), "status": "subscribed"})
            except Exception as error:
                return _json_response({"ok": False, "error": str(error)}, status=400)

        if path == "/newsletter/unsubscribe":
            email = (query.get("email", [""])[0] or "").strip()
            token = (query.get("token", [""])[0] or "").strip()
            source = (query.get("source", ["web"])[0] or "web").strip()[:40]
            try:
                if not _is_valid_unsubscribe_token(self.env, email, token):
                    return _json_response({"ok": False, "error": "invalid unsubscribe token"}, status=400)
                await _upsert_subscription(self.env, email, "unsubscribed", source)
                return _json_response({"ok": True, "email": _normalize_email(email), "status": "unsubscribed"})
            except Exception as error:
                return _json_response({"ok": False, "error": str(error)}, status=400)

        if path == "/jobs/run":
            job = (query.get("job", [""])[0] or "").strip().lower()
            source = (query.get("source", ["manual"])[0] or "manual").strip().lower()

            try:
                if job == "weekly-newsletter":
                    result = await _run_weekly_newsletter(self.env, source=source)
                elif job in {"monthly-magazine", "monthly-ebook", "monthly-emagazine"}:
                    result = await _run_monthly_magazine(self.env, source=source)
                elif job in {"course-certificates", "courses-certificates"}:
                    result = await _run_course_certificate_job(self.env, source=source)
                else:
                    return _json_response(
                        {
                            "ok": False,
                            "error": "unknown job",
                            "allowed": ["weekly-newsletter", "monthly-magazine", "course-certificates"],
                        },
                        status=400,
                    )
                return _json_response(result, status=200 if result.get("ok") else 500)
            except Exception as error:
                return _json_response({"ok": False, "error": str(error)}, status=500)

        return _json_response(
            {
                "ok": True,
                "service": "zenos-publications-jobs",
                "endpoints": [
                    "/health",
                    "/newsletter/subscribe?email=<email>",
                    "/newsletter/unsubscribe?email=<email>&token=<signed-token>",
                    "/jobs/run?job=weekly-newsletter",
                    "/jobs/run?job=monthly-magazine",
                    "/jobs/run?job=course-certificates",
                ],
            }
        )

    async def scheduled(self, controller, _ctx):
        cron = str(getattr(controller, "cron", ""))
        weekly = _env_str(self.env, "NEWSLETTER_WEEKLY_CRON", "30 18 * * 0")
        monthly = _env_str(self.env, "MAGAZINE_MONTHLY_CRON", "30 18 1 * *")

        try:
            if cron == weekly:
                result = await _run_weekly_newsletter(self.env, source="scheduled")
                # Trigger E2E tests after weekly newsletter
                e2e_result = await _trigger_e2e_runner(self.env)
                result["e2e_trigger"] = e2e_result
            elif cron == monthly:
                result = await _run_monthly_magazine(self.env, source="scheduled")
            else:
                result = {"ok": False, "error": f"no mapped job for cron {cron}"}
            print(json.dumps(result))
        except Exception as error:
            print(json.dumps({"ok": False, "job": "scheduled", "error": str(error)}))
