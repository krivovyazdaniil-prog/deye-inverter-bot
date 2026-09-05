import os
import asyncio
import hashlib
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_IDS = [7447712874, 5627080273]
SOLARMAN_EMAIL = os.getenv("SOLARMAN_EMAIL")
SOLARMAN_PASSWORD = os.getenv("SOLARMAN_PASSWORD")
SOLARMAN_APP_ID = os.getenv("SOLARMAN_APP_ID")
SOLARMAN_APP_SECRET = os.getenv("SOLARMAN_APP_SECRET")
INVERTER_HOME_SN = os.getenv("LOGGER_HOME_SN")
INVERTER_APT_SN = os.getenv("LOGGER_APT_SN")

BASE_URL = "https://globalapi.solarmanpv.com"
CHECK_INTERVAL_MINUTES = 1

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()

token_cache = {"token": None}
grid_status = {"home": None, "apt": None}

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

async def get_access_token():
    if token_cache["token"]:
        return token_cache["token"]
    url = f"{BASE_URL}/account/v1.0/token"
    params = {"appId": SOLARMAN_APP_ID, "language": "en"}
    payload = {
        "appSecret": SOLARMAN_APP_SECRET,
        "email": SOLARMAN_EMAIL,
        "password": hash_password(SOLARMAN_PASSWORD)
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, params=params, json=payload) as resp:
            data = await resp.json()
            if data.get("success") or "access_token" in data:
                token_cache["token"] = data.get("access_token")
                return token_cache["token"]
            else:
                raise Exception(f"Ошибка авторизации: {data}")

async def get_inverter_data(device_sn: str):
    if not device_sn:
        return None
    token = await get_access_token()
    url = f"{BASE_URL}/device/v1.0/currentData"
    params = {"appId": SOLARMAN_APP_ID}
    headers = {
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {"deviceSn": device_sn}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, params=params, json=payload, headers=headers) as resp:
            data = await resp.json()
            if not data.get("success"):
                token_cache["token"] = None
                return None
            data_dict = {}
            for item in data.get("dataList", []):
                key = item.get("key")
                val = item.get("value")
                if key:
                    data_dict[key] = val
            return data_dict

def get_val(data: dict, *keys):
    if not data:
        return "N/A"
    lower_data = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = k.lower()
        if lk in lower_data and lower_data[lk] not in (None, "", "N/A"):
            return lower_data[lk]
    return "N/A"

def format_status(title: str, data: dict, is_apt: bool = False) -> str:
    if not data:
        return f"<b>{title}:</b>\n❌ Нет данных от инвертора\n"
    soc = get_val(data, "bms_soc", "b_left_cap1", "soc", "batterysoc")
    pv = get_val(data, "pv_d_p_g", "ppv", "pvpower", "p_pv", "totalpvpower", "g_t_p")
    load = get_val(data, "lp_ln", "lpp_a", "pload", "loadpower", "p_load", "totalloadpower")
    if is_apt:
        grid = get_val(data, "g_p_ln", "g_p_l1", "g_t_p", "pgrid", "gridpower", "p_grid")
    else:
        grid = get_val(data, "g_p_l1", "g_p_ln", "pgrid", "gridpower", "p_grid")
    return (
        f"<b>{title}:</b>\n"
        f"🔋 <b>АКБ:</b> {soc}%\n"
        f"☀️ <b>Панели:</b> {pv} Вт\n"
        f"🏠 <b>Потребление:</b> {load} Вт\n"
        f"🔌 <b>Сеть:</b> {grid} Вт\n"
    )

async def notify_all(text: str):
    for chat_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение пользователю {chat_id}: {e}")

async def check_inverters_background():
    home_data, apt_data = await asyncio.gather(
        get_inverter_data(INVERTER_HOME_SN),
        get_inverter_data(INVERTER_APT_SN)
    )
    notifications = []
    if home_data:
        raw_grid = get_val(home_data, "g_p_l1", "g_p_ln", "pgrid", "gridpower")
        grid_pwr = float(raw_grid) if raw_grid != "N/A" else 0.0
        has_grid = grid_pwr > 10
        if grid_status["home"] is not None and grid_status["home"] != has_grid:
            if not has_grid:
                notifications.append("🔴 <b>СВЕТ ОТКЛЮЧЕН (ДОМ)</b>\nИнвертор перешел на работу от АКБ!")
            else:
                notifications.append("🟢 <b>СВЕТ ВКЛЮЧЕН (ДОМ)</b>\nСеть восстановилась!")
        grid_status["home"] = has_grid
    if apt_data:
        raw_grid = get_val(apt_data, "g_p_ln", "g_p_l1", "g_t_p", "pgrid", "gridpower")
        grid_pwr = float(raw_grid) if raw_grid != "N/A" else 0.0
        has_grid = grid_pwr > 10
        if grid_status["apt"] is not None and grid_status["apt"] != has_grid:
            if not has_grid:
                notifications.append("🔴 <b>СВЕТ ОТКЛЮЧЕН (КВАРТИРА)</b>\nИнвертор перешел на работу от АКБ!")
            else:
                notifications.append("🟢 <b>СВЕТ ВКЛЮЧЕН (КВАРТИРА)</b>\nСеть восстановилась!")
        grid_status["apt"] = has_grid
    for note in notifications:
        home_text = format_status("🏡 ДОМ", home_data, is_apt=False)
        apt_text = format_status("🏢 КВАРТИРА", apt_data, is_apt=True)
        full_msg = f"{note}\n\n📊 <b>Актуальный статус:</b>\n\n{home_text}\n{apt_text}"
        await notify_all(full_msg)

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "👋 Бот мониторинга Deye запущен!\n"
        "Опрос каждые 1 минуту с уведомлениями только по свету.\n"
        "Команды:\n/status — проверить текущие данные"
    )

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    wait_msg = await message.answer("⏳ Запрашиваю данные с инверторов...")
    try:
        home_data, apt_data = await asyncio.gather(
            get_inverter_data(INVERTER_HOME_SN),
            get_inverter_data(INVERTER_APT_SN)
        )
        home_text = format_status("🏡 ДОМ", home_data, is_apt=False)
        apt_text = format_status("🏢 КВАРТИРА", apt_data, is_apt=True)
        await wait_msg.edit_text(f"📊 <b>Статус инверторов Deye</b>\n\n{home_text}\n{apt_text}")
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка получения данных: {e}")

async def main():
    # 1. Запуск планировщика опрашивания инверторов
    scheduler.add_job(check_inverters_background, "interval", minutes=1)
    scheduler.start()

    # 2. Веб-сервер для прохождения health check на Render (убирает Timed Out)
    async def health_check(request):
        return web.Response(text="Bot is alive!")

    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {port}")

    # 3. Запуск телеграм-бота
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
