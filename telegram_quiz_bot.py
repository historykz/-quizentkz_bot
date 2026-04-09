import asyncio
import logging
import os
import random
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    PreCheckoutQueryHandler,
    filters,
)

# =========================================================
# ENV
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS_RAW = os.getenv("ADMINS", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # @username or -100...
REQUIRED_REFERRALS = int(os.getenv("REQUIRED_REFERRALS", "5"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "quiz_platform.db").strip()
STARS_ENABLED = os.getenv("STARS_ENABLED", "0").strip() == "1"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing")
if not ADMINS_RAW:
    raise ValueError("ADMINS is missing")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID is missing")

ADMINS = {int(x.strip()) for x in ADMINS_RAW.split(",") if x.strip()}

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# DB
# =========================================================

def db_conn():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                referred_by INTEGER,
                referral_code TEXT UNIQUE,
                referrals_confirmed INTEGER DEFAULT 0,
                is_blocked INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                is_paid INTEGER DEFAULT 0,
                price_stars INTEGER DEFAULT 0,
                required_referrals INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_id INTEGER NOT NULL,
                question_text TEXT NOT NULL,
                option_1 TEXT NOT NULL,
                option_2 TEXT NOT NULL,
                option_3 TEXT NOT NULL,
                option_4 TEXT NOT NULL,
                correct_option INTEGER NOT NULL,
                explanation TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(section_id) REFERENCES sections(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS access_rights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                section_id INTEGER NOT NULL,
                access_type TEXT NOT NULL,
                granted_by INTEGER,
                granted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, section_id),
                FOREIGN KEY(section_id) REFERENCES sections(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS referral_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_user_id INTEGER NOT NULL UNIQUE,
                is_channel_verified INTEGER DEFAULT 0,
                is_credited INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                section_id INTEGER NOT NULL,
                amount_stars INTEGER NOT NULL,
                payload TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                paid_at TEXT
            );

            CREATE TABLE IF NOT EXISTS quiz_sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                section_id INTEGER NOT NULL,
                total_questions INTEGER NOT NULL,
                current_index INTEGER DEFAULT 0,
                correct_answers INTEGER DEFAULT 0,
                finished INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS quiz_session_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                question_id INTEGER NOT NULL,
                poll_id TEXT UNIQUE,
                asked INTEGER DEFAULT 0,
                answered INTEGER DEFAULT 0,
                chosen_option INTEGER,
                is_correct INTEGER,
                FOREIGN KEY(session_id) REFERENCES quiz_sessions(session_id) ON DELETE CASCADE,
                FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
            );
            """
        )
        conn.commit()


def ensure_user(user):
    username = user.username or ""
    full_name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    referral_code = f"ref_{user.id}"
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (user_id, username, full_name, referral_code)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name
            """,
            (user.id, username, full_name, referral_code),
        )
        conn.commit()


def user_exists(user_id: int) -> bool:
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone() is not None


def set_referrer_if_first_time(user_id: int, referrer_id: Optional[int]) -> bool:
    if not referrer_id or referrer_id == user_id:
        return False
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return False
        if row[0] is not None:
            return False
        cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, user_id))
        cur.execute(
            "INSERT OR IGNORE INTO referral_events (referrer_id, referred_user_id) VALUES (?, ?)",
            (referrer_id, user_id),
        )
        conn.commit()
        return True


def get_referral_stats(user_id: int):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                SUM(CASE WHEN is_channel_verified=1 THEN 1 ELSE 0 END) AS verified,
                SUM(CASE WHEN is_credited=1 THEN 1 ELSE 0 END) AS credited,
                COUNT(*) AS total
            FROM referral_events
            WHERE referrer_id=?
            """,
            (user_id,),
        )
        row = cur.fetchone()
        return {
            "verified": row[0] or 0,
            "credited": row[1] or 0,
            "total": row[2] or 0,
        }


def update_referral_credit(referrer_id: int):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM referral_events
            WHERE referrer_id=? AND is_channel_verified=1 AND is_credited=1
            """,
            (referrer_id,),
        )
        credited = cur.fetchone()[0]
        cur.execute(
            "UPDATE users SET referrals_confirmed=? WHERE user_id=?",
            (credited, referrer_id),
        )
        conn.commit()


def verify_referral_signup(user_id: int):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, referrer_id FROM referral_events WHERE referred_user_id=?",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            "UPDATE referral_events SET is_channel_verified=1, is_credited=1 WHERE id=?",
            (row[0],),
        )
        conn.commit()
        update_referral_credit(row[1])
        return row[1]


def create_section(slug: str, title: str, description: str = ""):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sections (slug, title, description) VALUES (?, ?, ?)",
            (slug, title, description),
        )
        conn.commit()


def set_section_paid(slug: str, is_paid: int, price_stars: int = 0, required_referrals: int = 0):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE sections
            SET is_paid=?, price_stars=?, required_referrals=?
            WHERE slug=?
            """,
            (is_paid, price_stars, required_referrals, slug),
        )
        conn.commit()


def list_sections(active_only: bool = True):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        q = "SELECT * FROM sections"
        if active_only:
            q += " WHERE is_active=1"
        q += " ORDER BY id"
        cur.execute(q)
        return cur.fetchall()


def get_section_by_slug(slug: str):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sections WHERE slug=?", (slug,))
        return cur.fetchone()


def get_section_by_id(section_id: int):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sections WHERE id=?", (section_id,))
        return cur.fetchone()


def grant_access(user_id: int, section_id: int, access_type: str, granted_by: Optional[int]):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO access_rights (user_id, section_id, access_type, granted_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, section_id) DO UPDATE SET
                access_type=excluded.access_type,
                granted_by=excluded.granted_by,
                granted_at=CURRENT_TIMESTAMP
            """,
            (user_id, section_id, access_type, granted_by),
        )
        conn.commit()


def revoke_access(user_id: int, section_id: int):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM access_rights WHERE user_id=? AND section_id=?", (user_id, section_id))
        conn.commit()


def has_access(user_id: int, section_id: int) -> bool:
    section = get_section_by_id(section_id)
    if not section:
        return False
    if int(section["is_paid"]) == 0:
        return True
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM access_rights WHERE user_id=? AND section_id=?",
            (user_id, section_id),
        )
        if cur.fetchone():
            return True
        cur.execute("SELECT referrals_confirmed FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        referrals = row[0] if row else 0
        needed = int(section["required_referrals"] or 0)
        return needed > 0 and referrals >= needed


def count_questions(section_id: int) -> int:
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM questions WHERE section_id=? AND is_active=1",
            (section_id,),
        )
        return cur.fetchone()[0]


def save_question(section_id: int, question_text: str, options: list[str], correct_option: int, explanation: str = ""):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO questions (
                section_id, question_text, option_1, option_2, option_3, option_4,
                correct_option, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (section_id, question_text, options[0], options[1], options[2], options[3], correct_option, explanation),
        )
        conn.commit()


def get_random_question_ids(section_id: int, limit: int) -> list[int]:
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM questions WHERE section_id=? AND is_active=1",
            (section_id,),
        )
        ids = [r[0] for r in cur.fetchall()]
        random.shuffle(ids)
        return ids[:limit]


def get_question(question_id: int):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM questions WHERE id=?", (question_id,))
        return cur.fetchone()


def create_quiz_session(user_id: int, section_id: int, total_questions: int, question_ids: list[int]) -> str:
    session_id = f"{user_id}_{section_id}_{int(datetime.utcnow().timestamp())}_{random.randint(1000, 9999)}"
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO quiz_sessions (session_id, user_id, section_id, total_questions) VALUES (?, ?, ?, ?)",
            (session_id, user_id, section_id, total_questions),
        )
        cur.executemany(
            "INSERT INTO quiz_session_questions (session_id, question_id) VALUES (?, ?)",
            [(session_id, qid) for qid in question_ids],
        )
        conn.commit()
    return session_id


def get_session(session_id: str):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM quiz_sessions WHERE session_id=?", (session_id,))
        return cur.fetchone()


def get_next_unasked_question(session_id: str):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT qsq.id AS row_id, q.*
            FROM quiz_session_questions qsq
            JOIN questions q ON q.id = qsq.question_id
            WHERE qsq.session_id=? AND qsq.asked=0
            ORDER BY qsq.id
            LIMIT 1
            """,
            (session_id,),
        )
        return cur.fetchone()


def mark_question_sent(session_id: str, row_id: int, poll_id: str):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE quiz_session_questions SET asked=1, poll_id=? WHERE id=?",
            (poll_id, row_id),
        )
        cur.execute(
            "UPDATE quiz_sessions SET current_index=current_index+1 WHERE session_id=?",
            (session_id,),
        )
        conn.commit()


def record_poll_answer(poll_id: str, chosen_option: int):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT qsq.id, qsq.session_id, q.correct_option
            FROM quiz_session_questions qsq
            JOIN questions q ON q.id = qsq.question_id
            WHERE qsq.poll_id=?
            """,
            (poll_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        is_correct = 1 if chosen_option == int(row["correct_option"]) else 0
        cur.execute(
            """
            UPDATE quiz_session_questions
            SET answered=1, chosen_option=?, is_correct=?
            WHERE id=?
            """,
            (chosen_option, is_correct, row["id"]),
        )
        if is_correct:
            cur.execute(
                "UPDATE quiz_sessions SET correct_answers=correct_answers+1 WHERE session_id=?",
                (row["session_id"],),
            )
        conn.commit()
        return row["session_id"]


def finish_session(session_id: str):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE quiz_sessions SET finished=1 WHERE session_id=?", (session_id,))
        conn.commit()


def create_payment(user_id: int, section_id: int, amount_stars: int) -> str:
    payload = f"stars:{user_id}:{section_id}:{int(datetime.utcnow().timestamp())}"
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO payments (user_id, section_id, amount_stars, payload) VALUES (?, ?, ?, ?)",
            (user_id, section_id, amount_stars, payload),
        )
        conn.commit()
    return payload


def get_payment_by_payload(payload: str):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM payments WHERE payload=?", (payload,))
        return cur.fetchone()


def mark_payment_paid(payload: str):
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE payments SET status='paid', paid_at=CURRENT_TIMESTAMP WHERE payload=?",
            (payload,),
        )
        conn.commit()


# =========================================================
# PARSER
# =========================================================
OPTION_RE = re.compile(r"^[A-DÐ-Ða-dÐ°-Ð³1-4]\s*[\)\.]\s*(.+)$")
EXPLANATION_RE = re.compile(r"^(Ð¾Ð±ÑÑÑÐ½ÐµÐ½Ð¸Ðµ|Ð¿Ð¾ÑÑÐ½ÐµÐ½Ð¸Ðµ|explanation)\s*:\s*(.+)$", re.IGNORECASE)


def parse_questions_block(raw_text: str):
    """
    Ð¤Ð¾ÑÐ¼Ð°Ñ Ð¾Ð´Ð½Ð¾Ð³Ð¾ Ð²Ð¾Ð¿ÑÐ¾ÑÐ°:

    Ð ÐºÐ°ÐºÐ¾Ð¼ Ð³Ð¾Ð´Ñ ÑÐ¼ÐµÑ ÐÐ±ÑÐ»Ð°Ð¹ÑÐ°Ð½?
    A) 1778
    B) 1771
    C) 1767
    D) 1781*
    ÐÐ±ÑÑÑÐ½ÐµÐ½Ð¸Ðµ: ...

    ÐÐ¾Ð¿ÑÐ¾ÑÑ ÑÐ°Ð·Ð´ÐµÐ»ÑÑÑÑÑ Ð¿ÑÑÑÐ¾Ð¹ ÑÑÑÐ¾ÐºÐ¾Ð¹.
    Ð£ Ð¿ÑÐ°Ð²Ð¸Ð»ÑÐ½Ð¾Ð³Ð¾ Ð²Ð°ÑÐ¸Ð°Ð½ÑÐ° ÑÑÐ°Ð²Ð¸ÑÑÑ * Ð² ÐºÐ¾Ð½ÑÐµ.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", raw_text.strip()) if b.strip()]
    parsed = []

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 5:
            raise ValueError(f"Ð¡Ð»Ð¸ÑÐºÐ¾Ð¼ ÐºÐ¾ÑÐ¾ÑÐºÐ¸Ð¹ Ð±Ð»Ð¾Ðº:\n{block}")

        question = lines[0]
        options = []
        correct = None
        explanation = ""

        for line in lines[1:]:
            ex_match = EXPLANATION_RE.match(line)
            if ex_match:
                explanation = ex_match.group(2).strip()
                continue

            opt_match = OPTION_RE.match(line)
            if opt_match and len(options) < 4:
                option_text = opt_match.group(1).strip()
                is_correct = option_text.endswith("*")
                option_text = option_text.rstrip("*").strip()
                options.append(option_text)
                if is_correct:
                    correct = len(options) - 1
                continue

        if len(options) != 4:
            raise ValueError(f"Ð£ Ð²Ð¾Ð¿ÑÐ¾ÑÐ° Ð´Ð¾Ð»Ð¶Ð½Ð¾ Ð±ÑÑÑ ÑÐ¾Ð²Ð½Ð¾ 4 Ð²Ð°ÑÐ¸Ð°Ð½ÑÐ°:\n{block}")
        if correct is None:
            raise ValueError(f"ÐÐµ Ð½Ð°Ð¹Ð´ÐµÐ½ Ð¿ÑÐ°Ð²Ð¸Ð»ÑÐ½ÑÐ¹ Ð¾ÑÐ²ÐµÑ ÑÐ¾ Ð·Ð²ÐµÐ·Ð´Ð¾ÑÐºÐ¾Ð¹:\n{block}")

        parsed.append({
            "question": question,
            "options": options,
            "correct": correct,
            "explanation": explanation,
        })

    return parsed


# =========================================================
# HELPERS
# =========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.RESTRICTED,
        }
    except Exception:
        return False


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ð Ð Ð°Ð·Ð´ÐµÐ»Ñ", callback_data="menu:sections")],
        [InlineKeyboardButton("ð¥ ÐÑÐ¸Ð³Ð»Ð°ÑÐ¸ÑÑ Ð´ÑÑÐ·ÐµÐ¹", callback_data="menu:referrals")],
        [InlineKeyboardButton("â ÐÑÐ¾Ð²ÐµÑÐ¸ÑÑ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÑ", callback_data="menu:checksub")],
    ])


def sections_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows:
        label = f"ð {row['title']}"
        if int(row['is_paid']) == 1:
            label = f"ð {row['title']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"section:{row['slug']}")])
    buttons.append([InlineKeyboardButton("â¬ï¸ ÐÐ°Ð·Ð°Ð´", callback_data="menu:home")])
    return InlineKeyboardMarkup(buttons)


def section_actions_keyboard(slug: str, has_access_flag: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_access_flag:
        rows.append([
            InlineKeyboardButton("15 Ð²Ð¾Ð¿ÑÐ¾ÑÐ¾Ð²", callback_data=f"startquiz:{slug}:15"),
            InlineKeyboardButton("20 Ð²Ð¾Ð¿ÑÐ¾ÑÐ¾Ð²", callback_data=f"startquiz:{slug}:20"),
        ])
        rows.append([InlineKeyboardButton("30 Ð²Ð¾Ð¿ÑÐ¾ÑÐ¾Ð²", callback_data=f"startquiz:{slug}:30")])
    else:
        rows.append([InlineKeyboardButton("ð¥ ÐÑÐºÑÑÑÑ Ð·Ð° Ð´ÑÑÐ·ÐµÐ¹", callback_data=f"refunlock:{slug}")])
        if STARS_ENABLED:
            rows.append([InlineKeyboardButton("â­ ÐÑÐ¿Ð¸ÑÑ Ð´Ð¾ÑÑÑÐ¿", callback_data=f"buy:{slug}")])
    rows.append([InlineKeyboardButton("â¬ï¸ Ð ÑÐ°Ð·Ð´ÐµÐ»Ð°Ð¼", callback_data="menu:sections")])
    return InlineKeyboardMarkup(rows)


async def safe_send_long_message(target, text: str):
    chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)]
    for chunk in chunks:
        await target.reply_text(chunk)


# =========================================================
# USER COMMANDS
# =========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    was_existing = user_exists(user.id)
    ensure_user(user)

    referrer_id = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg.split("_", 1)[1])
            except Exception:
                referrer_id = None

    referral_attached = False
    if not was_existing and referrer_id:
        referral_attached = set_referrer_if_first_time(user.id, referrer_id)

    subscribed = await is_subscribed(context, user.id)
    if subscribed:
        credited_to = verify_referral_signup(user.id)
        if credited_to:
            try:
                await context.bot.send_message(
                    chat_id=credited_to,
                    text=f"ð ÐÐ°Ð¼ Ð·Ð°ÑÑÐ¸ÑÐ°Ð½ Ð½Ð¾Ð²ÑÐ¹ Ð´ÑÑÐ³. Ð¢ÐµÐºÑÑÐ¸Ð¹ Ð¿ÑÐ¾Ð³ÑÐµÑÑ: {get_referral_stats(credited_to)['credited']}/{REQUIRED_REFERRALS}",
                )
            except Exception:
                pass

    text = (
        "<b>ÐÐ¾Ð±ÑÐ¾ Ð¿Ð¾Ð¶Ð°Ð»Ð¾Ð²Ð°ÑÑ Ð² Ð±Ð¾Ñ-Ð²Ð¸ÐºÑÐ¾ÑÐ¸Ð½Ñ</b>\n\n"
        "ÐÐ´ÐµÑÑ Ð¼Ð¾Ð¶Ð½Ð¾ Ð²ÑÐ±ÑÐ°ÑÑ ÑÐ°Ð·Ð´ÐµÐ», Ð¿ÑÐ¾Ð¹ÑÐ¸ ÑÐµÑÑ Ð½Ð° 15 / 20 / 30 Ð²Ð¾Ð¿ÑÐ¾ÑÐ¾Ð² Ð¸ Ð¾ÑÐºÑÑÑÑ Ð·Ð°ÐºÑÑÑÑÐµ ÑÐµÐ¼Ñ.\n\n"
        f"ÐÐ»Ñ Ð´Ð¾ÑÑÑÐ¿Ð° Ðº Ð·Ð°ÐºÑÑÑÑÐ¼ ÑÐµÐ¼Ð°Ð¼ Ð¼Ð¾Ð¶Ð½Ð¾ Ð»Ð¸Ð±Ð¾ Ð¿Ð¾Ð»ÑÑÐ¸ÑÑ Ð´Ð¾ÑÑÑÐ¿ Ð¾Ñ Ð°Ð´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¾ÑÐ°, Ð»Ð¸Ð±Ð¾ Ð¿ÑÐ¸Ð³Ð»Ð°ÑÐ¸ÑÑ {REQUIRED_REFERRALS} Ð´ÑÑÐ·ÐµÐ¹, ÐºÐ¾ÑÐ¾ÑÑÐµ <b>Ð·Ð°Ð¿ÑÑÑÑÑ Ð±Ð¾ÑÐ° Ð¸ Ð¿Ð¾Ð´Ð¿Ð¸ÑÑÑÑÑ Ð½Ð° ÐºÐ°Ð½Ð°Ð»</b>."
    )
    if referral_attached:
        text += "\n\nâ ÐÑ Ð¿ÑÐ¸ÑÐ»Ð¸ Ð¿Ð¾ Ð¿ÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ñ. ÐÐ¾ÑÐ»Ðµ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ¸ Ð½Ð° ÐºÐ°Ð½Ð°Ð» Ð²Ð°Ñ Ð²ÑÐ¾Ð´ Ð±ÑÐ´ÐµÑ Ð·Ð°ÑÑÐ¸ÑÐ°Ð½ Ð¿ÑÐ¸Ð³Ð»Ð°ÑÐ¸Ð²ÑÐµÐ¼Ñ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ."

    await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    user = query.from_user
    ensure_user(user)

    if action == "menu:home":
        await query.edit_message_text(
            "ÐÐ»Ð°Ð²Ð½Ð¾Ðµ Ð¼ÐµÐ½Ñ:",
            reply_markup=main_menu_keyboard(),
        )
        return

    if action == "menu:sections":
        rows = list_sections(active_only=True)
        await query.edit_message_text("ÐÑÐ±ÐµÑÐ¸ÑÐµ ÑÐ°Ð·Ð´ÐµÐ»:", reply_markup=sections_keyboard(rows))
        return

    if action == "menu:referrals":
        ref_link = f"https://t.me/{context.bot.username}?start=ref_{user.id}"
        stats = get_referral_stats(user.id)
        text = (
            "<b>ÐÑÐ¸Ð³Ð»Ð°ÑÐµÐ½Ð¸Ñ</b>\n\n"
            f"ÐÐ°ÑÐ° ÑÑÑÐ»ÐºÐ°:\n<code>{ref_link}</code>\n\n"
            f"ÐÐ°ÑÑÐ¸ÑÐ°Ð½Ð¾: <b>{stats['credited']}/{REQUIRED_REFERRALS}</b>\n"
            "Ð£ÑÐ»Ð¾Ð²Ð¸Ñ:\n"
            "1) Ð´ÑÑÐ³ Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð¾ÑÐºÑÑÑÑ Ð±Ð¾ÑÐ° Ð¿Ð¾ Ð²Ð°ÑÐµÐ¹ ÑÑÑÐ»ÐºÐµ\n"
            "2) Ð´ÑÑÐ³ Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ°ÑÑÑÑ Ð½Ð° ÐºÐ°Ð½Ð°Ð»\n"
            "3) Ð·Ð°ÑÑÐ¸ÑÑÐ²Ð°ÑÑÑÑ ÑÐ¾Ð»ÑÐºÐ¾ ÑÐ½Ð¸ÐºÐ°Ð»ÑÐ½ÑÐµ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ð¸"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("â ÐÑÐ¾Ð²ÐµÑÐ¸ÑÑ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÑ", callback_data="menu:checksub")],
            [InlineKeyboardButton("â¬ï¸ ÐÐ°Ð·Ð°Ð´", callback_data="menu:home")],
        ]))
        return

    if action == "menu:checksub":
        subscribed = await is_subscribed(context, user.id)
        if subscribed:
            credited_to = verify_referral_signup(user.id)
            text = "â ÐÐ¾Ð´Ð¿Ð¸ÑÐºÐ° Ð½Ð° ÐºÐ°Ð½Ð°Ð» Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÐµÐ½Ð°."
            if credited_to:
                text += " ÐÐ°Ñ Ð²ÑÐ¾Ð´ Ð·Ð°ÑÑÐ¸ÑÐ°Ð½ Ð¿ÑÐ¸Ð³Ð»Ð°ÑÐ¸Ð²ÑÐµÐ¼Ñ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ."
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("â¬ï¸ Ð Ð¼ÐµÐ½Ñ", callback_data="menu:home")]
            ]))
        else:
            await query.edit_message_text(
                "â ÐÐ¾Ð´Ð¿Ð¸ÑÐºÐ° Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°. Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ¸ÑÐµÑÑ Ð½Ð° ÐºÐ°Ð½Ð°Ð», Ð¿Ð¾ÑÐ¾Ð¼ Ð½Ð°Ð¶Ð¼Ð¸ÑÐµ Ð¿ÑÐ¾Ð²ÐµÑÐºÑ ÑÐ½Ð¾Ð²Ð°.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("ð ÐÑÐ¾Ð²ÐµÑÐ¸ÑÑ ÑÐ½Ð¾Ð²Ð°", callback_data="menu:checksub")],
                    [InlineKeyboardButton("â¬ï¸ Ð Ð¼ÐµÐ½Ñ", callback_data="menu:home")],
                ]),
            )
        return


async def section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slug = query.data.split(":", 1)[1]
    section = get_section_by_slug(slug)
    if not section:
        await query.edit_message_text("Ð Ð°Ð·Ð´ÐµÐ» Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½.")
        return

    allowed = has_access(query.from_user.id, section["id"])
    total = count_questions(section["id"])
    text = f"<b>{section['title']}</b>\n\n{section['description'] or 'ÐÐµÐ· Ð¾Ð¿Ð¸ÑÐ°Ð½Ð¸Ñ.'}\n\nÐÐ¾Ð¿ÑÐ¾ÑÐ¾Ð² Ð² Ð±Ð°Ð·Ðµ: <b>{total}</b>"
    if int(section["is_paid"]) == 1 and not allowed:
        text += "\n\nð Ð­ÑÐ¾ Ð·Ð°ÐºÑÑÑÑÐ¹ ÑÐ°Ð·Ð´ÐµÐ»."
        if int(section["required_referrals"] or 0) > 0:
            text += f"\nÐÑÐºÑÑÐ²Ð°ÐµÑÑÑ Ð¿Ð¾ÑÐ»Ðµ {section['required_referrals']} Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÐµÐ½Ð½ÑÑ Ð´ÑÑÐ·ÐµÐ¹."
        if int(section["price_stars"] or 0) > 0 and STARS_ENABLED:
            text += f"\nÐ¦ÐµÐ½Ð°: {section['price_stars']} â­"
    elif allowed:
        text += "\n\nâ Ð£ Ð²Ð°Ñ ÐµÑÑÑ Ð´Ð¾ÑÑÑÐ¿."

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=section_actions_keyboard(slug, allowed),
    )


async def refunlock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slug = query.data.split(":", 1)[1]
    section = get_section_by_slug(slug)
    if not section:
        await query.edit_message_text("Ð Ð°Ð·Ð´ÐµÐ» Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½.")
        return
    stats = get_referral_stats(query.from_user.id)
    need = int(section["required_referrals"] or REQUIRED_REFERRALS or 5)
    text = (
        f"Ð§ÑÐ¾Ð±Ñ Ð¾ÑÐºÑÑÑÑ <b>{section['title']}</b>, Ð½ÑÐ¶Ð½Ð¾ {need} Ð´ÑÑÐ·ÐµÐ¹.\n\n"
        f"ÐÐ°Ñ Ð¿ÑÐ¾Ð³ÑÐµÑÑ: <b>{stats['credited']}/{need}</b>\n\n"
        f"ÐÐ°ÑÐ° ÑÑÑÐ»ÐºÐ°:\n<code>https://t.me/{context.bot.username}?start=ref_{query.from_user.id}</code>"
    )
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("ð ÐÐ±Ð½Ð¾Ð²Ð¸ÑÑ Ð¿ÑÐ¾Ð³ÑÐµÑÑ", callback_data=f"section:{slug}")],
            [InlineKeyboardButton("â¬ï¸ ÐÐ°Ð·Ð°Ð´", callback_data=f"section:{slug}")],
        ]),
    )


async def start_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, slug, limit_str = query.data.split(":")
    section = get_section_by_slug(slug)
    if not section:
        await query.edit_message_text("Ð Ð°Ð·Ð´ÐµÐ» Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½.")
        return

    if not has_access(query.from_user.id, section["id"]):
        await query.edit_message_text("Ð£ Ð²Ð°Ñ Ð½ÐµÑ Ð´Ð¾ÑÑÑÐ¿Ð° Ðº ÑÑÐ¾Ð¼Ñ ÑÐ°Ð·Ð´ÐµÐ»Ñ.")
        return

    available = count_questions(section["id"])
    limit = min(int(limit_str), available)
    if limit <= 0:
        await query.edit_message_text("Ð ÑÑÐ¾Ð¼ ÑÐ°Ð·Ð´ÐµÐ»Ðµ Ð¿Ð¾ÐºÐ° Ð½ÐµÑ Ð²Ð¾Ð¿ÑÐ¾ÑÐ¾Ð².")
        return

    qids = get_random_question_ids(section["id"], limit)
    session_id = create_quiz_session(query.from_user.id, section["id"], len(qids), qids)

    await query.edit_message_text(
        f"ð Ð¢ÐµÑÑ Ð½Ð°ÑÐ°Ñ: {section['title']}\nÐÐ¾Ð¿ÑÐ¾ÑÐ¾Ð²: {len(qids)}\n\nÐ¯ ÑÐµÐ¹ÑÐ°Ñ Ð½Ð°ÑÐ½Ñ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÑÑÑ Ð²Ð¸ÐºÑÐ¾ÑÐ¸Ð½Ñ Ð¿Ð¾ Ð¾ÑÐµÑÐµÐ´Ð¸.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("â¬ï¸ Ð Ð¼ÐµÐ½Ñ", callback_data="menu:home")]
        ]),
    )
    await send_next_poll(context, query.from_user.id, session_id)


async def send_next_poll(context: ContextTypes.DEFAULT_TYPE, user_id: int, session_id: str):
    session = get_session(session_id)
    if not session or int(session["finished"]) == 1:
        return

    next_question = get_next_unasked_question(session_id)
    if not next_question:
        finish_session(session_id)
        total = int(session["total_questions"])
        correct = int(session["correct_answers"])
        percent = round((correct / total) * 100, 1) if total else 0
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "ð Ð¢ÐµÑÑ Ð·Ð°Ð²ÐµÑÑÐµÐ½\n\n"
                f"ÐÑÐ°Ð²Ð¸Ð»ÑÐ½ÑÑ Ð¾ÑÐ²ÐµÑÐ¾Ð²: {correct}/{total}\n"
                f"Ð ÐµÐ·ÑÐ»ÑÑÐ°Ñ: {percent}%"
            ),
        )
        return

    message = await context.bot.send_poll(
        chat_id=user_id,
        question=next_question["question_text"],
        options=[
            next_question["option_1"],
            next_question["option_2"],
            next_question["option_3"],
            next_question["option_4"],
        ],
        type="quiz",
        correct_option_id=int(next_question["correct_option"]),
        explanation=next_question["explanation"] or None,
        is_anonymous=False,
    )
    mark_question_sent(session_id, next_question["row_id"], message.poll.id)


async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    if not answer.option_ids:
        return
    chosen = answer.option_ids[0]
    session_id = record_poll_answer(answer.poll_id, chosen)
    if not session_id:
        return
    await asyncio.sleep(0.5)
    await send_next_poll(context, answer.user.id, session_id)


# =========================================================
# PAYMENTS (Stars) - optional
# =========================================================
async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not STARS_ENABLED:
        await query.edit_message_text("ÐÐ¿Ð»Ð°ÑÐ° ÑÐµÐ¹ÑÐ°Ñ Ð¾ÑÐºÐ»ÑÑÐµÐ½Ð°.")
        return
    slug = query.data.split(":", 1)[1]
    section = get_section_by_slug(slug)
    if not section:
        await query.edit_message_text("Ð Ð°Ð·Ð´ÐµÐ» Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½.")
        return

    amount = int(section["price_stars"] or 0)
    if amount <= 0:
        await query.edit_message_text("ÐÐ»Ñ ÑÑÐ¾Ð³Ð¾ ÑÐ°Ð·Ð´ÐµÐ»Ð° ÑÐµÐ½Ð° Ð½Ðµ Ð·Ð°Ð´Ð°Ð½Ð°.")
        return

    payload = create_payment(query.from_user.id, section["id"], amount)
    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title=f"ÐÐ¾ÑÑÑÐ¿ Ðº ÑÐ°Ð·Ð´ÐµÐ»Ñ: {section['title']}",
        description=section['description'] or f"ÐÐ¾ÐºÑÐ¿ÐºÐ° Ð´Ð¾ÑÑÑÐ¿Ð° Ðº ÑÐ°Ð·Ð´ÐµÐ»Ñ {section['title']}",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=section['title'], amount=amount)],
    )
    await query.edit_message_text("Ð¡ÑÐµÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½ Ð² ÑÑÐ¾Ñ ÑÐ°Ñ.")


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    payload = query.invoice_payload
    payment = get_payment_by_payload(payload)
    if payment is None or payment["status"] not in ("created", "paid"):
        await query.answer(ok=False, error_message="ÐÐ»Ð°ÑÐµÐ¶ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½")
        return
    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.successful_payment:
        return
    payload = msg.successful_payment.invoice_payload
    payment = get_payment_by_payload(payload)
    if not payment:
        return
    mark_payment_paid(payload)
    grant_access(payment["user_id"], payment["section_id"], "stars", None)
    section = get_section_by_id(payment["section_id"])
    await msg.reply_text(f"â ÐÐ¿Ð»Ð°ÑÐ° Ð¿ÑÐ¾ÑÐ»Ð° ÑÑÐ¿ÐµÑÐ½Ð¾. ÐÐ¾ÑÑÑÐ¿ Ðº ÑÐ°Ð·Ð´ÐµÐ»Ñ Â«{section['title']}Â» Ð¾ÑÐºÑÑÑ.")


# =========================================================
# ADMIN COMMANDS
# =========================================================
ADMIN_HELP = """<b>ÐÐ´Ð¼Ð¸Ð½-ÐºÐ¾Ð¼Ð°Ð½Ð´Ñ</b>

/create_section <slug> | <ÐÐ°Ð·Ð²Ð°Ð½Ð¸Ðµ> | <ÐÐ¿Ð¸ÑÐ°Ð½Ð¸Ðµ>
/set_paid <slug> <stars> <friends>
/set_free <slug>
/list_sections_admin
/add_questions <slug>
/grant <user_id> <slug>
/revoke <user_id> <slug>
/userinfo <user_id>

Ð¤Ð¾ÑÐ¼Ð°Ñ Ð²Ð¾Ð¿ÑÐ¾ÑÐ¾Ð² Ð´Ð»Ñ /add_questions:

Ð ÐºÐ°ÐºÐ¾Ð¼ Ð³Ð¾Ð´Ñ ÑÐ¼ÐµÑ ÐÐ±ÑÐ»Ð°Ð¹ÑÐ°Ð½?
A) 1778
B) 1771
C) 1767
D) 1781*

ÐÑÐ¾ Ð½Ð°Ð¿Ð¸ÑÐ°Ð» Â«ÐÑÑÑ ÐÐ±Ð°ÑÂ»?
A) ÐÑÑÑÐ°Ñ ÐÑÑÐ·Ð¾Ð²*
B) ÐÐ±Ð°Ð¹ ÐÑÐ½Ð°Ð½Ð±Ð°ÐµÐ²
C) ÐÐ»ÑÑÑ ÐÑÐµÐ½Ð±ÐµÑÐ»Ð¸Ð½
D) ÐÐ»Ð¶Ð°Ñ Ð¡ÑÐ»ÐµÐ¹Ð¼ÐµÐ½Ð¾Ð²
"""


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ÐÐµÑ Ð´Ð¾ÑÑÑÐ¿Ð°.")
        return
    await update.message.reply_text(ADMIN_HELP, parse_mode=ParseMode.HTML)


async def create_section_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = update.message.text.replace("/create_section", "", 1).strip()
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 2:
        await update.message.reply_text("ÐÑÐ¸Ð¼ÐµÑ: /create_section history_kz | ÐÑÑÐ¾ÑÐ¸Ñ ÐÐ°Ð·Ð°ÑÑÑÐ°Ð½Ð° | Ð¢ÐµÑÑÑ Ð¿Ð¾ Ð¸ÑÑÐ¾ÑÐ¸Ð¸")
        return
    slug = parts[0]
    title = parts[1]
    description = parts[2] if len(parts) > 2 else ""
    try:
        create_section(slug, title, description)
        await update.message.reply_text(f"â Ð Ð°Ð·Ð´ÐµÐ» Â«{title}Â» ÑÐ¾Ð·Ð´Ð°Ð½.")
    except sqlite3.IntegrityError:
        await update.message.reply_text("Ð¢Ð°ÐºÐ¾Ð¹ slug ÑÐ¶Ðµ ÑÑÑÐµÑÑÐ²ÑÐµÑ.")


async def set_paid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 3:
        await update.message.reply_text("ÐÑÐ¸Ð¼ÐµÑ: /set_paid history_kz 50 5")
        return
    slug = context.args[0]
    stars = int(context.args[1])
    friends = int(context.args[2])
    set_section_paid(slug, 1, stars, friends)
    await update.message.reply_text(f"â Ð Ð°Ð·Ð´ÐµÐ» {slug} ÑÐ´ÐµÐ»Ð°Ð½ Ð¿Ð»Ð°ÑÐ½ÑÐ¼. Ð¦ÐµÐ½Ð°: {stars} â­, Ð´ÑÑÐ·ÐµÐ¹: {friends}")


async def set_free_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 1:
        await update.message.reply_text("ÐÑÐ¸Ð¼ÐµÑ: /set_free history_kz")
        return
    slug = context.args[0]
    set_section_paid(slug, 0, 0, 0)
    await update.message.reply_text(f"â Ð Ð°Ð·Ð´ÐµÐ» {slug} ÑÐ´ÐµÐ»Ð°Ð½ Ð±ÐµÑÐ¿Ð»Ð°ÑÐ½ÑÐ¼.")


async def list_sections_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = list_sections(active_only=False)
    if not rows:
        await update.message.reply_text("Ð Ð°Ð·Ð´ÐµÐ»Ð¾Ð² Ð¿Ð¾ÐºÐ° Ð½ÐµÑ.")
        return
    lines = []
    for r in rows:
        lines.append(
            f"â¢ {r['slug']} â {r['title']} | paid={r['is_paid']} | stars={r['price_stars']} | friends={r['required_referrals']} | questions={count_questions(r['id'])}"
        )
    await safe_send_long_message(update.message, "\n".join(lines))


async def add_questions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 1:
        await update.message.reply_text("ÐÑÐ¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°Ð½Ð¸Ðµ: /add_questions <slug>\nÐÐ¾ÑÐ»Ðµ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñ Ð¾ÑÐ¿ÑÐ°Ð²ÑÑÐµ Ð²Ð¾Ð¿ÑÐ¾ÑÑ ÑÑÐ¸Ð¼ Ð¶Ðµ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸ÐµÐ¼.")
        return

    slug = context.args[0]
    section = get_section_by_slug(slug)
    if not section:
        await update.message.reply_text("Ð Ð°Ð·Ð´ÐµÐ» Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½.")
        return

    raw = update.message.text
    prefix = f"/add_questions {slug}"
    content = raw[len(prefix):].strip()
    if not content:
        await update.message.reply_text(
            "ÐÐ¾ÑÐ»Ðµ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñ ÑÑÐ°Ð·Ñ Ð²ÑÑÐ°Ð²ÑÑÐµ Ð²Ð¾Ð¿ÑÐ¾ÑÑ.\n\nÐÑÐ¸Ð¼ÐµÑ:\n/add_questions history_kz\nÐ ÐºÐ°ÐºÐ¾Ð¼ Ð³Ð¾Ð´Ñ ÑÐ¼ÐµÑ ÐÐ±ÑÐ»Ð°Ð¹ÑÐ°Ð½?\nA) 1778\nB) 1771\nC) 1767\nD) 1781*"
        )
        return

    try:
        items = parse_questions_block(content)
        for item in items:
            save_question(section["id"], item["question"], item["options"], item["correct"], item["explanation"])
        await update.message.reply_text(f"â ÐÐ¾Ð±Ð°Ð²Ð»ÐµÐ½Ð¾ Ð²Ð¾Ð¿ÑÐ¾ÑÐ¾Ð²: {len(items)} Ð² ÑÐ°Ð·Ð´ÐµÐ» Â«{section['title']}Â».")
    except Exception as e:
        await update.message.reply_text(f"ÐÑÐ¸Ð±ÐºÐ° ÑÐ°Ð·Ð±Ð¾ÑÐ°: {e}")


async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("ÐÑÐ¸Ð¼ÐµÑ: /grant 123456789 history_kz")
        return
    user_id = int(context.args[0])
    slug = context.args[1]
    section = get_section_by_slug(slug)
    if not section:
        await update.message.reply_text("Ð Ð°Ð·Ð´ÐµÐ» Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½.")
        return
    grant_access(user_id, section["id"], "manual", update.effective_user.id)
    await update.message.reply_text("â ÐÐ¾ÑÑÑÐ¿ Ð²ÑÐ´Ð°Ð½.")
    try:
        await context.bot.send_message(user_id, f"â ÐÐ°Ð¼ Ð¾ÑÐºÑÑÑ Ð´Ð¾ÑÑÑÐ¿ Ðº ÑÐ°Ð·Ð´ÐµÐ»Ñ Â«{section['title']}Â».")
    except Exception:
        pass


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("ÐÑÐ¸Ð¼ÐµÑ: /revoke 123456789 history_kz")
        return
    user_id = int(context.args[0])
    slug = context.args[1]
    section = get_section_by_slug(slug)
    if not section:
        await update.message.reply_text("Ð Ð°Ð·Ð´ÐµÐ» Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½.")
        return
    revoke_access(user_id, section["id"])
    await update.message.reply_text("â ÐÐ¾ÑÑÑÐ¿ Ð¾ÑÐ¾Ð·Ð²Ð°Ð½.")


async def userinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 1:
        await update.message.reply_text("ÐÑÐ¸Ð¼ÐµÑ: /userinfo 123456789")
        return
    user_id = int(context.args[0])
    with closing(db_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        user = cur.fetchone()
        if not user:
            await update.message.reply_text("ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½.")
            return
        cur.execute(
            """
            SELECT s.title, a.access_type, a.granted_at
            FROM access_rights a
            JOIN sections s ON s.id = a.section_id
            WHERE a.user_id=?
            ORDER BY a.granted_at DESC
            """,
            (user_id,),
        )
        accesses = cur.fetchall()
    stats = get_referral_stats(user_id)
    lines = [
        f"ID: {user['user_id']}",
        f"Username: @{user['username']}" if user['username'] else "Username: â",
        f"ÐÐ¼Ñ: {user['full_name'] or 'â'}",
        f"Ð ÐµÑÐµÑÐ°Ð»Ñ: {stats['credited']}/{REQUIRED_REFERRALS}",
        "ÐÐ¾ÑÑÑÐ¿Ñ:",
    ]
    if accesses:
        lines.extend([f"â¢ {a['title']} ({a['access_type']})" for a in accesses])
    else:
        lines.append("â¢ Ð½ÐµÑ")
    await update.message.reply_text("\n".join(lines))


# =========================================================
# MAIN
# =========================================================

def main():
    init_db()
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("create_section", create_section_command))
    app.add_handler(CommandHandler("set_paid", set_paid_command))
    app.add_handler(CommandHandler("set_free", set_free_command))
    app.add_handler(CommandHandler("list_sections_admin", list_sections_admin_command))
    app.add_handler(CommandHandler("add_questions", add_questions_command))
    app.add_handler(CommandHandler("grant", grant_command))
    app.add_handler(CommandHandler("revoke", revoke_command))
    app.add_handler(CommandHandler("userinfo", userinfo_command))

    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(section_callback, pattern=r"^section:"))
    app.add_handler(CallbackQueryHandler(refunlock_callback, pattern=r"^refunlock:"))
    app.add_handler(CallbackQueryHandler(start_quiz_callback, pattern=r"^startquiz:"))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy:"))

    app.add_handler(PollAnswerHandler(poll_answer_handler))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    logger.info("Bot is running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
