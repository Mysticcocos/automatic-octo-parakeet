"""One-time owner login: run from a terminal to enter Telegram verification code."""
import os, asyncio
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient
ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')
async def main():
    api_id=os.getenv('TELEGRAM_API_ID')
    api_hash=os.getenv('TELEGRAM_API_HASH')
    if not api_id or not api_hash: raise SystemExit('Fill TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first')
    async with TelegramClient(str(ROOT/'owner_session'),int(api_id),api_hash) as client:
        me=await client.get_me()
        print('Telegram session authorized:',me.first_name,me.id)
if __name__=='__main__': asyncio.run(main())
