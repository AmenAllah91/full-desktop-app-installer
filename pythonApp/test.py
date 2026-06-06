import asyncio
import threading
import websockets
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

async def handler(ws):
    async for _ in ws:
        pass

def ws_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    server = loop.run_until_complete(websockets.serve(handler, "localhost", 8765))
    logger.info("WebSocket started on ws://localhost:8765")
    loop.run_forever()

if __name__ == "__main__":
    threading.Thread(target=ws_thread, daemon=True).start()
    while True:
        time.sleep(1)
