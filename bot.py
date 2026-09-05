import asyncio
import hashlib
import logging
import os
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_IDS = [744712874, 562708273]

SOLARMAN_EMAIL = os.getenv("SOLARMAN_EMAIL")
SOLARMAN_PASSWORD = os.getenv("SOLARMAN_PASSWORD")
SOLARMAN_APP_ID = os.getenv("SOLARMAN_APP_ID")
SOLARMAN_APP_SECRET = os.getenv("SOLARMAN_APP_SECRET")

INVERTER_HOME_SN = os.getenv("LOGGER_HOME_SN")
INVERTER_APT_SN = os.getenv("LOGGER_APT_SN")

BASE_URL = "https://globalapi.solarmanpv.com"
CHECK_INTERVAL_MINUTES = 5

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()

token_cache = {"token": None}
grid_status = {"home": None, "apt": None}

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

async def get_access_token() -> str:
    if token_cache["token"]:
        return token_cache["token"]
    
    url = f"{BASE_URL}/account/v1.0/token?appId={SOLARMAN_APP_ID}&language=en"
    payload = {
        "appSecret": SOLARMAN_APP_SECRET,
        "email": SOLARMAN_EMAIL,
        "password": hash_password(SOLARMAN_PASSWORD),
        "userType": 1
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if data.get("success"):
                token_cache["token"] = data.get("access_token")
                return token_cache["token"]
            raise Exception(f"Ошибка получения токена: {data}")

async def get_inverter_data(device_sn: str) -> dict:
    token = await get_access_token()
    url = f"{BASE_URL}/device/v1.0/currentData?appId={SOLARMAN_APP_ID}&language=en"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "deviceSn": device_sn,
        "deviceType": 1
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()
            if data.get("success"):
                raw_list = data.get("dataList", [])
                result = {}
                for item in raw_list:
                    result[item.get("key")] = item.get("value")
                return result
            else:
                if data.get("code") in [2101009, 1000]: 
                    token_cache["token"] = None
                raise Exception(f"Ошибка API Solarman: {data}")

def get_val(data: dict, *keys, default="N/A"):
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return default

def format_status(title: str, d: dict, is_apt: bool = False) -> str:
    if not d:
        return f"<b>{title}:</b> ❌ Нет данных"
    
    soc = get_val(d, "batteryCapacity", "soc", "BAT_SOC", "battery_soc")
    if soc != "N/A":
        soc = f"{soc}%"
        
    gen_pwr = get_val(d, "generationPower", "generation_power", "inv_gen_power", "inverterPower", "apower")
    if gen_pwr != "N/A":
        gen_pwr = f"{float(gen_pwr):.2f} Вт"
        
    use_pwr = get_val(d, "usePower", "use_power", "loadPower", "load_power", "homePower")
    if use_pwr != "N/A":
        use_pwr = f"{float(use_pwr):.2f} Вт"
        
    grid_pwr = get_val(d, "g_p_ln", "g_p_l1", "g_t_p", "pgrid", "gridpower")
    if grid_pwr != "N/A":
        grid_pwr = f"{float(grid_pwr):.2f} Вт"

    if is_apt:
        return (
            f"🏢 <b>{title}:</b>\n"
            f"🔋 АКБ: {soc}\n"
            f"☀️ Панели: {gen_pwr}\n"
            f"🔌 Потребление: {use_pwr}\n"
            f"⚡ Сеть: {grid_pwr}"
        )
    else:
        return (
            f"🏠 <b>{title}:</b>\n"
            f"🔋 АКБ: {soc}\n"
            f"☀️ Панели: {gen_pwr}\n"
            f"🔌 Потребление: {use_pwr}\n"
            f"⚡ Сеть: {grid_pwr}"
        )

async def notify_all(text: str):
    for chat_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление для {chat_id}: {e}")

async def check_inverters_background():
    try:
        home_data, apt_data = await asyncio.gather(
            get_inverter_data(INVERTER_HOME_SN),
            get_inverter_data(INVERTER_APT_SN)
        )
        
        notifications = []
        
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
            home_text = format_status("ДОМ", home_data, is_apt=False)
            apt_text = format_status("КВАРТИРА", apt_data, is_apt=True)
            full_msg = f"{note}\n\n<b>Актуальный статус:</b>\n\n{home_text}\n\n{apt_text}"
            await notify_all(full_msg)
            
    except Exception as e:
        logging.error(f"Ошибка в фоновой проверке: {e}")

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "🤖 Бот мониторинга Deye запущен!\n"
        "Опрос каждые 5 минут с уведомлениями только по свету.\n"
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
        home_text = format_status("ДОМ", home_data, is_apt=False)
        apt_text = format_status("КВАРТИРА", apt_data, is_apt=True)
        await wait_msg.edit_text(f"<b>Статус инверторов Deye:</b>\n\n{home_text}\n\n{apt_text}")
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка получения данных: {e}")

async def handle_web(request):
    return web.Response(text="Deye Bot is running!")

async def web_server_runner():
    app = web.Application()
    app.router.add_get("/", handle_web)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {port}")

async def main():
    await web_server_runner()
    
    scheduler.add_job(check_inverters_background, "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.start()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
