import asyncio
import logging
from datetime import datetime
from enum import Enum
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiosqlite
import os

from dotenv import load_dotenv

load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
if API_TOKEN is None:
    raise Exception("API token is not specified")

DEVELOPMENT = os.getenv("DEVEL", None) is not None

GROUP_CHAT_ID = -1004061028643
DB_PATH = os.getenv("DB_PATH", "./tasks.sqlite3")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()


class TaskStates(StatesGroup):
    waiting_for_edit = State()


class TaskStatus(str, Enum):
    OPEN = "Открыта"
    IN_PROGRESS = "В процессе"
    DONE = "Готово"

    @property
    def icon(self) -> str:
        return {
            self.OPEN: "⏳",
            self.IN_PROGRESS: "🟧",
            self.DONE: "✅",
        }[self]

    def next(self) -> "TaskStatus":
        members = list(TaskStatus)
        return members[(members.index(self) + 1) % len(members)]


# --- DATABASE ---


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name    TEXT NOT NULL
            )
        """)
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS tasks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                text       TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT '{TaskStatus.OPEN.value}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)
        for col in ("created_at", "updated_at"):
            try:
                await db.execute(
                    f"ALTER TABLE tasks ADD COLUMN"
                    f" {col} TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
                )
            except Exception as e:
                logging.error(f"failed migrate database: {e}")
        await db.commit()


async def ensure_user(user: types.User):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, name) VALUES (?, ?)",
            (user.id, user.full_name),
        )
        await db.commit()


async def add_task(user_id: int, text: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (user_id, text) VALUES (?, ?)", (user_id, text)
        )
        await db.commit()
        return cursor.lastrowid


async def get_user_tasks(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, text, status, created_at, updated_at"
            " FROM tasks WHERE user_id = ? ORDER BY status",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_task_by_id(task_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, text, status, created_at, updated_at"
            " FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
        return dict(row)


async def get_all_users_with_tasks() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT DISTINCT u.user_id, u.name FROM users u"
            " INNER JOIN tasks t ON u.user_id = t.user_id"
        )
        users = await cur.fetchall()
    result = []
    for user in users:
        tasks = await get_user_tasks(user["user_id"])
        if tasks:
            result.append({"name": user["name"], "tasks": tasks})
    return result


async def cycle_task_status(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        if row:
            new_status = TaskStatus(row[0]).next()
            await db.execute(
                "UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status, task_id),
            )
            await db.commit()


async def update_task_text(task_id: int, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET text = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (text, task_id),
        )
        await db.commit()


async def clear_all_tasks(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tasks t WHERE t.user_id = ?", (user_id))
        await db.commit()


# --- HELPERS ---


def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📋 Мои задачи на сегодня")
    builder.button(text="📊 Отправить отчет в группу")
    builder.button(text="🧹 Очистить мои задачи")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def build_task_keyboard(task: dict, u_id: int):
    builder = InlineKeyboardBuilder()
    icon = TaskStatus(task["status"]).icon
    builder.button(
        text=f"{icon} {task['status']}",
        callback_data=f"st_{task['id']}_{u_id}",
    )
    builder.button(
        text="✏️ Редактировать",
        callback_data=f"ed_{task['id']}_{u_id}",
    )
    builder.adjust(2)
    return builder.as_markup()


# --- HANDLERS ---


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.chat.type != "private":
        return
    await message.answer(
        "👋 Привет! Я твоя записная книжка для ежедневных отчетов.\n\n"
        "**Как пользоваться:**\n"
        "1. Напиши: `Задача: Текст задачи`\n"
        "2. Используй кнопки меню внизу для управления задачами.\n\n"
        "Все твои изменения автоматически попадут в общий отчет группы!",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(),
    )


async def send_task_message(message: types.Message, task):
    await message.answer(
        f"Задача #{task['id']}: {task['text']}",
        reply_markup=build_task_keyboard(task, message.from_user.id),
    )


@dp.message(F.text.lower().startswith("задача:"))
@dp.message(F.text.lower().startswith("task:"))
async def add_main_task(message: types.Message):
    if message.chat.type != "private":
        return

    [cmd, task_text] = message.text.split(":", 1)
    task_text = task_text.strip()
    if not task_text:
        return await message.reply("❌ Напишите текст задачи после двоеточия.")

    await ensure_user(message.from_user)
    id = await add_task(message.from_user.id, task_text)
    await message.reply(
        f"⏳ Задача **«{task_text}»** добавлена!",
        parse_mode="Markdown",
    )
    await send_task_message(
        message, task={"id": id, "text": task_text, "status": TaskStatus.OPEN.value}
    )


@dp.message(Command("my_tasks"))
@dp.message(F.text == "📋 Мои задачи на сегодня")
async def show_my_tasks_panel(message: types.Message):
    if message.chat.type != "private":
        return

    await ensure_user(message.from_user)
    tasks = await get_user_tasks(message.from_user.id)

    if not tasks:
        return await message.reply(
            "У вас пока нет задач на сегодня. Добавьте через `Задача: ...`",
            parse_mode="Markdown",
        )

    await message.reply(
        "📋 **Ваши задачи:**\n\n"
        "• Нажимайте на статус для его смены.\n"
        "• Нажимайте ✏️ для изменения текста.",
        parse_mode="Markdown",
    )
    for task in tasks:
        await send_task_message(message, task)


@dp.callback_query(F.data.startswith("st_"))
async def toggle_status(callback: types.CallbackQuery):
    _, t_id, u_id = callback.data.split("_")
    t_id, u_id = int(t_id), int(u_id)

    if callback.from_user.id != u_id:
        return await callback.answer("❌ Ошибка доступа.", show_alert=True)

    await cycle_task_status(t_id)

    tasks = await get_user_tasks(u_id)
    task = next((t for t in tasks if t["id"] == t_id), None)
    if task:
        await callback.message.edit_reply_markup(
            reply_markup=build_task_keyboard(task, u_id)
        )
    await callback.answer("Статус обновлен!")


@dp.callback_query(F.data.startswith("ed_"))
async def start_editing(callback: types.CallbackQuery, state: FSMContext):
    _, t_id, u_id = callback.data.split("_")
    t_id, u_id = int(t_id), int(u_id)

    if callback.from_user.id != u_id:
        return await callback.answer("❌ Ошибка доступа.", show_alert=True)

    await state.update_data(task_id=t_id)
    await state.set_state(TaskStates.waiting_for_edit)
    await callback.message.answer("📝 Введите новый текст для этой задачи:")
    await callback.answer()


@dp.message(TaskStates.waiting_for_edit)
async def save_edit(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = int(data["task_id"])
    await update_task_text(task_id, message.text)
    await message.reply(
        f"✅ Текст задачи изменен на: «{message.text}».",
        reply_markup=get_main_keyboard(),
    )
    await state.clear()
    await send_task_message(message, await get_task_by_id(task_id))


# --- REPORTS ---


@dp.message(Command("report"))
@dp.message(F.text == "📊 Отправить отчет в группу")
async def report_cmd(message: types.Message):
    if message.chat.type == "private":
        await send_daily_report()
        await message.reply(
            "✅ Тестовый отчет успешно улетел в вашу группу!",
            reply_markup=get_main_keyboard(),
        )
    else:
        await send_daily_report()


@dp.message(Command("clear"))
@dp.message(F.text == "🧹 Очистить мои задачи")
async def clear_cmd(message: types.Message):
    if message.chat.type == "private":
        await clear_all_tasks(message.from_user.id)
        await message.reply(
            "✅ База данных успешно очищена!", reply_markup=get_main_keyboard()
        )


async def send_daily_report(report_type="План"):
    current_date = datetime.now().strftime("%d.%m.%y")
    report_text = f"📊 **ОТЧЕТ — {current_date}**\n"
    report_text += "────────────────────\n\n"

    users = await get_all_users_with_tasks()

    if not users:
        report_text += "🤷‍♂️ На сегодня никто не заполнил задачи в ЛС бота."
        return await bot.send_message(GROUP_CHAT_ID, report_text, parse_mode="Markdown")

    for user in users:
        report_text += f"👤 **{user['name']}:**\n"
        for task in user["tasks"]:
            icon = TaskStatus(task["status"]).icon
            report_text += f"  {icon} {task['text']} _({task['status']})_\n"
        report_text += "\n"

    await bot.send_message(GROUP_CHAT_ID, report_text, parse_mode="Markdown")


async def main():
    logging.basicConfig(level=logging.DEBUG if DEVELOPMENT else logging.INFO)
    await init_db()

    scheduler.add_job(
        send_daily_report, "cron", hour=10, minute=0, args=["Утренний План"]
    )
    scheduler.add_job(send_daily_report, "cron", hour=13, minute=0, args=["13:00"])
    scheduler.add_job(send_daily_report, "cron", hour=18, minute=0, args=["18:00"])

    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
