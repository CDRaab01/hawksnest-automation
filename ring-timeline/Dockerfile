# ring-timeline — read-only Ring recorded-footage timeline for Hawksnest.
#
# Built on the self-hosted WSL runner and imported straight into k3s containerd
# (no registry), exactly like the Hawksnest web image. See .github/workflows/deploy.yml.
FROM node:22-alpine

WORKDIR /app

# --ignore-scripts is load-bearing, not a hardening reflex: ring-client-api depends on
# ffmpeg-for-homebridge, whose postinstall downloads a ~90 MB ffmpeg build. This service
# only ever makes REST calls (it hands clients Ring's own signed mp4 URLs and never
# touches media), so that download is pure image weight and a build-time network
# dependency that can fail the deploy. Nothing here shells out to ffmpeg.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

COPY server.mjs ./

# Token rotation is persisted here (see server.mjs); mounted from the ring-timeline-data PVC.
ENV TOKEN_PATH=/data/refresh-token
ENV NODE_ENV=production
EXPOSE 8080

# node:22-alpine ships an unprivileged `node` user. /data is chowned by the initContainer
# in the Deployment, since the PVC mounts root-owned.
USER node

CMD ["node", "server.mjs"]
