"""
Delete all existing application commands (global and optionally guild) and then optionally restart the bot
so the local `server_ws.py` registrations are synced freshly.

Usage:
  python clean_resync.py            # deletes global commands, asks to restart bot
  python clean_resync.py --guild GUILD_ID
  python clean_resync.py --restart  # after deleting, spawn server_ws.py

This script reads `BOT_TOKEN` from `server_ws.py` (no env required).
"""
import asyncio
import aiohttp
import sys
import argparse
import subprocess
from server_ws import BOT_TOKEN

API_BASE = "https://discord.com/api/v10"


async def fetch_app_id(session, token):
    url = f"{API_BASE}/oauth2/applications/@me"
    headers = {"Authorization": f"Bot {token}"}
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            text = await r.text()
            raise RuntimeError(f"Failed to fetch application info: {r.status} {text}")
        data = await r.json()
        return data.get("id")


async def list_global_commands(session, app_id, token):
    url = f"{API_BASE}/applications/{app_id}/commands"
    headers = {"Authorization": f"Bot {token}"}
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            text = await r.text()
            raise RuntimeError(f"Failed to list global commands: {r.status} {text}")
        return await r.json()


async def delete_global_command(session, app_id, cmd_id, token):
    url = f"{API_BASE}/applications/{app_id}/commands/{cmd_id}"
    headers = {"Authorization": f"Bot {token}"}
    async with session.delete(url, headers=headers) as r:
        return r.status


async def list_guild_commands(session, app_id, guild_id, token):
    url = f"{API_BASE}/applications/{app_id}/guilds/{guild_id}/commands"
    headers = {"Authorization": f"Bot {token}"}
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            text = await r.text()
            raise RuntimeError(f"Failed to list guild commands: {r.status} {text}")
        return await r.json()


async def delete_guild_command(session, app_id, guild_id, cmd_id, token):
    url = f"{API_BASE}/applications/{app_id}/guilds/{guild_id}/commands/{cmd_id}"
    headers = {"Authorization": f"Bot {token}"}
    async with session.delete(url, headers=headers) as r:
        return r.status


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--guild', '-g', help='Guild ID to also clear guild commands for')
    parser.add_argument('--restart', '-r', action='store_true', help='After cleaning, start server_ws.py')
    args = parser.parse_args()

    if not BOT_TOKEN or BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE':
        print('BOT_TOKEN is not set in server_ws.py. Please set it to your bot token.')
        return

    async with aiohttp.ClientSession() as session:
        app_id = await fetch_app_id(session, BOT_TOKEN)
        print(f'Application ID: {app_id}')

        # delete global commands
        print('Fetching global commands...')
        globals_cmds = await list_global_commands(session, app_id, BOT_TOKEN)
        if not globals_cmds:
            print('No global commands found.')
        else:
            print(f'Found {len(globals_cmds)} global commands — deleting...')
            for c in globals_cmds:
                cid = c.get('id')
                name = c.get('name')
                status = await delete_global_command(session, app_id, cid, BOT_TOKEN)
                print(f'  Deleted global {name} (id={cid}) -> status {status}')

        # optionally delete guild commands
        if args.guild:
            guild_id = args.guild
            print(f'Fetching guild commands for {guild_id}...')
            guild_cmds = await list_guild_commands(session, app_id, guild_id, BOT_TOKEN)
            if not guild_cmds:
                print('No guild commands found.')
            else:
                print(f'Found {len(guild_cmds)} guild commands — deleting...')
                for c in guild_cmds:
                    cid = c.get('id')
                    name = c.get('name')
                    status = await delete_guild_command(session, app_id, guild_id, cid, BOT_TOKEN)
                    print(f'  Deleted guild {name} (id={cid}) -> status {status}')

    print('\nFinished deleting commands.')
    if args.restart:
        print('Starting server_ws.py...')
        # spawn as detached process
        subprocess.Popen([sys.executable, 'server_ws.py'])
        print('server_ws.py started (detached).')
    else:
        print('Please restart your bot process (server_ws.py) to re-register commands.')

if __name__ == '__main__':
    asyncio.run(main())
