# --- Stage 1: Build ---
FROM debian:trixie AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    libflac-dev \
    libasound2-dev \
    libsoxr-dev \
    libssl-dev \
    libvorbis-dev \
    libmad0-dev \
    libfaad-dev \
    libmpg123-dev \
    libopusfile-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy the upstream squeezelite source (submodule: ralph-irving/squeezelite)
COPY squeezelite/ .

# Compile with optimized flags for high-end audio.
#   DSD          native DSD + DoP output
#   RESAMPLE_MP  soxr resampling, OpenMP multi-threaded (implies RESAMPLE)
#   VISEXPORT    shared-memory export for visualisers
#   OPUS         native Opus decoding (opusfile.h lives in /usr/include/opus)
#   USE_SSL      https streams / LMS over TLS
#   NO_SSLSYM    link libssl directly; the runtime dlopen path only probes
#                libssl.so.1.x, which does not exist on Debian trixie
# FLAC, Vorbis, MP3 (mad/mpg123) and AAC (faad) are always compiled in.
# LDADD is intentionally NOT overridden - the Makefile derives it from OPTS.
RUN make clean && \
    make -j"$(nproc)" OPTS="-DDSD -DRESAMPLE_MP -DVISEXPORT -DOPUS -DUSE_SSL -DNO_SSLSYM -I/usr/include/opus"

# Fail the build if any flag silently did not take effect (a typo in OPTS is
# otherwise ignored by make and only shows up as a missing feature at runtime)
RUN BUILD_OPTS="$(./squeezelite -? 2>&1 | grep '^Build options:')" && \
    echo "$BUILD_OPTS" && \
    for f in LINUX ALSA RESAMPLE_MP DSD OPUS VISEXPORT SSL; do \
        echo "$BUILD_OPTS" | grep -qw "$f" || { echo "ERROR: build option $f not enabled" >&2; exit 1; }; \
    done

# --- Stage 2: Runtime ---
FROM debian:trixie-slim

# Install only necessary runtime libraries and PipeWire tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    libflac14 \
    libasound2 \
    libsoxr0 \
    libssl3 \
    libvorbisfile3 \
    libmad0 \
    libfaad2 \
    libmpg123-0 \
    libopusfile0 \
    pipewire-bin \
    wireplumber \
    libasound2-plugins \
    pipewire-alsa \
    dbus \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Configure ALSA to use PipeWire by default
RUN echo 'pcm.pipewire { type pipewire } ctl.pipewire { type pipewire }' > /etc/asound.conf \
    && echo 'pcm.!default pcm.pipewire' >> /etc/asound.conf \
    && echo 'ctl.!default ctl.pipewire' >> /etc/asound.conf

# Copy binary from builder
COPY --from=builder /build/squeezelite /usr/local/bin/squeezelite

# Setup Entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/squeezelite

# Verify the runtime image can actually satisfy squeezelite:
#  - libssl/libcrypto are linked directly (NO_SSLSYM), so a missing one would
#    stop the binary from starting at all, not just disable TLS
#  - the codecs are dlopen()ed by soname, and a missing one fails silently at
#    play time with nothing but a "dlerror" line in the log
RUN ldd /usr/local/bin/squeezelite && \
    ! ldd /usr/local/bin/squeezelite 2>&1 | grep -q "not found" && \
    for lib in libFLAC.so libvorbisfile.so.3 libmad.so.0 libmpg123.so.0 \
               libfaad.so.2 libopusfile.so.0 libsoxr.so.0 libasound.so.2; do \
        ldconfig -p | grep -q "$lib" || { echo "ERROR: $lib missing from runtime image" >&2; exit 1; }; \
    done && \
    /usr/local/bin/squeezelite -? | grep '^Build options:'

# Run unprivileged. UID/GID 1000 is the default because the host socket that
# gets mounted in (/run/user/1000/pipewire-0) is normally owned by the desktop
# user; if yours differs, override with `user: "<uid>:<gid>"` in compose.
# The audio group is for the optional /dev/snd passthrough.
RUN groupadd -g 1000 squeezelite && \
    useradd -u 1000 -g 1000 -G audio -M -s /usr/sbin/nologin squeezelite && \
    install -d -o 1000 -g 1000 /home/squeezelite

# PipeWire/WirePlumber clients write state under $HOME, which must be writable
ENV HOME=/home/squeezelite

# Ensure /tmp is used for PipeWire runtime if not specified
ENV PIPEWIRE_RUNTIME_DIR=/tmp
ENV PIPEWIRE_REMOTE=pipewire-0

# Named, not numeric: Docker only applies supplementary groups (i.e. audio) when
# USER is a name it can look up in /etc/passwd. "USER 1000:1000" silently drops
# them. An override like compose `user: "1003:1003"` is numeric and drops them
# again, which is what the `group_add: audio` in the compose files is for.
USER squeezelite

# Confirm the unprivileged user can actually execute the binary
RUN /usr/local/bin/squeezelite -? > /dev/null

# Set the entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]