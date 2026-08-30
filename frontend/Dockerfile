FROM node:20-alpine

WORKDIR /app

# Dependencies first, so a source-only change does not reinstall them.
COPY package.json ./
RUN npm install

COPY . .

# NEXT_PUBLIC_* values are inlined into the browser bundle at BUILD time, not
# read from the environment at runtime, so the browser's API base URL has to be
# known here. Overridable at build with --build-arg for a non-localhost deploy.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN npm run build

EXPOSE 3000
CMD ["npm", "start"]
