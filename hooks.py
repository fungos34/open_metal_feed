def post_init_hook(env):
    env['open.metal.price'].sudo()._migrate_legacy_prices()
