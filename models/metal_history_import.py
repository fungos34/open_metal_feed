from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class OpenMetalHistoryImport(models.TransientModel):
    _name = 'open.metal.history.import'
    _description = 'Import Historical Metal Prices'

    asset_ids = fields.Many2many(
        'open.metal.asset', string="Metals", required=True,
        default=lambda self: self.env['open.metal.asset'].search([]),
    )
    date_from = fields.Date(
        string="From", required=True,
        default=lambda self: fields.Date.context_today(self) - timedelta(days=5 * 365),
    )
    date_to = fields.Date(
        string="To", required=True,
        default=lambda self: fields.Date.context_today(self) - timedelta(days=1),
    )
    group_by = fields.Selection(
        [('day', 'Day'), ('week', 'Week'), ('month', 'Month')],
        string="One price per", required=True, default='day',
    )

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for wizard in self:
            if wizard.date_from > wizard.date_to:
                raise ValidationError(self.env._("The start date must be before the end date."))

    def action_import(self):
        self.ensure_one()
        if not (self.env.user.has_group('open_metal_feed.group_metal_feed_manager')
                or self.env.user.has_group('base.group_system')):
            raise AccessError(self.env._("Only feed managers may import history."))
        if not self.env['ir.config_parameter'].sudo().get_param('open_metal_feed.api_key'):
            raise UserError(self.env._("Enter an API key in Metal Feed > Settings first."))
        Price = self.env['open.metal.price'].sudo()
        created, errors = Price._import_history(
            self.asset_ids.sudo(), self.date_from, self.date_to, self.group_by,
        )
        message = self.env._("%(count)s historical prices imported.", count=len(created))
        if errors:
            message += ' ' + self.env._(
                "Failed: %(errors)s", errors=', '.join(f'{k} ({v})' for k, v in errors.items()),
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._("Metal history"), 'message': message,
                'type': 'warning' if errors else 'success', 'sticky': bool(errors),
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
