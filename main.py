import asyncio
import html
import json
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
from utils import aiohttp_get

AZURACAST_API_KEY = os.environ.get("AZURACAST_API_KEY")
AZURACAST_API_URL = os.environ.get("AZURACAST_API_URL")
TELEGRAM_API_KEY = os.environ.get("TELEGRAM_API_KEY")
TELEGRAM_CHANNEL_URL = os.environ.get("TELEGRAM_CHANNEL_URL")
TELEGRAPH_API_KEY = os.environ.get("TELEGRAPH_API_KEY")
CHAT_ID = os.environ.get("CHAT_ID")
MAIN_STATION_ID = int(os.environ.get("MAIN_STATION_ID", 1))
ARTICLE_ID = os.environ.get("ARTICLE_ID")
AZURACAST_API_CALL_HEADERS = {
    "X-API-Key": AZURACAST_API_KEY,
    "accept": "application/json",
}
START_RETRIES = 10
START_RETRY_WAIT_SEC = 5
SKIP_PLAYLIST_ENTRIES = {"default", "Disabled", "Jingle"}


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
                logger.error(
                    "Network error while finding stations, retrying...",
                    exc_info=True,
                )

            await asyncio.sleep(START_RETRY_WAIT_SEC)


async def startup():
    logger.info(
        f"Starting bot for {AZURACAST_API_URL} in chat {CHAT_ID} with AzuraCast"
        f" key {AZURACAST_API_KEY[:5]}... and Telegram key"
        f" {TELEGRAM_API_KEY[:5]}... and Telegraph key {TELEGRAPH_API_KEY[:5]}... for {TELEGRAM_CHANNEL_URL}"
    )
    await gather_stations()
    if len(stations) == 0:
        logger.error(
            f"Cannot start. No stations detected after {START_RETRIES} retries"
            f" with delay {START_RETRY_WAIT_SEC} sec. Aborting"
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
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
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
        f"Preparing message for station {station_object['name']},"
        f" {'will' if return_empty_on_zero_listeners else 'will not'} return"
        " empty string if there are 0 listeners"
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
                    raise RuntimeError(msg)

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
                        f"Returning empty message for {station_object['name']}"
                        f" because there are {listeners_total} listeners"
                    )
                    return (station_object["station_id"], html_message)

                lines = [
                    (f"📻 <b>Станция: {station_name}</b> <a" f' href="{now_playing_song_art}">\u200b</a>'),
                    f"⚡ <b>{now_playing_song_artist}</b> - {now_playing_song_title}",
                    f"⏱️ Длительность: {now_playing_duration}",
                    (f"📂 Плейлист: {now_playing_playlist}" if now_playing_playlist else ""),
                    f"🫂 Слушателей: {listeners_total}",
                    (
                        f"🔜 <b>Далее:</b> {playing_next_song_artist} -"
                        f" {playing_next_song_title} ({playing_next_duration})"
                    ),
                    f'<a href="{listen_url}">📼</a> <a href="{station_public_player_url}">Открыть плеер</a>',
                    f'📅 <a href="{station_schedule}">Расписание</a>',
                ]

                html_message = "\n".join(lines)

        except (RuntimeError, aiohttp.ClientError, KeyError, TypeError):
            logger.error(
                "Could not check"
                f" {AZURACAST_API_URL}/nowplaying/{station_object['station_id']},"
                " got exception",
                exc_info=True,
            )

    logger.info(f"Prepared message for station {station_object['name']}")
    return (station_object["station_id"], html_message)


async def prepare_stations_list(main_station_object: dict) -> str:
    lines = [
        (
            f"⚡️ <b>{html.escape(main_station_object['name'])}</b>:"
            f" {html.escape(main_station_object.get('description') or '')}"
        ),
        "",
        f'📄 <a href="https://telegra.ph/{ARTICLE_ID}"><b>Список контента на станции</b></a>',
        "",
        ("📻 <a" f' href="{main_station_object["public_player_url"]}"><b>Главный' " эфир</b></a>"),
        ("📅 <a" f' href="{main_station_object["schedule_url"]}"><b>Расписание</b></a>'),
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
                        f"Got status {response.status}, cannot get schedule for"
                        f" station {station_object['station_id']}"
                    )
                    logger.error(msg)
                    raise RuntimeError(msg)

                response_json = await response.json()

                response_json.sort(key=lambda item: item["start"])
                lines = [
                    (
                        "📅 Расписание"
                        f" <b>{html.escape(station_object['name'])}</b> на"
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
                f"Could not check {AZURACAST_API_URL}/schedule/{station_object['station_id']}, got exception",
                exc_info=True,
            )

    logger.info(f"Prepared schedule for station {station_object['station_id']}")
    return schedule_message


async def prepare_station_content_telegraph_article(
    station_object: dict,
) -> None:
    logger.info(f"Preparing station content Telegraph article for station {station_object['station_id']}")
    content_per_playlist: dict[str, list[str]] = {}
    description_per_playlist: dict[str, list[str]] = {}
    total_num_songs = 0
    total_total_length = 0
    async with aiohttp.ClientSession(headers=AZURACAST_API_CALL_HEADERS) as session:
        try:
            async with session.get(
                f"{AZURACAST_API_URL}/station/{station_object['station_id']}/playlists",
            ) as response:
                if response.status != status.HTTP_200_OK:
                    msg = (
                        f"Got status {response.status}, cannot get playlists"
                        f" for station {station_object['station_id']}"
                    )
                    logger.error(msg)
                    raise RuntimeError(msg)

                response_json = await response.json()

                response_json.sort(key=lambda item: item["name"])

                for playlist in response_json:
                    if playlist["name"] in SKIP_PLAYLIST_ENTRIES:
                        continue
                    if not playlist["is_enabled"]:
                        continue
                    content_per_playlist[playlist["name"]] = []

                    num_songs = int(playlist["num_songs"])
                    total_length = float(playlist["total_length"])

                    total_num_songs += num_songs
                    total_total_length += total_length

                    stats = (
                        f"Всего треков: {num_songs}. Суммарная длительность: {format_duration(total_length)}"
                    )
                    description = playlist["description"] if playlist["description"] is not None else ""

                    description_per_playlist[playlist["name"]] = [stats, description]

        except (RuntimeError, aiohttp.ClientError):
            logger.error(
                f"Could not check {AZURACAST_API_URL}/station/{station_object['station_id']}/playlists, got"
                " exception",
                exc_info=True,
            )
            return

    async with aiohttp.ClientSession(headers=AZURACAST_API_CALL_HEADERS) as session:
        try:
            # gather preserves order
            responses = await asyncio.gather(*(
                aiohttp_get(
                    session,
                    f"{AZURACAST_API_URL}/station/{station_object['station_id']}/files/list",
                    params={"currentDirectory": playlist_name},
                )
                for playlist_name in content_per_playlist.keys()
            ))

            for playlist_name, playlist_directory_contents in zip(content_per_playlist.keys(), responses):
                for playlist_directory_content in playlist_directory_contents:

                    content_label = ""
                    if playlist_directory_content["type"] == "directory":
                        content_label = f"Альбом: {playlist_directory_content['path_short']}"
                    elif playlist_directory_content["type"] == "media":
                        content_label = (
                            "Трек:"
                            f" {playlist_directory_content['media']['artist']}"
                            f" - {playlist_directory_content['media']['title']}"
                        )

                    if content_label:
                        content_per_playlist[playlist_name].append(content_label)

                content_per_playlist[playlist_name].sort()

        except (RuntimeError, aiohttp.ClientError):
            logger.error(
                "Could not check"
                f" {AZURACAST_API_URL}/station/{station_object['station_id']}/files/list,"
                " got exception",
                exc_info=True,
            )
            return

    telegraph_article_title = f"{station_object['name']} - контент станции"

    total_stats = (
        f"Всего треков на станции: {total_num_songs}. Суммарная длительность:"
        f" {format_duration(total_total_length)}"
    )

    telegraph_article_content: list[dict] = []
    telegraph_article_content.append({"tag": "p", "children": [total_stats]})
    if station_object["description"]:
        telegraph_article_content.append({"tag": "blockquote", "children": [station_object["description"]]})
    for playlist, content_labels in content_per_playlist.items():
        telegraph_article_content.append({"tag": "h3", "children": [playlist]})
        telegraph_article_content.append({"tag": "p", "children": [description_per_playlist[playlist][0]]})
        if description_per_playlist[playlist][1]:
            telegraph_article_content.append(
                {"tag": "blockquote", "children": [description_per_playlist[playlist][1]]}
            )
        if content_labels:
            telegraph_article_content.append({
                "tag": "ol",
                "children": [{"tag": "li", "children": [content_label]} for content_label in content_labels],
            })
    telegraph_article_content.append({"tag": "hr"})
    telegraph_article_content.append({
        "tag": "p",
        "children": [{
            "tag": "a",
            "attrs": {"href": station_object["public_player_url"]},
            "children": ["Открыть плеер"],
        }],
    })
    telegraph_article_content.append({
        "tag": "p",
        "children": [{
            "tag": "a",
            "attrs": {"href": station_object["schedule_url"]},
            "children": ["Открыть расписание"],
        }],
    })
    telegraph_article_content.append({
        "tag": "p",
        "children": [{
            "tag": "a",
            "attrs": {"href": TELEGRAM_CHANNEL_URL},
            "children": ["Телеграм канал"],
        }],
    })

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                f"https://api.telegra.ph/editPage/{ARTICLE_ID}",
                data={
                    "access_token": TELEGRAPH_API_KEY,
                    "title": telegraph_article_title,
                    "content": json.dumps(telegraph_article_content, ensure_ascii=False),
                },
            ) as response:
                if response.status != status.HTTP_200_OK:
                    msg = (
                        f"Got status {response.status}, cannot update the"
                        f" article for {station_object['station_id']}"
                    )
                    logger.error(msg)
                    raise RuntimeError(msg)
                response_json = await response.json()
                if not response_json.get("ok"):
                    msg = (
                        f"Got response {response_json}, cannot update the"
                        f" article for {station_object['station_id']}"
                    )
                    logger.error(msg)
                    raise RuntimeError(msg)

        except (RuntimeError, aiohttp.ClientError):
            logger.error(
                f"Could not post https://api.telegra.ph/editPage/{ARTICLE_ID}, got exception",
                exc_info=True,
            )

    logger.info(
        f"Prepared station content Telegraph article for station {station_object['station_id']} with"
        f" {len(telegraph_article_content)} content elements"
    )


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
        prepare_message_per_station(station, station["station_id"] != MAIN_STATION_ID) for station in stations
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
    all_stations_message, main_station_schedule, _ = await asyncio.gather(
        prepare_stations_list(main_station_object),
        prepare_schedule(main_station_object),
        prepare_station_content_telegraph_article(main_station_object),
    )
    logger.info("Sending and pinning all stations message")
    logger.debug(f"{all_stations_message}")
    if all_stations_message:
        try:
            sent_message = await bot.send_message(
                chat_id=CHAT_ID,
                text=all_stations_message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            await bot.pin_chat_message(
                chat_id=CHAT_ID,
                message_id=sent_message.message_id,
                disable_notification=True,
            )
        except TelegramError:
            logger.error(
                f"Error sending all stations message. Tried to send:\n{all_stations_message}", exc_info=True
            )
    else:
        logger.warning("Message with stations list is empty. Something is wrong")
    logger.info("Sending and pinning main station today schedule message")
    logger.debug(f"{main_station_schedule}")
    if main_station_schedule:
        try:
            sent_message = await bot.send_message(
                chat_id=CHAT_ID,
                text=main_station_schedule,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            await bot.pin_chat_message(
                chat_id=CHAT_ID,
                message_id=sent_message.message_id,
                disable_notification=True,
            )
        except TelegramError:
            logger.error(
                f"Error sending main stataion schedule message. Tried to send:\n{main_station_schedule}",
                exc_info=True,
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
    version="27-11-2026",
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
            logger.error(f"Error sending Telegram message. Tried to send:\n{message}", exc_info=True)
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
            "Could not decode request's json. Not a problem, it is not needed anyways",
            exc_info=False,
        )
    await process_nightly_sweep(data)
    return Response(status_code=status.HTTP_200_OK)


@app.get("/health", description="Health check")
async def health():
    return {"status": "ok"}
