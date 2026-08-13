"""Tests of the Telegram layer: texts, keyboards, callback payloads.

Nothing here talks to Telegram or to a database. The bot layer of stage 0 is wording, a
menu registry and the `callback_data` scheme (plan task 21), and all three are checkable as
plain values — which is the point of keeping them out of the handlers.
"""
