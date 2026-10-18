FROM python:3.12-alpine

# Bridge runtime dependencies (pyproject): aiomqtt/aiohttp/zstandard and,
# since FE, amqtt (embedded MQTT broker) + cryptography (cert auto-generation
# on first boot). All of them ship musllinux wheels: alpine needs no build
# deps.
WORKDIR /app
COPY pyproject.toml ./
COPY ecovacs_bridge ./ecovacs_bridge

RUN pip install --no-cache-dir .

# Embedded servers (FE): lookup (robot bootstrap), XMPP (930-era robots),
# TLS MQTT broker (eco-ng T8+) and web panel. The 443 (app conf API) is
# FD and is not published.
EXPOSE 8007 5223 8883 8181

ENTRYPOINT ["python", "-m", "ecovacs_bridge"]
CMD ["serve", "--mqtt"]
