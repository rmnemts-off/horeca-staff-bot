"""Every user-visible string of the bot (TZ 8.2, CLAUDE.md).

This is the only place in `src/` where Russian literals are allowed, so that the customer
can reword messages without a developer. Button captions and notification bodies belong
here too (plan task 21). A static test enforces the rule: see
`tests/test_no_seed_data.py::test_no_cyrillic_literals_outside_texts`.

Interface wording only -- never venue content. Checklist items, writeoff reasons, product
categories, recipes and suppliers are entered by the venue itself and stay in the
database; the system ships empty (product principle 1.4#6, criterion 11.5).

Owner: plan task 21. Empty until then.
"""
