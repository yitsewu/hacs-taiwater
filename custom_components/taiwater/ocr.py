"""Default to bundled OCR; external endpoints are an explicit advanced override."""
async def async_resolve_ocr_url(hass, configured=""):
    if configured:
        return configured.rstrip("/")
    return ""
