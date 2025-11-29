import os
import json
import random
import asyncio
import logging
import sqlite3
import socket
from discord.ext import commands
import discord
from websockets.server import serve

logging.basicConfig(level=logging.INFO)

DB_FILE = "links.db"
WEBSOCKET_PORT = 8765
# REQUIRED: set your bot token here (development/testing only).
# This file is currently configured to use the hard-coded token instead
# of reading from the environment. Replace the placeholder below with
# your bot token string. Do NOT commit real tokens to version control.
# 
# For Replit deployment: comment out the line below and use:
#   BOT_TOKEN = os.getenv("BOT_TOKEN")
#
BOT_TOKEN = "MTQ0MzgwOTU4MjA2ODc5MzUzNw.GSXVIx.rJdtjjRqO9RmyNNBJScKXQMyr1zhzU9tFMR0fg"  # or set directly for local testing outside of hosting servers
# --------------------------------
# Database Setup
# --------------------------------
def _init_db_sync():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            user_id TEXT PRIMARY KEY,
            device_id TEXT,
            code TEXT,
            device_name TEXT
        )
        """
    )
    conn.commit()
    # ensure device_name column exists (for older DBs)
    cur.execute("PRAGMA table_info(links)")
    cols = [r[1] for r in cur.fetchall()]
    if 'device_name' not in cols:
        try:
            cur.execute("ALTER TABLE links ADD COLUMN device_name TEXT")
            conn.commit()
        except Exception:
            # ignore if alter fails for some reason
            pass
    conn.close()
    logging.info("Database initialized (links.db)")


async def init_db():
    await asyncio.to_thread(_init_db_sync)

# --------------------------------
# WebSocket Handler
# --------------------------------
# track connected device websockets: device_id -> websocket
connected_devices: dict = {}
connected_devices_lock = asyncio.Lock()
# pending request futures: (device_id, request_id) -> asyncio.Future
pending_requests: dict = {}


def _get_user_by_code_sync(code):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM links WHERE code=?", (code,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _get_user_by_device_sync(device_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM links WHERE device_id=?", (device_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _set_device_for_user_sync(user_id, device_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE links SET device_id=? WHERE user_id=?", (device_id, user_id))
    conn.commit()
    conn.close()


def _set_device_for_user_sync_with_name(user_id, device_id, device_name=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # set device_id and device_name
    cur.execute("UPDATE links SET device_id=?, device_name=? WHERE user_id=?", (device_id, device_name, user_id))
    conn.commit()
    conn.close()


def _clear_device_for_user_sync(user_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE links SET device_id=NULL, device_name=NULL WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


async def ws_handler(websocket, path):
    logging.info("WebSocket connection open")
    device_id = None

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except Exception:
                continue

            mtype = data.get("type")

            # hello handshake: register connection
            if mtype == "hello":
                device_id = data.get("device_id")
                device_name = data.get("device_name")
                if device_id:
                    async with connected_devices_lock:
                        connected_devices[device_id] = websocket
                    await websocket.send(json.dumps({"type": "hello", "ok": True}))
                    logging.info(f"Device connected: {device_id} (name={device_name})")
                    # if this device is already linked to a user, update stored device_name
                    if device_name:
                        try:
                            user_id = await asyncio.to_thread(_get_user_by_device_sync, device_id)
                            if user_id:
                                await asyncio.to_thread(_set_device_for_user_sync_with_name, user_id, device_id, device_name)
                        except Exception:
                            logging.exception("Failed to update device_name on hello")
                continue

            # pairing request from client
            if mtype == "pair":
                code = data.get("code")
                device_id = data.get("device_id")
                device_name = data.get("device_name")

                if not code or not device_id:
                    await websocket.send(json.dumps({"type": "pair_result", "ok": False, "reason": "missing_fields"}))
                    continue

                user_id = await asyncio.to_thread(_get_user_by_code_sync, code)
                if not user_id:
                    await websocket.send(json.dumps({"type": "pair_result", "ok": False, "reason": "invalid_code"}))
                    continue

                # set device for user (store device_name if provided)
                await asyncio.to_thread(_set_device_for_user_sync_with_name, user_id, device_id, device_name)

                # register websocket for this device
                async with connected_devices_lock:
                    connected_devices[device_id] = websocket

                await websocket.send(json.dumps({"type": "pair_result", "ok": True, "discord_id": user_id}))
                logging.info(f"Paired device {device_id} (name={device_name}) -> user {user_id}")
                continue

            # unlink request from client
            if mtype == "unlink":
                device_id = data.get("device_id")
                if not device_id:
                    await websocket.send(json.dumps({"type": "pair_result", "ok": False, "reason": "missing_device_id"}))
                    continue

                user_id = await asyncio.to_thread(_get_user_by_device_sync, device_id)
                if not user_id:
                    await websocket.send(json.dumps({"type": "pair_result", "ok": False, "reason": "not_linked"}))
                    continue

                await asyncio.to_thread(_clear_device_for_user_sync, user_id)

                await websocket.send(json.dumps({"type": "pair_result", "ok": True}))
                logging.info(f"Device unlinked by device request: {device_id} (user {user_id})")
                continue

            # device -> server: library response
            if mtype in ("library_response", "library"):
                req_id = data.get("request_id")
                apps = data.get("apps") or data.get("library") or data.get("items")
                if device_id and req_id:
                    key = (device_id, str(req_id))
                    future = None
                    async with connected_devices_lock:
                        future = pending_requests.pop(key, None)
                    if future and not future.done():
                        try:
                            future.set_result(apps)
                        except Exception:
                            logging.exception("Failed to set pending request result")
                continue

    except Exception as e:
        logging.error(f"WS error: {e}")
    finally:
        # cleanup device mapping on disconnect
        if device_id:
            async with connected_devices_lock:
                if connected_devices.get(device_id) is websocket:
                    del connected_devices[device_id]
                # cancel any pending requests for this device
                keys = [k for k in list(pending_requests.keys()) if k[0] == device_id]
                for k in keys:
                    fut = pending_requests.pop(k, None)
                    if fut and not fut.done():
                        try:
                            fut.set_exception(ConnectionError("Device disconnected"))
                        except Exception:
                            pass
            logging.info(f"Device disconnected: {device_id}")

# --------------------------------
# Discord Bot
# --------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


@bot.event
async def on_ready():
    async def _cleanup_duplicate_global_commands():
        try:
            logging.info("Checking for duplicate global commands...")
            cmds = await tree.fetch_commands()  # global commands
            by_name = {}
            for c in cmds:
                by_name.setdefault(c.name, []).append(c)

            to_delete = []
            for name, lst in by_name.items():
                if len(lst) > 1:
                    # keep one, delete the rest
                    lst_sorted = sorted(lst, key=lambda x: getattr(x, 'id', 0))
                    keep = lst_sorted[0]
                    extras = lst_sorted[1:]
                    for ex in extras:
                        to_delete.append(ex)

            if to_delete:
                logging.info(f"Found {len(to_delete)} duplicate command entries — removing extras")
                for cmd_obj in to_delete:
                    try:
                        # delete global command by id
                        await bot.http.delete_global_command(bot.application_id, cmd_obj.id)
                        logging.info(f"Deleted duplicate global command id={cmd_obj.id} name={cmd_obj.name}")
                    except Exception:
                        logging.exception(f"Failed to delete command id={getattr(cmd_obj, 'id', None)}")
                # small pause before syncing
                await asyncio.sleep(1)
            else:
                logging.info("No duplicate global commands found")
        except Exception:
            logging.exception("Failed while cleaning up duplicate commands")

    try:
        # remove duplicate global commands (if any) then sync
        await _cleanup_duplicate_global_commands()
        await tree.sync()
        logging.info(f"Bot ready: {bot.user} — commands synced")
    except Exception:
        logging.exception("Failed to sync commands on ready")

@tree.command(name="link", description="Generate a link code to pair your device.")
async def link_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    code = str(random.randint(100000, 999999))
    user_id = str(interaction.user.id)

    def _replace_link():
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("REPLACE INTO links (user_id, code, device_id) VALUES (?, ?, ?)", (user_id, code, None))
        conn.commit()
        conn.close()

    await asyncio.to_thread(_replace_link)

    await interaction.followup.send(
        f"🔗 **Your link code:** `{code}`\nGo to the website and enter this code."
    )

@tree.command(name="unlink", description="Disconnect your paired device.")
async def unlink_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    def _unlink_and_get_device():
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT device_id FROM links WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        device = row[0] if row else None
        # clear device mapping
        cur.execute("UPDATE links SET device_id=NULL WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        return device

    device = await asyncio.to_thread(_unlink_and_get_device)

    if device:
        await interaction.followup.send("Your device has been unlinked.")
        # notify device if connected
        try:
            async with connected_devices_lock:
                ws = connected_devices.get(device)
            if ws:
                try:
                    await ws.send(json.dumps({"type": "force_unlink"}))
                except Exception:
                    logging.exception(f"Failed to send force_unlink to {device}")
        except Exception:
            logging.exception("Error notifying device about unlink")
    else:
        await interaction.followup.send("You have no linked device.")

@tree.command(name="linkstatus", description="Check if your device is linked.")
async def linkstatus_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)

    def _get_device():
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT device_id, device_name FROM links WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row

    row = await asyncio.to_thread(_get_device)

    if row and row[0]:
        device_id = row[0]
        device_name = row[1] if len(row) > 1 else None
        if device_name:
            await interaction.followup.send(f"Your device: `{device_name}` (ID: `{device_id}`)")
        else:
            await interaction.followup.send(f"Your device ID: `{device_id}`")
    else:
        await interaction.followup.send("❌ No device linked.")

@tree.command(name="send", description="Send a message to your linked device.")
async def send_cmd(interaction: discord.Interaction, message: str):
    await interaction.response.defer()
    user_id = str(interaction.user.id)

    def _get_device_sync():
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT device_id FROM links WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None

    device_id = await asyncio.to_thread(_get_device_sync)

    if not device_id:
        await interaction.followup.send("❌ You have no linked device.")
        return

    # try to send message to the connected device
    try:
        async with connected_devices_lock:
            ws = connected_devices.get(device_id)
        
        if ws:
            try:
                await ws.send(json.dumps({"type": "discord_message", "text": message}))
                await interaction.followup.send(f"✅ Message sent to device `{device_id}`: {message}")
                logging.info(f"Message sent to device {device_id}: {message}")
            except Exception as e:
                await interaction.followup.send(f"⚠️ Failed to send message: {e}")
                logging.error(f"Failed to send message to {device_id}: {e}")
        else:
            await interaction.followup.send(f"⚠️ Device `{device_id}` is not currently connected.")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")
        logging.error(f"Error sending message: {e}")


@tree.command(name="vrlibrary", description="Show the app library on your paired VR device.")
async def vrlibrary_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)

    def _get_device():
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT device_id FROM links WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None

    device_id = await asyncio.to_thread(_get_device)

    if not device_id:
        await interaction.followup.send("❌ You have no linked device.")
        return

    async with connected_devices_lock:
        ws = connected_devices.get(device_id)

    if not ws:
        await interaction.followup.send(f"⚠️ Device `{device_id}` is not currently connected.")
        return

    request_id = str(random.randint(1000000000, 9999999999))
    fut = asyncio.get_event_loop().create_future()
    key = (device_id, request_id)

    async with connected_devices_lock:
        pending_requests[key] = fut

    try:
        await ws.send(json.dumps({"type": "get_library", "request_id": request_id}))
    except Exception as e:
        async with connected_devices_lock:
            pending_requests.pop(key, None)
        await interaction.followup.send(f"⚠️ Failed to send request to device: {e}")
        return

    try:
        apps = await asyncio.wait_for(fut, timeout=8.0)
    except asyncio.TimeoutError:
        async with connected_devices_lock:
            pending_requests.pop(key, None)
        await interaction.followup.send("⏱️ Timed out waiting for device to respond.")
        return
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")
        return

    if not apps:
        await interaction.followup.send("📭 Device returned an empty library.")
        return

    if isinstance(apps, list):
        lines = []
        for a in apps:
            if isinstance(a, dict):
                name = a.get("name") or a.get("title") or str(a)
            else:
                name = str(a)
            lines.append(f"- {name}")
        content = "\n".join(lines[:50])
        if len(lines) > 50:
            content += f"\n...and {len(lines)-50} more"
        await interaction.followup.send(f"📚 Device library for `{device_id}`:\n{content}")
    else:
        await interaction.followup.send(f"📚 Device library: {apps}")


# --------------------------------
# MAIN
# --------------------------------
async def run_websocket_server():
    logging.info(f"Starting WebSocket server on ws://0.0.0.0:{WEBSOCKET_PORT}")
    try:
        async with serve(ws_handler, "0.0.0.0", WEBSOCKET_PORT):
            logging.info("WebSocket server started successfully")
            # Keep the server running indefinitely
            await asyncio.sleep(float('inf'))
    except OSError as e:
        logging.error(f"Failed to bind WebSocket server: {e}")
        logging.info("Waiting 5 seconds for port to free up...")
        await asyncio.sleep(5)
        # Retry
        async with serve(ws_handler, "0.0.0.0", WEBSOCKET_PORT):
            logging.info("WebSocket server started successfully (retry)")
            await asyncio.sleep(float('inf'))


async def run_discord_bot():
    bot_token = BOT_TOKEN
    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN missing: please set the BOT_TOKEN value at the top of server_ws.py")
    logging.info("Starting Discord bot")
    try:
        await bot.start(bot_token)
    except Exception as e:
        logging.error(f"Discord bot error: {e}")
        raise


async def main():
    await init_db()
    # Run WebSocket server and Discord bot concurrently
    try:
        await asyncio.gather(
            run_websocket_server(),
            run_discord_bot(),
            return_exceptions=False
        )
    except KeyboardInterrupt:
        logging.info("Shutting down...")


if __name__ == "__main__":
    asyncio.run(main())
