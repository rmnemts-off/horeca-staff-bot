"""«⚙️ Управление»: the manager's and the owner's screens (TZ 5.8, 5.1, 8.1).

Stage 0 covers five of the blocks of TZ 5.8 — employees, schedule, checklists, recipes and
venue settings — plus the wizard that creates the venue itself (plan task 26). The rest of
5.8 (номенклатура, поставщики, база знаний, причины списания, импорт) has no wording here
yet: an empty section with a caption is a broken button, and TZ 8.1 forbids those.

The empty states are the manager's half of TZ 8.1: «Здесь пока пусто» plus the button that
fills it. Every one of these screens is empty on day one, which is the normal case and not
an edge one (principle 1.4#6).

The role labels are interface wording for a code-side enum (TZ 2), the same arrangement as
the units in `recipes.py`: the mapping holds references, never phrases of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from src.db.models import MemberRole

# --------------------------------------------------------------------------------------
# The section (TZ 5.8)
# --------------------------------------------------------------------------------------

ADMIN_TITLE: Final = "Управление"
ADMIN_STAFF_BUTTON: Final = "👥 Сотрудники"
ADMIN_SCHEDULE_BUTTON: Final = "📅 График"
ADMIN_CHECKLISTS_BUTTON: Final = "✅ Чек-листы"
ADMIN_CATALOGUE_BUTTON: Final = "📖 Рецептуры"
ADMIN_SETTINGS_BUTTON: Final = "⚙️ Настройки"

# --------------------------------------------------------------------------------------
# Employees (TZ 5.8, 5.1; decision B8)
# --------------------------------------------------------------------------------------

STAFF_TITLE: Final = "Сотрудники"
#: TZ 8.1: the manager's empty state comes with the button that ends it.
STAFF_EMPTY: Final = "Здесь пока пусто. Добавьте первого сотрудника."
STAFF_ADD_BUTTON: Final = "Добавить"
STAFF_LINE_TEMPLATE: Final = "{full_name} · {role}"
STAFF_LINE_WITH_POSITION_TEMPLATE: Final = "{full_name} · {position} · {role}"
#: TZ 6: a notification could not be delivered because the bot is blocked.
STAFF_BLOCKED_MARK: Final = "🚫 бот заблокирован"
#: TZ 5.1: deactivation keeps the history.
STAFF_DEACTIVATE_BUTTON: Final = "Отключить"
STAFF_ACTIVATE_BUTTON: Final = "Вернуть"
STAFF_DEACTIVATED_MARK: Final = "не работает"
#: TZ 2: an owner who switches himself off cannot switch himself back on — the code that
#: would do it is issued by an owner, and there is now one fewer. Walked into twice on the
#: live stand before the guard existed; the only repair was an UPDATE in the database.
STAFF_SELF_DEACTIVATE_REFUSED: Final = "Себя отключить нельзя."
#: TZ 2: `manager` hands out `staff` and nothing above it, so it does not switch off what
#: it could not create — the second road to a venue with no owner.
STAFF_OWNER_ONLY_REFUSED: Final = "Управляющего и владельца отключает только владелец."

#: Decision B8: the manager types the full name when issuing the code.
INVITE_NAME_PROMPT: Final = "Имя и фамилия сотрудника?"
INVITE_ROLE_PROMPT: Final = "Какая роль?"
INVITE_POSITION_PROMPT: Final = "Должность? Можно пропустить."
INVITE_SKIP_BUTTON: Final = "Пропустить"
#: TZ 5.1: the code or the deep link, whichever the manager finds easier to forward.
INVITE_READY_TEMPLATE: Final = "Код для {full_name}: <code>{code}</code>\nСсылка: {link}"
INVITE_HINT: Final = "Код одноразовый и живёт 7 дней."
INVITE_REVOKE_BUTTON: Final = "Отозвать код"
INVITE_REVOKED: Final = "Код отозван."

# --------------------------------------------------------------------------------------
# Schedule (TZ 5.8, 5.3)
# --------------------------------------------------------------------------------------

SCHEDULE_ADMIN_TITLE: Final = "График"
SCHEDULE_ADMIN_EMPTY: Final = "Смен на эти дни нет."
SCHEDULE_ADD_BUTTON: Final = "Добавить смену"
SCHEDULE_PICK_PERSON: Final = "Кто выходит?"
#: TZ 8.1: choosing from an empty list is the broken button 8.1 is about.
SCHEDULE_NO_STAFF: Final = "Сначала добавьте сотрудников — выбирать не из кого."
SCHEDULE_PICK_DATE: Final = "На какую дату?"
SCHEDULE_PICK_WINDOW: Final = "Со скольки и до скольки?"
SCHEDULE_OPENER_BUTTON: Final = "Открывает"
SCHEDULE_CLOSER_BUTTON: Final = "Закрывает"
SCHEDULE_DELETE_BUTTON: Final = "Удалить смену"
#: Decision B4: the shift is saved anyway, and this is said on the same screen.
SCHEDULE_NO_OPENER_WARNING: Final = "Открывающий не назначен — чек-лист открытия не придёт."
#: TZ 4.2: one opener and one closer per date.
SCHEDULE_OPENER_TAKEN_TEMPLATE: Final = "Открывает уже {full_name}."
SCHEDULE_CLOSER_TAKEN_TEMPLATE: Final = "Закрывает уже {full_name}."

# --------------------------------------------------------------------------------------
# Checklist editor (TZ 5.8; decisions B3, B6)
# --------------------------------------------------------------------------------------

EDITOR_TITLE_TEMPLATE: Final = "{checklist}: {total}"
#: TZ 8.1 and decision B1: the template is created empty with the venue.
EDITOR_EMPTY: Final = "Пунктов пока нет. Пришлите их списком или добавьте по одному."
EDITOR_ADD_BUTTON: Final = "Добавить"
EDITOR_ADD_TO_NEW_GROUP_BUTTON: Final = "Новая группа"
EDITOR_RENAME_GROUP_BUTTON: Final = "Переименовать"
EDITOR_MOVE_BUTTON: Final = "Порядок"
EDITOR_CRITICAL_BUTTON: Final = "Критичный"
EDITOR_PHOTO_BUTTON: Final = "С фото"
EDITOR_DELETE_BUTTON: Final = "Удалить"
EDITOR_TEXT_PROMPT: Final = "Напишите пункт."
EDITOR_GROUP_PROMPT: Final = "Название группы?"
#: Decision B6: the whole checklist in one message — a line is a line, «# Название» opens
#: a group. Forty lines through a step-by-step wizard is an hour on a phone (TZ 7).
#:
#: The third sentence is decision B11 and is not decoration: `parse_bulk` glues an indented
#: line onto the item above it, and a syntax nobody is told about is a syntax that does not
#: exist — the manager whose item ran onto a second line would get two broken items and no
#: idea why.
EDITOR_BULK_PROMPT: Final = (
    "Пришлите пункты одним сообщением: каждая строка — пункт, "
    "строка вида «# Станция» открывает новую группу. "
    "Строка, начатая с отступа, продолжает предыдущий пункт."
)
EDITOR_BULK_RESULT_TEMPLATE: Final = "Добавил {added}, групп: {groups}."
#: Decision B3: a template already used by a run is copied instead of edited in place.
EDITOR_NEW_VERSION_TEMPLATE: Final = (
    "Сохранил как версию {version}. Открытые чек-листы не изменились."
)

# --------------------------------------------------------------------------------------
# Recipes (TZ 5.8; decision B5)
# --------------------------------------------------------------------------------------

CARD_EDITOR_TITLE: Final = "Рецептуры"
CARD_EDITOR_EMPTY: Final = "Пока ничего нет. Добавьте первую карту."
#: What the venue has entered so far, read off the categories it has actually used —
#: `RecipeService` counts nothing yet, and a category is the half of the key of decision D6
#: that a manager recognises the section by.
CARD_EDITOR_GROUPS_TEMPLATE: Final = "Категории: {groups}"
CARD_ADD_BUTTON: Final = "Добавить"
CARD_NAME_PROMPT: Final = "Название?"
CARD_GROUP_PROMPT: Final = "Категория?"
CARD_GLASSWARE_PROMPT: Final = "Бокал? Можно пропустить."
CARD_METHOD_PROMPT: Final = "Метод? Можно пропустить."
CARD_ICE_PROMPT: Final = "Лёд? Можно пропустить."
#: Section 7: non-numeric quantities are kept as text, so the prompt does not demand a
#: number — «Содовая — топ» is a valid line.
CARD_COMPOSITION_PROMPT: Final = "Состав: каждая строка — «ингредиент — количество»."
CARD_GARNISH_PROMPT: Final = "Гарниш? Можно пропустить."
CARD_INSTRUCTION_PROMPT: Final = "Приготовление? Можно пропустить."
#: TZ 5.8: «изменённая рецептура немедленно доступна всем сотрудникам».
CARD_SAVED: Final = "Сохранил. Уже видно всей смене."
#: Decision D6: one name per category, and a second card carrying that key is refused
#: instead of overwriting the first. The manager has just typed a whole card, so the refusal
#: names what is already there and the screen offers to open it.
CARD_EXISTS_TEMPLATE: Final = "«{name}» в категории «{category}» уже есть."

# --------------------------------------------------------------------------------------
# Venue: the wizard and the settings (TZ 5.8; plan tasks 26 and 30)
# --------------------------------------------------------------------------------------

VENUE_WIZARD_TITLE: Final = "Новое заведение"
VENUE_NAME_PROMPT: Final = "Название заведения?"
VENUE_CITY_PROMPT: Final = "Город?"
VENUE_TIMEZONE_PROMPT: Final = "Часовой пояс?"
VENUE_WINDOW_PROMPT: Final = "Обычное время смены? Например, 08:00 и 23:00."
VENUE_CREATED_TEMPLATE: Final = "Заведение «{venue}» создано. Вы владелец."

SETTINGS_TITLE: Final = "Настройки заведения"
SETTINGS_TIMEZONE_BUTTON: Final = "Часовой пояс"
SETTINGS_SHIFT_WINDOW_BUTTON: Final = "Время смены"
SETTINGS_CHECKLIST_LEAD_BUTTON: Final = "Чек-лист заранее"
SETTINGS_CHECKLIST_LEAD_PROMPT: Final = "За сколько минут до смены присылать чек-лист?"
SETTINGS_LINE_TEMPLATE: Final = "{name}: {value}"
SETTINGS_MINUTES_TEMPLATE: Final = "{value} мин"

# --------------------------------------------------------------------------------------
# Roles (TZ 2: the set is code, who holds them is data)
# --------------------------------------------------------------------------------------

ROLE_STAFF: Final = "сотрудник"
ROLE_MANAGER: Final = "менеджер"
ROLE_OWNER: Final = "владелец"

_ROLE_LABELS: Final[Mapping[MemberRole, str]] = {
    MemberRole.STAFF: ROLE_STAFF,
    MemberRole.MANAGER: ROLE_MANAGER,
    MemberRole.OWNER: ROLE_OWNER,
}


def role_label(role: MemberRole) -> str:
    """How a role is written on a screen (TZ 2)."""
    return _ROLE_LABELS[role]


__all__ = [
    "ADMIN_CATALOGUE_BUTTON",
    "ADMIN_CHECKLISTS_BUTTON",
    "ADMIN_SCHEDULE_BUTTON",
    "ADMIN_SETTINGS_BUTTON",
    "ADMIN_STAFF_BUTTON",
    "ADMIN_TITLE",
    "CARD_ADD_BUTTON",
    "CARD_COMPOSITION_PROMPT",
    "CARD_EDITOR_EMPTY",
    "CARD_EDITOR_GROUPS_TEMPLATE",
    "CARD_EDITOR_TITLE",
    "CARD_EXISTS_TEMPLATE",
    "CARD_GARNISH_PROMPT",
    "CARD_GLASSWARE_PROMPT",
    "CARD_GROUP_PROMPT",
    "CARD_ICE_PROMPT",
    "CARD_INSTRUCTION_PROMPT",
    "CARD_METHOD_PROMPT",
    "CARD_NAME_PROMPT",
    "CARD_SAVED",
    "EDITOR_ADD_BUTTON",
    "EDITOR_ADD_TO_NEW_GROUP_BUTTON",
    "EDITOR_BULK_PROMPT",
    "EDITOR_BULK_RESULT_TEMPLATE",
    "EDITOR_CRITICAL_BUTTON",
    "EDITOR_DELETE_BUTTON",
    "EDITOR_EMPTY",
    "EDITOR_GROUP_PROMPT",
    "EDITOR_MOVE_BUTTON",
    "EDITOR_NEW_VERSION_TEMPLATE",
    "EDITOR_PHOTO_BUTTON",
    "EDITOR_RENAME_GROUP_BUTTON",
    "EDITOR_TEXT_PROMPT",
    "EDITOR_TITLE_TEMPLATE",
    "INVITE_HINT",
    "INVITE_NAME_PROMPT",
    "INVITE_POSITION_PROMPT",
    "INVITE_READY_TEMPLATE",
    "INVITE_REVOKED",
    "INVITE_REVOKE_BUTTON",
    "INVITE_ROLE_PROMPT",
    "INVITE_SKIP_BUTTON",
    "ROLE_MANAGER",
    "ROLE_OWNER",
    "ROLE_STAFF",
    "SCHEDULE_ADD_BUTTON",
    "SCHEDULE_ADMIN_EMPTY",
    "SCHEDULE_ADMIN_TITLE",
    "SCHEDULE_CLOSER_BUTTON",
    "SCHEDULE_CLOSER_TAKEN_TEMPLATE",
    "SCHEDULE_DELETE_BUTTON",
    "SCHEDULE_NO_OPENER_WARNING",
    "SCHEDULE_NO_STAFF",
    "SCHEDULE_OPENER_BUTTON",
    "SCHEDULE_OPENER_TAKEN_TEMPLATE",
    "SCHEDULE_PICK_DATE",
    "SCHEDULE_PICK_PERSON",
    "SCHEDULE_PICK_WINDOW",
    "SETTINGS_CHECKLIST_LEAD_BUTTON",
    "SETTINGS_CHECKLIST_LEAD_PROMPT",
    "SETTINGS_LINE_TEMPLATE",
    "SETTINGS_MINUTES_TEMPLATE",
    "SETTINGS_SHIFT_WINDOW_BUTTON",
    "SETTINGS_TIMEZONE_BUTTON",
    "SETTINGS_TITLE",
    "STAFF_ACTIVATE_BUTTON",
    "STAFF_ADD_BUTTON",
    "STAFF_BLOCKED_MARK",
    "STAFF_DEACTIVATED_MARK",
    "STAFF_DEACTIVATE_BUTTON",
    "STAFF_EMPTY",
    "STAFF_LINE_TEMPLATE",
    "STAFF_LINE_WITH_POSITION_TEMPLATE",
    "STAFF_OWNER_ONLY_REFUSED",
    "STAFF_SELF_DEACTIVATE_REFUSED",
    "STAFF_TITLE",
    "VENUE_CITY_PROMPT",
    "VENUE_CREATED_TEMPLATE",
    "VENUE_NAME_PROMPT",
    "VENUE_TIMEZONE_PROMPT",
    "VENUE_WINDOW_PROMPT",
    "VENUE_WIZARD_TITLE",
    "role_label",
]
