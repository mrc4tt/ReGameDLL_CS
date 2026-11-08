# Linux build image: mirrors the linux job of .github/workflows/build.yml
# (debian:11-slim, 32-bit multilib GCC). The source is bind-mounted at runtime.
FROM debian:11-slim

# bullseye is EOL: same archive/snapshot apt sources as CI.
RUN rm -f /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list \
 && printf '%s\n' \
      'deb http://archive.debian.org/debian bullseye main' \
      'deb http://archive.debian.org/debian bullseye-updates main' \
      'deb http://snapshot.debian.org/archive/debian-security/20260830T000000Z bullseye-security main' \
      > /etc/apt/sources.list \
 && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid-until \
 && dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      gcc-multilib g++-multilib \
      build-essential \
      libc6-dev libc6-dev-i386 \
      git cmake rsync \
      g++ gcc \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
ENTRYPOINT ["bash", "docker/build-linux.sh"]
