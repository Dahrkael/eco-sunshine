FROM python:3.12-alpine

WORKDIR /app
COPY pyproject.toml ./
COPY deebot930_bridge ./deebot930_bridge

RUN pip install --no-cache-dir aiomqtt

ENTRYPOINT ["python", "-m", "deebot930_bridge"]
CMD ["serve"]
