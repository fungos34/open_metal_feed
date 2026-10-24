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
        cache = {}
        for valuation in self.filtered(lambda record: record.state != 'ready'):
            valuation.write(self._prepare_conversion(
                valuation.observation_id, valuation.company_id, valuation.currency_id, cache,
            ))

    @api.model
    def _prepare_conversion(self, observation, company, target, cache):
        """Values for converting ``observation`` into ``target``; ``cache`` is shared per batch."""
        Rate = self.env['res.currency.rate']
        oldest = cache.setdefault('oldest', {})
        converted = cache.setdefault('convert', {})
        source = observation.currency_id
        root = company.root_id
        date = observation.updated_at.date()
        values = {'last_attempt_at': fields.Datetime.now(), 'conversion_date': date}
        rates = self.env['res.currency.rate']
        implicit = self.env['res.currency']
        if source != target:
            # Odoo otherwise falls back to future rates or 1.0. Neither is valid here.
            for currency in source | target:
                key = (currency.id, root.id)
                if key not in oldest:
                    scope = [('currency_id', '=', currency.id)]
                    first = {
                        company_id: Rate.search(
                            scope + [('company_id', '=', company_id)],
                            order='name asc, id asc', limit=1,
                        ) for company_id in (root.id, False)
                    }
                    dates = [rate.name for rate in first.values() if rate]
                    oldest[key] = min(dates) if dates else None
                first_date = oldest[key]
                if not first_date:
                    if currency == root.currency_id:
                        # Company currency without any rate row is the 1.0 base, as in core.
                        implicit |= currency
                        continue
                    rate = Rate
                elif date < first_date:
                    rate = Rate
                else:
                    domain = [('currency_id', '=', currency.id), ('name', '<=', date)]
                    rate = Rate.search(
                        domain + [('company_id', '=', root.id)], order='name desc, id desc', limit=1,
                    ) or Rate.search(
                        domain + [('company_id', '=', False)], order='name desc, id desc', limit=1,
                    )
                if not rate or not math.isfinite(rate.rate) or rate.rate <= 0:
                    values.update({'error_code': 'missing_rate', 'missing_currency_id': currency.id})
                    return values
                rates |= rate
        # Core conversion; its result only depends on the rate records selected above.
        key = (source.id, target.id, root.id, tuple(rates.ids))
        if key not in converted:
            converted[key] = source._convert(1.0, target, company, date, round=False)
        rate = converted[key]
        price = observation.price * rate
        if not math.isfinite(rate) or rate <= 0 or not math.isfinite(price) or price <= 0:
            values.update({'error_code': 'invalid', 'missing_currency_id': False})
            return values
        provenance = '\n'.join([
            f'{item.currency_id.name}: {item.rate!r}; {item.name}; '
            f'company={item.company_id.id or "shared"}; rate_id={item.id}'
            for item in rates
        ] + [f'{item.name}: 1.0; company currency base' for item in implicit])
        values.update({
            'state': 'ready', 'error_code': False, 'missing_currency_id': False,
            'price': price,
            'conversion_rate': rate, 'conversion_date': date,
            'converted_at': fields.Datetime.now(),
            'rate_provenance': provenance or self.env._("Same currency; no conversion."),
        })
        return values
