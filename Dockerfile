FROM rust:1-bookworm AS ab-av1-builder

ARG AB_AV1_VERSION=0.11.2

RUN cargo install ab-av1 --version "${AB_AV1_VERSION}" --locked --root /out \
    && /out/bin/ab-av1 --version

FROM debian:bookworm-slim AS ab-ffmpeg-builder

ARG FFMPEG_VERSION=7.1.1
ARG VMAF_VERSION=v3.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      git \
      libdav1d-dev \
      libopus-dev \
      libsvtav1-dev \
      meson \
      nasm \
      ninja-build \
      pkg-config \
      yasm \
      zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /build /opt/ab-av1

WORKDIR /build
RUN git clone --depth 1 --branch "${VMAF_VERSION}" https://github.com/Netflix/vmaf.git \
    && meson setup vmaf/libvmaf/build vmaf/libvmaf \
      --prefix=/opt/ab-av1 \
      --libdir=lib \
      --buildtype=release \
      --default-library=shared \
      -Denable_tests=false \
      -Denable_docs=false \
    && meson compile -C vmaf/libvmaf/build \
    && meson install -C vmaf/libvmaf/build \
    && mkdir -p /opt/ab-av1/share/vmaf/model \
    && cp vmaf/model/*.json /opt/ab-av1/share/vmaf/model/

RUN curl -fsSL -o ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
    && tar xf ffmpeg.tar.xz \
    && cd "ffmpeg-${FFMPEG_VERSION}" \
    && export PKG_CONFIG_PATH="/opt/ab-av1/lib/pkgconfig:/usr/lib/$(uname -m)-linux-gnu/pkgconfig:/usr/share/pkgconfig" \
    && pkg-config --modversion SvtAv1Enc \
    && pkg-config --modversion libvmaf \
    && ./configure \
      --prefix=/opt/ab-av1 \
      --extra-cflags=-I/opt/ab-av1/include \
      --extra-ldflags=-L/opt/ab-av1/lib \
      --enable-gpl \
      --enable-libdav1d \
      --enable-libopus \
      --enable-libsvtav1 \
      --enable-libvmaf \
      --disable-debug \
      --disable-doc \
    && make -j"$(nproc)" \
    && make install \
    && LD_LIBRARY_PATH=/opt/ab-av1/lib /opt/ab-av1/bin/ffmpeg -hide_banner -filters | grep libvmaf \
    && LD_LIBRARY_PATH=/opt/ab-av1/lib /opt/ab-av1/bin/ffmpeg -hide_banner -encoders | grep libsvtav1

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    LD_LIBRARY_PATH=/opt/ab-av1/lib

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ffmpeg \
      handbrake-cli \
      libimage-exiftool-perl \
      libsvtav1enc1 \
      libtcmalloc-minimal4 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ab-av1-builder /out/bin/ab-av1 /usr/local/bin/ab-av1
COPY --from=ab-ffmpeg-builder /opt/ab-av1 /opt/ab-av1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

EXPOSE 8097

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8097"]
