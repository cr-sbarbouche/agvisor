# Pinned by digest; Dependabot (docker ecosystem) bumps the tag and digest together.
FROM node:24-alpine@sha256:8a32c8cf649a49915a66e81e7a609223c708d0adabf288ee3f4077d765542f85
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev
COPY gateway.js ./
USER node
CMD ["node", "gateway.js"]
