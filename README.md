# Open Metal Feed (Odoo 19)

A free-only precious-metal dashboard with locally collected history and
company-currency valuations. Works with Odoo Community; no Enterprise charting
dependency is required.

## Community edition and version portability

The supported target is **Odoo 19 Community**. Dependencies are the standard
Community modules `base`, `web` and `base_setup` (the latter supplies the company
context for Settings). There is no dependency on Accounting, Studio, Enterprise
dashboards, spreadsheet, or a paid currency-rate provider. Views use standard
Community kanban, list, form, graph and pivot components; no custom JavaScript
or frontend build is needed.

For a port, keep the provider validation in `feed.py` unchanged where possible.
Version-sensitive integration points are deliberately kept in the models/views:

- SQL declarations use Odoo 19 `models.Constraint`; older releases may require
  `_sql_constraints`.
- Numeric aggregation uses `aggregator='avg'`; older releases may require
  `group_operator='avg'`.
- Currency rates follow Odoo 19 `company.root_id` and `res.currency._convert`.
  Verify rate precedence and company hierarchy semantics on the target version.
- Views use `<list>`, the kanban `card` template and Settings `<app>/<block>/<setting>`.
  Older versions may require `<tree>`, `kanban-box` and different Settings XML.
- The cron has no removed `numbercall` field; check the target scheduler API.
- Translation calls use `env._`; older releases may require imported `_`.
- Hooks/migrations receive an Odoo 19 environment; older hook signatures differ.
- Tests use Odoo 19 user `group_ids`; older releases use `groups_id`.

Keep a version-specific branch/manifest and migration directory for each supported
major version rather than runtime version detection. Validate that branch against
its own Community instance. Other Odoo versions are not claimed as tested.

## Install and configure

1. Put `open_metal_feed` on the Odoo addons path. Install the Python `requests`
   dependency in the Odoo environment, then install the app.
2. In **Metal Feed > Settings**, enable collection for each desired company.
   Collection defaults to enabled and the global interval defaults to five minutes.
   Disabling one company stops new valuations for that company; shared public
   observations continue while another company remains enabled.
3. Configure dated USD and company-currency rates in Odoo for non-USD valuations.
   Both currencies need a valid rate on or before the market observation's UTC
   date. As in Odoo core, the company currency without any rate record is the
   1.0 base (e.g. ECB rates for a EUR company only store the foreign currency).
   Company/root-company rates take precedence over shared rates, following
   Odoo conversion conventions. No exchange-rate subscription is enabled.
4. Gold, silver and platinum are active initially. Feed managers can enable
   palladium using the **Archived** dashboard search filter.
5. Open a metal for **Price trend (USD)**, **Price trend (company currency)**,
   **Source observations**, or manual refresh. Refresh uses the same cache and
   backoff as the scheduler.
6. Optional: with an API key, use **Metal Feed > Import History** to backfill
   historical prices (see below).

Administrators configure collection in Settings. The **Metal Feed Manager** group
can configure assets and refresh the provider. All internal users can read public
source observations and valuations for their allowed companies; they cannot
create, edit or delete observations/valuations. Company switching changes the
dashboard currency and valuation context. Names and interface text are
translatable, with German translations included; formatting and time zones use
the user's Odoo preferences.

## Free-only policy

The only request made by the regular collection is the keyless live price:

```
GET {base URL}/price/{XAU|XAG|XPT|XPD}/USD
```

The base URL (default `https://api.gold-api.com`, `https://` only, redirects are
never followed) and an optional API key are set in **Metal Feed > Settings > API
access** and stored as system parameters (`open_metal_feed.base_url`,
`open_metal_feed.api_key`) readable by administrators only.

The API key is sent as `x-api-key` **only** to endpoints that require it, through
`open.metal.price._gold_api_get(path, params, authenticated=True)`. Live prices
never carry the key, so they are not counted against the keyed quota. According
to the provider documentation, keyed endpoints (`/history`, `/ohlc/{symbol}`) are
limited to 10 requests per hour on the free tier; `hour`/`minute` history grouping
requires a premium plan. **Test API key** in Settings validates the key with one
small `/ohlc/XAU` request (one quota request).

**Historical import** (`GET /history`, keyed): **Metal Feed > Import History**
(feed managers/administrators) fetches one price per day, week or month (average)
for the selected metals and date range, using one request per metal. Defaults are
all active metals, daily, last five years; data back to 1970 is available. Only
completed periods are stored (`source_kind = history`, USD), existing periods are
never rewritten, and imported history never becomes the "latest" live price. A
daily scheduled action appends completed days since the last imported day
(30 days if none) when an API key is configured.

No premium groupings, subscriptions or paid fallbacks are used. Provider terms
can change; this module cannot guarantee a third party's future pricing. HTTP
failures remain visible and retain the last valid observation.

Requests are coordinated across workers **within one database**, cached for at
least 30 seconds (longer if `Cache-Control: max-age` says so), and backed off on
failure. `Retry-After` is respected. Separate databases/installations do not share
a cache. Transaction rollback can also roll back cache state; the provider's
documented keyless endpoint has no request quota.

The scheduler is periodic, not a streaming connection. The dashboard reads stored
data and does not poll the API on page load. Reload it to see newly collected
prices. Staleness is evaluated when data is read, using market timestamp and last
successful fetch; the threshold is three configured intervals, at least five
minutes. A provider can return an old timestamp even after a successful request.

## History and currencies

- Source observations retain eight decimal places and the original currency,
  provider timestamp and retrieval timestamp. Repeated provider timestamps for an
  asset/currency are deduplicated, not rewritten.
- Local history starts with collection; older periods can be imported with an
  API key (see above). Imported history is stored in USD only and creates **no**
  company valuations, so no historical exchange rates are needed.
- **Price trend** charts read the stored USD prices (live and imported history).
  **Price trend (USD)** is exact. **Price trend (company currency)** scales the
  whole trend with **today's** USD rate from Odoo (`res.currency._convert`); it
  shows the shape of the trend in that currency, not historical prices in that
  currency. It is refused if Odoo has no current rate. Values are converted for
  chart, pivot and the list's *Price* column; the stored price is unchanged.
- A company valuation snapshots the converted value, date, conversion multiplier
  and rate provenance. Later rate edits do not change successful valuations.
  A company currency change produces new valuations in the new currency going
  forward; it does not relabel or recalculate old valuations.
- Missing/invalid rates create an explicit unavailable valuation, not a value
  of zero or an assumed rate of one. Up to 100 pending valuations are retried per
  collection, oldest attempt first, after rates become available. These are
  valuations using subsequently configured **historically dated** rates, not
  quotes retrieved retroactively from the provider.
- Trend charts are opened per metal; the currency and today's rate are shown in
  the title. Reopen a chart after switching companies. **Company Valuations**
  remain available as a menu for exact, dated-rate conversions of live prices.
  Daily/weekly/monthly aggregation averages sampled prices rather than
  summing them. These are sample averages, not time-weighted market averages.
  Cross-currency/cross-asset overview menus are lists, not mixed-price charts.
- The supplied API contract does not specify a pricing unit. Until verified,
  prices are explicitly labeled **Provider unit (unverified)**. Do not use them
  as an ounce/gram quotation or for automated financial posting.

History is retained without automatic deletion. At five-minute collection with
three metals, allow roughly 315,000 source rows/year plus up to that many
valuation rows **per enabled company**, depending on provider updates.
Timestamp, asset, company and deduplication indexes support common queries.
Plan PostgreSQL storage, backups and an explicit archival policy before
high-frequency or long-running deployments.

## Upgrade from 19.0.1.0.0

Back up the database, deploy the new code, then upgrade `open_metal_feed`.
The migration retains original price rows as **Legacy snapshots** with their
original currency/value and retrieval timestamp. That timestamp is not claimed
to be a verified market timestamp. Unmappable legacy symbols/currencies are
retained unchanged and logged for administrator review.

The existing cron's code is upgraded despite its `noupdate` flag. The original
12-hour default becomes five minutes; other customized intervals and the active
flag are preserved. If your intentionally customized interval is exactly 12
hours, reapply it after upgrade. Prefer changing the cadence through Metal Feed
Settings so the staleness threshold stays synchronized.

## Validation

Standalone feed validation (no Odoo, network or third-party packages required):

```powershell
python -m unittest discover -s tests -p test_feed.py -v
```

Odoo integration tests with an isolated PostgreSQL test database:

```powershell
odoo-bin -d metal_feed_test -i open_metal_feed --test-enable --test-tags /open_metal_feed --stop-after-init
```

All provider calls in integration tests are mocked. Run an upgrade against a
backup of an old database too, and smoke-test English/German UI, company
switching, currency formatting, archived palladium activation and historical
graphs before deploying to production.
