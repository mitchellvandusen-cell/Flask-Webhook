# db/integrations.py — Discord, Slack, and Google Calendar persistence
#
# OAuth token storage and workspace/server management for third-party
# integrations embedded in the dashboard.

from db_legacy import (
    # Discord
    save_discord_connection,
    get_discord_connection,
    delete_discord_connection,
    save_discord_servers,
    get_discord_servers,
    save_discord_webhook_channel,
    get_discord_webhook_channels,
    delete_discord_webhook_channel,
    # Slack
    save_slack_connection,
    get_slack_connection,
    delete_slack_connection,
    save_slack_workspaces,
    get_slack_workspaces,
    # Google Calendar
    save_google_calendar_config,
    get_google_calendar_config,
    delete_google_calendar_config,
)

__all__ = [
    "save_discord_connection", "get_discord_connection", "delete_discord_connection",
    "save_discord_servers", "get_discord_servers",
    "save_discord_webhook_channel", "get_discord_webhook_channels",
    "delete_discord_webhook_channel",
    "save_slack_connection", "get_slack_connection", "delete_slack_connection",
    "save_slack_workspaces", "get_slack_workspaces",
    "save_google_calendar_config", "get_google_calendar_config",
    "delete_google_calendar_config",
]
