# SCRipper suite — native audio dependencies are built in an isolated stage.
ARG DEBIAN_IMAGE=debian:bookworm@sha256:813017f3d62be4b5891a7acca6a01bdcd4b8513daa81b1ab99d3a50385b26931

FROM ${DEBIAN_IMAGE} AS panako-builder

ARG OPENLDAP_COMMIT=dec5992cd5014b737a20f55dc5aad015bc6fccde
ARG JGABORATOR_COMMIT=196ecb11d622987ec1894e400b31080f978b8cae
ARG PANAKO_COMMIT=e4b0e1dbb55e340bc66c90bac0ceb82b2cf84211
ARG GRADLE_7_2_BIN_SHA256=f581709a9c35e9cb92e16f585d2c4bc99b2b1a5f85d2badbd3dc6bff59e1e6dd

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates git default-jdk make gcc g++ libc-dev && \
    rm -rf /var/lib/apt/lists/*

# LMDB key-value store. Dereference make's versioned symlink so the runtime
# stage receives a self-contained shared object rather than a dangling link.
RUN git init /openldap && \
    git -C /openldap remote add origin https://git.openldap.org/openldap/openldap.git && \
    git -C /openldap fetch --depth 1 origin "$OPENLDAP_COMMIT" && \
    git -C /openldap checkout --detach FETCH_HEAD && \
    make -C /openldap/libraries/liblmdb && \
    mkdir -p /artifacts/lib && \
    cp -L /openldap/libraries/liblmdb/liblmdb.so /artifacts/lib/liblmdb.so

# JGaborator native spectral transform.
ENV JAVA_HOME=/usr/lib/jvm/default-java
RUN git init /JGaborator && \
    git -C /JGaborator remote add origin https://github.com/JorenSix/JGaborator && \
    git -C /JGaborator fetch --depth 1 origin "$JGABORATOR_COMMIT" && \
    git -C /JGaborator checkout --detach FETCH_HEAD && \
    make -C /JGaborator/gaborator plain && \
    cp /JGaborator/src/main/resources/jni/libjgaborator.so /artifacts/lib/

# Panako fingerprinting runtime.
ENV JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF8
RUN git init /Panako && \
    git -C /Panako remote add origin https://github.com/JorenSix/Panako.git && \
    git -C /Panako fetch --depth 1 origin "$PANAKO_COMMIT" && \
    git -C /Panako checkout --detach FETCH_HEAD && \
    printf '\ndistributionSha256Sum=%s\n' "$GRADLE_7_2_BIN_SHA256" >> \
        /Panako/gradle/wrapper/gradle-wrapper.properties && \
    cd /Panako && ./gradlew shadowJar && ./gradlew install && \
    cp resources/defaults/panako /artifacts/panako


FROM ${DEBIAN_IMAGE} AS runtime-base

ENV HOME=/home/scripper
ENV JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF8
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        default-jre-headless ffmpeg gosu python3 python3-pip aubio-tools \
        ca-certificates libstdc++6 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=panako-builder /artifacts/lib/liblmdb.so /lib/liblmdb.so
COPY --from=panako-builder /artifacts/lib/libjgaborator.so /lib/libjgaborator.so
COPY --from=panako-builder /root/.panako /home/scripper/.panako
COPY --from=panako-builder /artifacts/panako /usr/local/bin/panako
RUN chmod +x /usr/local/bin/panako && \
    groupadd -g 1000 scripper && \
    useradd -m -u 1000 -g 1000 -d /home/scripper scripper && \
    chown -R scripper:scripper /home/scripper/.panako

COPY requirements.lock /tmp/requirements.lock
RUN pip3 install --no-cache-dir --break-system-packages --require-hashes -r /tmp/requirements.lock && \
    rm /tmp/requirements.lock

WORKDIR /app
COPY app.py .
COPY app_config.py .
COPY application_state.py .
COPY fingerprint_manifest.py .
COPY safe_upload.py .
COPY safe_download.py .
COPY download_service.py .
COPY job_state.py .
COPY job_service.py .
COPY process_utils.py .
COPY waveform.py .
COPY track_identity.py .
COPY request_security.py .
COPY history_updates.py .
COPY request_validation.py .
COPY runtime_hygiene.py .
COPY state_storage.py .
COPY worker_runtime.py .
COPY web_routes.py .
COPY metadata_store.py .
COPY gunicorn.conf.py .
COPY static ./static
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && \
    mkdir -p /music /uploads /cookies /logs && \
    chown -R scripper:scripper /home/scripper /uploads /cookies /logs /app

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health', timeout=2).read()"]

# One process is intentional: queues, locks, retained mixes, and job status are
# in-process state. gthread supplies concurrent HTTP handling safely.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]


FROM runtime-base AS test
USER root
COPY requirements-dev.lock /tmp/requirements-dev.lock
RUN pip3 install --no-cache-dir --break-system-packages --require-hashes -r /tmp/requirements-dev.lock && \
    rm /tmp/requirements-dev.lock
COPY pyproject.toml .
COPY browser_controller.py .
COPY Dockerfile browser-controller.Dockerfile docker-compose.yml requirements.lock setup.ps1 ./
COPY scripts ./scripts
COPY tests ./tests
ENTRYPOINT []
HEALTHCHECK NONE
CMD ["sh", "-c", "ruff check --no-cache . && ruff format --no-cache --check *.py tests/*.py && python3 -m unittest discover -s tests -v"]


# Keep the deployable runtime as the default final stage.
FROM runtime-base AS runtime
