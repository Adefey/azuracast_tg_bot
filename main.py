import asyncio
import html
import logging
import os
import sys
from datetime import datetime, timedelta
from random import choice

import aiocron
import aiohttp
from fastapi import FastAPI, Request, Response, status
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from motd import HOLIDAY_MOTD, RANDOM_MOTD, local_tz

AZURACAST_API_KEY = os.environ.get("AZURACAST_API_KEY")
AZURACAST_API_URL = os.environ.get("AZURACAST_API_URL")
CHAT_ID = os.environ.get("CHAT_ID")
TELEGRAM_API_KEY = os.environ.get("TELEGRAM_API_KEY")
MAIN_STATION_ID = int(os.environ.get("MAIN_STATION_ID", 1))
AZURACAST_API_CALL_HEADERS = {"X-API-Key": AZURACAST_API_KEY, "accept": "application/json"}
START_RETRIES = 10
START_RETRY_WAIT_SEC = 5

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(filename=f'logs/log_{datetime.now().strftime("%y_%m_%d_%H-%M-%S")}.log'),
        logging.StreamHandler(stream=sys.stdout),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
bot: Bot | None = None
# List that is filled only on init with dicts of keys:
stations: list[dict] = []
#   station_id: int,
#   name: str,
#   public_player_url: str
#   schedule_url: str
#   listen_url: str
#   description: str
emojis = [
    "☠️",
    "⛓️",
    "🌃",
    "🌈",
    "🌙",
    "🌟",
    "🍕",
    "🎉",
    "🎸",
    "👾",
    "🔥",
    "🔩",
    "😊",
    "🚀",
    "🤖",
]


async def gather_stations():
    async with aiohttp.ClientSession(headers=AZURACAST_API_CALL_HEADERS) as session:
        for attempt in range(1, START_RETRIES + 1):
            logger.info(f"Trying to find stations (attempt {attempt}/{START_RETRIES})")

            try:
                async with session.get(f"{AZURACAST_API_URL}/stations") as response:
                    if response.status == status.HTTP_200_OK:
                        response_json = await response.json()
                        new_stations = []
                        for station in response_json:
                            new_stations.append({
                                "station_id": station["id"],
                                "name": station["name"],
                                "public_player_url": station["public_player_url"],
                                "schedule_url": f"{station['public_player_url']}/schedule",
                                "listen_url": station["listen_url"],
                                "description": station["description"],
                            })
                        new_stations.sort(key=lambda x: x["station_id"])

                        stations.clear()
                        stations.extend(new_stations)
                        logger.info(f"Found {len(stations)} stations")
                        return

                    logger.warning(f"Got status {response.status}, retrying...")
            except (RuntimeError, aiohttp.ClientError):
                logger.error("Network error while finding stations, retrying...", exc_info=True)

            await asyncio.sleep(START_RETRY_WAIT_SEC)


async def startup():
    logger.info(
        f"Starting bot for {AZURACAST_API_URL} in chat {CHAT_ID} with AzuraCast key"
        f" {AZURACAST_API_KEY[:5]}... and Telegram key {TELEGRAM_API_KEY[:5]}..."
    )
    await gather_stations()
    if len(stations) == 0:
        logger.error(
            f"Cannot start. No stations detected after {START_RETRIES} retries with delay"
            f" {START_RETRY_WAIT_SEC} sec. Aborting"
        )
        raise RuntimeError("Cannot start, no stations detected")

    global bot
    bot = Bot(token=TELEGRAM_API_KEY)


async def shutdown():
    logger.info("Server is stopping")


def format_duration(seconds: int | float) -> str:
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes}:{seconds:02d}"


async def prepare_message_per_station(
    station_object: dict, return_empty_on_zero_listeners: bool = True
) -> tuple[int, str]:
    """
    Return:
    int - station_id - used for ordering in final list of stations
    str - html string - actual content
    """
    logger.info(
        f"Preparing message for station {station_object['station_id']},"
        f" {'will' if return_empty_on_zero_listeners else 'will not'} return empty string if there"
        " are 0 listeners"
    )
    html_message = ""
    async with aiohttp.ClientSession(headers=AZURACAST_API_CALL_HEADERS) as session:
        try:
            async with session.get(
                f"{AZURACAST_API_URL}/nowplaying/{station_object['station_id']}",
            ) as response:
                if response.status != status.HTTP_200_OK:
                    msg = (
                        f"Got status {response.status}, cannot get data for station {station_object['name']}"
                    )
                    logger.error(msg)
                    raise RuntimeError(
                        f"Cannot get data for station {station_object['name']}, status: {response.status}"
                    )

                response_json = await response.json()

                station_name = html.escape(response_json["station"]["name"] or "")
                now_playing_song_artist = html.escape(response_json["now_playing"]["song"]["artist"] or "")
                now_playing_song_title = html.escape(response_json["now_playing"]["song"]["title"] or "")
                now_playing_song_art = html.escape(
                    (response_json.get("now_playing", {}).get("song", {}) or {}).get("art") or ""
                )
                now_playing_duration = format_duration(response_json["now_playing"]["duration"])
                now_playing_playlist = html.escape(response_json["now_playing"]["playlist"] or "")
                listeners_total = response_json["listeners"]["total"]
                playing_next = response_json.get("playing_next") or {}
                playing_next_song = playing_next.get("song") or {}
                playing_next_song_artist = html.escape(playing_next_song.get("artist") or "")
                playing_next_song_title = html.escape(playing_next_song.get("title") or "")
                playing_next_duration = format_duration(playing_next.get("duration"))
                listen_url = response_json["station"]["listen_url"]
                station_public_player_url = response_json["station"]["public_player_url"]
                station_schedule = f"{response_json['station']['public_player_url']}/schedule"

                if listeners_total == 0 and return_empty_on_zero_listeners:
                    logger.info(
                        f"Returning empty message for {station_object['name']} because there are"
                        f" {listeners_total} listeners"
                    )
                    return ({station_object["station_id"]}, html_message)

                lines = [
                    (f"📻 <b>Станция: {station_name}</b> <a" f' href="{now_playing_song_art}">\u200b</a>'),
                    f"⚡ <b>{now_playing_song_artist}</b> - {now_playing_song_title}",
                    f"⏱️ Длительность: {now_playing_duration}",
                    f"📂 Плейлист: {now_playing_playlist}" if now_playing_playlist else "",
                    f"🫂 Слушателей: {listeners_total}",
                    (
                        f"🔜 <b>Далее:</b> {playing_next_song_artist} - {playing_next_song_title}"
                        f" ({playing_next_duration})"
                    ),
                    f'<a href="{listen_url}">📼</a> <a href="{station_public_player_url}">Открыть плеер</a>',
                    f'📅 <a href="{station_schedule}">Расписание</a>',
                ]

                html_message = "\n".join(lines)

        except (RuntimeError, aiohttp.ClientError):
            logger.error(
                f"Could not check {AZURACAST_API_URL}/nowplaying/{station_object['station_id']}, got"
                " exception",
                exc_info=True,
            )

    logger.info(f"Prepared message for station {station_object['name']}")
    return (station_object["station_id"], html_message)


async def prepare_stations_list(main_station_object: dict) -> str:
    lines = [
        f"⚡️ <b>{html.escape(main_station_object['name'])}</b>",
        "",
        f'📻 <a href="{main_station_object["public_player_url"]}"><b>Главный эфир</b></a>',
        f'📅 <a href="{main_station_object["schedule_url"]}"><b>Расписание</b></a>',
        "",
        "🎧 <b>Специальные станции</b>",
    ]

    for station in [station for station in stations if station["station_id"] != MAIN_STATION_ID]:
        lines.append(
            f"<a href=\"{station['listen_url']}\">{choice(emojis)}</a> <a"
            f" href=\"{station['public_player_url']}\">{html.escape(station.get('name') or '')}</a>:"
            f" {html.escape(station.get('description') or '')}"
        )

    stations_message = "\n".join(lines)

    return stations_message


async def prepare_schedule(station_object: dict) -> str:
    logger.info(f"Preparing schedule for station {station_object['station_id']}")
    now = datetime.now(tz=local_tz)
    now_iso = now.isoformat()
    end = now + timedelta(hours=23, minutes=59)
    end_iso = end.isoformat()
    tz_iso = local_tz.key
    schedule_message = ""
    async with aiohttp.ClientSession(headers=AZURACAST_API_CALL_HEADERS) as session:
        try:
            async with session.get(
                f"{AZURACAST_API_URL}/station/{station_object['station_id']}/schedule",
                params={"start": now_iso, "end": end_iso, "timeZone": tz_iso},
            ) as response:
                if response.status != status.HTTP_200_OK:
                    msg = (
                        f"Got status {response.status}, cannot get schedule for station"
                        f" {station_object['station_id']}"
                    )
                    logger.error(msg)
                    raise RuntimeError(
                        f"Cannot get schedule for station {station_object['station_id']}, status:"
                        f" {response.status}"
                    )

                response_json = await response.json()

                response_json.sort(key=lambda item: item["start"])
                lines = [
                    (
                        f"📅 Расписание <b>{html.escape(station_object['name'])}</b> на"
                        f" {now.strftime('%d %B %Y')}"
                    ),
                    "",
                ]

                if not response_json:
                    lines.append("🤷 Нет расписания")
                else:
                    for i, item in enumerate(response_json, start=1):
                        title = html.escape(item.get("title") or "")
                        start = datetime.fromisoformat(item["start"])
                        end = datetime.fromisoformat(item["end"])
                        lines.append(
                            f"{i}. <b>{title}</b>: {start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
                        )

                lines += [
                    "",
                    f"🗓️ <a href=\"{station_object['schedule_url']}\">Расписание на неделю</a>",
                ]

                schedule_message = "\n".join(lines)

        except (RuntimeError, aiohttp.ClientError):
            logger.error(
                f"Could not check {AZURACAST_API_URL}/nowplaying/{station_object['station_id']},"
                " got exception",
                exc_info=True,
            )
    logger.info(f"Prepared schedule for station {station_object['station_id']}")
    return schedule_message


def prepare_motd_message() -> str:
    now = datetime.now(tz=local_tz)
    selected_motd = None

    holiday_motd_list = []
    for start_time, end_time, list_of_motd in HOLIDAY_MOTD:
        if start_time <= now <= end_time:
            holiday_motd_list.extend(list_of_motd)

    if holiday_motd_list:
        selected_motd = choice(holiday_motd_list)
    else:
        selected_motd = choice(RANDOM_MOTD)

    return f"<i>{html.escape(selected_motd)}</i>"


async def prepare_webhook_message(webhook_data: dict | None = None) -> str:
    logger.info(f"Gathering data for {len(stations)} stations")
    futures = [
        prepare_message_per_station(station["station_id"], station["station_id"] != MAIN_STATION_ID)
        for station in stations
    ]
    per_station_messages = await asyncio.gather(*futures)
    per_station_messages = [message for message in per_station_messages if message[1]]
    if not per_station_messages:
        logger.warning("Output from prepare_message_per_station is empty")
        return ""
    per_station_messages = sorted(per_station_messages, key=lambda x: x[0])
    motd_message = prepare_motd_message()
    messages = [message[1] for message in per_station_messages] + [motd_message]
    html_message = "\n\n".join(messages)
    return html_message


async def process_nightly_sweep(webhook_data: dict | None = None) -> str:
    logger.info("Cleaning pinned messages")
    await bot.unpin_all_chat_messages(chat_id=CHAT_ID)
    logger.info("Getting all stations and schedule of main station")
    await gather_stations()  # Force update all stations
    main_station_object = None
    for station in stations:
        if station["station_id"] == MAIN_STATION_ID:
            main_station_object = station
    # gather preserves order
    all_stations_message, main_station_schedule = await asyncio.gather(
        prepare_stations_list(main_station_object), prepare_schedule(main_station_object)
    )
    logger.info("Sending and pinning all stations message")
    logger.debug(f"{all_stations_message}")
    if all_stations_message:
        sent_message = await bot.send_message(
            chat_id=CHAT_ID,
            text=all_stations_message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await bot.pin_chat_message(
            chat_id=CHAT_ID, message_id=sent_message.message_id, disable_notification=True
        )
    else:
        logger.warning("Message with stations list is empty. Something is wrong")
    logger.info("Sending and pinning main station today schedule message")
    logger.debug(f"{main_station_schedule}")
    if main_station_schedule:
        sent_message = await bot.send_message(
            chat_id=CHAT_ID,
            text=main_station_schedule,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await bot.pin_chat_message(
            chat_id=CHAT_ID, message_id=sent_message.message_id, disable_notification=True
        )
    else:
        logger.warning("Message with main station schedule is empty. Something is wrong")

    logger.info("Nightly sweep done")


async def lifespan(app: FastAPI):
    await startup()
    cron = aiocron.crontab("0 0 * * *", func=process_nightly_sweep, tz=local_tz)
    yield
    cron.stop()
    await shutdown()


app = FastAPI(
    title="Adefe Radio bot",
    summary="Bot for AzuraCast instance that gathers information about multiple stations",
    version="20-11-2026",
    lifespan=lifespan,
)


@app.post("/webhook", description="AzuraCast is supposed to call this handle")
async def webhook(request: Request) -> Response:
    data = None
    try:
        data = await request.json()
        logger.debug(f"Got data from AzuraCast webhook: {data}")
    except ValueError:
        logger.warning(
            "Could not decode request's json. Not a problem, it is not needed anyways",
            exc_info=False,
        )
    message = await prepare_webhook_message(data)
    if message:
        try:
            await bot.send_message(CHAT_ID, message, parse_mode=ParseMode.HTML)
        except TelegramError:
            logger.error("Error sending Telegram message", exc_info=True)
    else:
        logger.warning("Tried to send empty message. Something is wrong")
    return Response(status_code=status.HTTP_200_OK)


@app.post("/nightly", description="Handle for debugging")
async def nightly(request: Request) -> Response:
    data = None
    try:
        data = await request.json()
        logger.debug(f"Got data: {data}")
    except ValueError:
        logger.warning(
            f"Could not decode request's json. Not a problem, it is not needed anyways",
            exc_info=False,
        )
    await process_nightly_sweep(data)
    return Response(status_code=status.HTTP_200_OK)
