"""僅供設定流程使用、三分鐘失效的一次性驗證碼圖片。"""
import time
from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN


class CaptchaView(HomeAssistantView):
    url = "/api/taiwater/captcha/{nonce}"
    name = "api:taiwater:captcha"
    requires_auth = False  # 圖片標籤無法附 Bearer；使用 256-bit 隨機能力網址。

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request, nonce):
        images = self.hass.data.get(DOMAIN, {}).get("images", {})
        challenge = images.get(nonce)
        if challenge is None or challenge.used or time.monotonic() - challenge.created_at > 180:
            images.pop(nonce, None)
            raise web.HTTPNotFound()
        return web.Response(body=challenge.image, content_type=challenge.content_type,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                     "X-Content-Type-Options": "nosniff"})


def register_manual_image(hass, challenge):
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("http_registered"):
        hass.http.register_view(CaptchaView(hass))
        data["http_registered"] = True
    images = data.setdefault("images", {})
    for nonce, old in list(images.items()):
        if old.used or time.monotonic() - old.created_at > 180:
            images.pop(nonce, None)
    images[challenge.nonce] = challenge
    return f"/api/taiwater/captcha/{challenge.nonce}"


def clear_manual_image(hass, challenge):
    hass.data.get(DOMAIN, {}).get("images", {}).pop(challenge.nonce, None)
