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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy your local squeezelite source (submodule)
# COPY squeezelite/ .
COPY squeezelite-orig/ .

# Compile with optimized flags for high-end audio
RUN make clean && \
    make OPTS="-DLINUX -DALSA -DFLAC -DRESAMPLE -DSSL -DVISEXPORT -DDSD -DRESAMPLE_MP" \
    LDADD="-lFLAC -lsoxr -lssl -lcrypto -lasound -lpthread -lm -lrt"

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

# Ensure /tmp is used for PipeWire runtime if not specified
ENV PIPEWIRE_RUNTIME_DIR=/tmp
ENV PIPEWIRE_REMOTE=pipewire-0

# Set the entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]