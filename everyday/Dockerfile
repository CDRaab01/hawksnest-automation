# everyday — daily "one second a day" picker over Frigate. See everyday/README.md.
#
# Built on the self-hosted WSL runner and imported straight into k3s containerd (no
# registry), exactly like ring-timeline. Stdlib Python + ffmpeg; nothing pip-installed.
FROM python:3.12-alpine

# tzdata: day boundaries are LOCAL midnight (zoneinfo needs the database on alpine).
RUN apk add --no-cache ffmpeg tzdata

WORKDIR /app
COPY picker.py server.py ./

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8080

# Unprivileged; /data is chowned by the Deployment's initContainer (the NFS PV mounts
# root-owned, same as ring-timeline's PVC).
RUN adduser -D -u 1000 everyday
USER everyday

CMD ["python", "server.py"]
