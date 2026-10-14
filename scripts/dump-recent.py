"""Throwaway: dump recent messages per channel to see what actually arrived on Discord."""
import os, sys, discord
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

token = os.environ["DISCORD_BOT_TOKEN"]
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    try:
        for guild in client.guilds:
            for ch in guild.text_channels:
                try:
                    async for m in ch.history(limit=12, oldest_first=True):
                        atts = ", ".join(a.filename for a in m.attachments)
                        stickers = ", ".join(s.name for s in m.stickers)
                        extra = f" [file: {atts}]" if atts else ""
                        extra += f" [sticker: {stickers}]" if stickers else ""
                        print(f"[{guild.name}/{ch.name}] {m.created_at} {m.author.name}: {m.content[:80]!r}{extra}")
                except Exception as e:
                    print(f"[{guild.name}/{ch.name}] error: {e}")
        for ch in client.private_channels:
            try:
                async for m in ch.history(limit=12, oldest_first=True):
                    atts = ", ".join(a.filename for a in m.attachments)
                    print(f"[DM:{ch.recipient.name}] {m.created_at} {m.author.name}: {m.content[:80]!r} [{atts}]")
            except Exception as e:
                print(f"[DM] error: {e}")
    finally:
        await client.close()

client.run(token)
