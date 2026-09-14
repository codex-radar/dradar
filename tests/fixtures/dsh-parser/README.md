# Pinned DSH parser contract fixtures

Unmodified published sources from the MIT-licensed official npm packages:
- `bin.js`: https://registry.npmjs.org/@deepseek-ai/dsh/-/dsh-0.1.2-rc.1.tgz (`package/lib/bin.js`)
- `startup.js`: https://registry.npmjs.org/@deepseek-ai/dsh-headless/-/dsh-headless-0.1.2-rc.1.tgz (`package/lib/startup.js`)

The harness evaluates only the outer argument parser and inner headless startup
functions, with Commander 15.0.0. It never imports the CLI entry point, profiles,
providers or model runtime. The inner `apply` action is used unchanged, including
the empty-task check. Install the test dependency with `npm ci --ignore-scripts`
in this directory; run `pytest tests/test_dsh_parser_contract.py` with Python 3.12+
and datacurve-pier 0.3.0. Node >=22.12 is required by Commander.
The test skips when Node/Commander are absent; release QA must install them and
verify that this suite has no skips. These fixtures must be revalidated when the
pinned DSH version changes.

Source SHA256:
- `bin.js`: `dc23f6c5dd7df8834e3e38bdb9609d77b459834681ae9b7133b417b0c35f3166`
- `startup.js`: `1d93363af3f955b37a044cd1bd7b438881c9f842ef79c4ec222b082d3a65c157`
