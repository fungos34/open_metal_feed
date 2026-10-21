from odoo import api, fields, models
from odoo.exceptions import ValidationError


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
