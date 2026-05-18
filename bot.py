import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

API_TOKEN = '8855493374:AAHFYkmPmtW3FxeQnEvjBviWlEkB-DxjDCI'
GROUP_CHAT_ID = -1004061028643

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

class TaskStates(StatesGroup):
    waiting_for_subtask = State()
    waiting_for_edit_main = State()
    waiting_for_edit_sub = State()

db = {}
task_id_counter = 1

STATUS_ICONS = {
    "План": "⏳",
    "В процессе": "⚙️",
    "Готово": "✅"
}

def get_or_create_user(user: types.User):
    if user.id not in db:
        db[user.id] = {"name": user.full_name, "tasks": []}
    return db[user.id]

def get_main_keyboard():
    """Создает постоянные кнопки меню внизу экрана"""
    builder = ReplyKeyboardBuilder()
    builder.button(text="📋 Мои задачи на сегодня")
    builder.button(text="📊 Отправить отчет в группу")
    builder.button(text="🧹 Очистить мои задачи")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)

# --- ДОБАВЛЕНИЕ ЗАДАЧ И ПОДПУНКТОВ (В ЛС) ---

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
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text.lower().startswith("задача:"))
async def add_main_task(message: types.Message):
    if message.chat.type != "private":
        return
        
    global task_id_counter
    user_data = get_or_create_user(message.from_user)
    task_text = message.text[7:].strip()
    
    if not task_text:
        return await message.reply("❌ Напишите текст задачи после двоеточия.")

    new_task = {
        "id": task_id_counter,
        "text": task_text,
        "status": "План",
        "subs": []
    }
    user_data["tasks"].append(new_task)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить подпункт", callback_data=f"sub_{task_id_counter}_{message.from_user.id}")
    
    await message.reply(
        f"⏳ Задача **«{task_text}»** добавлена!", 
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    task_id_counter += 1

@dp.callback_query(F.data.startswith("sub_"))
async def request_subtask_text(callback: types.CallbackQuery, state: FSMContext):
    _, t_id, u_id = callback.data.split("_")
    if callback.from_user.id != int(u_id):
        return await callback.answer("❌ Ошибка доступа.", show_alert=True)
        
    await state.update_data(task_id=int(t_id), user_id=int(u_id))
    await state.set_state(TaskStates.waiting_for_subtask)
    await callback.message.answer("✍️ Напишите текст подпункта следующим сообщением:")
    await callback.answer()

@dp.message(TaskStates.waiting_for_subtask)
async def save_subtask(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        return
    state_data = await state.get_data()
    user_data = db.get(state_data['user_id'])
    
    if user_data:
        for task in user_data["tasks"]:
            if task["id"] == state_data['task_id']:
                task["subs"].append({"text": message.text, "status": "План"})
                await message.reply(f"✅ Подпункт «{message.text}» добавлен.")
                break
    await state.clear()

# --- ПАНЕЛЬ УПРАВЛЕНИЯ + РЕДАКТИРОВАНИЕ ---

@dp.message(Command("my_tasks"))
@dp.message(F.text == "📋 Мои задачи на сегодня")
async def show_my_tasks_panel(message: types.Message):
    if message.chat.type != "private":
        return
        
    user_data = get_or_create_user(message.from_user)
    if not user_data["tasks"]:
        return await message.reply("У вас пока нет задач на сегодня. Добавьте через `Задача: ...`")
        
    await message.reply("📋 **Ваши задачи:**\n• Нажимайте на статус для его смены.\n• Нажимайте ✏️ для изменения текста.")
    
    for task in user_data["tasks"]:
        builder = InlineKeyboardBuilder()
        icon = STATUS_ICONS.get(task['status'], "❓")
        
        builder.button(text=f"{icon} {task['text']}", callback_data=f"st_main_{task['id']}_{message.from_user.id}")
        builder.button(text="✏️", callback_data=f"ed_main_{task['id']}_{message.from_user.id}")
        
        for sub_idx, sub in enumerate(task["subs"]):
            sub_icon = STATUS_ICONS.get(sub['status'], "❓")
            builder.button(text=f"↳ {sub_icon} {sub['text']}", callback_data=f"st_sub_{task['id']}_{sub_idx}_{message.from_user.id}")
            builder.button(text="✏️", callback_data=f"ed_sub_{task['id']}_{sub_idx}_{message.from_user.id}")
            
        builder.adjust(2)
        await message.answer(f"Задача #{task['id']}:", reply_markup=builder.as_markup())

# Переключение статусов (ИСПРАВЛЕНО!)
@dp.callback_query(F.data.startswith("st_"))
async def toggle_status(callback: types.CallbackQuery):
    data_parts = callback.data.split("_")
    action = data_parts[1]  # "main" или "sub"
    t_id = int(data_parts[2])
    u_id = int(data_parts[-1])
    
    if callback.from_user.id != u_id:
        return await callback.answer("❌ Ошибка доступа.", show_alert=True)
        
    user_data = db.get(u_id)
    statuses = ["План", "В процессе", "Готово"]
    
    if action == "main":
        for task in user_data["tasks"]:
            if task["id"] == t_id:
                curr_idx = statuses.index(task["status"])
                task["status"] = statuses[(curr_idx + 1) % len(statuses)]
                break
    elif action == "sub":
        sub_idx = int(data_parts[3])
        for task in user_data["tasks"]:
            if task["id"] == t_id:
                curr_idx = statuses.index(task["subs"][sub_idx]["status"])
                task["subs"][sub_idx]["status"] = statuses[(curr_idx + 1) % len(statuses)]
                break

    # Обновление клавиатуры
    builder = InlineKeyboardBuilder()
    for task in user_data["tasks"]:
        if task["id"] == t_id:
            icon = STATUS_ICONS.get(task['status'], "❓")
            builder.button(text=f"{icon} {task['text']}", callback_data=f"st_main_{task['id']}_{u_id}")
            builder.button(text="✏️", callback_data=f"ed_main_{task['id']}_{u_id}")
            for s_idx, sub in enumerate(task["subs"]):
                s_icon = STATUS_ICONS.get(sub['status'], "❓")
                builder.button(text=f"↳ {s_icon} {sub['text']}", callback_data=f"st_sub_{task['id']}_{s_idx}_{u_id}")
                builder.button(text="✏️", callback_data=f"ed_sub_{task['id']}_{s_idx}_{u_id}")
    builder.adjust(2)
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    await callback.answer("Статус обновлен!")

# Клик на "Редактировать текст" (ИСПРАВЛЕНО!)
@dp.callback_query(F.data.startswith("ed_"))
async def start_editing(callback: types.CallbackQuery, state: FSMContext):
    data_parts = callback.data.split("_")
    action = data_parts[1]  # "main" или "sub"
    t_id = int(data_parts[2])
    u_id = int(data_parts[-1])
    
    if callback.from_user.id != u_id:
        return await callback.answer("❌ Ошибка доступа.", show_alert=True)
        
    await state.update_data(task_id=t_id, user_id=u_id)
    
    if action == "main":
        await state.set_state(TaskStates.waiting_for_edit_main)
        await callback.message.answer("📝 Введите новый ТЕКСТ для этой задачи:")
    elif action == "sub":
        sub_idx = int(data_parts[3])
        await state.update_data(sub_index=sub_idx)
        await state.set_state(TaskStates.waiting_for_edit_sub)
        await callback.message.answer("📝 Введите новый ТЕКСТ для этого подпункта:")
    await callback.answer()

@dp.message(TaskStates.waiting_for_edit_main)
async def save_edit_main(message: types.Message, state: FSMContext):
    state_data = await state.get_data()
    user_data = db.get(state_data['user_id'])
    
    for task in user_data["tasks"]:
        if task["id"] == state_data['task_id']:
            task["text"] = message.text
            await message.reply(f"✅ Текст задачи изменен на: «{message.text}».", reply_markup=get_main_keyboard())
            break
    await state.clear()

@dp.message(TaskStates.waiting_for_edit_sub)
async def save_edit_sub(message: types.Message, state: FSMContext):
    state_data = await state.get_data()
    user_data = db.get(state_data['user_id'])
    
    for task in user_data["tasks"]:
        if task["id"] == state_data['task_id']:
            task["subs"][state_data['sub_index']]["text"] = message.text
            await message.reply(f"✅ Текст подпункта изменен на: «{message.text}».", reply_markup=get_main_keyboard())
            break
    await state.clear()

# --- ОТЧЕТЫ И ТЕСТЫ ---

@dp.message(Command("test_report"))
@dp.message(F.text == "📊 Отправить отчет в группу")
async def test_report_cmd(message: types.Message):
    if message.chat.type == "private":
        await send_daily_report("Тестовый отчет")
        await message.reply("✅ Тестовый отчет успешно улетел в вашу группу!", reply_markup=get_main_keyboard())

@dp.message(Command("test_clear"))
@dp.message(F.text == "🧹 Очистить мои задачи")
async def test_clear_cmd(message: types.Message):
    if message.chat.type == "private":
        await clear_database()
        await message.reply("✅ База данных успешно очищена!", reply_markup=get_main_keyboard())

async def send_daily_report(report_type="План"):
    current_date = datetime.now().strftime("%d.%m.%y")
    report_text = f"📊 **ОТЧЕТ [{report_type.upper()}] — {current_date}**\n"
    report_text += "────────────────────\n\n"
    
    if not db or not any(d["tasks"] for d in db.values()):
        report_text += "🤷‍♂️ На сегодня никто не заполнил задачи в ЛС бота."
        return await bot.send_message(GROUP_CHAT_ID, report_text, parse_mode="Markdown")
        
    for user_id, data in db.items():
        if not data["tasks"]:
            continue
        report_text += f"👤 **{data['name']}:**\n"
        for task in data["tasks"]:
            icon = STATUS_ICONS.get(task['status'], "⏳")
            report_text += f"  {icon} {task['text']}\n"
            for sub in task["subs"]:
                sub_icon = STATUS_ICONS.get(sub['status'], "⏳")
                report_text += f"      └ {sub_icon} {sub['text']}\n"
        report_text += "\n"
        
    await bot.send_message(GROUP_CHAT_ID, report_text, parse_mode="Markdown")

async def clear_database():
    global db
    db.clear()
    await bot.send_message(GROUP_CHAT_ID, "🌅 **Новый день начался!**\nСтарая база задач очищена. Все участники могут присылать новые планы в ЛС бота через `Задача: ...`", parse_mode="Markdown")

async def main():
    logging.basicConfig(level=logging.INFO)
    
    scheduler.add_job(send_daily_report, 'cron', hour=10, minute=0, args=["Утренний План"])
    scheduler.add_job(send_daily_report, 'cron', hour=13, minute=0, args=["13:00"])
    scheduler.add_job(send_daily_report, 'cron', hour=18, minute=0, args=["18:00"])
    scheduler.add_job(clear_database, 'cron', hour=8, minute=0)
    
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
