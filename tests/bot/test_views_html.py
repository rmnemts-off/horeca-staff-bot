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
from src.bot import texts
from src.bot.renderers import StageZeroRenderers
from src.bot.views import (
    admin,
    checklist,
    editor,
    inline,
    onboarding,
    quoted,
    recipe_form,
    recipes,
    schedule,
    shifts,
    staff,
)
from src.db.models import ChecklistType, InviteCode, MemberRole, Venue, VenueSettings
from src.services.access import InviteRejection
from src.services.notifications import NotificationType
from src.services.recipes import RecipeField
from src.services.shifts import ShiftRole, ShiftWarning
from src.services.templates import TemplateGroupView, TemplateItemView, TemplateView
from src.services.venues import VenueConfiguration

from tests.bot.test_admin_schedule import make_entry as make_roster_entry
from tests.bot.test_admin_schedule import make_view as make_shift_view
from tests.bot.test_admin_staff import OWNER as STAFF_ACTOR
from tests.bot.test_admin_staff import make_entry
from tests.bot.test_checklist import group, item, run_view
from tests.bot.test_menu import make_shift, view_of
from tests.bot.test_recipes_screen import make_card, make_hit, make_line, make_page
from tests.bot.test_renderers import deps
from tests.test_worker import make_notification

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


def hostile_code() -> InviteCode:
    """A pending invite whose name is the venue's own text, and therefore hostile."""
    return InviteCode(
        id=1,
        venue_id=1,
        code="1-A7K9QX4M",
        role=MemberRole.STAFF,
        full_name=HOSTILE,
        expires_at=dt.datetime(2026, 8, 22, 9, 0, tzinfo=dt.UTC),
    )


def test_staff_screens_survive_hostile_text() -> None:
    """TZ 5.8: the employee list, a card, and the code screen that is HTML on purpose."""
    entry = make_entry(full_name=HOSTILE, position=HOSTILE, role=MemberRole.MANAGER)
    blocked = make_entry(2, full_name=HOSTILE, is_active=False, is_bot_blocked=True)
    assert_telegram_html(
        staff.roster_screen([entry, blocked], [hostile_code()], timezone="Europe/Moscow").text
    )
    assert_telegram_html(staff.member_screen(entry, actor=STAFF_ACTOR).text)
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


def hostile_template() -> TemplateView:
    """A checklist whose every word came out of the venue: group names and lines alike."""
    line = TemplateItemView(
        item_id=1,
        text=HOSTILE,
        group_index=0,
        group_name=HOSTILE,
        order_index=0,
        is_critical=True,
        requires_photo=True,
        requires_comment=False,
    )
    second = TemplateItemView(
        item_id=2,
        text=HOSTILE,
        group_index=1,
        group_name=None,
        order_index=0,
        is_critical=False,
        requires_photo=False,
        requires_comment=False,
    )
    return TemplateView(
        template_id=5,
        checklist_type=ChecklistType.OPENING,
        name=HOSTILE,
        version=2,
        is_active=True,
        groups=(
            TemplateGroupView(index=0, name=HOSTILE, items=(line,)),
            TemplateGroupView(index=1, name=None, items=(second,)),
        ),
    )


def test_checklist_editor_screens_survive_hostile_text() -> None:
    """TZ 5.8, task 28: the manager types these lines, so the manager can type a `<`."""
    view = hostile_template()
    assert_telegram_html(editor.template_screen(view).text)
    assert_telegram_html(editor.template_screen(view, group_index=1).text)
    assert_telegram_html(
        editor.template_screen(view, notice=editor.bulk_notice(added=2, groups=2, version=3)).text
    )
    assert_telegram_html(editor.item_screen(view, view.items[0]).text)
    assert_telegram_html(editor.text_prompt(view).text)
    assert_telegram_html(editor.group_prompt(view).text)
    assert_telegram_html(editor.bulk_prompt(view).text)
    empty = TemplateView(
        template_id=None,
        checklist_type=ChecklistType.OPENING,
        name=None,
        version=None,
        is_active=False,
        groups=(),
    )
    assert_telegram_html(editor.template_screen(empty).text)


def test_schedule_admin_screens_survive_hostile_text() -> None:
    """TZ 5.3, 5.8, task 29: every line of the graph is somebody's name."""
    mine = make_shift_view(1, full_name=HOSTILE, is_opener=True)
    theirs = make_shift_view(2, user_id=108, full_name=HOSTILE)
    assert_telegram_html(schedule.schedule_screen([mine, theirs]).text)
    assert_telegram_html(
        schedule.schedule_screen([], warnings=[ShiftWarning.NO_OPENER]).text,
    )
    assert_telegram_html(schedule.shift_screen(mine).text)
    assert_telegram_html(schedule.shift_screen(mine, warnings=[ShiftWarning.NO_OPENER]).text)
    assert_telegram_html(schedule.shift_screen(mine, taken=ShiftRole.OPENER, taken_by=HOSTILE).text)
    assert_telegram_html(schedule.person_step([make_roster_entry(full_name=HOSTILE)]).text)
    assert_telegram_html(schedule.person_step([]).text)
    assert_telegram_html(schedule.date_step().text)
    assert_telegram_html(
        schedule.window_step(member_id=7, start=dt.time(8, 0), end=dt.time(23, 0)).text
    )


def test_recipe_form_screens_survive_hostile_text() -> None:
    """TZ 5.5, task 30a: the name and the category the manager typed, echoed back."""
    assert_telegram_html(recipe_form.section([HOSTILE, HOSTILE]).text)
    assert_telegram_html(recipe_form.section([]).text)
    assert_telegram_html(recipe_form.category_step([HOSTILE]).text)
    assert_telegram_html(recipe_form.name_step().text)
    assert_telegram_html(recipe_form.composition_step().text)
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
    assert_telegram_html(recipe_form.saved(card).text)
    assert_telegram_html(recipe_form.exists(name=HOSTILE, category=HOSTILE, recipe_id=1).text)
    # The manager's half of TZ 5.8: the card again, now with a button per field, the
    # question that precedes a deletion, and the listing a search comes back with. Every one
    # of them formats a name the venue typed.
    assert_telegram_html(recipe_form.managed(card).text)
    # `note` is wording and not venue text (`texts.CARD_UPDATED`), so it is passed as such:
    # feeding it hostile input would assert about a value nothing can produce.
    assert_telegram_html(recipe_form.managed(card, note=texts.CARD_UPDATED).text)
    assert_telegram_html(recipe_form.delete_question(card).text)
    assert_telegram_html(recipe_form.deleted(HOSTILE, [HOSTILE]).text)
    for field in RecipeField:
        assert_telegram_html(recipe_form.field_step(card, field).text)
    hits = [make_hit(recipe_id=1, name=HOSTILE, category=HOSTILE)]
    assert_telegram_html(recipe_form.listing(make_page(hits)).text)
    assert_telegram_html(recipe_form.listing(make_page(query=HOSTILE)).text)


def test_an_inline_row_sends_the_card_and_survives_hostile_text() -> None:
    """What a row of the dropdown sends is a message like any other (part IV).

    It is HTML for the same reason every screen is — the parse mode belongs to the bot — and
    it is *not* drawn here: the row carries `views/recipes.py::card`, which is the point of
    the feature and the reason this assertion is one line.
    """
    card = make_card(name=HOSTILE, category=HOSTILE, ingredients=[make_line(HOSTILE)])

    (result,) = inline.results([card])

    assert_telegram_html(result.input_message_content.message_text)


async def test_notification_bodies_survive_hostile_text() -> None:
    """TZ 6, task 31: a push is an HTML message too, and nothing here is a screen a
    handler drew.

    The manager's four types are built from `payload` — a snapshot of the venue's own
    checklist lines and of the words an employee typed into a skip comment or a search box.
    A `<` in any of them is the same outage as one on a screen, except that nobody is
    looking: the row is marked `failed` in a table and the manager is simply never told
    the checklist was skipped.
    """
    renderers = StageZeroRenderers(deps())
    pending = {
        "checklist_type": ChecklistType.OPENING.value,
        "user_id": None,
        "items": [
            {"text": HOSTILE, "group_name": HOSTILE, "is_critical": True},
            {"text": HOSTILE, "group_name": HOSTILE, "is_critical": False},
            {"text": HOSTILE, "group_name": None, "is_critical": False},
        ],
        "skip_comment": HOSTILE,
    }
    skipped = await renderers.checklist_skipped(
        make_notification(1, notification_type=NotificationType.CHECKLIST_SKIPPED, payload=pending)
    )
    assert_telegram_html(skipped.text)

    overdue = await renderers.checklist_overdue(
        make_notification(2, notification_type=NotificationType.CHECKLIST_OVERDUE, payload=pending)
    )
    assert_telegram_html(overdue.text)

    empty = await renderers.checklist_template_empty(
        make_notification(
            3,
            notification_type=NotificationType.CHECKLIST_TEMPLATE_EMPTY,
            payload={"checklist_type": ChecklistType.OPENING.value},
        )
    )
    assert_telegram_html(empty.text)

    missing = await renderers.recipe_missing(
        make_notification(
            4,
            notification_type=NotificationType.RECIPE_MISSING,
            payload={"query": HOSTILE, "reported_by": None},
        )
    )
    assert_telegram_html(missing.text)
