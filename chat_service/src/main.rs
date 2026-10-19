//! chat_service: the chat product.
//!
//! A page to upload text files, watch them get indexed and ask about them,
//! and behind it a thin API that forwards every call to the platform API
//! (be_service). No Temporal, no storage: the platform owns both. What a
//! product holds lives here later (the login session), never the data.

mod api;
mod platform;

use std::sync::Arc;

use anyhow::{Context, Result};
use tokio::net::TcpListener;
use tracing_subscriber::EnvFilter;

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_owned())
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    let platform = platform::Platform::new(&env_or("PLATFORM_API_URL", "http://127.0.0.1:9001"))?;
    tracing::info!(url = %platform.base(), "platform API");

    let addr = env_or("LISTEN_ADDR", "0.0.0.0:8080");
    let listener = TcpListener::bind(&addr).await.with_context(|| format!("bind {addr}"))?;
    tracing::info!(%addr, "listening");
    axum::serve(listener, api::router(Arc::new(platform))).with_graceful_shutdown(shutdown()).await?;
    Ok(())
}

/// Kubernetes stops a pod with SIGTERM: finish requests in flight, then exit.
async fn shutdown() {
    let ctrl_c = async { tokio::signal::ctrl_c().await.ok() };
    let term = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM handler").recv().await
    };
    tokio::select! {
        _ = ctrl_c => {}
        _ = term => {}
    }
    tracing::info!("shutting down");
}
