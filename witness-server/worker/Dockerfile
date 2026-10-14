# Build from the repository root. No .env, credentials or local state are copied.
FROM golang:1.27.1-bookworm@sha256:69a7b9788769bec032d238959b61854e9ae87f57be9029ec04e9885fabf99195 AS proof
ENV GOTOOLCHAIN=local
WORKDIR /src
COPY witness-client/native/go.mod witness-client/native/go.sum ./
RUN go mod download
COPY witness-client/native/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -o /privateauth . && go version /privateauth

FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS node
WORKDIR /witness
COPY witness-server/verifier/package.json witness-server/verifier/package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev --no-audit --no-fund

FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b
WORKDIR /app/witness-server/worker
COPY witness-server/worker/worker-requirements.txt ./
RUN pip install --no-cache-dir -r worker-requirements.txt
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /witness/node_modules /app/witness-server/verifier/node_modules
COPY --from=proof /privateauth /app/witness-client/native/privateauth
COPY witness-client/LICENSE witness-client/THIRD_PARTY_NOTICES.md /app/licenses/
COPY witness-server/verifier/*.mjs /app/witness-server/verifier/
COPY witness-server/verifier/params/verifying.bin /app/witness-server/verifier/params/verifying.bin
COPY witness-server/worker/worker.py witness-server/worker/storage.py witness-server/worker/identity.py witness-server/worker/channel.py witness-server/worker/timing.py witness-server/worker/verify.mjs ./
ENV PYTHONUNBUFFERED=1 WITNESS_TIMINGS=1
CMD ["python", "-u", "worker.py"]
