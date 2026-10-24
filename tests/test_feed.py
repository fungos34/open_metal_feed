import unittest
from datetime import date, datetime

if __package__:
    from ..feed import FeedError, cache_seconds, parse_history, parse_quote
else:
    from feed import FeedError, cache_seconds, parse_history, parse_quote


class TestFreeFeed(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 12)
        self.payload = {
            'symbol': 'XAU', 'currency': 'USD', 'price': 4165.200195,
            'updatedAt': '2026-10-21T11:59:00Z',
        }

    def test_quote_preserves_precision_and_market_timestamp(self):
        quote = parse_quote(self.payload, 'XAU', self.now)
        self.assertEqual(quote.price, 4165.200195)
        self.assertEqual(quote.updated_at, datetime(2026, 10, 21, 11, 59))
        self.assertEqual(quote.key, 'XAU:USD:2026-10-21T11:59:00')

    def test_timestamp_normalized_to_utc(self):
        self.payload['updatedAt'] = '2026-10-21T13:59:00+02:00'
        self.assertEqual(
            parse_quote(self.payload, 'XAU', self.now).updated_at,
            datetime(2026, 10, 21, 11, 59),
        )

    def test_reject_invalid_payloads(self):
        for payload in (None, [], 'bad', 1):
            with self.subTest(payload=payload), self.assertRaises(FeedError):
                parse_quote(payload, 'XAU', self.now)

    def test_reject_invalid_prices(self):
        for price in (None, True, False, '10', 0, -1, float('nan'), float('inf'), 10 ** 1000):
            with self.subTest(price=price), self.assertRaises(FeedError):
                parse_quote(dict(self.payload, price=price), 'XAU', self.now)

    def test_reject_symbol_currency_and_timestamp_mismatch(self):
        for changes in (
            {'symbol': 'XAG'}, {'currency': 'EUR'}, {'currency': None},
            {'updatedAt': None}, {'updatedAt': 'invalid'},
            {'updatedAt': '2026-10-21T11:59:00'},
            {'updatedAt': '2026-10-21T12:05:01Z'},
        ):
            with self.subTest(changes=changes), self.assertRaises(FeedError):
                parse_quote(dict(self.payload, **changes), 'XAU', self.now)
        with self.assertRaises(FeedError):
            parse_quote(self.payload, 'BTC', self.now)

    def test_cache_floor_and_longer_provider_lifetime(self):
        for header, expected in (
            ('', 30), ('no-cache', 30), ('max-age=0', 30),
            ('public, max-age=20', 30), ('public, max-age=120', 120),
            ('MAX-AGE="60", public', 60), ('s-maxage=90', 30),
            ('max-age=invalid', 30),
        ):
            with self.subTest(header=header):
                self.assertEqual(cache_seconds(header), expected)

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(FeedError('429', '600').retry_seconds(self.now), 600)
        self.assertEqual(
            FeedError('429', 'Wed, 21 Oct 2026 12:10:00 GMT').retry_seconds(self.now), 600,
        )
        self.assertEqual(
            FeedError('429', 'Wed, 21 Oct 2026 14:10:00 +0200').retry_seconds(self.now), 600,
        )
        self.assertEqual(FeedError('429', 'invalid').retry_seconds(self.now), 0)
        self.assertEqual(
            FeedError('429', 'Wed, 21 Oct 2026 11:00:00 GMT').retry_seconds(self.now), 0,
        )

    def test_history_parses_string_prices_and_skips_bad_or_incomplete_rows(self):
        rows = [
            {'day': '1970-01-01 00:00:00', 'avg_price': '35.675000'},
            {'day': '2026-10-20', 'avg_price': 4011.26},
            {'day': '2026-10-21 00:00:00', 'avg_price': '4000'},
            {'day': 'garbage', 'avg_price': '1'},
            {'day': '2026-10-19', 'avg_price': 'nan'},
            {'day': '2026-10-18', 'avg_price': '-3'},
            {'day': '2026-10-17', 'max_price': '5'},
            'not a row',
        ]
        self.assertEqual(parse_history(rows, 'day', 'avg', date(2026, 10, 21)), {
            date(1970, 1, 1): 35.675, date(2026, 10, 20): 4011.26,
        })
        with self.assertRaises(FeedError):
            parse_history({'error': 'Invalid API key'}, 'day', 'avg', date(2026, 10, 21))
        with self.assertRaises(FeedError):
            parse_history([], 'hour', 'avg', date(2026, 10, 21))

