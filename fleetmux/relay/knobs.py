"""Read relay parameters. Every threshold, period and weight comes from the `relay` section of the fleet config
(docs/relay.md, "Parameters"); this module has no default values for them."""


def get(params, key):
    """`params.hot(key)` when the parameter object has it (the config reader), else `params[key]`."""
    hot = getattr(params, 'hot', None)
    if callable(hot):
        return hot(key)
    try:
        return params[key]
    except KeyError:
        raise KeyError('relay parameter ' + key + ' is not set') from None


def cold(params, key):
    """A cold key of the relay block: an attribute of Settings, or `params[key]` (a dict in tests)."""
    if hasattr(params, key):
        return getattr(params, key)
    try:
        return params[key]
    except (KeyError, TypeError):
        raise KeyError('relay parameter ' + key + ' is not set') from None


class Limits:
    """The text limits of the relay block as attributes (what the state layer's Events reads)."""

    def __init__(self, params):
        self.notify_max = cold(params, 'notify_max')
        self.reason_max = cold(params, 'reason_max')
