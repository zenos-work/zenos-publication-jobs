# zenos-publications-jobs

Self-contained Python Cloudflare Worker for publication delivery automation:

- Weekly newsletter to newsletter subscribers.
- Monthly magazine/e-magazine/e-book to all known email addresses.
- PDF attachment delivery for monthly magazine.
- Future-ready publication approval workflow and delivery tracking.

## Runtime

- Python Worker (`src/index.py`)
- Cloudflare Workers + D1 binding

## Schedules (UTC)

- Weekly newsletter: `NEWSLETTER_WEEKLY_CRON` (default `30 18 * * 0`, Monday 00:00 IST)
- Monthly magazine: `MAGAZINE_MONTHLY_CRON` (default `30 18 1 * *`, 1st day 00:00 IST)

## Endpoints

- `GET /health`
- `GET /newsletter/subscribe?email=<email>&source=web`
- `GET /newsletter/unsubscribe?email=<email>&token=<signed-token>&source=web`
- `GET /jobs/run?job=weekly-newsletter`
- `GET /jobs/run?job=monthly-magazine`

Frontend integration:

- Use `VITE_PUBLICATIONS_JOBS_URL` in frontend and call:
  - `GET <VITE_PUBLICATIONS_JOBS_URL>/newsletter/subscribe?email=<email>&source=frontend-home`

## Delivery Rules

- Weekly newsletter recipients:
  - `newsletter_subscriptions.status = 'subscribed'`
- Monthly magazine recipients:
  - All active users from `users.email`
  - Plus subscribed newsletter emails
  - Dedupe by normalized lowercase email

## Magazine Generation (Initial Launch)

- Pulls top published content from API (`latest` + `trending`).
- Generates:
  - Cover-led issue title
  - Editorial preface
  - Index / TOC
  - Feature sections from top content
- Produces a simple multi-page PDF in-worker.
- Target pages configurable and clamped to launch range:
  - `MAGAZINE_MIN_PAGES` default 80
  - `MAGAZINE_MAX_PAGES` default 120
  - `MAGAZINE_TARGET_PAGES` default 80

## Future-Ready Approval

- `PUBLICATION_REQUIRE_APPROVAL=true` creates issue in `pending_review` and skips delivery.
- This is ready for future SUPERADMIN approval UI/API integration.

## Env Vars

See `.dev.vars.example`.

Key vars:

- `API_BASE_URL`
- `RESEND_API_KEY`
- `NEWSLETTER_FROM`
- `MAGAZINE_FROM`
- `PUBLICATIONS_BASE_URL` (used in newsletter footer unsubscribe links)
- `UNSUBSCRIBE_SIGNING_SECRET` (HMAC secret for signed unsubscribe tokens)
- `PUBLICATION_REQUIRE_APPROVAL`
- `NEWSLETTER_TOP_ARTICLE_LIMIT`
- `MAGAZINE_TOP_ARTICLE_LIMIT`

## Local Development

```bash
cd /mnt/ai-enterprise-machine-shared-disk/projects/zenos/zenos-publications-jobs
npm install
cp .dev.vars.example .dev.vars
npm run dev
```

## Deploy

```bash
npm run deploy
```

CI/CD workflow: `.github/workflows/deploy.yml`
