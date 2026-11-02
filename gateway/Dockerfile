# Pinned by digest; Dependabot (docker ecosystem) bumps the tag and digest together.
FROM node:25-alpine@sha256:bdf2cca6fe3dabd014ea60163eca3f0f7015fbd5c7ee1b0e9ccb4ced6eb02ef4
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev
COPY gateway.js ./
USER node
CMD ["node", "gateway.js"]
