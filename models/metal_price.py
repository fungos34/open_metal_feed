import logging
from datetime import timedelta
from urllib.parse import urlsplit

import requests

from odoo import api, fields, models
from odoo.exceptions import AccessError

from ..feed import FeedError, cache_seconds, parse_quote

_logger = logging.getLogger(__name__)
_FETCH_LOCK = 1948062101
DEFAULT_BASE_URL = 'https://api.gold-api.com'


def normalize_base_url(url):
    url = (url or DEFAULT_BASE_URL).strip().rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or '@' in parsed.netloc \
            or parsed.query or parsed.fragment:
        raise FeedError('The API base URL must be a plain https:// URL without credentials')
    return url


class OpenMetalPrice(models.Model):
    _name = 'open.metal.price'
    _description = 'Metal Source Observation'
    _order = 'updated_at desc, id desc'
    _rec_name = 'asset_id'

    # Kept for upgrading the original overwrite-only feed without losing its data.
    name = fields.Char(string="Legacy name", readonly=True)
    symbol = fields.Char(string="Legacy symbol", readonly=True)
    currency = fields.Char(string="Legacy currency", readonly=True)

    asset_id = fields.Many2one('open.metal.asset', index=True, readonly=True, ondelete='restrict')
    price = fields.Float(string="Source price", digits=(24, 8), readonly=True, aggregator='avg')
    currency_id = fields.Many2one('res.currency', readonly=True, ondelete='restrict')
    updated_at = fields.Datetime(string="Market timestamp", index=True, readonly=True)
    fetched_at = fields.Datetime(string="Retrieved at", readonly=True)
    source_kind = fields.Selection(
        [('live', 'Live observation'), ('legacy', 'Legacy snapshot')],
        default='live', required=True, readonly=True,
    )
    unit = fields.Selection(
        [('provider_unit', 'Provider unit (unverified)')],
        string="Pricing unit", default='provider_unit', required=True, readonly=True,
    )
    provider_key = fields.Char(readonly=True, index=True, copy=False)

    _provider_key_unique = models.Constraint(
        'UNIQUE(provider_key)', 'This provider observation has already been collected.',
    )

    @api.model
    def action_fetch_all_prices(self):
        if not (self.env.user.has_group('open_metal_feed.group_metal_feed_manager')
                or self.env.user.has_group('base.group_system')):
            raise AccessError(self.env._("Only feed managers may refresh prices."))
        result = self.sudo()._collect_prices()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._("Metal feed"),
                'message': result,
                'type': 'warning',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    @api.model
    def _gold_api_get(self, path, params=None, authenticated=False, base_url=None, api_key=None):
        """GET from gold-api.com. The key is only sent to endpoints that require it,
        so keyless live prices never consume the keyed (rate-limited) quota."""
        config = self.env['ir.config_parameter'].sudo()
        url = normalize_base_url(base_url or config.get_param('open_metal_feed.base_url'))
        kwargs = {'timeout': (5, 10), 'allow_redirects': False}
        if params:
            kwargs['params'] = params
        if authenticated:
            api_key = (api_key or config.get_param('open_metal_feed.api_key') or '').strip()
            if not api_key:
                raise FeedError('No API key is configured')
            kwargs['headers'] = {'x-api-key': api_key}
        return requests.get(f'{url}/{path.lstrip("/")}', **kwargs)

    @api.model
    def _cron_fetch_prices(self):
        self.sudo()._collect_prices()

    @api.model
    def _collect_prices(self):
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s)', [_FETCH_LOCK])
        if not self.env.cr.fetchone()[0]:
            return self.env._("Another refresh is running. No API request was made.")

        companies = self.env['res.company'].search([('metal_feed_enabled', '=', True)])
        if not companies:
            return self.env._("Collection is disabled for all companies.")
        usd = self.env.ref('base.USD')
        assets = self.env['open.metal.asset'].search([])
        assets.invalidate_recordset()
        attempted = failed = 0
        for asset in assets:
            observation = self.browse()
            now = fields.Datetime.now()
            if not asset.next_fetch_at or asset.next_fetch_at <= now:
                attempted += 1
                asset.write({'last_attempt_at': now})
                try:
                    # Live prices are free and unlimited; never send the API key here.
                    response = self._gold_api_get(f'price/{asset.symbol}/USD')
                    with response:
                        if response.status_code != 200:
                            retry_after = response.headers.get('Retry-After', '')
                            raise FeedError(
                                f'HTTP {response.status_code}',
                                retry_after=retry_after,
                            )
                        quote = parse_quote(response.json(), asset.symbol, now)
                        delay = cache_seconds(response.headers.get('Cache-Control', ''))
                except (requests.RequestException, ValueError, FeedError) as error:
                    failed += 1
                    failures = min(asset.failure_count + 1, 10)
                    delay = min(3600, 60 * (2 ** failures))
                    if isinstance(error, FeedError):
                        delay = max(delay, error.retry_seconds(now))
                    _logger.warning("Metal feed %s: %s", asset.symbol, error)
                    asset.write({
                        'last_error': str(error),
                        'failure_count': failures,
                        'next_fetch_at': fields.Datetime.now() + timedelta(seconds=delay),
                    })
                else:
                    observation = self.search([('provider_key', '=', quote.key)], limit=1)
                    if not observation:
                        observation = self.create({
                            'asset_id': asset.id,
                            'currency_id': usd.id,
                            'price': quote.price,
                            'updated_at': quote.updated_at,
                            'fetched_at': fields.Datetime.now(),
                            'provider_key': quote.key,
                        })
                    values = {
                        'last_success_at': fields.Datetime.now(),
                        'last_error': False,
                        'failure_count': 0,
                        'next_fetch_at': fields.Datetime.now() + timedelta(seconds=delay),
                    }
                    if (not asset.latest_id or asset.latest_id.source_kind == 'legacy'
                            or observation.updated_at >= asset.latest_id.updated_at):
                        values['latest_id'] = observation.id
                    asset.write(values)
            (observation | asset.latest_id)._ensure_valuations(companies)

        pending = self.env['open.metal.valuation'].search([
            ('state', '=', 'unavailable'), ('company_id', 'in', companies.ids),
        ], order='last_attempt_at, id', limit=100)
        pending._try_convert()
        return self.env._(
            "%(attempted)s requests attempted; %(failed)s failed. "
            "Cached prices were reused where applicable. Check each asset's status.",
            attempted=attempted, failed=failed,
        )

    def _ensure_valuations(self, companies):
        Valuation = self.env['open.metal.valuation']
        for observation in self:
            for company in companies:
                existing = Valuation.search([
                    ('observation_id', '=', observation.id),
                    ('company_id', '=', company.id),
                    ('currency_id', '=', company.currency_id.id),
                ], limit=1)
                if not existing:
                    Valuation.create({
                        'observation_id': observation.id,
                        'company_id': company.id,
                        'currency_id': company.currency_id.id,
                    })._try_convert()

    @api.model
    def _migrate_legacy_prices(self):
        assets = self.env['open.metal.asset'].with_context(active_test=False).search([])
        by_symbol = {asset.symbol: asset for asset in assets}
        companies = self.env['res.company'].search([('metal_feed_enabled', '=', True)])
        for record in self.search([('asset_id', '=', False)]):
            asset = by_symbol.get(record.symbol)
            currency = self.env['res.currency'].with_context(active_test=False).search(
                [('name', '=', record.currency)], limit=1,
            )
            if not asset or not currency:
                _logger.warning("Unmapped legacy metal snapshot %s; retained unchanged", record.id)
                continue
            record.write({
                'asset_id': asset.id, 'currency_id': currency.id,
                'source_kind': 'legacy', 'fetched_at': record.updated_at or record.create_date,
                'updated_at': record.updated_at or record.create_date,
            })
            if (not asset.latest_id or (
                    asset.latest_id.source_kind == 'legacy'
                    and record.updated_at > asset.latest_id.updated_at)):
                asset.latest_id = record
            record._ensure_valuations(companies)
