"""FSM state groups (TZ 3.2).

State lives in Redis with a TTL so that a scenario survives a bot restart (plan task 20).
Wizard steps do not count towards the two-level navigation limit (decision D10).

Owner: plan task 20 (bot framework). Empty until then.
"""
