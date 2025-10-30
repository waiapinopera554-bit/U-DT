#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from html import escape
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

def convert_number_to_bases(value: int) -> Dict[str, str]:
    abs_value = abs(value)
    sign = "-" if value < 0 else ""

    def fmt(spec: str) -> str:
        digits = "0" if abs_value == 0 else format(abs_value, spec)
        return sign + digits

    return {
        "decimal": str(value),
        "binary": fmt("b"),
        "octal": fmt("o"),
        "hexadecimal": fmt("X"),
    }


def detect_number_system(raw: str) -> tuple[int, int]:
    text = raw.strip()
    if not text:
        raise ValueError("empty")

    sign = ""
    if text[0] in "+-":
        sign = text[0]
        text = text[1:]

    text = text.replace("_", "")
    if not text:
        raise ValueError("empty")

    base: int | None = None
    body = text
    lower_body = body.lower()

    if lower_body.startswith("0b"):
        base = 2
        body = body[2:]
    elif lower_body.startswith("0o"):
        base = 8
        body = body[2:]
    elif lower_body.startswith("0x"):
        base = 16
        body = body[2:]
    else:
        upper = body.upper()
        if any(c in "ABCDEF" for c in upper):
            base = 16
        elif any(c in "89" for c in upper):
            base = 10
        elif set(upper) <= {"0", "1"} and len(body) > 1:
            base = 2
        elif set(upper) <= set("01234567") and body.startswith("0") and len(body) > 1:
            base = 8
        else:
            base = 10

    if not body:
        raise ValueError("empty")

    try:
        value = int(sign + body, base)
    except ValueError as exc:
        raise ValueError from exc

    return value, base


IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_]\w*")
RESERVED_WORDS = {
    "sin",
    "cos",
    "tan",
    "sqrt",
    "sqr",
    "ln",
    "exp",
    "log",
    "abs",
}


def parse_assignments(expr: str) -> List[Tuple[str, str]]:
    """Parse one or more assignments separated by semicolons or newlines."""
    assignments: List[Tuple[str, str]] = []
    for segment in re.split(r"[;\n]+", expr):
        stripped = segment.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            raise ValueError("Each assignment must contain '='.")
        lhs, rhs = stripped.split("=", 1)
        lhs = lhs.strip()
        rhs = rhs.strip()
        if not lhs:
            raise ValueError("Left side of assignment is empty.")
        assignments.append((lhs, rhs))
    if not assignments:
        raise ValueError("No assignments provided.")
    return assignments


def ordered_identifiers(expressions: Iterable[str]) -> List[str]:
    """Return identifiers in the order they appear, excluding reserved names."""
    seen = OrderedDict()
    for expr in expressions:
        for match in IDENTIFIER_PATTERN.finditer(expr):
            name = match.group(0)
            if name.lower() in RESERVED_WORDS:
                continue
            if name not in seen:
                seen[name] = None
    return list(seen.keys())


def generate_algo(
    name: str,
    assignments: Sequence[Tuple[str, str]],
    inputs: Sequence[str],
    variables: Sequence[str],
) -> str:
    """Build an ALGO snippet for one or multiple assignments."""
    vars_line = ", ".join(variables)
    lines: List[str] = [
        f"Algo {name};",
        f"Var {vars_line} : Reel;",
        "Debut",
    ]
    if inputs:
        for var_name in inputs:
            lines.append(f'    Ecrire("{var_name} : "); Lire({var_name});')
    else:
        lines.append("    // Pas d'entrees supplementaires")
    for lhs, rhs in assignments:
        lines.append(f"    {lhs} := {rhs};")
        lines.append(f'    Ecrire("{lhs} = ", {lhs});')
    lines.append("Fin.")
    return "\n".join(lines)


def generate_pascal(
    program: str,
    assignments: Sequence[Tuple[str, str]],
    inputs: Sequence[str],
    variables: Sequence[str],
) -> str:
    """Build a Pascal snippet for one or multiple assignments."""
    vars_block = ", ".join(variables)
    input_lines: List[str] = []
    for var_name in inputs:
        input_lines.append(f"  Write('{var_name} : '); ReadLn({var_name});")
    if not input_lines:
        input_lines.append("  { Pas d'entrees a lire }")
    lines = [
        f"program {program};",
        "",
        "var",
        f"  {vars_block}: Real;",
        "",
        "begin",
        *input_lines,
    ]
    for lhs, rhs in assignments:
        lines.append(f"  {lhs} := {rhs};")
        lines.append(f"  WriteLn('{lhs} = ', {lhs});")
    lines.append("end.")
    return "\n".join(lines)


def build_response(
    expr: str, algo_name: str = "Calcul", pascal_name: str = "Calcul"
) -> Dict[str, str]:
    """Return ALGO and Pascal snippets for one or multiple assignments."""
    assignments = parse_assignments(expr)
    assigned_vars = [lhs for lhs, _ in assignments]
    rhs_texts = [rhs for _, rhs in assignments]
    identifiers = ordered_identifiers([*rhs_texts, *assigned_vars])
    assigned_set = set(assigned_vars)
    input_vars: List[str] = [
        name
        for name in identifiers
        if name not in assigned_set and name not in assigned_vars
    ]

    dep_map = {
        lhs: [name for name in IDENTIFIER_PATTERN.findall(rhs) if name in assigned_set]
        for lhs, rhs in assignments
    }
    remaining = [lhs for lhs, _ in assignments]
    ordered_lhs: List[str] = []
    ordered_set = set()

    while remaining:
        progress = False
        for lhs in list(remaining):
            deps = dep_map.get(lhs, [])
            if all(dep in ordered_set for dep in deps):
                ordered_lhs.append(lhs)
                ordered_set.add(lhs)
                remaining.remove(lhs)
                progress = True
        if not progress:
            ordered_lhs.extend(remaining)
            break

    lhs_to_rhs = {lhs: rhs for lhs, rhs in assignments}
    ordered_assignments = [(lhs, lhs_to_rhs[lhs]) for lhs in ordered_lhs]
    variables = ordered_lhs + [
        name for name in input_vars if name not in ordered_lhs
    ]
    algo_snippet = generate_algo(algo_name, ordered_assignments, input_vars, variables)
    pascal_snippet = generate_pascal(
        pascal_name, ordered_assignments, input_vars, variables
    )
    return {"algo": algo_snippet, "pascal": pascal_snippet}


logger = logging.getLogger(__name__)

BOT_TOKEN = "8240445622:AAGrwB3au_8H1ugv98T9v0D2TQ9dJAso4u4"
DATA_FILE = Path(__file__).with_name("data.json")
DATA_LOCK = asyncio.Lock()

LANGUAGE_FLAGS = {
    "ar": "\U0001F1E9\U0001F1FF",
    "fr": "\U0001F1EB\U0001F1F7",
    "en": "\U0001F1EC\U0001F1E7",
}

LANGUAGE_LABELS = {
    "ar": "العربية",
    "fr": "Français",
    "en": "English",
}

MESSAGES: Dict[str, Dict[str, str]] = {
    "ar": {
        "welcome_new": "مرحباً {name}! اختر اللغة للمتابعة:",
        "welcome_back": "أهلاً {name} 💻! هذا بوت Algo & Pascal.",
        "menu_prompt": "اضغط وش راك حاب ⬇️ :",
        "btn_convert_number": "🔢 تحويل عدد",
        "btn_detect_number": "🔍 تعرّف على الرقم",
        "btn_algo_pascal": "🧮 Algo & Pascal",
        "btn_change_language": "🌐 تغيير اللغة",
        "btn_back": "⬅️ رجوع",
        "btn_developer": "❤️ المطور",
        "ask_number": "أرسل العدد العشري الذي تريد تحويله:",
        "invalid_number": "تعذر قراءة العدد. الرجاء إدخال عدد صحيح (مثل 125 أو -42).",
        "number_result": "النتائج :\nالنظام العشري: {decimal}\nالنظام الثنائي (0/1): {binary}\nالنظام الثماني (0/7): {octal}\nالنظام السادس عشر(0/15): {hexadecimal}",
        "ask_detect": "أرسل رقماً بأي نظام (مثال: 0b1010، 7F، 075، 42...):",
        "invalid_detect": "تعذر تحديد النظام. تأكد من كتابة الرقم بشكل صحيح.",
        "detect_result": "النظام المكتشف: {base_label}\nالنظام العشري: {decimal}\nالنظام الثنائي (0/1): {binary}\nالنظام الثماني (0/7): {octal}\nالنظام السادس عشر(0/15): {hexadecimal}",
        "ask_expression": """حط هنا اي معادلة ALGO كيما قريت
EXP :
SOM = A + B
تقدر تضيف ; باش تضيف معادلة ثانية
EXP : 
SOM = A / H + B;H = T + 10
في حالة جذر حط
SQRT(25)
نتيجة 5
وفي حالة تربيع
SQR(5)
نتيجة 25
""",
        "invalid_expression": "تأكد أن المعادلة مكتوبة بشكل صحيح وتحتوي على '='.",
        "choose_language": "اختر اللغة:",
        "start_hint": "اضغط /start لاعادة تشغيل بوت .",
        "cancelled": "تم إلغاء العملية.",
        "admin_denied": "هذه الميزة للمشرف فقط.",
        "admin_overview": "قائمة المستخدمين:",
        "admin_user_count": "عدد المستخدمين: {count}",
        "admin_user_line": "- @{name} (ID: {id}, لغة: {language})",
        "admin_no_users": "لا يوجد مستخدمون بعد.",
    },
    "fr": {
        "welcome_new": "Salut {name} ! Choisis la langue pour continuer.",
        "welcome_back": "Bonjour {name} 🖥! Voici le bot Algo & Pascal.",
        "menu_prompt": "Choisir les options :",
        "btn_convert_number": "🔢 Conversion d'un nombre",
        "btn_detect_number": "🔍 Détecter un nombre",
        "btn_algo_pascal": "🧮 Algo & Pascal",
        "btn_change_language": "🌐 Changer de langue",
        "btn_back": "⬅️ Retour",
        "btn_developer": "❤️ Développeur",
        "ask_number": "Envoie le nombre décimal à convertir :",
        "invalid_number": "Nombre invalide. Envoie un entier (ex. 125 ou -42).",
        "number_result": "Résultats :\nDécimal (0/9): {decimal}\nBinaire (1/0) : {binary}\nOctal (0/7) : {octal}\nHexadécimal (0/15) : {hexadecimal}",
        "ask_detect": "Envoie un nombre (0b1010, 7F, 075, 42...) :",
        "invalid_detect": "Impossible de déterminer la base. Vérifie l'écriture du nombre.",
        "detect_result": "Base détectée : {base_label}\nDécimal : {decimal}\nBinaire (1/0) : {binary}\nOctal (0/7) : {octal}\nHexadécimal (0/15) : {hexadecimal}",
        "ask_expression": "Envoie une expression du type A = ... ; tu peux enchaîner avec ';'.",
        "invalid_expression": "Vérifie la présence de '=' et des noms valides.",
        "choose_language": "Choisis la langue :",
        "start_hint": "Tap /start pour redémarrer le bot.",
        "cancelled": "Action annulée.",
        "admin_denied": "Fonction réservée à l'administrateur.",
        "admin_overview": "Liste des utilisateurs :",
        "admin_user_count": "Utilisateurs enregistrés : {count}",
        "admin_user_line": "- @{name} (ID : {id}, langue : {language})",
        "admin_no_users": "Aucun utilisateur pour le moment.",
    },
    "en": {
        "welcome_new": "Hi {name}! Pick your language to continue.",
        "welcome_back": "Welcome back {name} 💻! This is the Algo & Pascal bot.",
        "menu_prompt": "Choose Options :",
        "btn_convert_number": "🔢 Convert Number",
        "btn_detect_number": "🔍 Detect Number",
        "btn_algo_pascal": "🧮 Algo & Pascal",
        "btn_change_language": "🌐 Change Language",
        "btn_back": "⬅️ Back",
        "btn_developer": "❤️ Developer",
        "ask_number": "Send the decimal number you want to convert:",
        "invalid_number": "Could not parse that number. Please send an integer (e.g. 125 or -42).",
        "number_result": "Results :\nDecimal (0/9): {decimal}\nBinary (1/0): {binary}\nOctal (0/7): {octal}\nHexadecimal (0/15): {hexadecimal}",
        "ask_detect": "Send a number in any base (e.g. 0b1010, 7F, 075, 42...):",
        "invalid_detect": "Couldn't determine the base. Please check the number.",
        "detect_result": "Detected base: {base_label}\nDecimal (0/9): {decimal}\nBinary (1/0): {binary}\nOctal (0/7): {octal}\nHexadecimal (0/15): {hexadecimal}",
        "ask_expression": "Send an expression like A = ...; chain more with ';'.",
        "invalid_expression": "Make sure the expression contains '=' and valid variable names.",
        "choose_language": "Choose language:",
        "start_hint": "Click /start to restart the bot.",
        "cancelled": "Action cancelled.",
        "admin_denied": "This feature is restricted to the admin.",
        "admin_overview": "User list:",
        "admin_user_count": "Registered users: {count}",
        "admin_user_line": "- @{name} (ID: {id}, language: {language})",
        "admin_no_users": "No users recorded yet.",
    },
}

BASE_NAMES = {
    "ar": {
        2: "النظام الثنائي (1/0)",
        8: "النظام الثماني (0/7)",
        10: "النظام العشري (0/9)",
        16: "النظام السادس عشر (0/15)",
    },
    "fr": {
        2: "Binaire (1/0)",
        8: "Octal (0/7)",
        10: "Décimal (0/9)",
        16: "Hexadécimal (0/15)",
    },
    "en": {
        2: "Binary (1/0)",
        8: "Octal (0/7)",
        10: "Decimal (0/9)",
        16: "Hexadecimal (0/15)",
    },
}


def ensure_data_structure(data: Dict) -> Dict:
    if not isinstance(data, dict):
        data = {}
    data.setdefault("users", {})
    data.setdefault("admins", [])
    return data


def load_data() -> Dict:
    if not DATA_FILE.exists():
        return {"users": {}, "admins": []}
    try:
        content = DATA_FILE.read_text(encoding="utf-8")
        return ensure_data_structure(json.loads(content))
    except json.JSONDecodeError:
        logger.warning("data.json is corrupted. Recreating.")
        return {"users": {}, "admins": []}


def save_data(data: Dict) -> None:
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_language_keyboard(language: str = "ar", include_back: bool = False) -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{LANGUAGE_FLAGS['ar']} {LANGUAGE_LABELS['ar']}", callback_data="lang:ar"
            ),
            InlineKeyboardButton(
                f"{LANGUAGE_FLAGS['fr']} {LANGUAGE_LABELS['fr']}", callback_data="lang:fr"
            ),
            InlineKeyboardButton(
                f"{LANGUAGE_FLAGS['en']} {LANGUAGE_LABELS['en']}", callback_data="lang:en"
            ),
        ]
    ]
    if include_back:
        labels = MESSAGES.get(language, MESSAGES["en"])
        buttons.append(
            [InlineKeyboardButton(labels["btn_back"], callback_data="action:back")]
        )
    return InlineKeyboardMarkup(buttons)


def build_main_menu(language: str) -> InlineKeyboardMarkup:
    labels = MESSAGES[language]
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(labels["btn_convert_number"], callback_data="menu:convert")],
            [InlineKeyboardButton(labels["btn_detect_number"], callback_data="menu:detect")],
            [InlineKeyboardButton(labels["btn_algo_pascal"], callback_data="menu:algo")],
            [InlineKeyboardButton(labels["btn_change_language"], callback_data="menu:language")],
            [InlineKeyboardButton(labels["btn_developer"], url="https://t.me/V_X_L1")],
        ]
    )


def get_display_name(user) -> str:
    if user.full_name:
        return user.full_name
    if user.username:
        return user.username
    return str(user.id)


async def fetch_user_entry(user_id: int, username: str | None) -> Dict | None:
    async with DATA_LOCK:
        data = load_data()
        entry = data["users"].get(str(user_id))
        if entry:
            entry["username"] = username or entry.get("username") or ""
            save_data(data)
            return entry
        return None


async def store_user_language(user_id: int, language: str, username: str | None) -> None:
    async with DATA_LOCK:
        data = load_data()
        users = data["users"]
        admins: List[int] = data["admins"]
        users[str(user_id)] = {"language": language, "username": username or ""}
        if not admins:
            admins.append(user_id)
        save_data(data)


async def ensure_language(chat_data: Dict, user_id: int) -> str:
    lang = chat_data.get("lang")
    if lang:
        return lang
    async with DATA_LOCK:
        data = load_data()
    entry = data["users"].get(str(user_id))
    if entry:
        lang = entry.get("language", "ar")
        chat_data["lang"] = lang
        return lang
    return "ar"


async def delete_main_message(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, chat_data: Dict
) -> None:
    message_id = chat_data.pop("main_message_id", None)
    if not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as exc:
        if "message to delete not found" not in exc.message.lower():
            raise


async def edit_or_send(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    chat_data: Dict,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    message_id = chat_data.get("main_message_id")
    try:
        if message_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            return
    except BadRequest as exc:
        lowered = exc.message.lower()
        if "message is not modified" in lowered:
            return
        if "message to edit not found" not in lowered:
            raise
        message_id = None

    sent = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=reply_markup
    )
    chat_data["main_message_id"] = sent.message_id


async def show_menu(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    chat_data: Dict,
    lang: str,
    name: str,
) -> None:
    labels = MESSAGES[lang]
    text = f"{labels['welcome_back'].format(name=name)}\n{labels['menu_prompt']}"
    await edit_or_send(
        context, chat_id, chat_data, text, build_main_menu(lang)
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    chat_data = context.chat_data
    chat_id = update.effective_chat.id
    name = get_display_name(user)
    await delete_main_message(context, chat_id, chat_data)
    entry = await fetch_user_entry(user.id, user.username or user.full_name)
    if entry:
        lang = entry.get("language", "ar")
        chat_data["lang"] = lang
        await store_user_language(user.id, lang, user.username or user.full_name)
        await show_menu(context, chat_id, chat_data, lang, name)
    else:
        chat_data["awaiting_language"] = True
        await edit_or_send(
            context,
            chat_id,
            chat_data,
            MESSAGES["ar"]["welcome_new"].format(name=name),
            build_language_keyboard(include_back=False),
        )


async def handle_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    _, lang = query.data.split(":", 1)
    if lang not in MESSAGES:
        lang = "ar"
    chat_data = context.chat_data
    chat_data["lang"] = lang
    chat_data.pop("awaiting_language", None)
    chat_data.pop("mode", None)

    user = query.from_user
    await store_user_language(user.id, lang, user.username or user.full_name)

    await show_menu(
        context,
        query.message.chat_id,
        chat_data,
        lang,
        get_display_name(user),
    )


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    action = query.data.split(":", 1)[1]
    user = query.from_user
    chat_data = context.chat_data
    lang = await ensure_language(chat_data, user.id)
    labels = MESSAGES[lang]

    if action == "convert":
        chat_data["mode"] = "number"
        await edit_or_send(
            context,
            query.message.chat_id,
            chat_data,
            labels["ask_number"],
        )
        return
    if action == "detect":
        chat_data["mode"] = "detect"
        await edit_or_send(
            context,
            query.message.chat_id,
            chat_data,
            labels["ask_detect"],
        )
        return

    if action == "algo":
        chat_data["mode"] = "expression"
        await edit_or_send(
            context,
            query.message.chat_id,
            chat_data,
            labels["ask_expression"],
        )
        return

    if action == "language":
        chat_data["awaiting_language"] = True
        await edit_or_send(
            context,
            query.message.chat_id,
            chat_data,
            labels["choose_language"],
            build_language_keyboard(language=lang, include_back=True),
        )
        return

async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    if query.data != "action:back":
        return

    user = query.from_user
    chat_data = context.chat_data
    chat_data.pop("awaiting_language", None)
    lang = await ensure_language(chat_data, user.id)
    await show_menu(
        context,
        query.message.chat_id,
        chat_data,
        lang,
        get_display_name(user),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return

    chat_data = context.chat_data
    lang = await ensure_language(chat_data, user.id)
    labels = MESSAGES[lang]
    name = get_display_name(user)
    chat_id = update.effective_chat.id
    mode = chat_data.get("mode")
    text = message.text.strip()

    if chat_data.get("awaiting_language"):
        await edit_or_send(
            context,
            chat_id,
            chat_data,
            labels["choose_language"],
            build_language_keyboard(language=lang, include_back=True),
        )
        return

    if mode is None and "=" in text:
        mode = "expression"
        chat_data["mode"] = mode

    if mode == "number":
        try:
            value = int(text)
        except ValueError:
            await message.reply_text(labels["invalid_number"])
            return

        conversions = convert_number_to_bases(value)
        result_text = labels["number_result"].format(value=value, **conversions)
        await message.reply_text(f"{result_text}\n\n{labels['start_hint']}")
        chat_data.pop("mode", None)
        await show_menu(context, chat_id, chat_data, lang, name)
        return
    if mode == "detect":
        try:
            value, base = detect_number_system(text)
        except ValueError:
            await message.reply_text(labels["invalid_detect"])
            return

        conversions = convert_number_to_bases(value)
        base_names = BASE_NAMES.get(lang, BASE_NAMES["en"])
        base_label = base_names.get(base, str(base))
        result_text = labels["detect_result"].format(
            base_label=base_label,
            **conversions,
        )
        await message.reply_text(f"{result_text}\n\n{labels['start_hint']}")
        chat_data.pop("mode", None)
        await show_menu(context, chat_id, chat_data, lang, name)
        return

    if mode == "expression":
        try:
            snippets = build_response(text, algo_name="Calcul", pascal_name="Calcul")
        except ValueError:
            await message.reply_text(labels["invalid_expression"])
            return

        algo_code = escape(snippets["algo"])
        pascal_code = escape(snippets["pascal"])
        hint_html = escape(labels["start_hint"])
        payload = (
            f"<b>ALGO</b>\n<pre>{algo_code}</pre>\n\n"
            f"<b>Pascal</b>\n<pre>{pascal_code}</pre>\n\n{hint_html}"
        )
        await message.reply_html(payload)
        chat_data.pop("mode", None)
        await show_menu(context, chat_id, chat_data, lang, name)
        return

    await show_menu(context, chat_id, chat_data, lang, name)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    chat_data = context.chat_data
    lang = await ensure_language(chat_data, user.id)
    labels = MESSAGES[lang]
    name = get_display_name(user)

    chat_data.pop("mode", None)
    chat_data.pop("awaiting_language", None)
    await update.message.reply_text(labels["cancelled"])
    await show_menu(
        context,
        update.effective_chat.id,
        chat_data,
        lang,
        name,
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    lang = await ensure_language(context.chat_data, user.id)
    labels = MESSAGES[lang]

    async with DATA_LOCK:
        data = load_data()
    admins: List[int] = data.get("admins", [])

    if user.id not in admins:
        await update.message.reply_text(labels["admin_denied"])
        return

    users = data.get("users", {})
    if not users:
        await update.message.reply_text(labels["admin_no_users"])
        return

    lines = [
        labels["admin_overview"],
        labels["admin_user_count"].format(count=len(users)),
    ]
    for user_id, info in users.items():
        lines.append(
            labels["admin_user_line"].format(
                name=info.get("username") or "—",
                id=user_id,
                language=info.get("language", "ar"),
            )
        )
    await update.message.reply_text("\n".join(lines))


def build_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(handle_language, pattern=r"^lang:"))
    application.add_handler(CallbackQueryHandler(handle_menu, pattern=r"^menu:"))
    application.add_handler(CallbackQueryHandler(handle_action, pattern=r"^action:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    asyncio.set_event_loop(asyncio.new_event_loop())
    application = build_application()
    logger.info("Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
