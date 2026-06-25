# Build the connector as a static binary, then ship it on distroless nonroot.
FROM golang:1.22 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/mcp-cerbos-shim .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/mcp-cerbos-shim /usr/local/bin/mcp-cerbos-shim
USER nonroot:nonroot
EXPOSE 4445
ENTRYPOINT ["/usr/local/bin/mcp-cerbos-shim"]
