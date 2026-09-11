"""從 Supervisor 已知附加元件找出本機 OCR，不安裝或修改系統。"""
async def async_resolve_ocr_url(hass, configured=""):
    if configured:
        return configured.rstrip("/")
    if "hassio" not in hass.config.components:
        return ""
    try:
        from homeassistant.components.hassio import get_addons_info, hostname_from_addon_slug
        addons = get_addons_info(hass)
        matches = [slug for slug in addons if slug.endswith("_taiwater_ocr")]
        if len(matches) == 1:
            return f"http://{hostname_from_addon_slug(matches[0])}:8080"
    except Exception:
        pass
    return ""
