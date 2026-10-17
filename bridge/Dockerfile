FROM python:3.12-alpine

# Dependencias runtime del bridge (pyproject): aiomqtt/aiohttp/zstandard y,
# desde FE, amqtt (broker MQTT embebido) + cryptography (auto-generación de
# certs al primer arranque). Todos publican wheels musllinux: alpine no
# necesita build deps.
WORKDIR /app
COPY pyproject.toml ./
COPY deebot930_bridge ./deebot930_bridge

RUN pip install --no-cache-dir .

# Servidores embebidos (FE): lookup (bootstrap del robot), XMPP (era 930),
# broker MQTT TLS (eco-ng T8+) y panel web. El 443 (conf de la app) llega
# en FD y no se publica.
EXPOSE 8007 5223 8883 8181

ENTRYPOINT ["python", "-m", "deebot930_bridge"]
CMD ["serve", "--mqtt"]
