from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class OpenMetalAsset(models.Model):
    _name = 'open.metal.asset'
    _description = 'Precious Metal'
    _order = 'symbol'

    name = fields.Char(required=True, translate=True)
    symbol = fields.Selection(
        [('XAU', 'XAU'), ('XAG', 'XAG'), ('XPT', 'XPT'), ('XPD', 'XPD')],
        required=True, index=True,
    )
    active = fields.Boolean(default=True)
    latest_id = fields.Many2one('open.metal.price', readonly=True, ondelete='restrict')
    source_price = fields.Float(related='latest_id.price', digits=(24, 8))
    source_currency_id = fields.Many2one(related='latest_id.currency_id', string="Source Currency")
    updated_at = fields.Datetime(related='latest_id.updated_at')
    fetched_at = fields.Datetime(related='latest_id.fetched_at')
    unit = fields.Selection(related='latest_id.unit')
    last_attempt_at = fields.Datetime(readonly=True)
    last_success_at = fields.Datetime(readonly=True)
    next_fetch_at = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    failure_count = fields.Integer(readonly=True)
    status = fields.Selection(
        [('waiting', 'Waiting'), ('fresh', 'Fresh'), ('stale', 'Stale'), ('error', 'Fetch failed')],
        compute='_compute_status',
    )
    company_currency_id = fields.Many2one(
        'res.currency', compute='_compute_valuation', string="Company Currency",
    )
    company_price = fields.Float(
        compute='_compute_valuation', digits=(24, 8), string="Company price",
    )
    conversion_state = fields.Selection(
        [('ready', 'Available'), ('unavailable', 'Conversion unavailable'), ('disabled', 'Disabled')],
        compute='_compute_valuation',
    )
    conversion_error = fields.Text(compute='_compute_valuation')

    _symbol_unique = models.Constraint('UNIQUE(symbol)', 'The metal symbol must be unique.')

    def write(self, values):
        if 'symbol' in values and any(asset.symbol != values['symbol'] for asset in self):
            raise ValidationError(self.env._("A metal's symbol cannot be changed. Archive it instead."))
        return super().write(values)

    @api.depends('latest_id', 'last_error', 'last_success_at', 'updated_at')
    def _compute_status(self):
        minutes = int(self.env['ir.config_parameter'].sudo().get_param(
            'open_metal_feed.interval_minutes', '5',
        ))
        cutoff = fields.Datetime.now() - timedelta(minutes=max(5, minutes * 3))
        for asset in self:
            if asset.last_error:
                asset.status = 'error'
            elif not asset.latest_id:
                asset.status = 'waiting'
            elif asset.updated_at < cutoff or not asset.last_success_at or asset.last_success_at < cutoff:
                asset.status = 'stale'
            else:
                asset.status = 'fresh'

    @api.depends_context('company', 'lang')
    @api.depends('latest_id')
    def _compute_valuation(self):
        company = self.env.company
        for asset in self:
            valuation = self.env['open.metal.valuation'].search([
                ('observation_id', '=', asset.latest_id.id),
                ('company_id', '=', company.id),
                ('currency_id', '=', company.currency_id.id),
            ], limit=1) if asset.latest_id else self.env['open.metal.valuation']
            asset.company_currency_id = company.currency_id
            asset.company_price = valuation.price if valuation.state == 'ready' else False
            asset.conversion_state = (
                'disabled' if not company.metal_feed_enabled
                else 'ready' if valuation.state == 'ready' else 'unavailable'
            )
            asset.conversion_error = valuation.error or self.env._(
                "Awaiting collection or suitable exchange rates."
            )

    def action_refresh(self):
        return self.env['open.metal.price'].action_fetch_all_prices()

    def action_open_history(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._(
                "Price history: %(metal)s (%(currency)s, %(company)s)",
                metal=self.display_name, currency=self.env.company.currency_id.name,
                company=self.env.company.display_name,
            ),
            'res_model': 'open.metal.valuation',
            'view_mode': 'graph,list,pivot,form',
            'domain': [
                ('asset_id', '=', self.id),
                ('company_id', '=', self.env.company.id),
                ('currency_id', '=', self.env.company.currency_id.id),
                ('state', '=', 'ready'),
            ],
        }

    def action_open_source_history(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Source observations: %s", self.display_name),
            'res_model': 'open.metal.price',
            'view_mode': 'list,form',
            'domain': [('asset_id', '=', self.id)],
        }
