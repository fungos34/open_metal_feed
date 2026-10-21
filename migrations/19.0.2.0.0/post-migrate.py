from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['open.metal.price']._migrate_legacy_prices()
    cron = env.ref('open_metal_feed.ir_cron_fetch_open_metal_prices')
    values = {'code': 'model._cron_fetch_prices()'}
    if cron.interval_number == 12 and cron.interval_type == 'hours':
        values.update({'interval_number': 5, 'interval_type': 'minutes'})
    cron.write(values)
    minutes_per_unit = {'minutes': 1, 'hours': 60, 'days': 1440, 'weeks': 10080, 'months': 43200}
    env['ir.config_parameter'].set_param(
        'open_metal_feed.interval_minutes',
        cron.interval_number * minutes_per_unit[cron.interval_type],
    )
