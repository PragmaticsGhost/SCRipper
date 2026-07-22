FROM docker:29.6.1-cli-alpine3.24@sha256:862099ada15c669000bef53aa4cb9d821262829f45b0dda2159ccb276443043b

RUN apk add --no-cache python3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /controller
COPY browser_controller.py .

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=2)"]

CMD ["python3", "browser_controller.py"]
