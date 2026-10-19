# chat_service: static-ish Rust binary on distroless. The same build as
# be_service without protoc: no Temporal SDK here.
# cargo-chef builds the dependencies as their own layer, so a code change
# rebuilds only the crate itself. Builder and runtime are both Debian 13
# (trixie), so the binary links against the same glibc it runs with.

FROM lukemathwalker/cargo-chef:0.1.78-rust-1.99.0-slim-trixie AS chef
WORKDIR /app

FROM chef AS planner
COPY . .
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS build
COPY --from=planner /app/recipe.json recipe.json
RUN cargo chef cook --release --recipe-path recipe.json
COPY . .
RUN cargo build --release --locked

# libc, libgcc and CA certificates; no shell, runs as uid 65532.
FROM gcr.io/distroless/cc-debian13:nonroot
COPY --from=build /app/target/release/chat_service /chat_service
EXPOSE 8080
ENTRYPOINT ["/chat_service"]
