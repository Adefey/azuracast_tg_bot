import asyncio
import logging
import os
import sys
from datetime import datetime

import aiohttp
from fastapi import FastAPI, Request, Response, status
from telegram import Bot
from telegram.constants import ParseMode

AZURACAST_API_KEY = os.environ.get("AZURACAST_API_KEY")
AZURACAST_API_URL = os.environ.get("AZURACAST_API_URL")
CHAT_ID = os.environ.get("CHAT_ID")
TELEGRAM_API_KEY = os.environ.get("TELEGRAM_API_KEY")
MAIN_STATION_ID = 1  # Follow AzuraCast logic
MAX_STATION_ID = 9999
AZURACAST_API_CALL_HEADERS = {"X-API-Key": AZURACAST_API_KEY, "accept": "application/json"}


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(
            filename=f'logs/log_{datetime.now().strftime("%y_%m_%d_%H-%M-%S")}.log'
        ),
        logging.StreamHandler(stream=sys.stdout),
    ],
)

logger = logging.getLogger(__name__)
bot: Bot | None = None
# List that is filled only on init with dicts of keys:
stations: list[dict] = []
#   station_id: int,
#   name: str,


async def gather_stations():
    async with aiohttp.ClientSession(headers=AZURACAST_API_CALL_HEADERS) as session:
        for station_id in range(1, MAX_STATION_ID):
            logger.info(f"Trying to find station #{station_id}")
            try:
                async with session.get(
                    f"{AZURACAST_API_URL}/station/{station_id}",
                ) as response:
                    if response.status != status.HTTP_200_OK:
                        logger.info(
                            f"Got status {response.status}, stopping search. Found {len(stations)}"
                            " stations"
                        )
                        break

                    logger.info(f"Station {station_id} exists")
                    response_json = await response.json()
                    name = response_json["name"]
                    station_data = {
                        "station_id": station_id,
                        "name": name,
                    }
                    stations.append(station_data)
                    logger.info(f"Station {station_id} added: {station_data}")
            except RuntimeError:
                logger.error(
                    f"Could not check {AZURACAST_API_URL}/station/{station_id}, got exception",
                    exc_info=True,
                )


async def startup():
    logger.info(
        f"Starting bot for {AZURACAST_API_URL} in chat {CHAT_ID} with AzuraCast key"
        f" {AZURACAST_API_KEY[:5]}... and Telegram key {TELEGRAM_API_KEY[:5]}..."
    )
    await gather_stations()
    global bot
    bot = Bot(token=TELEGRAM_API_KEY)


async def shutdown():
    logger.info("Server is stopping")


async def lifespan(app: FastAPI):
    await startup()
    yield
    await shutdown()


def format_duration(seconds: int | float) -> str:
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes}:{seconds:02d}"


async def prepare_message_per_station(
    station_id: int, return_empty_on_zero_listeners: bool = True
) -> tuple[int, str]:
    """
    int - station_id - used for ordering
    str - html string - actual content
    """
    logger.info(
        f"Preparing message for station {station_id},"
        f" {'will' if return_empty_on_zero_listeners else 'will not'} return empty string if there"
        " are 0 listeners"
    )
    html_message = ""
    async with aiohttp.ClientSession(headers=AZURACAST_API_CALL_HEADERS) as session:
        try:
            async with session.get(
                f"{AZURACAST_API_URL}/nowplaying/{station_id}",
            ) as response:
                if response.status != status.HTTP_200_OK:
                    msg = f"Got status {response.status}, cannot get data for station {station_id}"
                    if response.status == status.HTTP_404_NOT_FOUND:
                        # Station might be disables
                        logger.warning(msg)
                    else:
                        logger.error(msg)
                        raise RuntimeError(
                            f"Cannot get data for station {station_id}, status: {response.status}"
                        )

                response_json = await response.json()

                station_name = response_json["station"]["name"]
                now_playing_song_artist = response_json["now_playing"]["song"]["artist"]
                now_playing_song_title = response_json["now_playing"]["song"]["artist"]
                now_playing_song_art = response_json["now_playing"]["song"]["art"]
                now_playing_duration = format_duration(response_json["now_playing"]["duration"])
                now_playing_playlist = response_json["now_playing"]["playlist"]
                listeners_total = response_json["listeners"]["total"]
                playing_next_song_artist = response_json["playing_next"]["song"]["artist"]
                playing_next_song_title = response_json["playing_next"]["song"]["title"]
                playing_next_duration = format_duration(response_json["playing_next"]["duration"])
                station_public_player_url = response_json["station"]["public_player_url"]
                station_schedule = f"{response_json["station"]["public_player_url"]}/schedule"

                if listeners_total == 0 and return_empty_on_zero_listeners:
                    return (station_id, html_message)

                html_message = f"""📻 Станция: <b>{station_name}</b>
⚡ <b>{now_playing_song_artist}</b> - {now_playing_song_title}
<a href="{now_playing_song_art}">​</a>
⏱️ Длительность: {now_playing_duration}
📂 Плейлист: {now_playing_playlist}
🫂 Слушателей: {listeners_total}

🔜 <b>Далее:</b> {playing_next_song_artist} - {playing_next_song_title} ({playing_next_duration})

📼 <a href="{station_public_player_url}">Открыть плеер</a>
📅 <a href="{station_schedule}/schedule">Расписание</a>
"""

        except RuntimeError:
            logger.error(
                f"Could not check {AZURACAST_API_URL}/nowplaying/{station_id}, got exception",
                exc_info=True,
            )

    logger.info(f"Prepared message for station {station_id}")
    return (station_id, html_message)


async def prepare_message(webhook_data: dict | None) -> str:
    logger.info(f"Gathering data for {len(stations)} stations")
    futures = [
        prepare_message_per_station(station["station_id"], station["station_id"] != MAIN_STATION_ID)
        for station in stations
    ]
    per_station_messages = await asyncio.gather(*futures)
    per_station_messages = sorted(per_station_messages, key=lambda x: x[0])
    html_message = "\n".join(message[1] for message in per_station_messages)
    return html_message


app = FastAPI(
    title="Adefe Radio bot",
    summary="Bot for AzuraCast instance that gathers information about multiple stations",
    version="08-11-2026",
    lifespan=lifespan,
)


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    data = None
    try:
        data = await request.json()
    except ValueError:
        logger.warning(
            f"Could not decode request's json. Not a problem, it is not needed anyways",
            exc_info=False,
        )
    message = await prepare_message(data)
    await bot.send_message(CHAT_ID, message, parse_mode=ParseMode.HTML)
    return Response(status_code=status.HTTP_200_OK)
