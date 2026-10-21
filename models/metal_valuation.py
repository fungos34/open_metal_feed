import math

from odoo import api, fields, models


class OpenMetalValuation(models.Model):
    _name = 'open.metal.valuation'
    _description = 'Company Metal Valuation'
    _order = 'updated_at desc, id desc'
    _rec_name = 'asset_id'
    _check_company_auto = True

    observation_id = fields.Many2one(
        'open.metal.price', required=True, readonly=True, ondelete='restrict', index=True,
    )
    asset_id = fields.Many2one(related='observation_id.asset_id', store=True, index=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    currency_id = fields.Many2one('res.currency', required=True, readonly=True, ondelete='restrict')
    updated_at = fields.Datetime(related='observation_id.updated_at', store=True, index=True)
    source_kind = fields.Selection(related='observation_id.source_kind', store=True)
    unit = fields.Selection(related='observation_id.unit')
    price = fields.Float(digits=(24, 8), readonly=True, aggregator='avg')
    conversion_rate = fields.Float(digits=(24, 12), readonly=True, aggregator='avg')
    conversion_date = fields.Date(readonly=True)
    converted_at = fields.Datetime(readonly=True)
    rate_provenance = fields.Text(readonly=True)
    state = fields.Selection(
        [('ready', 'Available'), ('unavailable', 'Conversion unavailable')],
        default='unavailable', required=True, readonly=True, index=True,
    )
    error_code = fields.Selection(
        [('missing_rate', 'Missing rate'), ('invalid', 'Invalid conversion')], readonly=True,
    )
    missing_currency_id = fields.Many2one('res.currency', readonly=True)
    error = fields.Text(compute='_compute_error')
    last_attempt_at = fields.Datetime(readonly=True, index=True)

    _valuation_unique = models.Constraint(
        'UNIQUE(observation_id, company_id, currency_id)',
        'This company valuation already exists.',
    )

    @api.depends('error_code', 'missing_currency_id', 'conversion_date')
    @api.depends_context('lang')
    def _compute_error(self):
        for valuation in self:
            if valuation.error_code == 'missing_rate':
                valuation.error = self.env._(
                    "Missing valid %(currency)s exchange rate on or before %(date)s.",
                    currency=valuation.missing_currency_id.name, date=valuation.conversion_date,
                )
            elif valuation.error_code == 'invalid':
                valuation.error = self.env._("Currency conversion returned an invalid value.")
            else:
                valuation.error = False

    def _try_convert(self):
        for valuation in self.filtered(lambda record: record.state != 'ready'):
            observation = valuation.observation_id
            source = observation.currency_id
            target = valuation.currency_id
            company = valuation.company_id
            date = observation.updated_at.date()
            values = {'last_attempt_at': fields.Datetime.now(), 'conversion_date': date}
            rates = self.env['res.currency.rate']
            if source != target:
                # Odoo otherwise falls back to future rates or 1.0. Neither is valid here.
                for currency in source | target:
                    domain = [('currency_id', '=', currency.id), ('name', '<=', date)]
                    rate = self.env['res.currency.rate'].search(
                        domain + [('company_id', '=', company.root_id.id)],
                        order='name desc, id desc', limit=1,
                    ) or self.env['res.currency.rate'].search(
                        domain + [('company_id', '=', False)],
                        order='name desc, id desc', limit=1,
                    )
                    if not rate or not math.isfinite(rate.rate) or rate.rate <= 0:
                        values.update({
                            'error_code': 'missing_rate', 'missing_currency_id': currency.id,
                        })
                        break
                    rates |= rate
            if 'error_code' not in values:
                rate = source._convert(1.0, target, company, date, round=False)
                price = observation.price * rate
                if not math.isfinite(rate) or rate <= 0 or not math.isfinite(price) or price <= 0:
                    values.update({'error_code': 'invalid', 'missing_currency_id': False})
                else:
                    provenance = '\n'.join(
                        f'{item.currency_id.name}: {item.rate!r}; {item.name}; '
                        f'company={item.company_id.id or "shared"}; rate_id={item.id}'
                        for item in rates
                    )
                    values.update({
                        'state': 'ready', 'error_code': False, 'missing_currency_id': False,
                        'price': price,
                        'conversion_rate': rate, 'conversion_date': date,
                        'converted_at': fields.Datetime.now(),
                        'rate_provenance': provenance or self.env._("Same currency; no conversion."),
                    })
            valuation.write(values)
