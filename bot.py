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
    waiting_for_subtask = State()
    waiting_for_edit_main = State()
    waiting_for_edit_sub = State()


class TaskStatus(str, Enum):
    OPEN = "Открыта"
    IN_PROGRESS = "В процессе"
    DONE = "Готово"

    @property
    def icon(self) -> str:
        if self.value not in list(TaskStatus):
            logging.error(f"Unknown task status: {self.value}")
            return "❓"

        return {self.OPEN: "⏳", self.IN_PROGRESS: "⚙️", self.DONE: "✅"}[self.value]

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
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text    TEXT NOT NULL,
                status  TEXT NOT NULL DEFAULT '{TaskStatus.OPEN.value}',
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS subtasks (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                text    TEXT NOT NULL,
                status  TEXT NOT NULL DEFAULT '{TaskStatus.OPEN.value}',
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            )
        """)
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


async def add_subtask(task_id: int, text: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO subtasks (task_id, text) VALUES (?, ?)", (task_id, text)
        )
        await db.commit()
        return cursor.lastrowid


async def get_user_tasks(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        tasks_cur = await db.execute(
            "SELECT id, text, status FROM tasks WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        tasks = await tasks_cur.fetchall()
        result = []
        for task in tasks:
            subs_cur = await db.execute(
                "SELECT id, text, status FROM subtasks WHERE task_id = ? ORDER BY id",
                (task["id"],),
            )
            subs = await subs_cur.fetchall()
            result.append(
                {
                    "id": task["id"],
                    "text": task["text"],
                    "status": task["status"],
                    "subs": [
                        {"id": s["id"], "text": s["text"], "status": s["status"]}
                        for s in subs
                    ],
                }
            )
        return result


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
                "UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id)
            )
            await db.commit()


async def cycle_subtask_status(sub_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT status FROM subtasks WHERE id = ?", (sub_id,))
        row = await cur.fetchone()
        if row:
            new_status = TaskStatus(row[0]).next()
            await db.execute(
                "UPDATE subtasks SET status = ? WHERE id = ?", (new_status, sub_id)
            )
            await db.commit()


async def update_task_text(task_id: int, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET text = ? WHERE id = ?", (text, task_id))
        await db.commit()


async def update_subtask_text(sub_id: int, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE subtasks SET text = ? WHERE id = ?", (text, sub_id))
        await db.commit()


async def clear_all_tasks():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subtasks")
        await db.execute("DELETE FROM tasks")
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
        callback_data=f"st_main_{task['id']}_{u_id}",
    )
    builder.button(
        text="✏️ Редактировать", callback_data=f"ed_main_{task['id']}_{u_id}"
    )
    for sub in task["subs"]:
        s_icon = TaskStatus(sub["status"]).icon
        builder.button(
            text=f"↳ {s_icon} {sub['text']}",
            callback_data=f"st_sub_{task['id']}_{sub['id']}_{u_id}",
        )
        builder.button(
            text="✏️", callback_data=f"ed_sub_{task['id']}_{sub['id']}_{u_id}"
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
        "1. Напиши: `Задача: Текст задачи` (без слова бот)\n"
        "2. Нажми кнопку под задачей, чтобы добавить подпункт.\n"
        "3. Используй кнопки меню внизу для управления задачами.\n\n"
        "Все твои изменения автоматически попадут в общий отчет группы!",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(),
    )


@dp.message(F.text.lower().startswith("задача:"))
async def add_main_task(message: types.Message):
    if message.chat.type != "private":
        return

    task_text = message.text[7:].strip()
    if not task_text:
        return await message.reply("❌ Напишите текст задачи после двоеточия.")

    await ensure_user(message.from_user)
    task_id = await add_task(message.from_user.id, task_text)

    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ Добавить подпункт",
        callback_data=f"sub_{task_id}_{message.from_user.id}",
    )
    await message.reply(
        f"⏳ Задача **«{task_text}»** добавлена!",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("sub_"))
async def request_subtask_text(callback: types.CallbackQuery, state: FSMContext):
    _, t_id, u_id = callback.data.split("_")
    if callback.from_user.id != int(u_id):
        return await callback.answer("❌ Ошибка доступа.", show_alert=True)

    await state.update_data(task_id=int(t_id))
    await state.set_state(TaskStates.waiting_for_subtask)
    await callback.message.answer("✍️ Напишите текст подпункта следующим сообщением:")
    await callback.answer()


@dp.message(TaskStates.waiting_for_subtask)
async def save_subtask(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        return
    data = await state.get_data()
    await add_subtask(data["task_id"], message.text)
    await message.reply(f"✅ Подпункт «{message.text}» добавлен.")
    await state.clear()


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
        await message.answer(
            f"Задача #{task['id']}: {task['text']}",
            reply_markup=build_task_keyboard(task, message.from_user.id),
        )


@dp.callback_query(F.data.startswith("st_"))
async def toggle_status(callback: types.CallbackQuery):
    data_parts = callback.data.split("_")
    action = data_parts[1]  # "main" or "sub"
    t_id = int(data_parts[2])
    u_id = int(data_parts[-1])

    if callback.from_user.id != u_id:
        return await callback.answer("❌ Ошибка доступа.", show_alert=True)

    if action == "main":
        await cycle_task_status(t_id)
    elif action == "sub":
        sub_id = int(data_parts[3])
        await cycle_subtask_status(sub_id)

    tasks = await get_user_tasks(u_id)
    task = next((t for t in tasks if t["id"] == t_id), None)
    if task:
        await callback.message.edit_reply_markup(
            reply_markup=build_task_keyboard(task, u_id)
        )
    await callback.answer("Статус обновлен!")


@dp.callback_query(F.data.startswith("ed_"))
async def start_editing(callback: types.CallbackQuery, state: FSMContext):
    data_parts = callback.data.split("_")
    action = data_parts[1]  # "main" or "sub"
    t_id = int(data_parts[2])
    u_id = int(data_parts[-1])

    if callback.from_user.id != u_id:
        return await callback.answer("❌ Ошибка доступа.", show_alert=True)

    await state.update_data(task_id=t_id, user_id=u_id)

    if action == "main":
        await state.set_state(TaskStates.waiting_for_edit_main)
        await callback.message.answer("📝 Введите новый ТЕКСТ для этой задачи:")
    elif action == "sub":
        sub_id = int(data_parts[3])
        await state.update_data(sub_id=sub_id)
        await state.set_state(TaskStates.waiting_for_edit_sub)
        await callback.message.answer("📝 Введите новый ТЕКСТ для этого подпункта:")
    await callback.answer()


@dp.message(TaskStates.waiting_for_edit_main)
async def save_edit_main(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await update_task_text(data["task_id"], message.text)
    await message.reply(
        f"✅ Текст задачи изменен на: «{message.text}».",
        reply_markup=get_main_keyboard(),
    )
    await state.clear()


@dp.message(TaskStates.waiting_for_edit_sub)
async def save_edit_sub(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await update_subtask_text(data["sub_id"], message.text)
    await message.reply(
        f"✅ Текст подпункта изменен на: «{message.text}».",
        reply_markup=get_main_keyboard(),
    )
    await state.clear()


# --- REPORTS ---


@dp.message(Command("test_report"))
@dp.message(F.text == "📊 Отправить отчет в группу")
async def test_report_cmd(message: types.Message):
    if message.chat.type == "private":
        await send_daily_report("Тестовый отчет")
        await message.reply(
            "✅ Тестовый отчет успешно улетел в вашу группу!",
            reply_markup=get_main_keyboard(),
        )


@dp.message(Command("test_clear"))
@dp.message(F.text == "🧹 Очистить мои задачи")
async def test_clear_cmd(message: types.Message):
    if message.chat.type == "private":
        await clear_all_tasks()
        await message.reply(
            "✅ База данных успешно очищена!", reply_markup=get_main_keyboard()
        )


async def send_daily_report(report_type="План"):
    current_date = datetime.now().strftime("%d.%m.%y")
    report_text = f"📊 **ОТЧЕТ [{report_type.upper()}] — {current_date}**\n"
    report_text += "────────────────────\n\n"

    users = await get_all_users_with_tasks()

    if not users:
        report_text += "🤷‍♂️ На сегодня никто не заполнил задачи в ЛС бота."
        return await bot.send_message(GROUP_CHAT_ID, report_text, parse_mode="Markdown")

    for user in users:
        report_text += f"👤 **{user['name']}:**\n"
        for task in user["tasks"]:
            icon = TaskStatus(task["status"]).icon
            report_text += f"  {icon} {task['text']}\n"
            for sub in task["subs"]:
                sub_icon = TaskStatus(sub["status"]).icon
                report_text += f"      └ {sub_icon} {sub['text']}\n"
        report_text += "\n"

    await bot.send_message(GROUP_CHAT_ID, report_text, parse_mode="Markdown")


async def clear_database():
    await clear_all_tasks()
    await bot.send_message(
        GROUP_CHAT_ID,
        "🌅 **Новый день начался!**\n"
        "Старая база задач очищена. Все участники могут присылать новые планы в ЛС бота через `Задача: ...`",
        parse_mode="Markdown",
    )


async def main():
    logging.basicConfig(level=logging.DEBUG if DEVELOPMENT else logging.INFO)
    await init_db()

    scheduler.add_job(
        send_daily_report, "cron", hour=10, minute=0, args=["Утренний План"]
    )
    scheduler.add_job(send_daily_report, "cron", hour=13, minute=0, args=["13:00"])
    scheduler.add_job(send_daily_report, "cron", hour=18, minute=0, args=["18:00"])
    # scheduler.add_job(clear_database, "cron", hour=8, minute=0)

    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
