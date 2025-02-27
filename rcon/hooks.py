import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from functools import partial, wraps
from typing import DefaultDict, Dict, List, Optional, Sequence, Union

import discord
from discord_webhook import DiscordEmbed

from rcon.commands import CommandFailedError
from rcon.config import get_config
from rcon.discord import (
    dict_to_discord,
    get_prepared_discord_hooks,
    send_to_discord_audit,
)
from rcon.discord_chat import make_hook
from rcon.game_logs import on_camera, on_chat, on_connected, on_disconnected, on_generic
from rcon.map_recorder import VoteMap
from rcon.models import LogLineWebHookField, enter_session
from rcon.player_history import (
    _get_set_player,
    get_player,
    safe_save_player_action,
    save_end_player_session,
    save_player,
    save_start_player_session,
)
from rcon.recorded_commands import RecordedRcon
from rcon.steam_utils import get_player_bans, get_steam_profile, update_db_player_info
from rcon.user_config import CameraConfig, RealVipConfig, VoteMapConfig
from rcon.workers import temporary_broadcast, temporary_welcome

logger = logging.getLogger(__name__)


@on_chat
def count_vote(rcon: RecordedRcon, struct_log):
    config = VoteMapConfig()
    if not config.get_vote_enabled():
        return

    v = VoteMap()
    if vote := v.is_vote(struct_log.get("sub_content")):
        logger.debug("Vote chat detected: %s", struct_log["message"])
        map_name = v.register_vote(
            struct_log["player"], struct_log["timestamp_ms"] / 1000, vote
        )
        try:
            temporary_broadcast(
                rcon,
                config.get_votemap_thank_you_text().format(
                    player_name=struct_log["player"], map_name=map_name
                ),
                5,
            )
        except Exception:
            logger.warning("Unable to output thank you message")
        v.apply_with_retry(nb_retry=2)


MAX_DAYS_SINCE_BAN = os.getenv("BAN_ON_VAC_HISTORY_DAYS", 0)
AUTO_BAN_REASON = os.getenv(
    "BAN_ON_VAC_HISTORY_REASON", "VAC ban history ({DAYS_SINCE_LAST_BAN} days ago)"
)
MAX_GAME_BAN_THRESHOLD = os.getenv("MAX_GAME_BAN_THRESHOLD", 0)


def ban_if_blacklisted(rcon: RecordedRcon, steam_id_64, name):
    with enter_session() as sess:
        player = get_player(sess, steam_id_64)

        if not player:
            logger.error("Can't check blacklist, player not found %s", steam_id_64)
            return

        if player.blacklist and player.blacklist.is_blacklisted:
            try:
                logger.info(
                    "Player %s was banned due blacklist, reason: %s",
                    str(name),
                    player.blacklist.reason,
                )
                rcon.do_perma_ban(
                    player=name,
                    reason=player.blacklist.reason,
                    by=f"BLACKLIST: {player.blacklist.by}",
                )
                safe_save_player_action(
                    rcon=rcon,
                    player_name=name,
                    action_type="PERMABAN",
                    reason=player.blacklist.reason,
                    by=f"BLACKLIST: {player.blacklist.by}",
                    steam_id_64=steam_id_64,
                )
                try:
                    send_to_discord_audit(
                        f"`BLACKLIST` -> {dict_to_discord(dict(player=name, reason=player.blacklist.reason))}",
                        "BLACKLIST",
                    )
                except:
                    logger.error("Unable to send blacklist to audit log")
            except:
                send_to_discord_audit(
                    "Failed to apply ban on blacklisted players, please check the logs and report the error",
                    "ERROR",
                )


def should_ban(bans, max_game_bans, max_days_since_ban):
    try:
        days_since_last_ban = int(bans["DaysSinceLastBan"])
        number_of_game_bans = int(bans.get("NumberOfGameBans", 0))
    except ValueError:  # In case DaysSinceLastBan can be null
        return

    has_a_ban = bans.get("VACBanned") == True or number_of_game_bans >= max_game_bans

    if days_since_last_ban <= 0:
        return False

    if days_since_last_ban <= max_days_since_ban and has_a_ban:
        return True

    return False


def ban_if_has_vac_bans(rcon: RecordedRcon, steam_id_64, name):
    try:
        max_days_since_ban = int(MAX_DAYS_SINCE_BAN)
        max_game_bans = (
            float("inf")
            if int(MAX_GAME_BAN_THRESHOLD) <= 0
            else int(MAX_GAME_BAN_THRESHOLD)
        )
    except ValueError:  # No proper value is given
        logger.error(
            "Invalid value given for environment variable BAN_ON_VAC_HISTORY_DAYS or MAX_GAME_BAN_THRESHOLD"
        )
        return

    if max_days_since_ban <= 0:
        return  # Feature is disabled

    with enter_session() as sess:
        player = get_player(sess, steam_id_64)

        if not player:
            logger.error("Can't check VAC history, player not found %s", steam_id_64)
            return

        bans = get_player_bans(steam_id_64)
        if not bans or not isinstance(bans, dict):
            logger.warning(
                "Can't fetch Bans for player %s, received %s", steam_id_64, bans
            )
            # Player couldn't be fetched properly (logged by get_player_bans)
            return

        if should_ban(bans, max_game_bans, max_days_since_ban):
            reason = AUTO_BAN_REASON.format(
                DAYS_SINCE_LAST_BAN=bans.get("DaysSinceLastBan"),
                MAX_DAYS_SINCE_BAN=str(max_days_since_ban),
            )
            logger.info(
                "Player %s was banned due VAC history, last ban: %s days ago",
                str(player),
                bans.get("DaysSinceLastBan"),
            )
            rcon.do_perma_ban(player=name, reason=reason, by="VAC BOT")

            try:
                audit_params = dict(
                    player=name,
                    steam_id_64=player.steam_id_64,
                    reason=reason,
                    days_since_last_ban=bans.get("DaysSinceLastBan"),
                    vac_banned=bans.get("VACBanned"),
                    number_of_game_bans=bans.get("NumberOfGameBans"),
                )
                send_to_discord_audit(
                    f"`VAC/GAME BAN` -> {dict_to_discord(audit_params)}", "AUTOBAN"
                )
            except:
                logger.exception("Unable to send vac ban to audit log")


def inject_steam_id_64(func):
    @wraps(func)
    def wrapper(rcon, struct_log):
        try:
            name = struct_log["player"]
            info = rcon.get_player_info(name, can_fail=True)
            steam_id_64 = info.get("steam_id_64")
        except KeyError:
            logger.exception("Unable to inject steamid %s", struct_log)
            raise
        if not steam_id_64:
            logger.warning("Can't get player steam_id for %s", name)
            return

        return func(rcon, struct_log, steam_id_64)

    return wrapper


@on_connected
def handle_on_connect(rcon, struct_log):
    steam_id_64 = rcon.get_player_info.get_cached_value_for(struct_log["player"])

    try:
        if type(rcon) == RecordedRcon:
            rcon.invalidate_player_list_cache()
        else:
            rcon.get_player.cache_clear()
        rcon.get_player_info.clear_for(struct_log["player"])
        rcon.get_player_info.clear_for(player=struct_log["player"])
    except Exception:
        logger.exception("Unable to clear cache for %s", steam_id_64)
    try:
        info = rcon.get_player_info(struct_log["player"], can_fail=True)
        steam_id_64 = info.get("steam_id_64")
    except (CommandFailedError, KeyError):
        if not steam_id_64:
            logger.exception("Unable to get player steam ID for %s", struct_log)
            raise
        else:
            logger.error(
                "Unable to get player steam ID for %s, falling back to cached value %s",
                struct_log,
                steam_id_64,
            )

    timestamp = int(struct_log["timestamp_ms"]) / 1000
    if not steam_id_64:
        logger.error(
            "Unable to get player steam ID for %s, can't process connection",
            struct_log,
        )
        return
    save_player(
        struct_log["player"],
        steam_id_64,
        timestamp=int(struct_log["timestamp_ms"]) / 1000,
    )
    save_start_player_session(steam_id_64, timestamp=timestamp)
    ban_if_blacklisted(rcon, steam_id_64, struct_log["player"])
    ban_if_has_vac_bans(rcon, steam_id_64, struct_log["player"])


@on_disconnected
@inject_steam_id_64
def handle_on_disconnect(rcon, struct_log, steam_id_64):
    save_end_player_session(steam_id_64, struct_log["timestamp_ms"] / 1000)


@on_connected
@inject_steam_id_64
def update_player_steaminfo_on_connect(rcon, struct_log, steam_id_64):
    if not steam_id_64:
        logger.error(
            "Can't update steam info, no steam id available for %s",
            struct_log.get("player"),
        )
        return
    profile = get_steam_profile(steam_id_64)
    if not profile:
        logger.error(
            "Can't update steam info, no steam profile returned for %s",
            struct_log.get("player"),
        )
        return

    logger.info("Updating steam profile for player %s", struct_log["player"])
    with enter_session() as sess:
        player = _get_set_player(
            sess, player_name=struct_log["player"], steam_id_64=steam_id_64
        )
        update_db_player_info(player, profile)
        sess.commit()


def _set_real_vips(rcon: RecordedRcon, struct_log):
    config = RealVipConfig()
    if not config.get_enabled():
        logger.debug("Real VIP is disabled")
        return

    desired_nb_vips = config.get_desired_total_number_vips()
    min_vip_slot = config.get_minimum_number_vip_slot()
    vip_count = rcon.get_vips_count()

    remaining_vip_slots = max(desired_nb_vips - vip_count, max(min_vip_slot, 0))
    rcon.set_vip_slots_num(remaining_vip_slots)
    logger.info("Real VIP set slots to %s", remaining_vip_slots)


@on_connected
def do_real_vips(rcon: RecordedRcon, struct_log):
    _set_real_vips(rcon, struct_log)


@on_disconnected
def undo_real_vips(rcon: RecordedRcon, struct_log):
    _set_real_vips(rcon, struct_log)


@on_camera
def notify_camera(rcon: RecordedRcon, struct_log):
    send_to_discord_audit(message=struct_log["message"], by=struct_log["player"])

    try:
        if hooks := get_prepared_discord_hooks("camera"):
            embeded = DiscordEmbed(
                title=f'{struct_log["player"]}  - {struct_log["steam_id_64_1"]}',
                description=struct_log["sub_content"],
                color=242424,
            )
            for h in hooks:
                h.add_embed(embeded)
                h.execute()
    except Exception:
        logger.exception("Unable to forward to hooks")

    config = CameraConfig()
    if config.is_broadcast():
        temporary_broadcast(rcon, struct_log["message"], 60)

    if config.is_welcome():
        temporary_welcome(rcon, struct_log["message"], 60)


def make_allowed_mentions(mentions: Sequence[str]) -> discord.AllowedMentions:
    """Convert the provided sequence of users and roles to a discord.AllowedMentions

    Similar to discord_chat.make_allowed_mentions but doesn't strip @everyone/@here
    """
    allowed_mentions: DefaultDict[str, List[discord.Object]] = defaultdict(list)

    for role_or_user in mentions:
        if match := re.match(r"<@(\d+)>", role_or_user):
            allowed_mentions["users"].append(discord.Object(int(match.group(1))))
        if match := re.match(r"<@&(\d+)>", role_or_user):
            allowed_mentions["roles"].append(discord.Object(int(match.group(1))))

    return discord.AllowedMentions(
        users=allowed_mentions["users"], roles=allowed_mentions["roles"]
    )


def send_log_line_webhook_message(
    webhook_url: str,
    mentions: Optional[Sequence[str]],
    _,
    log_line: Dict[str, Union[str, int, float, None]],
) -> None:
    """Send a time stammped embed of the log_line and mentions to the provided Discord Webhook"""

    mentions = mentions or []

    webhook = make_hook(webhook_url)
    allowed_mentions = make_allowed_mentions(mentions)

    SERVER_SHORT_NAME = os.getenv("SERVER_SHORT_NAME", "No Server Name Set")

    content = " ".join(mentions)
    description = log_line["line_without_time"]
    embed = discord.Embed(
        description=description,
        timestamp=datetime.utcfromtimestamp(log_line["timestamp_ms"] / 1000),
    )
    embed.set_footer(text=SERVER_SHORT_NAME)
    webhook.send(content=content, embed=embed, allowed_mentions=allowed_mentions)


def load_generic_hooks():
    """Load and validate all the subscribed log line webhooks from config.yml"""
    server_id = os.getenv("SERVER_NUMBER")
    if not server_id:
        # Shouldn't get here because SERVER_NUMBER is a mandatory ENV Var
        raise ValueError("SERVER_NUMBER is not set, can't record logs")

    try:
        raw_config = get_config()["LOG_LINE_WEBHOOKS"]
    except KeyError:
        logger.error("No config.yml or no LOG_LINE_WEBHOOKS configuration")
        return

    for key, value in raw_config.items():
        if value:
            for field in value:
                validated_field = LogLineWebHookField(
                    url=field["URL"],
                    mentions=field["MENTIONS"],
                    servers=field["SERVERS"],
                )

                func = partial(
                    send_log_line_webhook_message,
                    validated_field.url,
                    validated_field.mentions,
                )

                # Have to set these attributes as the're used in LogLoop.process_hooks()
                func.__name__ = send_log_line_webhook_message.__name__
                func.__module__ = __name__

                on_generic(key, func)


load_generic_hooks()

if __name__ == "__main__":
    from rcon.settings import SERVER_INFO

    log = {
        "version": 1,
        "timestamp_ms": 1627734269000,
        "relative_time_ms": 221.212,
        "raw": "[543 ms (1627734269)] CONNECTED Dr.WeeD",
        "line_without_time": "CONNECTED Dr.WeeD",
        "action": "CONNECTED",
        "player": "Dr.WeeD",
        "steam_id_64_1": None,
        "player2": None,
        "steam_id_64_2": None,
        "weapon": None,
        "message": "Dr.WeeD",
        "sub_content": None,
    }
    real_vips(RecordedRcon(SERVER_INFO), struct_log=log)
