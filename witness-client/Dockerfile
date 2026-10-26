# Build from the repository root.
# Build and check the Linux wheel.
FROM golang:1.27.3-bookworm@sha256:972b4110ba0f47539a975c31e52c64765e1ff19f2d74469ae4a595c6676e80a5 AS proof
ENV GOTOOLCHAIN=local
WORKDIR /src
COPY witness-client/native/go.mod witness-client/native/go.sum ./
RUN go mod download
COPY witness-client/native/*.go ./
RUN CGO_ENABLED=1 go build -trimpath -buildmode=c-shared -o /libwitnessproof.so .

FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b AS build
COPY witness-client /app/witness-client
COPY --from=proof /libwitnessproof.so /app/witness-client/src/witness_client/libwitnessproof.so
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir --upgrade pip build==1.6.1 setuptools==84.0.0 wheel==0.48.0 auditwheel==6.8.2 patchelf==0.19.1.0
WORKDIR /app/witness-client
RUN PATH="/venv/bin:$PATH" /venv/bin/python -m build --wheel --no-isolation --outdir /tmp/unrepaired-wheel \
    && PATH="/venv/bin:$PATH" /venv/bin/auditwheel repair --plat manylinux_2_34_x86_64 --wheel-dir dist /tmp/unrepaired-wheel/*.whl \
    && /venv/bin/auditwheel show dist/*.whl \
    && /venv/bin/pip install --no-cache-dir dist/*.whl pytest==9.1.1
ENV GOMAXPROCS=2 GOMEMLIMIT=768MiB
CMD ["/venv/bin/python", "-m", "pytest", "-q", "-o", "pythonpath="]
