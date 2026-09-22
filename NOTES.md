**Notes During Development**

0. Docker Setup

Pre-flight:

```
youssefqteishat@Youssefs-MacBook-Air-2 ~ % docker info --format '{{.ServerVersion}} {{.Architecture}} {{.NCPU}}cpu {{.MemTotal}}'
29.7.2 aarch64 8cpu 4108632064
youssefqteishat@Youssefs-MacBook-Air-2 ~ % docker info --format '{{.NCPU}}, {{.MemTotal}}'
8, 4108632064
youssefqteishat@Youssefs-MacBook-Air-2 ~ % docker run --rm debian:bookworm-slim dpkg --print-architecture
Unable to find image 'debian:bookworm-slim' locally
bookworm-slim: Pulling from library/debian
333125b5cee9: Pull complete
Digest: sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
Status: Downloaded newer image for debian:bookworm-slim
arm64
youssefqteishat@Youssefs-MacBook-Air-2 ~ % docker run --rm debian:bookworm-slim sh -c \
  'apt-get update -qq && apt-cache policy chromium | head -3'
chromium:
  Installed: (none)
  Candidate: 153.0.8010.52-1~deb12u1
youssefqteishat@Youssefs-MacBook-Air-2 ~ % docker run --rm --shm-size=1g debian:bookworm-slim df -h /dev/shm
Filesystem      Size  Used Avail Use% Mounted on
shm             1.0G     0  1.0G   0% /dev/shm
youssefqteishat@Youssefs-MacBook-Air-2 ~ % docker network create --internal preflight_net
a838be46ba0569f9d7e32c0ebd37eff65a34875d4ad5b43716d9c2af9212c4dd
youssefqteishat@Youssefs-MacBook-Air-2 ~ % docker run --rm --network preflight_net debian:bookworm-slim \
    sh -c 'timeout 5 getent hosts example.com || echo "no egress - correct"'
no egress - correct
youssefqteishat@Youssefs-MacBook-Air-2 ~ % docker network rm preflight_net
preflight_net
```

1. Computer Use Tool

- mock bank app needs to be running on the same machine as Agent
