"""Transport-independent validation for the public, keyless live-price endpoint."""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

SYMBOLS = ('XAU', 'XAG', 'XPT', 'XPD')


class FeedError(Exception):
    def __init__(self, message: str, retry_after: str = ''):
        super().__init__(message)
        self.retry_after = retry_after

    def retry_seconds(self, now: datetime) -> int:
        if self.retry_after.isdigit():
            return int(self.retry_after)
        if self.retry_after:
            try:
                retry = parsedate_to_datetime(self.retry_after)
                if retry.tzinfo is None:
                    retry = retry.replace(tzinfo=timezone.utc)
                retry = retry.astimezone(timezone.utc).replace(tzinfo=None)
                return max(0, math.ceil((retry - now).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                return 0
        return 0


@dataclass(frozen=True)
class Quote:
    price: float
    updated_at: datetime
    key: str


def parse_quote(data: object, symbol: str, now: datetime) -> Quote:
    if symbol not in SYMBOLS:
        raise FeedError('Unsupported precious-metal symbol')
    if not isinstance(data, dict):
        raise FeedError('Expected a JSON object')
    if data.get('symbol') != symbol or data.get('currency') != 'USD':
        raise FeedError('Response symbol or currency does not match the request')
    price = data.get('price')
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        raise FeedError('Price must be numeric')
    try:
        price = float(price)
    except OverflowError as error:
        raise FeedError('Price is outside the supported numeric range') from error
    if not math.isfinite(price) or price <= 0:
        raise FeedError('Price must be finite and positive')
    timestamp = data.get('updatedAt')
    if not isinstance(timestamp, str):
        raise FeedError('Missing market timestamp')
    try:
        updated = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except ValueError as error:
        raise FeedError('Invalid market timestamp') from error
    if updated.tzinfo is None:
        raise FeedError('Market timestamp must include a timezone')
    updated = updated.astimezone(timezone.utc).replace(tzinfo=None)
    if updated > now + timedelta(minutes=5):
        raise FeedError('Market timestamp is in the future')
    return Quote(float(price), updated, f'{symbol}:USD:{updated.isoformat()}')


def cache_seconds(header: str) -> int:
    match = re.search(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)"?(?:\s*,|\s*$)', header, re.I)
    return max(30, int(match.group(1)) if match else 30)
