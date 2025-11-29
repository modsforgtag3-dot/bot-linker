"""
Print registered global and (optionally) guild commands for the bot defined in `server_ws.py`.
Usage:
  python print_commands.py            # prints global commands
  python print_commands.py <GUILD_ID> # prints global and guild commands for the guild

This script imports `BOT_TOKEN` from `server_ws.py` (no env needed) and uses the Discord REST API.
"""
import sys
import asyncio
import aiohttp
import discord
from server_ws import BOT_TOKEN

API_BASE = "https://discord.com/api/v10"


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("BOT_TOKEN is not set in server_ws.py. Please set it to your bot token.")
        return

    guild_id = None
    if len(sys.argv) > 1:
        guild_id = sys.argv[1]

    # login with a lightweight client to retrieve application id
    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(BOT_TOKEN)
        app_info = await client.application_info()
        app_id = app_info.id
    except Exception as e:
        print(f"Failed to login or fetch application info: {e}")
        return
    finally:
        await client.close()

    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "User-Agent": "DiscordBot (print_commands, 1.0)",
    }

    async with aiohttp.ClientSession() as sess:
        # global commands
        url = f"{API_BASE}/applications/{app_id}/commands"
        async with sess.get(url, headers=headers) as r:
            if r.status != 200:
                print(f"Failed to fetch global commands: {r.status} {await r.text()}")
            else:
                data = await r.json()
                print("Global commands:")
                if not data:
                    print("  (none)")
                for c in data:
                    print(f"  - {c.get('name')} (id={c.get('id')}) - {c.get('description')}")

        # guild commands (optional)
        if guild_id:
            url = f"{API_BASE}/applications/{app_id}/guilds/{guild_id}/commands"
            async with sess.get(url, headers=headers) as r:
                if r.status != 200:
                    print(f"Failed to fetch guild commands: {r.status} {await r.text()}")
                else:
                    data = await r.json()
                    print(f"\nGuild commands for {guild_id}:")
                    if not data:
                        print("  (none)")
                    for c in data:
                        print(f"  - {c.get('name')} (id={c.get('id')}) - {c.get('description')}")

if __name__ == '__main__':
    asyncio.run(main())
