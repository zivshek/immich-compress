FROM debian:bookworm-slim AS av1-ffmpeg-builder

ARG FFMPEG_VERSION=7.1.1
ARG SVT_AV1_VERSION=v2.3.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      cmake \
      curl \
      git \
      libdav1d-dev \
      libopus-dev \
      nasm \
      ninja-build \
      pkg-config \
      yasm \
      zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /build /opt/av1

WORKDIR /build
RUN git clone --depth 1 --branch "${SVT_AV1_VERSION}" https://gitlab.com/AOMediaCodec/SVT-AV1.git svt-av1 \
    && cmake -S svt-av1 -B svt-av1/build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/opt/av1 \
      -DBUILD_APPS=OFF \
      -DBUILD_SHARED_LIBS=ON \
      -DBUILD_TESTING=OFF \
    && cmake --build svt-av1/build --parallel \
    && cmake --install svt-av1/build

RUN curl -fsSL -o ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
    && tar xf ffmpeg.tar.xz \
    && cd "ffmpeg-${FFMPEG_VERSION}" \
    && export PKG_CONFIG_PATH="/opt/av1/lib/pkgconfig:/usr/lib/$(uname -m)-linux-gnu/pkgconfig:/usr/share/pkgconfig" \
    && pkg-config --modversion SvtAv1Enc \
    && ./configure \
      --prefix=/opt/av1 \
      --extra-cflags=-I/opt/av1/include \
      --extra-ldflags=-L/opt/av1/lib \
      --enable-gpl \
      --enable-libdav1d \
      --enable-libopus \
      --enable-libsvtav1 \
      --disable-debug \
      --disable-doc \
    && make -j"$(nproc)" \
    && make install \
    && LD_LIBRARY_PATH=/opt/av1/lib /opt/av1/bin/ffmpeg -hide_banner -encoders | grep libsvtav1

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    LD_LIBRARY_PATH=/opt/av1/lib

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ffmpeg \
      libimage-exiftool-perl \
      libsvtav1enc1 \
      libtcmalloc-minimal4 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=av1-ffmpeg-builder /opt/av1 /opt/av1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

EXPOSE 8097

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8097"]
