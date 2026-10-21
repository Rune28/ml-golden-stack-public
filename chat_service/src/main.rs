//! chat_service: the chat product.
//!
//! A page behind a login to upload files, watch them get indexed and ask
//! about them, and behind it a thin API that forwards every call to the
//! platform API (be_service). No Temporal and none of the data: the platform
//! owns both. What the product holds is its users and their sessions, in a
//! Postgres database of its own.
//!
//! `chat_service` serves the page. The admin commands run in its pod (make
//! user-add, user-password), with the same database settings:
//!   chat_service user-add EMAIL        the password on stdin
//!   chat_service user-password EMAIL   the new password on stdin; every session ends
//!   chat_service user-id EMAIL         prints the user_id
//!   chat_service token EMAIL [MINUTES] prints a platform token of the user
//!                                      (make token: calls to be_service by hand)

mod api;
mod auth;
mod login;
mod platform;
mod store;
mod token;

use std::sync::Arc;

use anyhow::{Context, Result, bail};
use tokio::io::AsyncBufReadExt;
use tokio::net::TcpListener;
use tracing_subscriber::EnvFilter;

use crate::store::PgStore;

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_owned())
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        [] | ["serve"] => serve().await,
        ["user-add", email] => user_add(email).await,
        ["user-password", email] => user_password(email).await,
        ["user-id", email] => user_id(email).await,
        ["token", email] => user_token(email, 60).await,
        ["token", email, minutes] => user_token(email, minutes.parse().context("MINUTES")?).await,
        _ => bail!(
            "usage: chat_service [serve | user-add EMAIL | user-password EMAIL | user-id EMAIL | token EMAIL [MINUTES]]"
        ),
    }
}

async fn serve() -> Result<()> {
    let signer = token::Signer::from_env()?;
    let platform = platform::Platform::new(&env_or("PLATFORM_API_URL", "http://127.0.0.1:9001"), Some(signer))?;
    tracing::info!(url = %platform.base(), "platform API");
    let origins: Vec<String> = env_or("PUBLIC_ORIGINS", "http://127.0.0.1:9002")
        .split(',')
        .map(|o| o.trim().trim_end_matches('/').to_owned())
        .filter(|o| !o.is_empty())
        .collect();
    tracing::info!(?origins, "page origins");
    let checks = Arc::new(tokio::sync::Semaphore::new(login::PASSWORD_CHECKS));
    let app = Arc::new(api::App { platform, store: PgStore::from_env()?, origins, checks });

    let addr = env_or("LISTEN_ADDR", "0.0.0.0:8080");
    let listener = TcpListener::bind(&addr).await.with_context(|| format!("bind {addr}"))?;
    tracing::info!(%addr, "listening");
    axum::serve(listener, api::router(app)).with_graceful_shutdown(shutdown()).await?;
    Ok(())
}

/// The email checked and normalized, and a password from the first line of stdin, checked.
async fn credentials(raw_email: &str) -> Result<(String, String)> {
    let email = auth::normalize_email(raw_email);
    auth::check_email(&email).map_err(anyhow::Error::msg)?;
    let mut line = String::new();
    tokio::io::BufReader::new(tokio::io::stdin()).read_line(&mut line).await.context("reading the password")?;
    let password = line.trim_end_matches(['\r', '\n']).to_owned();
    auth::check_password(&password, &email).map_err(anyhow::Error::msg)?;
    Ok((email, password))
}

async fn user_add(email: &str) -> Result<()> {
    let (email, password) = credentials(email).await?;
    let user_id = PgStore::from_env()?.create_user(&email, &auth::hash_password(&password)).await?;
    println!("{user_id} {email}");
    Ok(())
}

async fn user_password(email: &str) -> Result<()> {
    let (email, password) = credentials(email).await?;
    PgStore::from_env()?.set_password(&email, &auth::hash_password(&password)).await?;
    println!("password of {email} changed; every session of the user ended");
    Ok(())
}

async fn user_id(email: &str) -> Result<()> {
    let email = auth::normalize_email(email);
    match PgStore::from_env()?.user_id(&email).await? {
        Some(id) => {
            println!("{id}");
            Ok(())
        }
        None => bail!("no user with the email {email}"),
    }
}

async fn user_token(email: &str, minutes: u64) -> Result<()> {
    let email = auth::normalize_email(email);
    let Some(id) = PgStore::from_env()?.user_id(&email).await? else {
        bail!("no user with the email {email}");
    };
    println!("{}", token::Signer::from_env()?.issue(id, std::time::Duration::from_secs(minutes * 60))?);
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
