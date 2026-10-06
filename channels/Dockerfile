# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# The bridge from the chat platforms to a Robinauts deployment (README.md). Node runs the
# TypeScript as it is, with its types stripped, so there is no build step and no compiler in
# the image. The image's tag is an exact Node release.

FROM node:24.21.0-slim

# The SDK's anonymous telemetry is off, here and again in src/channel.ts. The state file is
# which platform thread is which conversation, on the volume so that it survives the container.
ENV NODE_ENV=production \
    COPILOTKIT_TELEMETRY_DISABLED=true \
    DO_NOT_TRACK=1 \
    CHANNELS_STATE_FILE=/data/threads.json

WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN npm ci --omit=dev && npm cache clean --force
COPY src ./src

RUN mkdir /data && chown node:node /data
VOLUME /data
USER node

CMD ["node", "src/main.ts"]
