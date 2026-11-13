# First Channel Proxy

Dynamic HLS reverse proxy for Channel One.

## Features

- Dynamic HLS playlist retrieval
- 576p stream selection
- Automatic TS segment URL rewriting
- Streaming without transcoding
- No local video caching
- VLC and IPTV compatibility

## Upstream

https://streaming.televizor24.cc/live/4.m3u8

## Local endpoint

/first.m3u8

## Health check

/health

## Requirements

- Python 3
- Flask
- Requests
- Gunicorn

## Deployment

Hosted on Render.
