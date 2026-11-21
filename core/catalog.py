"""Known self-hosted apps: how to recognise them from their image, and sensible health-check defaults.

Used only to *suggest* settings (health-check URL, dashboard link, icon). Nothing here is applied unless the
owner saves it on the Settings page, and any app not listed here can still be configured by hand.
"""
import re

ICON = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/{}.png"

# id, display name, image regex, internal port, health path, icon slug (None = no icon), kind
APPS = [
    ("plex", "Plex", r"plex|pms-docker", 32400, "/identity", "plex", "media"),
    ("jellyfin", "Jellyfin", r"jellyfin", 8096, "/health", "jellyfin", "media"),
    ("emby", "Emby", r"emby", 8096, "/emby/System/Ping", "emby", "media"),
    ("audiobookshelf", "Audiobookshelf", r"audiobookshelf", 80, "/healthcheck", "audiobookshelf", "media"),
    ("navidrome", "Navidrome", r"navidrome", 4533, "/ping", "navidrome", "media"),
    ("kavita", "Kavita", r"kavita", 5000, "/api/health", "kavita", "media"),
    ("komga", "Komga", r"komga", 25600, "/", "komga", "media"),
    ("calibre-web", "Calibre-Web", r"calibre-web", 8083, "/", "calibre-web", "media"),
    ("sonarr", "Sonarr", r"sonarr", 8989, "/ping", "sonarr", "arr"),
    ("radarr", "Radarr", r"radarr", 7878, "/ping", "radarr", "arr"),
    ("lidarr", "Lidarr", r"lidarr", 8686, "/ping", "lidarr", "arr"),
    ("readarr", "Readarr", r"readarr", 8787, "/ping", "readarr", "arr"),
    ("whisparr", "Whisparr", r"whisparr", 6969, "/ping", "whisparr", "arr"),
    ("bazarr", "Bazarr", r"bazarr", 6767, "/", "bazarr", "arr"),
    ("prowlarr", "Prowlarr", r"prowlarr", 9696, "/ping", "prowlarr", "arr"),
    ("jackett", "Jackett", r"jackett", 9117, "/UI/Login", "jackett", "arr"),
    ("flaresolverr", "FlareSolverr", r"flaresolverr", 8191, "/", None, "arr"),
    ("seerr", "Seerr", r"(^|/)seerr", 5055, "/api/v1/status", "overseerr", "requests"),
    ("jellyseerr", "Jellyseerr", r"jellyseerr", 5055, "/api/v1/status", "jellyseerr", "requests"),
    ("overseerr", "Overseerr", r"overseerr", 5055, "/api/v1/status", "overseerr", "requests"),
    ("ombi", "Ombi", r"ombi", 3579, "/", "ombi", "requests"),
    ("tautulli", "Tautulli", r"tautulli", 8181, "/status", "tautulli", "tools"),
    ("qbittorrent", "qBittorrent", r"qbittorrent", 8080, "/", "qbittorrent", "download"),
    ("transmission", "Transmission", r"transmission", 9091, "/transmission/web/", "transmission", "download"),
    ("deluge", "Deluge", r"deluge", 8112, "/", "deluge", "download"),
    ("sabnzbd", "SABnzbd", r"sabnzbd", 8080, "/", "sabnzbd", "download"),
    ("nzbget", "NZBGet", r"nzbget", 6789, "/", "nzbget", "download"),
    ("tdarr", "Tdarr", r"tdarr", 8265, "/", "tdarr", "tools"),
    ("homarr", "Homarr", r"homarr", 7575, "/", "homarr", "tools"),
    ("portainer", "Portainer", r"portainer", 9000, "/api/system/status", "portainer", "tools"),
    ("uptime-kuma", "Uptime Kuma", r"uptime-kuma", 3001, "/", "uptime-kuma", "tools"),
    ("home-assistant", "Home Assistant", r"home-assistant|homeassistant", 8123, "/", "home-assistant", "tools"),
    ("gluetun", "Gluetun (VPN)", r"gluetun", None, None, None, "infra"),
    ("caddy", "Caddy", r"caddy", None, None, "caddy", "infra"),
    ("traefik", "Traefik", r"traefik", None, None, "traefik", "infra"),
    ("nginx-proxy-manager", "Nginx Proxy Manager", r"nginx-proxy-manager", 81, "/", "nginx-proxy-manager", "infra"),
]
_RX = [(re.compile(a[2], re.I), a) for a in APPS]


def match(image, service=""):
    """Best catalog entry for an image (falls back to the compose service name), or None."""
    for text in (image or "", service or ""):
        for rx, a in _RX:
            if rx.search(text):
                app_id, name, _, port, path, icon, kind = a
                return {"id": app_id, "name": name, "port": port, "path": path, "kind": kind,
                        "icon": ICON.format(icon) if icon else ""}
    return None
