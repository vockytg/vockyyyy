import logging
import asyncio
import aiohttp
import random
from aiogram import Bot, Dispatcher, executor, types

# === КОНФИГУРАЦИЯ ===
# ВАЖНО: Рекомендуется вынести токены в переменные окружения (.env) для безопасности
DO_TOKEN = "dop_v1_dbc6ae9c544a11172d44afbda506e44d30351b40b542dc07a79e329c206cf68b"
BOT_TOKEN = "8406674405:AAFexrmq5yWLpssf3BO1hx2s4JUfRyexOfo"
USER_ID = 1213977370

PASSWORD = "aCIopJ6by_;Q"
MAX_DROPLETS = 10
TARGET_SUBNETS = [140, 141, 143, 168, 169, 170, 171, 172, 173, 174, 175, 204, 205, 206, 207]

DO_HEADERS = {
    "Authorization": f"Bearer {DO_TOKEN}",
    "Content-Type": "application/json"
}

# Cloud-config для установки пароля при развертывании
USER_DATA_SCRIPT = f"""#cloud-config
chpasswd:
  list: |
    root:{PASSWORD}
  expire: False
ssh_pwauth: True
"""

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ СОСТОЯНИЯ ===
current_batch_droplets = []
is_cycling = False

# Настройки по умолчанию
server_settings = {
    "region": "ams3",
    "size": "s-2vcpu-4gb"
}

def is_target_ip(ip: str) -> bool:
    parts = ip.split('.')
    if len(parts) == 4 and parts[0] == '188' and parts[1] == '166':
        try:
            return int(parts[2]) in TARGET_SUBNETS
        except ValueError:
            return False
    return False

async def get_current_droplets():
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.digitalocean.com/v2/droplets", headers=DO_HEADERS) as resp:
            data = await resp.json()
            return data.get("droplets", [])

async def create_droplets(count: int):
    names = [f"server-hunt-{random.randint(10000, 99999)}" for _ in range(count)]
    payload = {
        "names": names,
        "region": server_settings["region"],
        "size": server_settings["size"],
        "image": "ubuntu-22-04-x64",
        "user_data": USER_DATA_SCRIPT
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.digitalocean.com/v2/droplets", json=payload, headers=DO_HEADERS) as resp:
            data = await resp.json()
            if "droplets" in data:
                return [d["id"] for d in data["droplets"]]
            else:
                logging.error(f"Ошибка создания DO: {data}")
                return []

async def wait_for_ips(droplet_ids: list):
    ips = {}
    pending = list(droplet_ids)
    
    async with aiohttp.ClientSession() as session:
        while pending:
            for d_id in list(pending):
                async with session.get(f"https://api.digitalocean.com/v2/droplets/{d_id}", headers=DO_HEADERS) as resp:
                    data = await resp.json()
                    droplet = data.get("droplet", {})
                    
                    if droplet.get("status") == "active":
                        networks = droplet.get("networks", {}).get("v4", [])
                        for net in networks:
                            if net.get("type") == "public":
                                ips[d_id] = net.get("ip_address")
                                pending.remove(d_id)
                                break
            if pending:
                await asyncio.sleep(5)
    return ips

async def delete_droplet(droplet_id: int):
    async with aiohttp.ClientSession() as session:
        async with session.delete(f"https://api.digitalocean.com/v2/droplets/{droplet_id}", headers=DO_HEADERS) as resp:
            return resp.status == 204

async def run_hunt_iteration(message: types.Message, is_auto: bool) -> bool:
    global current_batch_droplets
    
    existing_droplets = await get_current_droplets()
    current_count = len(existing_droplets)
    needed = MAX_DROPLETS - current_count

    if needed <= 0:
        if not is_auto:
            await message.answer(f"⚠️ <b>Лимит достигнут!</b> У тебя уже {current_count} серверов.", parse_mode="HTML")
        else:
            await message.answer("⚠️ <b>Лимит серверов исчерпан!</b> Авто-цикл остановлен. Удали ненужные серверы.", parse_mode="HTML")
            global is_cycling
            is_cycling = False
        return False

    status_msg = None
    if not is_auto:
        status_msg = await message.answer(f"🖥 Создаю <b>{needed}</b> серверов (<code>{server_settings['region']}</code> | <code>{server_settings['size']}</code>)...", parse_mode="HTML")
    
    created_ids = await create_droplets(needed)
    if not created_ids:
        logging.error("Ошибка при создании серверов в DigitalOcean")
        if not is_auto:
            await status_msg.edit_text("❌ Ошибка при создании серверов. Проверь токен и лимиты.")
        return False

    current_batch_droplets.extend(created_ids)
    
    if not is_auto:
        await status_msg.edit_text(f"⏳ <b>{len(created_ids)}</b> серверов создаются...\n<i>Жду выдачи публичных IP...</i> 🌐", parse_mode="HTML")

    ips_dict = await wait_for_ips(created_ids)

    # Если ручной режим — выводим все найденные IP
    if not is_auto:
        summary_text = "📋 <b>Выданные IP-адреса:</b>\n\n"
        for d_id, ip in ips_dict.items():
            summary_text += f"🔹 <code>{ip}</code>\n"
        await message.answer(summary_text, parse_mode="HTML")

    found_target = False
    for d_id, ip in ips_dict.items():
        if is_target_ip(ip):
            found_target = True
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("✅ Оставить", callback_data=f"keep_{d_id}"),
                types.InlineKeyboardButton("🗑 Удалить", callback_data=f"del_{d_id}")
            )
            await message.answer(
                f"🔥 <b>СЕРВЕР НАЙДЕН!</b> 🔥\n\n"
                f"🌍 <b>IP:</b> <code>{ip}</code>\n"
                f"🔑 <b>Password:</b> <code>{PASSWORD}</code>",
                reply_markup=markup,
                parse_mode="HTML"
            )
            
    if found_target:
        return True
    else:
        # Удаляем мусор
        for d_id in created_ids:
            await delete_droplet(d_id)
            if d_id in current_batch_droplets:
                current_batch_droplets.remove(d_id)
                
        if not is_auto:
            markup_delete_all = types.InlineKeyboardMarkup()
            markup_delete_all.add(types.InlineKeyboardButton("🗑 Удалить всю партию (на всякий случай)", callback_data="del_all"))
            await message.answer("😔 К сожалению, нужных подсетей не найдено. Серверы удалены.", reply_markup=markup_delete_all)
        return False


# === КОМАНДЫ ===

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    if message.from_user.id != USER_ID:
        return
        
    first_name = message.from_user.first_name or "Пользователь"
    last_name = message.from_user.last_name or ""
    full_name = f"{first_name} {last_name}".strip()

    welcome_text = (
        f"👋 Приветствую, <b>{full_name}</b>!\n\n"
        f"🤖 Бот-ассистент для охоты за IP в <b>DigitalOcean</b>.\n\n"
        f"<b>Текущие настройки:</b>\n"
        f"🌍 Регион: <code>{server_settings['region']}</code>\n"
        f"💻 Размер: <code>{server_settings['size']}</code>\n\n"
        f"<b>Доступные команды:</b>\n"
        f"🛒 /buy — Купить партию серверов <i>(ручной режим с логами)</i>\n"
        f"🔄 /cycle — Бесконечный цикл <i>(тихий фоновый поиск)</i>\n"
        f"🛑 /stop — Остановить цикл\n"
        f"🌍 /region — Изменить регион (локацию)\n"
        f"⚙️ /size — Изменить тариф (размер сервера)"
    )
    await message.answer(welcome_text, parse_mode="HTML")

@dp.message_handler(commands=['buy'])
async def cmd_buy(message: types.Message):
    if message.from_user.id != USER_ID:
        return
    if is_cycling:
        await message.answer("⚠️ Сейчас запущен автоматический цикл! Сначала останови его командой /stop.")
        return
    await run_hunt_iteration(message, is_auto=False)

@dp.message_handler(commands=['cycle'])
async def cmd_cycle(message: types.Message):
    if message.from_user.id != USER_ID:
        return

    global is_cycling
    if is_cycling:
        await message.answer("🔄 <b>Цикл уже запущен и работает!</b>", parse_mode="HTML")
        return
        
    is_cycling = True
    await message.answer(f"🛰 <b>Фоновый поиск запущен.</b>\nПараметры: <code>{server_settings['region']}</code> | <code>{server_settings['size']}</code>\n\nЯ напишу только тогда, когда найду подходящий сервер.\n\n🛑 /stop — для отмены.", parse_mode="HTML")
    
    while is_cycling:
        found = await run_hunt_iteration(message, is_auto=True)
        
        if found:
            is_cycling = False
            await message.answer("🎯 <b>Поиск успешно завершен. Авто-цикл остановлен.</b>", parse_mode="HTML")
            break
            
        if not is_cycling:
            break
            
        # Пауза перед следующей попыткой
        await asyncio.sleep(10)

@dp.message_handler(commands=['stop'])
async def cmd_stop(message: types.Message):
    if message.from_user.id != USER_ID:
        return

    global is_cycling
    if is_cycling:
        is_cycling = False
        await message.answer("🛑 <b>Цикл поиска успешно остановлен!</b>\nНовые серверы больше не будут создаваться.", parse_mode="HTML")
    else:
        await message.answer("ℹ️ Сейчас нет активных циклов поиска.")

# === НАСТРОЙКИ (РЕГИОН И РАЗМЕР) ===

@dp.message_handler(commands=['region'])
async def cmd_region(message: types.Message):
    if message.from_user.id != USER_ID:
        return
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    regions = {
        "ams3": "🇳🇱 AMS3",
        "fra1": "🇩🇪 FRA1",
        "lon1": "🇬🇧 LON1",
        "nyc1": "🇺🇸 NYC1",
        "nyc3": "🇺🇸 NYC3",
        "sfo2": "🇺🇸 SFO2",
        "sfo3": "🇺🇸 SFO3",
        "sgp1": "🇸🇬 SGP1",
        "tor1": "🇨🇦 TOR1",
        "blr1": "🇮🇳 BLR1",
        "syd1": "🇦🇺 SYD1"
    }
    
    for reg_code, reg_name in regions.items():
        prefix = "✅ " if server_settings["region"] == reg_code else ""
        markup.insert(types.InlineKeyboardButton(f"{prefix}{reg_name}", callback_data=f"setreg_{reg_code}"))
        
    await message.answer(f"🌍 <b>Выбор региона</b>\nТекущий: <code>{server_settings['region']}</code>", reply_markup=markup, parse_mode="HTML")

@dp.message_handler(commands=['size'])
async def cmd_size(message: types.Message):
    if message.from_user.id != USER_ID:
        return
        
    markup = types.InlineKeyboardMarkup(row_width=1)
    sizes = [
        "s-1vcpu-1gb",
        "s-1vcpu-2gb",
        "s-2vcpu-2gb",
        "s-2vcpu-4gb",
        "s-4vcpu-8gb"
    ]
    
    for size in sizes:
        prefix = "✅ " if server_settings["size"] == size else ""
        markup.insert(types.InlineKeyboardButton(f"{prefix}{size}", callback_data=f"setsz_{size}"))
        
    await message.answer(f"⚙️ <b>Выбор размера (тарифа)</b>\nТекущий: <code>{server_settings['size']}</code>", reply_markup=markup, parse_mode="HTML")

# === ОБРАБОТЧИКИ КНОПОК ===

@dp.callback_query_handler(lambda c: c.data.startswith('setreg_'))
async def process_set_region(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != USER_ID:
        return
        
    new_region = callback_query.data.split('_')[1]
    server_settings["region"] = new_region
    
    await bot.answer_callback_query(callback_query.id, f"Регион изменен на {new_region}")
    await bot.edit_message_text(f"🌍 <b>Регион успешно изменен!</b>\nНовый регион: <code>{new_region}</code>", 
                                chat_id=callback_query.message.chat.id, 
                                message_id=callback_query.message.message_id, 
                                parse_mode="HTML")

@dp.callback_query_handler(lambda c: c.data.startswith('setsz_'))
async def process_set_size(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != USER_ID:
        return
        
    new_size = callback_query.data.split('_')[1]
    server_settings["size"] = new_size
    
    await bot.answer_callback_query(callback_query.id, f"Тариф изменен на {new_size}")
    await bot.edit_message_text(f"⚙️ <b>Тариф успешно изменен!</b>\nНовый тариф: <code>{new_size}</code>", 
                                chat_id=callback_query.message.chat.id, 
                                message_id=callback_query.message.message_id, 
                                parse_mode="HTML")

@dp.callback_query_handler(lambda c: c.data.startswith('keep_'))
async def process_keep(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != USER_ID:
        return

    droplet_id = int(callback_query.data.split('_')[1])
    global current_batch_droplets

    await bot.answer_callback_query(callback_query.id, "Сохраняю сервер, остальной мусор удаляю... 🧹")
    
    deleted_count = 0
    for d_id in list(current_batch_droplets):
        if d_id != droplet_id:
            await delete_droplet(d_id)
            deleted_count += 1
            
    current_batch_droplets.clear()
    
    await bot.edit_message_reply_markup(callback_query.message.chat.id, callback_query.message.message_id, reply_markup=None)
    await bot.send_message(callback_query.from_user.id, f"✅ <b>Сервер успешно сохранен!</b>\nУдалено {deleted_count} неподходящих серверов из партии 🗑.", parse_mode="HTML")

@dp.callback_query_handler(lambda c: c.data.startswith('del_') and c.data != "del_all")
async def process_delete(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != USER_ID:
        return

    droplet_id = int(callback_query.data.split('_')[1])
    global current_batch_droplets

    await bot.answer_callback_query(callback_query.id, "Удаляю этот сервер... 🗑")
    await delete_droplet(droplet_id)
    
    if droplet_id in current_batch_droplets:
        current_batch_droplets.remove(droplet_id)
        
    await bot.edit_message_reply_markup(callback_query.message.chat.id, callback_query.message.message_id, reply_markup=None)
    await bot.send_message(callback_query.from_user.id, "🗑 <b>Сервер удален.</b>", parse_mode="HTML")

@dp.callback_query_handler(lambda c: c.data == "del_all")
async def process_delete_all(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != USER_ID:
        return

    global current_batch_droplets
    await bot.answer_callback_query(callback_query.id, "Удаляю всю созданную партию... 🧹")
    
    for d_id in list(current_batch_droplets):
        await delete_droplet(d_id)
        
    current_batch_droplets.clear()
    await bot.edit_message_reply_markup(callback_query.message.chat.id, callback_query.message.message_id, reply_markup=None)
    await bot.send_message(callback_query.from_user.id, "💥 <b>Вся партия серверов успешно удалена.</b>", parse_mode="HTML")

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
