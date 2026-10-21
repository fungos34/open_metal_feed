from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import Command, fields, sql_db
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged

_REQUEST = 'odoo.addons.open_metal_feed.models.metal_price.requests.get'


@tagged('post_install', '-at_install')
class TestMetalFeed(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Feed = cls.env['open.metal.price']
        cls.Asset = cls.env['open.metal.asset']
        cls.Valuation = cls.env['open.metal.valuation']
        cls.usd = cls.env.ref('base.USD')
        cls.eur = cls.env.ref('base.EUR')
        cls.eur.active = True
        cls.env['res.company'].search([]).write({'metal_feed_enabled': False})
        cls.company = cls.env['res.company'].create({
            'name': 'USD Metal Test', 'currency_id': cls.usd.id, 'metal_feed_enabled': True,
        })
        cls.other_company = cls.env['res.company'].create({
            'name': 'EUR Metal Test', 'currency_id': cls.eur.id, 'metal_feed_enabled': True,
        })
        cls.Asset.with_context(active_test=False).search([]).write({'active': False})
        cls.gold = cls.env.ref('open_metal_feed.asset_gold')
        cls.gold.write({
            'active': True, 'next_fetch_at': False, 'latest_id': False,
            'last_error': False, 'failure_count': 0,
        })

    def _response(self, timestamp=None, **changes):
        response = MagicMock()
        response.status_code = 200
        response.headers = {'Cache-Control': 'max-age=60'}
        payload = {
            'symbol': 'XAU', 'currency': 'USD', 'price': 4000.123456,
            'updatedAt': (timestamp or fields.Datetime.now()).isoformat() + 'Z',
        }
        payload.update(changes)
        response.json.return_value = payload
        return response

    def _rates(self, date):
        for currency, rate in ((self.usd, 1.0), (self.eur, 0.8)):
            self.env['res.currency.rate'].create({
                'currency_id': currency.id, 'company_id': self.other_company.id,
                'name': date, 'rate': rate,
            })

    def test_free_url_cache_precision_and_deduplication(self):
        stamp = fields.Datetime.now()
        response = self._response(stamp)
        with patch(_REQUEST, return_value=response) as get:
            self.Feed._collect_prices()
            first = self.gold.latest_id
            self.assertEqual(first.price, 4000.123456)
            self.assertEqual(first.updated_at, stamp)
            self.assertEqual(first.currency_id, self.usd)
            get.assert_called_once_with(
                'https://api.gold-api.com/price/XAU/USD',
                timeout=(5, 10), allow_redirects=False,
            )
            self.Feed._collect_prices()
            self.assertEqual(get.call_count, 1)
            self.gold.next_fetch_at = fields.Datetime.now() - timedelta(seconds=1)
            self.Feed._collect_prices()
            self.assertEqual(self.gold.latest_id, first)
            self.assertEqual(self.Feed.search_count([('provider_key', '=', first.provider_key)]), 1)

    def test_history_and_conversion_are_not_rewritten(self):
        stamp = fields.Datetime.now() - timedelta(minutes=1)
        self._rates(stamp.date())
        with patch(_REQUEST, return_value=self._response(stamp)):
            self.Feed._collect_prices()
        first = self.gold.latest_id
        valuation = self.Valuation.search([
            ('observation_id', '=', first.id), ('company_id', '=', self.other_company.id),
        ])
        self.assertEqual(valuation.state, 'ready')
        self.assertAlmostEqual(valuation.price, first.price * 0.8)
        self.assertTrue(valuation.rate_provenance)
        self.env['res.currency.rate'].search([
            ('company_id', '=', self.other_company.id), ('currency_id', '=', self.eur.id),
        ]).write({'rate': 0.7})
        valuation._try_convert()
        self.assertAlmostEqual(valuation.price, first.price * 0.8)
        self.gold.next_fetch_at = False
        with patch(_REQUEST, return_value=self._response(stamp + timedelta(seconds=30), price=4100)):
            self.Feed._collect_prices()
        self.assertNotEqual(self.gold.latest_id, first)
        self.assertTrue(first.exists())
        self.assertEqual(first.price, 4000.123456)

    def test_missing_rates_do_not_assume_parity_or_use_future_rates(self):
        stamp = fields.Datetime.now() - timedelta(days=3)
        self.env['res.currency.rate'].search([
            ('currency_id', 'in', (self.usd | self.eur).ids),
            ('company_id', 'in', [False, self.other_company.id]),
        ]).unlink()
        self._rates(stamp.date() + timedelta(days=1))
        with patch(_REQUEST, return_value=self._response(stamp)):
            self.Feed._collect_prices()
        valuation = self.Valuation.search([
            ('observation_id', '=', self.gold.latest_id.id),
            ('company_id', '=', self.other_company.id),
        ])
        self.assertEqual(valuation.state, 'unavailable')
        self.assertTrue(valuation.error)
        self.assertFalse(valuation.price)
        self._rates(stamp.date())
        valuation._try_convert()
        self.assertEqual(valuation.state, 'ready')
        self.assertAlmostEqual(valuation.conversion_rate, 0.8)

    def test_failures_preserve_price_backoff_and_visible_error(self):
        with patch(_REQUEST, return_value=self._response()):
            self.Feed._collect_prices()
        first = self.gold.latest_id
        self.gold.next_fetch_at = False
        with patch(_REQUEST, side_effect=requests.Timeout('Timed out')):
            self.Feed._collect_prices()
        self.assertEqual(self.gold.latest_id, first)
        self.assertEqual(self.gold.status, 'error')
        self.assertIn('Timed out', self.gold.last_error)
        self.assertGreater(self.gold.next_fetch_at, fields.Datetime.now())
        with patch(_REQUEST) as get:
            self.Feed._collect_prices()
            get.assert_not_called()

    def test_rate_limit_retry_after_and_invalid_payload(self):
        response = self._response()
        response.status_code = 429
        response.headers = {'Retry-After': '900'}
        with patch(_REQUEST, return_value=response):
            self.Feed._collect_prices()
        self.assertIn('429', self.gold.last_error)
        self.assertGreaterEqual(
            self.gold.next_fetch_at, self.gold.last_attempt_at + timedelta(seconds=900),
        )
        self.gold.next_fetch_at = False
        with patch(_REQUEST, return_value=self._response(currency='EUR')):
            self.Feed._collect_prices()
        self.assertIn('does not match', self.gold.last_error)
        self.assertFalse(self.gold.latest_id)

    def test_disabled_companies_make_no_requests(self):
        (self.company | self.other_company).write({'metal_feed_enabled': False})
        with patch(_REQUEST) as get:
            self.Feed._collect_prices()
            get.assert_not_called()

    def test_concurrent_worker_does_not_bypass_cache_lock(self):
        with sql_db.db_connect(self.cr.dbname).cursor() as other_cr:
            other_cr.execute('SELECT pg_advisory_xact_lock(%s)', [1948062101])
            with patch(_REQUEST) as get:
                result = self.Feed._collect_prices()
                self.assertIn('Another refresh', result)
                get.assert_not_called()

    def test_user_permissions_company_context_and_history_domain(self):
        with patch(_REQUEST, return_value=self._response()):
            self.Feed._collect_prices()
        user = self.env['res.users'].create({
            'name': 'Metal Reader', 'login': 'metal_reader_test',
            'company_id': self.company.id, 'company_ids': [Command.set([self.company.id])],
            'group_ids': [Command.set([self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            self.Feed.with_user(user).action_fetch_all_prices()
        with self.assertRaises(AccessError):
            self.gold.latest_id.with_user(user).write({'price': 1})
        with self.assertRaises(AccessError):
            self.gold.with_user(user).write({'active': False})
        other = self.Valuation.search([('company_id', '=', self.other_company.id)], limit=1)
        self.assertFalse(self.Valuation.with_user(user).search([('id', '=', other.id)]))
        gold = self.gold.with_company(self.company)
        self.assertEqual(gold.company_currency_id, self.usd)
        self.assertEqual(gold.conversion_state, 'ready')
        self.assertEqual(gold.with_company(self.other_company).company_currency_id, self.eur)
        action = gold.action_open_history()
        self.assertIn(('asset_id', '=', gold.id), action['domain'])
        self.assertIn(('company_id', '=', self.company.id), action['domain'])
        self.assertIn(('currency_id', '=', self.usd.id), action['domain'])
        self.assertEqual(self.Valuation._fields['price'].aggregator, 'avg')

    def test_legacy_snapshots_are_labeled_and_preserved(self):
        stamp = fields.Datetime.now() - timedelta(days=1)
        legacy = self.Feed.create({
            'name': 'Gold', 'symbol': 'XAU', 'currency': 'EUR', 'price': 1234.56,
            'updated_at': stamp,
        })
        self.Feed._migrate_legacy_prices()
        self.assertEqual(legacy.source_kind, 'legacy')
        self.assertEqual(legacy.asset_id, self.gold)
        self.assertEqual(legacy.currency_id, self.eur)
        self.assertEqual(legacy.fetched_at, stamp)
        self.assertEqual(legacy.price, 1234.56)
        self.assertFalse(legacy.provider_key)
        self.assertEqual(self.gold.latest_id, legacy)
        with patch(_REQUEST, return_value=self._response()):
            self.Feed._collect_prices()
        self.assertEqual(self.gold.latest_id.source_kind, 'live')
        self.assertTrue(legacy.exists())

    def test_asset_identity_cannot_relabel_historical_prices(self):
        with self.assertRaises(ValidationError):
            self.gold.write({'symbol': 'XPD'})

    def test_stale_market_timestamp_is_not_mistaken_for_fresh_data(self):
        with patch(_REQUEST, return_value=self._response(fields.Datetime.now() - timedelta(days=1))):
            self.Feed._collect_prices()
        self.assertEqual(self.gold.status, 'stale')

    def test_out_of_order_quotes_are_valued_without_replacing_latest(self):
        stamp = fields.Datetime.now()
        with patch(_REQUEST, return_value=self._response(stamp)):
            self.Feed._collect_prices()
        latest = self.gold.latest_id
        self.gold.next_fetch_at = False
        with patch(_REQUEST, return_value=self._response(stamp - timedelta(minutes=1))):
            self.Feed._collect_prices()
        self.assertEqual(self.gold.latest_id, latest)
        older = self.Feed.search([
            ('asset_id', '=', self.gold.id),
            ('updated_at', '=', stamp - timedelta(minutes=1)),
        ])
        self.assertEqual(len(older), 1)
        valuation = self.Valuation.search([
            ('observation_id', '=', older.id), ('company_id', '=', self.company.id),
        ])
        self.assertEqual(valuation.state, 'ready')

    def test_saving_unrelated_settings_preserves_custom_cron(self):
        cron = self.env.ref('open_metal_feed.ir_cron_fetch_open_metal_prices')
        cron.write({'interval_number': 2, 'interval_type': 'hours'})
        previous = int(self.env['ir.config_parameter'].sudo().get_param(
            'open_metal_feed.interval_minutes', '5',
        ))
        settings = self.env['res.config.settings'].create({
            'company_id': self.company.id, 'metal_feed_interval_minutes': previous,
        })
        settings.set_values()
        self.assertEqual(cron.interval_number, 2)
        self.assertEqual(cron.interval_type, 'hours')
        settings.metal_feed_interval_minutes = previous + 1
        settings.set_values()
        self.assertEqual(cron.interval_number, previous + 1)
        self.assertEqual(cron.interval_type, 'minutes')
