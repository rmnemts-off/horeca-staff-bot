"""Every screen is HTML, so every screen must survive a venue with `<` in its name.

**What this file is for.** `src/bot/dispatcher.py::build_bot` sets `parse_mode=HTML` for the
whole process, because two texts need it — an invite code is `<code>` so a manager can tap
to copy it, a drink's name is `<b>`. The parse mode is a property of the bot and cannot be
set per message, so *every* screen is parsed as HTML whether it wanted to be or not.

That turns unquoted venue text into an outage rather than a cosmetic bug. Telegram does not
mangle a message it cannot parse, it **rejects** it: a bar called «Rum & Cola», an employee
who typed `<` into their name, a checklist line reading `t < 4°C` — each one means the
employee is shown nothing at all, and only for the venues whose data happens to contain the
character. Nothing in a per-module test can see this, because every one of them passes
well-behaved ASCII through the view and asserts the wording.

So this file passes :data:`HOSTILE` through every screen that carries text from a person or
a venue, and parses the result the way Telegram would. :func:`assert_telegram_html` is the
whole point of the file — it fails on an unescaped `<`, on a bare `&`, on a tag Telegram
does not know and on one that is never closed.

**A new view belongs here.** The list below is the guard against the next module that
formats a name straight into a template; a screen that is not in it is a screen nobody has
asked this question of.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Final

import pytest
from src.bot.views import admin, checklist, onboarding, quoted, recipes, shifts, staff
from src.db.models import MemberRole, Venue, VenueSettings
from src.services.access import InviteRejection
from src.services.venues import VenueConfiguration

from tests.bot.test_admin_staff import make_entry
from tests.bot.test_checklist import group, item, run_view
from tests.bot.test_menu import make_shift, view_of
from tests.bot.test_recipes_screen import make_card, make_hit, make_line, make_page

#: Text as hostile as a venue can make it without trying: an ampersand (Telegram reads it as
#: the start of an entity), a `<` that opens a tag it does not know, and a stray `>`.
HOSTILE: Final = '<script>Rum & Cola</b> 5 > 4"'

#: What Telegram's HTML parser accepts (Bot API, "Formatting options"). Anything else in a
#: message body is an error and the message is refused.
ALLOWED_TAGS: Final = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "span",
        "a",
        "code",
        "pre",
        "blockquote",
        "tg-spoiler",
        "tg-emoji",
    }
)

_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)/?>")
_ENTITY = re.compile(r"&(?:amp|lt|gt|quot|apos|#\d+|#[xX][0-9a-fA-F]+);")


def assert_telegram_html(text: str) -> None:
    """Fail unless Telegram would accept `text` as an HTML message body.

    Written out rather than taken from a library because the failure being guarded against
    is precisely the one a lenient parser forgives: `html.parser` is happy to read
    `<script>` as a tag and a bare `&` as a literal, which is what makes the bug invisible
    until a real venue types one.
    """
    stack: list[str] = []
    position = 0
    while position < len(text):
        character = text[position]
        if character == "&":
            match = _ENTITY.match(text, position)
            assert match is not None, (
                f"bare '&' at {position} in {text!r} — venue text reached the screen "
                "without src.bot.views.quoted()"
            )
            position = match.end()
            continue
        if character == ">":
            raise AssertionError(
                f"bare '>' at {position} in {text!r} — venue text reached the screen "
                "without src.bot.views.quoted()"
            )
        if character != "<":
            position += 1
            continue
        match = _TAG.match(text, position)
        assert match is not None, (
            f"'<' at {position} does not open a tag in {text!r} — venue text reached the "
            "screen without src.bot.views.quoted()"
        )
        closing, name, _ = match.groups()
        assert name.lower() in ALLOWED_TAGS, f"Telegram rejects <{name}> in {text!r}"
        if closing:
            assert stack and stack[-1] == name.lower(), f"</{name}> closes nothing in {text!r}"
            stack.pop()
        else:
            stack.append(name.lower())
        position = match.end()
    assert not stack, f"unclosed {stack} in {text!r}"


# --------------------------------------------------------------------------------------
# The validator itself, so that a green suite below means something
# --------------------------------------------------------------------------------------


def test_the_validator_accepts_what_telegram_accepts() -> None:
    assert_telegram_html("plain text")
    assert_telegram_html("<b>bold</b> and <code>mono</code>")
    assert_telegram_html("Rum &amp; Cola, 5 &gt; 4, &lt;script&gt;")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Rum & Cola", id="bare ampersand"),
        pytest.param("<script>x</script>", id="tag Telegram does not know"),
        pytest.param("<b>never closed", id="unclosed tag"),
        pytest.param("5 > 4", id="bare greater-than"),
        pytest.param("</b>", id="closes nothing"),
    ],
)
def test_the_validator_rejects_what_telegram_rejects(text: str) -> None:
    """A validator that passed everything would make every test below vacuously green."""
    with pytest.raises(AssertionError):
        assert_telegram_html(text)


def test_quoting_hostile_text_makes_it_safe() -> None:
    assert_telegram_html(f"<b>{quoted(HOSTILE)}</b>")


# --------------------------------------------------------------------------------------
# The screens (TZ 5.1-5.8)
# --------------------------------------------------------------------------------------


def test_onboarding_screens_survive_hostile_text() -> None:
    """TZ 5.1: the venue name and the name the manager typed onto the code."""
    assert_telegram_html(
        onboarding.invite_confirmation(venue=HOSTILE, full_name=HOSTILE, position=HOSTILE).text
    )
    assert_telegram_html(
        onboarding.invite_confirmation(venue=HOSTILE, full_name=HOSTILE, position=None).text
    )
    assert_telegram_html(onboarding.name_prompt(venue=HOSTILE).text)
    assert_telegram_html(onboarding.activated(full_name=HOSTILE, venue=HOSTILE).text)
    for rejection in InviteRejection:
        assert_telegram_html(onboarding.invite_rejected(rejection).text)


def test_staff_screens_survive_hostile_text() -> None:
    """TZ 5.8: the employee list, a card, and the code screen that is HTML on purpose."""
    entry = make_entry(full_name=HOSTILE, position=HOSTILE, role=MemberRole.MANAGER)
    blocked = make_entry(2, full_name=HOSTILE, is_active=False, is_bot_blocked=True)
    assert_telegram_html(staff.roster_screen([entry, blocked]).text)
    assert_telegram_html(staff.member_screen(entry).text)
    assert_telegram_html(
        staff.invite_code_screen(
            full_name=HOSTILE,
            code=HOSTILE,
            link=staff.deeplink(HOSTILE, HOSTILE),
            code_id=1,
        ).text
    )


def test_checklist_screens_survive_hostile_text() -> None:
    """TZ 5.4: both the group names and the lines are the venue's own words."""
    view = run_view(
        group(0, HOSTILE, item(1, HOSTILE, group_index=0, is_critical=True)),
        group(1, None, item(2, HOSTILE, group_index=1, is_done=True)),
    )
    assert_telegram_html(checklist.render_run(view).text)
    assert_telegram_html(checklist.render_run(view, group_index=1).text)
    pending = [item(1, HOSTILE, group_index=0, is_critical=True)]
    assert_telegram_html(checklist.render_skip_question(view.run_id, pending).text)


def test_shift_screens_survive_hostile_text() -> None:
    """TZ 5.3: the names of the colleagues on the same date."""
    mine = view_of(make_shift(1), full_name=HOSTILE)
    theirs = view_of(make_shift(2, user_id=99), full_name=HOSTILE)
    assert_telegram_html(shifts.shift_screen(mine, now=mine.starts_at, roster=[mine, theirs]).text)
    assert_telegram_html(shifts.schedule_screen([mine, theirs]).text)


def test_admin_screens_survive_hostile_text() -> None:
    """TZ 5.8: the venue's own name, echoed back at the end of the wizard."""
    assert_telegram_html(admin.created(HOSTILE).text)
    configuration = VenueConfiguration(
        venue=Venue(id=1, name=HOSTILE, city=HOSTILE, timezone="Europe/Moscow", is_active=True),
        settings=VenueSettings(
            venue_id=1,
            opening_checklist_lead_minutes=10,
            closing_checklist_lead_minutes=30,
            default_shift_start=dt.time(8, 0),
            default_shift_end=dt.time(23, 0),
        ),
    )
    assert_telegram_html(admin.settings(configuration).text)


def test_recipe_screens_survive_hostile_text() -> None:
    """TZ 5.5: every field of a card is a word somebody typed into a spreadsheet."""
    card = make_card(
        name=HOSTILE,
        category=HOSTILE,
        glassware=HOSTILE,
        method=HOSTILE,
        ice=HOSTILE,
        garnish=HOSTILE,
        instruction=HOSTILE,
        ingredients=[make_line(HOSTILE, qty_text=HOSTILE)],
    )
    assert_telegram_html(recipes.card(card).text)
    page = make_page([make_hit(1, HOSTILE, HOSTILE)], query=HOSTILE)
    assert_telegram_html(recipes.results(page).text)
    assert_telegram_html(recipes.results(make_page(query=HOSTILE)).text)
    assert_telegram_html(recipes.section([HOSTILE]).text)
