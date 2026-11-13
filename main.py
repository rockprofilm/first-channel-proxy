
import re
import logging
from urllib.parse import (
    urljoin,
    urlsplit,
    urlunsplit,
    unquote,
    quote,
)

import requests
from flask import Flask, Response, request, stream_with_context

app = Flask(__name__)

# ==========================================
# CONFIGURATION
# ==========================================

UPSTREAM_HOST = "streaming.televizor24.cc"

MASTER_URL = (
    "https://streaming.televizor24.cc/live/4.m3u8"
)

UPSTREAM_HEADERS = {
    "Referer": "https://televizor24.cc/",
    "User-Agent": "VLC/3.0.21 LibVLC/3.0.21",
    "Accept": "*/*",
}

REQUEST_TIMEOUT = (10, 30)

# Разрешённый динамический плейлист 576p
CHILD_PATTERN = re.compile(
    r"^4-req_offset_[0-9]+-req_window_[0-9]+-1k_v5\.m3u8$"
)

# Разрешённые видеосегменты
SEGMENT_PATTERN = re.compile(
    r"^4-1k-[0-9]+\.ts$"
)

logging.basicConfig(level=logging.INFO)


# ==========================================
# HTTP HELPERS
# ==========================================

class ProxyError(Exception):
    pass


def fetch_playlist(url):
    """Получить текстовый HLS-плейлист."""

    parsed = urlsplit(url)

    if parsed.hostname != UPSTREAM_HOST:
        raise ProxyError("Invalid upstream host")

    if parsed.path != "/live/4.m3u8" and not (
        parsed.path.startswith("/live/")
        and CHILD_PATTERN.fullmatch(
            unquote(parsed.path.rsplit("/", 1)[-1])
        )
    ):
        raise ProxyError("Invalid playlist path")

    response = requests.get(
        url,
        headers=UPSTREAM_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    if urlsplit(response.url).hostname != UPSTREAM_HOST:
        raise ProxyError("Unexpected redirect destination")

    return response.text


# ==========================================
# MASTER PLAYLIST
# ==========================================

def find_576p_playlist(master_text):
    """Найти динамический адрес варианта 1024x576."""

    lines = [
        line.strip()
        for line in master_text.splitlines()
        if line.strip()
    ]

    for index, line in enumerate(lines):

        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue

        resolution = re.search(
            r"RESOLUTION=(\d+)x(\d+)",
            line
        )

        if not resolution:
            continue

        width = int(resolution.group(1))
        height = int(resolution.group(2))

        if (width, height) != (1024, 576):
            continue

        # URI находится в следующей строке
        for next_line in lines[index + 1:]:

            if next_line.startswith("#"):
                continue

            parsed = urlsplit(
                urljoin(MASTER_URL, next_line)
            )

            filename = unquote(
                parsed.path.rsplit("/", 1)[-1]
            )

            if (
                parsed.hostname == UPSTREAM_HOST
                and parsed.path.startswith("/live/")
                and CHILD_PATTERN.fullmatch(filename)
            ):
                return urlunsplit((
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    parsed.query,
                    ""
                ))

            raise ProxyError("Invalid 576p playlist URL")

    raise ProxyError("576p stream not found")


# ==========================================
# MEDIA PLAYLIST
# ==========================================

def rewrite_media_playlist(playlist_text, playlist_url):
    """Переписать TS-ссылки на адрес нашего сервера."""

    result = []

    for line in playlist_text.splitlines():

        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue

        absolute_url = urljoin(
            playlist_url,
            stripped
        )

        parsed = urlsplit(absolute_url)

        filename = unquote(
            parsed.path.rsplit("/", 1)[-1]
        )

        if (
            parsed.hostname != UPSTREAM_HOST
            or not parsed.path.startswith("/live/")
            or not SEGMENT_PATTERN.fullmatch(filename)
        ):
            raise ProxyError(
                "Unsupported segment URL"
            )

        local_url = "/segment/" + quote(filename)

        if parsed.query:
            local_url += "?" + parsed.query

        result.append(local_url)

    return "\n".join(result) + "\n"


# ==========================================
# MAIN HLS ENDPOINT
# ==========================================

@app.route("/first.m3u8")
def first_channel():

    try:

        # Получаем родительский плейлист
        master = fetch_playlist(MASTER_URL)

        # Находим актуальный вариант 576p
        child_url = find_576p_playlist(master)

        # Загружаем дочерний плейлист
        media_playlist = fetch_playlist(child_url)

        # Переписываем адреса сегментов
        output = rewrite_media_playlist(
            media_playlist,
            child_url
        )

        response = Response(
            output,
            content_type="application/vnd.apple.mpegurl"
        )

        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Access-Control-Allow-Origin"] = "*"

        return response

    except (requests.RequestException, ProxyError) as error:

        app.logger.warning(
            "Playlist proxy error: %s",
            error
        )

        return Response(
            "Upstream playlist unavailable",
            status=502,
            mimetype="text/plain"
        )


# ==========================================
# TS SEGMENT PROXY
# ==========================================

@app.route("/segment/<filename>")
def segment(filename):

    if not SEGMENT_PATTERN.fullmatch(filename):
        return Response(
            "Invalid segment",
            status=400
        )

    upstream_url = (
        f"https://{UPSTREAM_HOST}/live/{filename}"
    )

    headers = dict(UPSTREAM_HEADERS)

    # Поддержка Range-запросов плеера
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    try:

        upstream = requests.get(
            upstream_url,
            headers=headers,
            params=request.args,
            timeout=(10, 60),
            stream=True
        )

        if upstream.status_code not in (200, 206):

            status = upstream.status_code
            upstream.close()

            return Response(
                "Upstream segment unavailable",
                status=status
            )

        def generate():

            try:

                for chunk in upstream.iter_content(
                    chunk_size=64 * 1024
                ):

                    if chunk:
                        yield chunk

            finally:
                upstream.close()

        response_headers = {}

        for header in (
            "Content-Type",
            "Content-Length",
            "Content-Range",
            "Accept-Ranges"
        ):

            value = upstream.headers.get(header)

            if value:
                response_headers[header] = value

        response_headers["Cache-Control"] = "no-store"
        response_headers["Access-Control-Allow-Origin"] = "*"

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=response_headers
        )

    except requests.RequestException as error:

        app.logger.warning(
            "Segment proxy error: %s",
            error
        )

        return Response(
            "Upstream segment unavailable",
            status=502
        )


# ==========================================
# HEALTH CHECK
# ==========================================

@app.route("/health")
def health():

    return {
        "status": "ok",
        "service": "first-channel-proxy",
        "quality": "576p"
    }, 200


@app.route("/")
def index():

    return {
        "service": "First Channel HLS Proxy",
        "status": "running",
        "stream": "/first.m3u8",
        "health": "/health"
    }, 200


# ==========================================
# LOCAL DEVELOPMENT
# ==========================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
