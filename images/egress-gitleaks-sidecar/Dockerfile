# Build the sidecar as a static binary, then ship it on distroless nonroot.
FROM golang:1.26 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/egress-gitleaks-sidecar .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/egress-gitleaks-sidecar /usr/local/bin/egress-gitleaks-sidecar
USER nonroot:nonroot
EXPOSE 8081
ENTRYPOINT ["/usr/local/bin/egress-gitleaks-sidecar"]
