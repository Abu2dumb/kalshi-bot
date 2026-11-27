import os
import logging
import requests

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")


def send_discord_message(content: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        logging.warning("DISCORD_WEBHOOK_URL is not set; Discord message skipped.")
        return False

    if not content:
        return False

    content = str(content)[:2000]

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": content},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logging.error("Failed to send Discord message: %s", exc)
        return False
