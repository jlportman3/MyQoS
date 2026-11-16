//! Provides an interface with Insight/LTS2

use parking_lot::Mutex;
pub mod license_grant;
pub(crate) mod lts2_client;
pub mod shared_types;

use crate::lts2_sys::shared_types::{
    FreeTrialDetails, LtsStatus, OneWayFlow, ShaperThroughput, TwoWayFlow,
};
use anyhow::Result;
use once_cell::sync::Lazy;
pub use shared_types::RemoteCommand;
pub mod control_channel;

/// Returns true only when Insight/LTS2 is actually licensed for this install:
/// `gather_stats` is on AND a `license_key` is configured.
///
/// Alamo customization: upstream starts the LTS2 client unconditionally, so an
/// unlicensed install retries the ingestor every second ("Failed to parse license
/// key") and keeps an outbound connection to insight.libreqos.com alive. We don't
/// use Insight, so the client is gated on this predicate. This is a pure gate: when
/// a key IS present the full client runs exactly as before and the Insight wire
/// protocol is untouched.
pub(crate) fn insight_licensed() -> bool {
    lqos_config::load_config()
        .map(|c| c.long_term_stats.gather_stats && c.long_term_stats.license_key.is_some())
        .unwrap_or(false)
}

pub fn start_lts2(
    control_tx: tokio::sync::mpsc::Sender<control_channel::ControlChannelCommand>,
) -> Result<()> {
    // Alamo: do not start the Insight/LTS2 ingestor unless licensed (see insight_licensed).
    if !insight_licensed() {
        tracing::info!("Insight/LTS2 not licensed (no license key); ingestor not started");
        return Ok(());
    }
    // Launch the process
    lts2_client::spawn_lts2(control_tx)?;

    Ok(())
}

pub fn request_free_trial(details: FreeTrialDetails) -> Result<String> {
    lts2_client::request_free_trial(details)
}

pub fn network_tree(timestamp: u64, tree: &[u8]) -> Result<()> {
    lts2_client::submit_network_tree(timestamp, tree)
}

pub fn shaped_devices(timestamp: u64, devices: &[u8]) -> Result<()> {
    lts2_client::submit_shaped_devices(timestamp, devices)
}

pub fn total_throughput(throughput: ShaperThroughput) -> Result<()> {
    lts2_client::submit_total_throughput(throughput)
}

pub fn shaper_utilization(
    tick: u64,
    average_cpu: f32,
    peak_cpu: f32,
    memory_percent: f32,
) -> Result<()> {
    lts2_client::submit_shaper_utilization(tick, average_cpu, peak_cpu, memory_percent)
}

pub fn circuit_throughput(data: &[shared_types::CircuitThroughput]) -> Result<()> {
    lts2_client::submit_circuit_throughput_batch(data)
}

pub fn circuit_retransmits(data: &[shared_types::CircuitRetransmits]) -> Result<()> {
    lts2_client::submit_circuit_retransmits_batch(data)
}

pub fn circuit_rtt(data: &[shared_types::CircuitRtt]) -> Result<()> {
    lts2_client::submit_circuit_rtt_batch(data)
}

pub fn circuit_cake_drops(data: &[shared_types::CircuitCakeDrops]) -> Result<()> {
    lts2_client::submit_circuit_cake_drops_batch(data)
}

pub fn circuit_cake_marks(data: &[shared_types::CircuitCakeMarks]) -> Result<()> {
    lts2_client::submit_circuit_cake_marks_batch(data)
}

pub fn site_throughput(data: &[shared_types::SiteThroughput]) -> Result<()> {
    lts2_client::submit_site_throughput_batch(data)
}

pub fn site_retransmits(data: &[shared_types::SiteRetransmits]) -> Result<()> {
    lts2_client::submit_site_retransmits_batch(data)
}

pub fn site_rtt(data: &[shared_types::SiteRtt]) -> Result<()> {
    lts2_client::submit_site_rtt_batch(data)
}

pub fn site_cake_drops(data: &[shared_types::SiteCakeDrops]) -> Result<()> {
    lts2_client::submit_site_cake_drops_batch(data)
}

pub fn site_cake_marks(data: &[shared_types::SiteCakeMarks]) -> Result<()> {
    lts2_client::submit_site_cake_marks_batch(data)
}

pub fn get_lts_license_status() -> (LtsStatus, i32) {
    // Use async path under the hood to avoid blocking inside a Tokio runtime.
    // If we're already inside a runtime, switch to a blocking section and run a tiny
    // current-thread runtime there; otherwise, build a small runtime and block_on.
    if tokio::runtime::Handle::try_current().is_ok() {
        tokio::task::block_in_place(|| {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("Failed to build temporary Tokio runtime");
            rt.block_on(async {
                let remaining = lts2_client::get_lts_license_trial_remaining_async()
                    .await
                    .unwrap_or(0);
                let status = lts2_client::get_lts_license_status_async()
                    .await
                    .unwrap_or(-1);
                (LtsStatus::from_i32(status), remaining)
            })
        })
    } else {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("Failed to build temporary Tokio runtime");
        rt.block_on(async {
            let remaining = lts2_client::get_lts_license_trial_remaining_async()
                .await
                .unwrap_or(0);
            let status = lts2_client::get_lts_license_status_async()
                .await
                .unwrap_or(-1);
            (LtsStatus::from_i32(status), remaining)
        })
    }
}

pub async fn get_lts_license_status_async() -> (LtsStatus, i32) {
    let remaining = lts2_client::get_lts_license_trial_remaining_async()
        .await
        .unwrap_or(0);
    let status = lts2_client::get_lts_license_status_async()
        .await
        .unwrap_or(-1);
    (LtsStatus::from_i32(status), remaining)
}

pub fn ingest_batch_complete() -> Result<()> {
    lts2_client::ingest_batch_complete()
}

pub fn one_way_flow(flow: OneWayFlow) -> Result<()> {
    lts2_client::one_way_flow(flow)
}

pub fn two_way_flow(flow: TwoWayFlow) -> Result<()> {
    lts2_client::two_way_flow(flow)
}

pub fn ip_policies(allow_subnets: &Vec<String>, ignore_subnets: &Vec<String>) -> Result<()> {
    for subnet in allow_subnets {
        lts2_client::allow_subnet(subnet.to_string())?;
    }
    for subnet in ignore_subnets {
        lts2_client::ignore_subnet(subnet.to_string())?;
    }
    Ok(())
}

pub fn blackboard(json: &[u8]) -> Result<()> {
    lts2_client::submit_blackboard(json)?;
    Ok(())
}

pub fn flow_count(timestamp: u64, count: u64) -> Result<()> {
    lts2_client::flow_count(timestamp, count)
}

pub fn remote_command_count() -> u64 {
    lts2_client::remote_command_count()
}

fn command_callback(buffer: Vec<u8>) {
    let mut lock = COMMANDS.lock();
    lock.clear();
    let Ok(commands) = serde_cbor::from_slice::<Vec<shared_types::RemoteCommand>>(&buffer) else {
        return;
    };
    lock.extend(commands);
}

static COMMANDS: Lazy<Mutex<Vec<shared_types::RemoteCommand>>> =
    Lazy::new(|| Mutex::new(Vec::new()));

pub fn remote_commands() -> Vec<shared_types::RemoteCommand> {
    lts2_client::get_commands(command_callback);

    let lock = COMMANDS.lock();
    lock.clone()
}
