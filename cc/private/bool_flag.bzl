"""A boolean command-line build setting, without a bazel_skylib dependency."""

def _bool_flag_impl(_ctx):
    return []

bool_flag = rule(
    implementation = _bool_flag_impl,
    build_setting = config.bool(flag = True),
    doc = "`--<label>=true|false`; read with a `config_setting`'s `flag_values`.",
)
