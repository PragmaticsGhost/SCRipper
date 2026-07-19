# SCRipper suite — self-contained image.
# Builds Panako (audio fingerprinting) from source, then adds the
# Python web app. First build takes ~10 minutes; later builds are cached.
FROM debian:bullseye

# —— Panako + native dependencies (from Panako's official Dockerfile) ——
RUN apt-get update && \
    apt-get install -y --no-install-recommends git default-jdk ffmpeg gosu && \
    apt-get install -y --no-install-recommends make gcc g++ libc-dev

# LMDB key-value store (native lib)
WORKDIR /
RUN git clone --depth 1 https://git.openldap.org/openldap/openldap.git
WORKDIR /openldap/libraries/liblmdb
RUN make && mv liblmdb.so /lib/

# JGaborator (native spectral transform)
WORKDIR /
RUN git clone --depth 1 https://github.com/JorenSix/JGaborator
WORKDIR /JGaborator/gaborator/
ENV JAVA_HOME=/usr/lib/jvm/default-java
RUN make plain && cp ../src/main/resources/jni/libjgaborator.so /lib/

# —— Non-root runtime user; Panako installs into its home ——
RUN groupadd -g 1000 scripper && \
    useradd -m -u 1000 -g 1000 -d /home/scripper scripper
ENV HOME=/home/scripper

# Panako itself. `gradlew install` targets the JVM's user.home, which
# resolves to /root during the (root) build regardless of $HOME — so we
# relocate the install into the runtime user's home afterward.
WORKDIR /
RUN git clone --depth 1 https://github.com/JorenSix/Panako.git
ENV JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF8
WORKDIR /Panako
RUN ./gradlew shadowJar && ./gradlew install && \
    cp resources/defaults/panako /usr/local/bin/
RUN cp -r /root/.panako /home/scripper/.panako && \
    rm -rf /root/.panako && \
    chown -R scripper:scripper /home/scripper/.panako

# Cleanup build sources
RUN rm -rf /Panako /JGaborator /openldap && apt-get clean

# —— Web app ——
# aubio-tools provides the `aubio tempo` BPM detector used by MixID.
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip aubio-tools && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir flask yt-dlp mutagen requests

WORKDIR /app
COPY app.py .
COPY static ./static
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN mkdir -p /music /uploads /cookies /logs && \
    chown -R scripper:scripper /home/scripper /uploads /cookies /logs /app

EXPOSE 8080
# Starts as root only to align volume ownership, then drops to `scripper`.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python3", "app.py"]
