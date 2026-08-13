"""Aiogram middlewares: authorisation, venue context, logging, rate limit (TZ 3.2).

The venue-context middleware puts `venue_id` into the handler context on every update;
repositories are then required to filter by it (TZ 3.3). Rights are checked on the server
for every action, never by hiding buttons.

Owner: plan task 20 (bot framework). Empty until then.
"""
