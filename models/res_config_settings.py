import re
import time

import requests

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..feed import FeedError
from .metal_price import DEFAULT_BASE_URL, normalize_base_url


class ResCompany(models.Model):
    _inherit = 'res.company'

    metal_feed_enabled = fields.Boolean(string="Collect metal prices", default=True)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    metal_feed_enabled = fields.Boolean(
        related='company_id.metal_feed_enabled', readonly=False,
    )
    metal_feed_interval_minutes = fields.Integer(
        string="Collection interval (minutes)", default=5,
        config_parameter='open_metal_feed.interval_minutes',
    )
    metal_feed_base_url = fields.Char(
        string="API base URL", default=DEFAULT_BASE_URL,
        config_parameter='open_metal_feed.base_url',
    )
    metal_feed_api_key = fields.Char(
        string="API key", config_parameter='open_metal_feed.api_key',
    )

    @api.constrains('metal_feed_base_url')
    def _check_metal_base_url(self):
        for settings in self:
            try:
                normalize_base_url(settings.metal_feed_base_url)
            except FeedError as error:
                raise ValidationError(self.env._(
                    "The API base URL must be a plain https:// URL without credentials.",
                )) from error

    @api.constrains('metal_feed_api_key')
    def _check_metal_api_key(self):
        for settings in self:
            if settings.metal_feed_api_key and re.search(r'\s', settings.metal_feed_api_key):
                raise ValidationError(self.env._(
                    "The API key must not contain spaces, tabs or line breaks. "
                    "Copy only the key itself.",
                ))

    def action_metal_feed_test_api_key(self):
        """Uses one request of the keyed quota (free tier: 10 per hour)."""
        self.ensure_one()
        if not (self.metal_feed_api_key or '').strip():
            raise ValidationError(self.env._("Enter an API key first."))
        end = int(time.time())
        message, kind = '', 'danger'
        try:
            response = self.env['open.metal.price']._gold_api_get(
                'ohlc/XAU', params={'startTimestamp': end - 86400, 'endTimestamp': end},
                authenticated=True, base_url=self.metal_feed_base_url,
                api_key=self.metal_feed_api_key,
            )
            with response:
                if response.status_code == 200 and isinstance(response.json(), dict) \
                        and 'close' in response.json():
                    message, kind = self.env._("The API key was accepted."), 'success'
                elif response.status_code in (401, 403):
                    message = self.env._("The API key was rejected (HTTP %s).", response.status_code)
                elif response.status_code == 429:
                    message, kind = self.env._(
                        "The keyed request quota is exhausted (HTTP 429). Try again later.",
                    ), 'warning'
                else:
                    message = self.env._("Unexpected response (HTTP %s).", response.status_code)
        except FeedError as error:
            message = str(error)
        except (requests.RequestException, ValueError):
            message = self.env._("The API could not be reached or returned invalid data.")
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': self.env._("Metal feed API key"), 'message': message,
                       'type': kind, 'sticky': False},
        }

    @api.constrains('metal_feed_interval_minutes')
    def _check_metal_interval(self):
        for settings in self:
            if settings.metal_feed_interval_minutes < 1:
                raise ValidationError(self.env._("The collection interval must be at least one minute."))

    def set_values(self):
        previous_interval = int(self.env['ir.config_parameter'].sudo().get_param(
            'open_metal_feed.interval_minutes', '5',
        ))
        super().set_values()
        if self.metal_feed_interval_minutes != previous_interval:
            cron = self.env.ref('open_metal_feed.ir_cron_fetch_open_metal_prices')
            cron.write({
                'interval_number': self.metal_feed_interval_minutes,
                'interval_type': 'minutes',
            })
